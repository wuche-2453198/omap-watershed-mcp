"""验证用 MCP stdio 客户端 harness（阶段 1 起）。

职责边界：
- **只**为验证服务：真实以 ``[python, -m, ovital_mcp]`` 子进程启动 MCP 服务，
  用 mcp SDK（mcp 2.3.0）的 ``Client`` 走一次真实 MCP 握手，同时在传输层**逐行
  记录服务端原始 stdout**，用于断言"stdout 只允许 MCP 协议帧"这条红线；
- 捕获服务端 stderr（日志）供证据审计，绝不把它当协议数据；
- 不修改业务代码，不 mock MCP 协议本身（协议帧是真实双向 JSON-RPC）。

被两个消费方复用：
- ``scripts/verify_mcp_stdio.py``：生成证据（JSON + PASS/FAIL 摘要）；
- ``tests/integration/test_stdio_client.py``：回归断言。

约定：
- 每次运行使用独立临时目录（``<repo>/.cache/verify/runs/...``）与独立配置，
  数据目录不污染仓库根 ``.ovital-mcp``；
- 默认把 ``ovital.endpoint`` 指向 ``ws://127.0.0.1:1``（必然拒绝连接），
  以验证"无奥维时服务可启动、工具显式结构化报错"；
- 测试凭据是随机生成的假 PSK，用于断言它不会出现在 stdout/stderr/证据里。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import anyio
import mcp_types as types
from anyio.streams.text import TextReceiveStream
from mcp import Client
from mcp.client.stdio import StdioServerParameters, get_default_environment
from mcp.shared.message import SessionMessage

#: 仓库根（本文件位于 <repo>/scripts/，验证脚本可被移动，但仓库内运行是硬前提）。
REPO_ROOT = Path(__file__).resolve().parent.parent

#: docs/architecture.md §6 契约的 18 个工具（17 + list_jobs）。
EXPECTED_TOOLS: tuple[str, ...] = (
    "get_capabilities",
    "get_selection",
    "read_object",
    "get_scene",
    "inspect_dem",
    "start_watershed",
    "get_job",
    "cancel_job",
    "list_jobs",
    "review_segment",
    "revise_candidate",
    "validate_geometry",
    "measure_geometry",
    "write_draft",
    "read_back",
    "finalize_draft",
    "rollback_change",
    "export_result",
)

#: instructions 中必须出现的红线关键词（任务书/架构 §2）。
REQUIRED_INSTRUCTION_NEEDLES: tuple[str, ...] = (
    "集水区",
    "不得为了凑目标面积而修改边界",
    "不得伪造坐标",
    "get_capabilities",
    "simulated",
    "verified_against_real_device",
    "review_segment",
    "回读",
)

#: 客户端声明的实现名（出现在子进程日志/证据里，不含秘密）。
CLIENT_INFO_NAME = "omap-verify-stdio"


def _iso_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.localtime())


def make_run_dir(prefix: str = "run", *, root: Path | None = None) -> Path:
    """在 ``<repo>/.cache/verify/runs``（或给定 root）下创建本次运行的隔离目录。"""

    base = root if root is not None else REPO_ROOT / ".cache" / "verify" / "runs"
    base.mkdir(parents=True, exist_ok=True)
    run_dir = base / f"{_iso_stamp()}-{prefix}-{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def write_verify_config(
    run_dir: Path,
    *,
    include_psk: bool = True,
    endpoint: str = "ws://127.0.0.1:1",
    connect_timeout_s: float = 0.3,
    engine: str = "native",
    psk_seed: str | None = None,
    ovital_extra: dict[str, Any] | None = None,
    gis_extra: dict[str, Any] | None = None,
) -> tuple[Path, str]:
    """写一份隔离配置，返回 (配置路径, 假 PSK 明文)。

    PSK 用随机假值，便于"凭据不得出现在任何输出/证据里"的检索断言。
    ``include_psk=False`` 用于验证"无凭据时仍可启动 + 结构化报错"。
    """

    psk = psk_seed or f"verify-fake-psk-{uuid.uuid4().hex}"
    data_root = run_dir / "data"
    lines = [
        "# 验证脚本生成的隔离配置（非交付配置，不含真实凭据）。",
        "ovital:",
        f'  endpoint: "{endpoint}"',
        f"  connect_timeout_s: {connect_timeout_s}",
        "  request_timeout_s: 2.0",
    ]
    if include_psk:
        lines.append(f'  psk: "{psk}"')
    for extra_key, extra_value in (ovital_extra or {}).items():
        lines.append(f"  {extra_key}: {json.dumps(extra_value)}")
    lines += [
        "storage:",
        f"  data_root: {json.dumps(str(data_root))}",
        "gis:",
        f'  engine: "{engine}"',
    ]
    for extra_key, extra_value in (gis_extra or {}).items():
        lines.append(f"  {extra_key}: {json.dumps(extra_value)}")
    lines += [
        "logging:",
        "  level: \"INFO\"",
    ]
    config_path = run_dir / "ovital.yaml"
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path, psk


@dataclass
class RecordingStdioTransport:
    """最小 stdio 传输实现：spawn 子进程 + 逐行记录原始 stdout/stderr。

    行为对齐官方 ``mcp.client.stdio.stdio_client``（同样的 stdin/stdout 行协议、
    同样的 JSON-RPC 编解码器），额外记录原始行以便审计 stdout 纯净性。
    """

    server: StdioServerParameters
    raw_stdout_lines: list[str] = field(default_factory=list)
    nonprotocol_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    parsed_methods: list[str] = field(default_factory=list)
    sent_raw_lines: list[str] = field(default_factory=list)
    sent_methods: list[str] = field(default_factory=list)
    _process: Any = field(init=False, default=None)
    _tg: Any = field(init=False, default=None)

    async def __aenter__(self) -> tuple[Any, Any]:
        env = get_default_environment() | (self.server.env or {})
        self._process = await anyio.open_process(
            [self.server.command, *self.server.args],
            env=env,
            cwd=self.server.cwd,
            start_new_session=True,
        )
        read_writer, read_stream = anyio.create_memory_object_stream(0)
        write_stream, write_reader = anyio.create_memory_object_stream(0)
        self._read_writer = read_writer
        self._write_reader = write_reader
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        self._tg.start_soon(self._read_stdout)
        self._tg.start_soon(self._write_stdin)
        self._tg.start_soon(self._read_stderr)
        return read_stream, write_stream

    # -- pipe tasks -----------------------------------------------------

    async def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        stream = TextReceiveStream(self._process.stdout, encoding="utf-8")
        buffer = ""
        try:
            async with self._read_writer:
                async for chunk in stream:
                    buffer += chunk
                    lines = buffer.split("\n")
                    buffer = lines.pop()
                    for line in lines:
                        if not line.strip():
                            continue
                        self.raw_stdout_lines.append(line)
                        try:
                            message = types.jsonrpc_message_adapter.validate_json(line, by_name=False)
                        except Exception as exc:  # noqa: BLE001 - 记录并交回会话
                            self.nonprotocol_lines.append(line)
                            await self._read_writer.send(exc)
                            continue
                        method = getattr(message, "method", None)
                        if method:
                            self.parsed_methods.append(str(method))
                        await self._read_writer.send(SessionMessage(message))
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream):
            pass

    async def _write_stdin(self) -> None:
        assert self._process is not None and self._process.stdin is not None
        try:
            async with self._write_reader:
                async for session_message in self._write_reader:
                    payload = session_message.message.model_dump_json(
                        by_alias=True, exclude_unset=True
                    )
                    self.sent_raw_lines.append(payload)
                    method = getattr(session_message.message, "method", None)
                    if method:
                        self.sent_methods.append(str(method))
                    await self._process.stdin.send((payload + "\n").encode("utf-8"))
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
            pass

    async def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        stream = TextReceiveStream(self._process.stderr, encoding="utf-8", errors="replace")
        try:
            async for chunk in stream:
                self.stderr_lines.extend(chunk.splitlines())
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream):
            pass

    # -- shutdown -------------------------------------------------------

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        process = self._process
        try:
            self._read_writer.close()
            try:
                await self._write_reader.aclose()
            except Exception:  # noqa: BLE001
                pass
            if process is not None:
                with anyio.move_on_after(1.0):
                    await process.stdin.aclose()
                with anyio.move_on_after(5.0):
                    await process.wait()
                if process.returncode is None:  # 兜底：不让验证脚本挂死
                    process.kill()
                    with anyio.move_on_after(2.0):
                        await process.wait()
        finally:
            if self._tg is not None:
                self._tg.cancel_scope.cancel()
                await self._tg.__aexit__(None, None, None)
        return False


@dataclass
class HarnessSession:
    """一次真实 MCP stdio 会话：客户端句柄 + 原始传输证据。"""

    client: Client
    transport: RecordingStdioTransport
    run_dir: Path
    config_path: Path
    psk: str
    command: list[str]
    started_at: float = field(default_factory=time.monotonic)
    exit_code: int | None = None
    elapsed_s: float | None = None

    @property
    def raw_stdout_lines(self) -> list[str]:
        return self.transport.raw_stdout_lines

    @property
    def stderr_lines(self) -> list[str]:
        return self.transport.stderr_lines

    @property
    def sent_raw_lines(self) -> list[str]:
        return self.transport.sent_raw_lines

    @property
    def tools(self) -> list[Any]:
        return self._tools

    def remember_tools(self, tools: list[Any]) -> None:
        self._tools = tools


@asynccontextmanager
async def open_stdio_session(
    *,
    include_psk: bool = True,
    endpoint: str = "ws://127.0.0.1:1",
    connect_timeout_s: float = 0.3,
    engine: str = "native",
    env_extra: dict[str, str] | None = None,
    mode: str = "legacy",
    run_dir: Path | None = None,
    psk: str | None = None,
    ovital_extra: dict[str, Any] | None = None,
    gis_extra: dict[str, Any] | None = None,
) -> AsyncIterator[HarnessSession]:
    """启动独立 MCP 子进程并完成真实握手（默认 legacy initialize 握手）。

    产出：已握手、可 ``await session.client.call_tool(...)`` 的会话对象。
    ``run_dir`` 可指定隔离目录（pytest 传入 tmp_path；验证脚本用仓库 .cache）。
    """

    if run_dir is None:
        run_dir = make_run_dir("stdio")
    else:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
    config_path, resolved_psk = write_verify_config(
        run_dir,
        include_psk=include_psk,
        endpoint=endpoint,
        connect_timeout_s=connect_timeout_s,
        engine=engine,
        psk_seed=psk,
        ovital_extra=ovital_extra,
        gis_extra=gis_extra,
    )
    config_env = {"OVITAL_MCP_CONFIG": str(config_path)}
    if env_extra:
        config_env.update(env_extra)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ovital_mcp"],
        cwd=str(REPO_ROOT),
        env=config_env,
    )
    transport = RecordingStdioTransport(server=params)
    client = Client(
        transport,  # type: ignore[arg-type] - 满足 Transport 协议的 async CM
        mode=mode,  # type: ignore[arg-type]
        client_info=types.Implementation(name=CLIENT_INFO_NAME, version="0.0.0-verify"),
        read_timeout_seconds=30.0,
    )
    session = HarnessSession(
        client=client,
        transport=transport,
        run_dir=run_dir,
        config_path=config_path,
        psk=resolved_psk,
        command=[sys.executable, "-m", "ovital_mcp"],
    )
    started = time.monotonic()
    try:
        async with client:
            yield session
    finally:
        process = transport._process
        session.exit_code = getattr(process, "returncode", None)
        session.elapsed_s = round(time.monotonic() - started, 3)


# ---------------------------------------------------------------------------
# 结果解析辅助
# ---------------------------------------------------------------------------


def tool_result_text(result: Any) -> str:
    """取出 CallToolResult 的第一个 text 内容块。"""

    blocks = getattr(result, "content", None) or []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    raise AssertionError(f"工具返回没有 text 内容块：{blocks!r}")


def tool_result_json(result: Any) -> dict[str, Any]:
    """把工具返回的 text 块按 JSON 解析（结构化工具的统一出口）。"""

    return json.loads(tool_result_text(result))


def tool_result_is_error(result: Any) -> bool:
    for attr in ("is_error", "isError"):
        value = getattr(result, attr, None)
        if value is not None:
            return bool(value)
    return False


def write_evidence(run_dir: Path, name: str, payload: dict[str, Any]) -> Path:
    """把证据 JSON 写到运行目录（仓库内 .cache，不进入交付物）。"""

    path = run_dir / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def desensitize(text: str, run_dir: Path | None = None) -> str:
    """证据入库前脱敏：把绝对临时路径替换为占位符。"""

    if run_dir is not None:
        text = text.replace(str(run_dir), "<run_dir>")
    return text.replace(str(REPO_ROOT), "<repo>")


async def _self_test() -> int:
    """harness 自检（直接运行本文件时）：握手 + tools/list + get_capabilities。"""

    async with open_stdio_session() as session:
        tools = await session.client.list_tools()
        session.remember_tools(tools.tools)
        capabilities = await session.client.call_tool("get_capabilities", {})
        payload = tool_result_json(capabilities)
        print("[harness] protocol_version =", session.client.protocol_version)
        print("[harness] server =", session.client.server_info)
        print("[harness] tools =", len(tools.tools))
        print("[harness] capabilities.status =", payload.get("status"))
        print("[harness] connection.available =", payload.get("connection", {}).get("available"))
        print("[harness] raw stdout lines =", len(session.raw_stdout_lines),
              "nonprotocol =", len(session.transport.nonprotocol_lines))
        print("[harness] stderr lines =", len(session.stderr_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_self_test()))
