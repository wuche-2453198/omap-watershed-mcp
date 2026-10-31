"""阶段 1 集成测试：真实 MCP stdio 握手、工具契约、stdout 纯净性、错误结构化。

这些测试**不 mock MCP 协议**：每个用例通过 ``scripts/verify_stdio_harness.py``
以 ``[python, -m, ovital_mcp]`` 启动真实子进程，用 mcp SDK 2.3.0 的 Client 完成
真实 initialize 握手与 tools/call 往返，并在传输层逐行审计服务端原始 stdout。

覆盖（task-4 阶段 1 要求）：
1. 真实握手成功、instructions 含红线、18 个工具、schema 可解析；
2. 无奥维时 ``get_capabilities`` 如实报 ``connection.available=false``，
   provenance 标注 ``verified_against_real_device=false``；
3. 启动无副作用：不自动连奥维、不建任务/作业、不写地图；
4. 工具调用错误结构化（``status=error`` + 稳定 code），失败后服务仍存活；
5. 服务端 stdout 只有 MCP 协议帧（逐行 JSON-RPC 2.0 校验）；日志只在 stderr；
6. ``--dump-tools`` 诊断入口 stdout 是单行纯 JSON；
7. 假 PSK 不出现在协议帧/日志/证据里。

运行::

    .venv/bin/python -m pytest tests/integration -q
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Coroutine

import pytest

from verify_stdio_harness import (
    EXPECTED_TOOLS,
    REPO_ROOT,
    REQUIRED_INSTRUCTION_NEEDLES,
    open_stdio_session,
    tool_result_json,
)

def run(coro: Coroutine[Any, Any, Any]) -> Any:
    """在同步测试中跑一次完整异步会话（避免 pytest-asyncio 事件循环作用域问题）。"""

    return asyncio.run(coro)


def assert_stdout_lines_are_jsonrpc(lines: list[str], nonprotocol: list[str]) -> None:
    """服务端 stdout 的每一行都必须是 JSON-RPC 2.0 消息。"""

    assert nonprotocol == [], f"服务端 stdout 出现非协议输出/无法解析的行：{nonprotocol[:2]}"
    for line in lines:
        obj = json.loads(line)  # 解析失败会直接让测试失败
        assert isinstance(obj, dict)
        assert obj.get("jsonrpc") == "2.0", f"stdout 行不是 JSON-RPC 2.0：{line[:120]}"
    kinds = set()
    for line in lines:
        obj = json.loads(line)
        kinds.add("request" if "method" in obj else ("response" if "result" in obj or "error" in obj else "other"))
    assert "response" in kinds, f"stdout 没有响应帧，握手/调用未真正发生：{kinds}"


# ---------------------------------------------------------------------------
# 1. 握手 + 工具契约 + instructions 红线 + stdout 纯净性（单会话覆盖）
# ---------------------------------------------------------------------------


def test_handshake_tools_and_instructions(stdio_run_dir: Path) -> None:
    async def scenario() -> None:
        async with open_stdio_session(run_dir=stdio_run_dir) as session:
            client = session.client
            # 真实握手结果
            assert client.protocol_version, "initialize 未协商出协议版本"
            server_info = client.server_info
            assert server_info is not None
            assert server_info.name == "ovital-watershed"
            assert server_info.version == "0.1.0"

            # instructions 红线
            instructions = client.instructions or ""
            missing = [needle for needle in REQUIRED_INSTRUCTION_NEEDLES if needle not in instructions]
            assert missing == [], f"instructions 缺少红线关键词：{missing}"

            # tools/list：18 个工具，schema 可解析
            tools_result = await client.list_tools()
            tools = tools_result.tools
            names = {tool.name for tool in tools}
            assert names == set(EXPECTED_TOOLS), (
                f"工具集合不一致：缺 {sorted(set(EXPECTED_TOOLS) - names)}，"
                f"多 {sorted(names - set(EXPECTED_TOOLS))}"
            )
            assert len(tools) == 18
            for tool in tools:
                assert tool.description and tool.description.strip(), f"{tool.name} 描述为空"
                schema = tool.input_schema
                assert isinstance(schema, dict), f"{tool.name} inputSchema 不是 dict"
                assert schema.get("type") == "object", f"{tool.name} inputSchema.type != object"
                assert isinstance(schema.get("properties", {}), dict)
                required = schema.get("required", [])
                assert isinstance(required, list)
                assert set(required) <= set(schema.get("properties", {})), (
                    f"{tool.name}: required 引用了未声明的属性"
                )
                json.dumps(schema)  # 必须可序列化

            # 真实调用：无奥维时 get_capabilities 仍成功且诚实
            payload = tool_result_json(await client.call_tool("get_capabilities", {}))
            assert payload["status"] == "ok"
            assert payload["connection"]["available"] is False
            assert payload["connection"]["error"]["code"], "连接失败必须有稳定错误码"
            provenance = payload["provenance"]
            assert provenance["simulated"] is False
            assert provenance["verified_against_real_device"] is False
            assert payload["unverified"], "必须列出未验证清单"
            assert payload["warnings"], "无奥维时必须有 warning"

            # stdout 纯净性 + stderr 行为
            assert_stdout_lines_are_jsonrpc(
                session.raw_stdout_lines, session.transport.nonprotocol_lines
            )
            stderr_text = "\n".join(session.stderr_lines)
            assert session.stderr_lines, "日志应写 stderr（stderr 非空）"
            assert "Traceback (most recent call last)" not in stderr_text, "可预期错误路径不得打印异常栈"
            assert " INFO " in stderr_text, "stderr 应有 INFO 级启动日志"

            # 假 PSK 不得泄漏
            all_output = "\n".join(
                session.raw_stdout_lines + session.sent_raw_lines + session.stderr_lines
            )
            assert session.psk and session.psk not in all_output, "假 PSK 泄漏到协议帧/日志"

        # 会话退出后进程正常结束
        assert session.exit_code == 0, f"服务端退出码异常：{session.exit_code}"

    run(scenario())


# ---------------------------------------------------------------------------
# 2. 启动无副作用（红线：懒连接、启动不写地图/不建任务）
# ---------------------------------------------------------------------------


def test_startup_has_no_side_effects(stdio_run_dir: Path) -> None:
    async def scenario() -> None:
        async with open_stdio_session(run_dir=stdio_run_dir) as session:
            # 只做握手 + 列工具，不调用任何需要奥维的工具
            await session.client.list_tools()
            stderr_text = "\n".join(session.stderr_lines).lower()
            assert "ovital connected" not in stderr_text, "启动阶段不得自动连接奥维"
            # 不应出现连接失败的尝试记录（连接必须懒建立）
            assert "connection refused" not in stderr_text, "启动阶段不应尝试连接奥维"

            data_root = stdio_run_dir / "data"
            task_dir = data_root / "tasks"
            job_dir = data_root / "jobs"
            assert not task_dir.exists() or list(task_dir.iterdir()) == [], "启动不得创建任务"
            assert not job_dir.exists() or list(job_dir.iterdir()) == [], "启动不得创建作业记录"

        assert session.exit_code == 0

    run(scenario())


# ---------------------------------------------------------------------------
# 3. 工具错误结构化 + 失败后服务存活
# ---------------------------------------------------------------------------


def test_tool_errors_are_structured_and_server_survives(stdio_run_dir: Path) -> None:
    async def scenario() -> None:
        async with open_stdio_session(run_dir=stdio_run_dir) as session:
            client = session.client

            selection = tool_result_json(await client.call_tool("get_selection", {}))
            assert selection["status"] == "error", "无奥维时 get_selection 必须显式失败"
            assert selection["error"]["code"] == "OVITAL_CONNECTION_ERROR"

            missing_dem = stdio_run_dir / "no-such.tif"
            dem = tool_result_json(await client.call_tool("inspect_dem", {"dem_path": str(missing_dem)}))
            assert dem["status"] == "error", "不存在的 DEM 必须显式失败"
            assert dem["error"]["code"] == "DATA_VALIDATION_ERROR"

            # 失败后服务仍存活
            again = tool_result_json(await client.call_tool("get_capabilities", {}))
            assert again["status"] == "ok"

            # stdio 仍然纯净
            assert_stdout_lines_are_jsonrpc(
                session.raw_stdout_lines, session.transport.nonprotocol_lines
            )

        assert session.exit_code == 0

    run(scenario())


# ---------------------------------------------------------------------------
# 4. 诊断输出：--dump-tools 的 stdout 必须是一行纯 JSON
# ---------------------------------------------------------------------------


def test_dump_tools_stdout_is_pure_json(stdio_run_dir: Path) -> None:
    data_root = stdio_run_dir / "data"
    config_path = stdio_run_dir / "ovital.yaml"
    config_path.write_text(
        "ovital:\n"
        '  endpoint: "ws://127.0.0.1:1"\n'
        '  psk: "verify-fake-psk-dump-test"\n'
        "storage:\n"
        f"  data_root: {json.dumps(str(data_root))}\n"
        "logging:\n"
        '  level: "INFO"\n',
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["OVITAL_MCP_CONFIG"] = str(config_path)
    proc = subprocess.run(
        [sys.executable, "-m", "ovital_mcp", "--dump-tools"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"诊断入口退出码异常：{proc.returncode}\nstderr={proc.stderr}"
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"stdout 只允许一行 JSON（诊断输出），实际 {len(lines)} 行：{proc.stdout[:300]}"
    payload = json.loads(lines[0])
    assert set(payload["tools"]) == set(EXPECTED_TOOLS)
    assert payload["server"]["name"] == "ovital-watershed-mcp"
    # 日志走 stderr；stdout 不出现日志格式
    assert " INFO " not in proc.stdout
    assert " INFO " in proc.stderr
    # 假 PSK 不得泄漏
    assert "verify-fake-psk-dump-test" not in proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# 5. 无凭据时服务仍可启动（DSH 探测进程兼容）+ 结构化配置错误
# ---------------------------------------------------------------------------


def test_starts_without_psk_and_reports_config_error(stdio_run_dir: Path) -> None:
    async def scenario() -> None:
        async with open_stdio_session(run_dir=stdio_run_dir, include_psk=False) as session:
            payload = tool_result_json(await session.client.call_tool("get_capabilities", {}))
            assert payload["status"] == "ok", "无凭据时服务仍须能响应 get_capabilities"
            connection = payload["connection"]
            assert connection["available"] is False
            assert connection["error"]["code"] == "CONFIG_ERROR"
            assert "PSK" in connection["error"]["message"] or "预共享密钥" in connection["error"]["message"]

        assert session.exit_code == 0

    run(scenario())


# ---------------------------------------------------------------------------
# 6. 重复握手（DSH 探测进程 + 正式进程先后启动的常见场景）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("round_no", [1, 2])
def test_repeated_handshakes_are_stable(stdio_run_dir: Path, round_no: int) -> None:
    async def scenario() -> None:
        run_dir = stdio_run_dir / f"round-{round_no}"
        run_dir.mkdir(parents=True, exist_ok=True)
        async with open_stdio_session(run_dir=run_dir) as session:
            tools = await session.client.list_tools()
            assert len(tools.tools) == 18
            assert session.client.protocol_version
        assert session.exit_code == 0

    run(scenario())
