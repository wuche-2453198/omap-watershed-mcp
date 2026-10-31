#!/usr/bin/env python3
"""阶段 1 / 阶段 2 通用：MCP stdio 真实性验证（独立验收脚本）。

它回答的问题（不是"代码里有什么"，而是"跑起来真实发生了什么"）：

1. ``python -m ovital_mcp`` 能否被 **mcp SDK 2.3.0 的 stdio 客户端真实握手**（legacy
   initialize，真实子进程 + 真实 JSON-RPC 往返），而不是进程内 mock；
2. ``tools/list`` 是否恰好 18 个工具、schema 是否可解析、描述是否非空；
3. server instructions 是否带质量红线（不得凑面积 / 不得伪造坐标 / simulated 语义等）；
4. 本机无奥维时 ``get_capabilities`` 是否 ``status=ok``、``connection.available=false``
   且错误结构化（不得启动即崩溃、不得伪成功），provenance 是否如实标注
   ``verified_against_real_device=false``；
5. 缺依赖/错误输入的工具调用是否结构化失败（``status=error`` + 稳定 code），
   且失败之后服务仍存活；
6. **stdout 纯净性**：服务端 stdout 的每一行都必须能被 JSON-RPC 解析器解析，
   任何非协议输出都是缺陷；stderr 才允许出现日志；
7. 诊断入口 ``--dump-tools`` 的 stdout 是否只有一行 JSON；
8. 假 PSK 是否泄漏到 stdout / stderr / 证据文件；
9. 启动是否产生奥维连接副作用（红线：懒连接）。

用法::

    .venv/bin/python scripts/verify_mcp_stdio.py
    .venv/bin/python scripts/verify_mcp_stdio.py --evidence-dir .cache/verify

退出码：0 = 全部检查通过；1 = 有失败（失败项打印期望 vs 实际）。
证据写入 ``<evidence-dir>/stdio-evidence-<stamp>.json`` 与 ``-latest.json``，
原始协议 transcript 写入 ``<evidence-dir>/stdio-transcript-<stamp>.log``。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from verify_stdio_harness import (  # noqa: E402
    EXPECTED_TOOLS,
    REQUIRED_INSTRUCTION_NEEDLES,
    REPO_ROOT as HARNESS_REPO_ROOT,
    HarnessSession,
    open_stdio_session,
    tool_result_is_error,
    tool_result_json,
)

EVIDENCE_DIR_DEFAULT = REPO_ROOT / ".cache" / "verify"


class Checklist:
    """轻量检查清单：收集 PASS/FAIL 与期望 vs 实际，最后统一退出。"""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def check(self, name: str, passed: bool, detail: Any = None) -> bool:
        self.items.append({"name": name, "passed": bool(passed), "detail": detail})
        mark = "PASS" if passed else "FAIL"
        suffix = ""
        if detail is not None:
            text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
            suffix = f"  [{text[:400]}]"
        print(f"[{mark}] {name}{suffix}")
        return bool(passed)

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [item for item in self.items if not item["passed"]]

    @property
    def passed(self) -> list[dict[str, Any]]:
        return [item for item in self.items if item["passed"]]


def _mvp(value: Any) -> Any:
    """把 SDK 模型压成 JSON 友好摘要（证据用）。"""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    for attr in ("model_dump", "to_dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return _mvp(fn())
            except Exception:  # noqa: BLE001
                pass
    if isinstance(value, dict):
        return {str(k): _mvp(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mvp(v) for v in value]
    return str(value)


def _parse_jsonrpc(line: str) -> tuple[bool, str | None]:
    """独立于 harness 再校验一次每行 stdout 是否合法 JSON-RPC 2.0。"""

    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return False, None
    if not isinstance(obj, dict):
        return False, None
    if obj.get("jsonrpc") != "2.0":
        return False, None
    method = obj.get("method") if isinstance(obj.get("method"), str) else None
    has_id = "id" in obj and obj["id"] is not None
    has_result = "result" in obj
    has_error = "error" in obj
    if method is not None:
        return True, f"request:{method}" if has_id else f"notification:{method}"
    if has_id and (has_result or has_error):
        return True, "response:error" if has_error else "response:result"
    return False, None


async def run_checks(checklist: Checklist, evidence_dir: Path) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repo_root": str(REPO_ROOT),
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.machine()} {platform.release()}",
        "harness": "scripts/verify_stdio_harness.py (mcp SDK Client over real stdio subprocess)",
    }

    # ------------------------------------------------------------------
    # A. 真实握手 + 无奥维时的能力查询 + 错误路径（同一会话）
    # ------------------------------------------------------------------
    async with open_stdio_session() as session:
        client = session.client
        protocol_version = client.protocol_version
        server_info = client.server_info
        instructions = client.instructions
        evidence["protocol_version"] = protocol_version
        evidence["server_info"] = _mvp(server_info)
        evidence["instructions_sha256"] = (
            hashlib.sha256(instructions.encode("utf-8")).hexdigest() if instructions else None
        )
        checklist.check(
            "A1 真实 stdio 握手成功（legacy initialize，子进程 python -m ovital_mcp）",
            bool(protocol_version) and bool(server_info),
            {"protocol_version": protocol_version, "server": _mvp(server_info)},
        )
        checklist.check(
            "A2 服务身份正确（ovital-watershed / 0.1.0）",
            getattr(server_info, "name", None) == "ovital-watershed"
            and getattr(server_info, "version", None) == "0.1.0",
            {"name": getattr(server_info, "name", None), "version": getattr(server_info, "version", None)},
        )

        # --- instructions 红线 -------------------------------------------------
        missing = [n for n in REQUIRED_INSTRUCTION_NEEDLES if not instructions or n not in instructions]
        checklist.check(
            "A3 server instructions 包含质量红线关键词",
            not missing,
            {"missing": missing, "instruction_chars": len(instructions or "")},
        )

        # --- tools/list ---------------------------------------------------------
        tools_result = await client.list_tools()
        tools = tools_result.tools
        session.remember_tools(tools)
        names = [tool.name for tool in tools]
        evidence["tools_count"] = len(names)
        evidence["tools"] = [
            {
                "name": tool.name,
                "description_chars": len(getattr(tool, "description", None) or ""),
                "input_schema_keys": sorted((getattr(tool, "input_schema", None) or {}).keys())
                if isinstance(getattr(tool, "input_schema", None), dict)
                else None,
            }
            for tool in tools
        ]
        checklist.check(
            "A4 tools/list 恰好 18 个工具且名称与契约一致",
            len(names) == 18 and set(names) == set(EXPECTED_TOOLS),
            {
                "count": len(names),
                "missing": sorted(set(EXPECTED_TOOLS) - set(names)),
                "unexpected": sorted(set(names) - set(EXPECTED_TOOLS)),
            },
        )
        schema_problems: list[str] = []
        for tool in tools:
            schema = getattr(tool, "input_schema", None)
            if not isinstance(getattr(tool, "description", None), str) or not tool.description.strip():
                schema_problems.append(f"{tool.name}: description 为空")
            if not isinstance(schema, dict):
                schema_problems.append(f"{tool.name}: inputSchema 不是 dict")
                continue
            try:
                json.dumps(schema)
            except (TypeError, ValueError) as exc:
                schema_problems.append(f"{tool.name}: schema 不可 JSON 序列化：{exc}")
                continue
            if schema.get("type") != "object":
                schema_problems.append(f"{tool.name}: schema.type={schema.get('type')!r}")
            properties = schema.get("properties", {})
            if not isinstance(properties, dict):
                schema_problems.append(f"{tool.name}: schema.properties 不是 dict")
            required = schema.get("required", [])
            if not isinstance(required, list):
                schema_problems.append(f"{tool.name}: schema.required 不是 list")
            elif not set(required).issubset(set(properties)):
                schema_problems.append(f"{tool.name}: required 含未声明属性 {set(required) - set(properties)}")
        checklist.check(
            "A5 每个工具的 inputSchema 可解析且为 object 型 JSON Schema",
            not schema_problems,
            {"problems": schema_problems[:12]},
        )

        # --- get_capabilities（无奥维） ----------------------------------------
        t0 = time.monotonic()
        cap_result = await client.call_tool("get_capabilities", {})
        cap_elapsed = round(time.monotonic() - t0, 3)
        cap_payload = tool_result_json(cap_result)
        evidence["get_capabilities"] = {
            "elapsed_s": cap_elapsed,
            "is_error": tool_result_is_error(cap_result),
            "status": cap_payload.get("status"),
            "connection": _mvp(cap_payload.get("connection")),
            "warnings": cap_payload.get("warnings"),
            "provenance": _mvp(cap_payload.get("provenance")),
            "unverified_count": len(cap_payload.get("unverified") or []),
        }
        connection = cap_payload.get("connection") or {}
        prov = cap_payload.get("provenance") or {}
        checklist.check(
            "A6 get_capabilities 在无奥维时 status=ok（不崩溃、不抛协议错误）",
            cap_payload.get("status") == "ok" and not tool_result_is_error(cap_result),
            {"status": cap_payload.get("status"), "is_error": tool_result_is_error(cap_result)},
        )
        checklist.check(
            "A7 get_capabilities 如实报告 connection.available=false 且错误结构化",
            connection.get("available") is False
            and isinstance(connection.get("error"), dict)
            and bool(connection["error"].get("code")),
            {
                "available": connection.get("available"),
                "code": (connection.get("error") or {}).get("code"),
                "message": (connection.get("error") or {}).get("message", "")[:160],
            },
        )
        checklist.check(
            "A8 get_capabilities provenance 诚实（simulated=false, verified_against_real_device=false）",
            prov.get("simulated") is False and prov.get("verified_against_real_device") is False,
            {
                "simulated": prov.get("simulated"),
                "verified_against_real_device": prov.get("verified_against_real_device"),
                "evidence_level": prov.get("evidence_level"),
            },
        )
        checklist.check(
            "A9 get_capabilities 返回 unverified[] 清单与 warnings",
            bool(cap_payload.get("unverified")) and bool(cap_payload.get("warnings")),
            {
                "unverified_count": len(cap_payload.get("unverified") or []),
                "warnings": (cap_payload.get("warnings") or [])[:2],
            },
        )
        checklist.check(
            "A10 get_capabilities 单次调用 < 5s（架构 §6）",
            cap_elapsed < 5.0,
            {"elapsed_s": cap_elapsed},
        )

        # --- 错误路径：缺奥维的工具 / 缺文件 -----------------------------------
        t0 = time.monotonic()
        sel_result = await client.call_tool("get_selection", {})
        sel_elapsed = round(time.monotonic() - t0, 3)
        sel_payload = tool_result_json(sel_result)
        evidence["get_selection_error_path"] = {
            "elapsed_s": sel_elapsed,
            "status": sel_payload.get("status"),
            "error": _mvp(sel_payload.get("error")),
        }
        checklist.check(
            "A11 无奥维时 get_selection 结构化失败（status=error + 稳定 code）",
            sel_payload.get("status") == "error"
            and isinstance(sel_payload.get("error"), dict)
            and bool(sel_payload["error"].get("code")),
            {
                "status": sel_payload.get("status"),
                "code": (sel_payload.get("error") or {}).get("code"),
                "elapsed_s": sel_elapsed,
            },
        )

        missing_dem = session.run_dir / "does-not-exist.tif"
        dem_result = await client.call_tool("inspect_dem", {"dem_path": str(missing_dem)})
        dem_payload = tool_result_json(dem_result)
        evidence["inspect_dem_error_path"] = {
            "status": dem_payload.get("status"),
            "error": _mvp(dem_payload.get("error")),
        }
        checklist.check(
            "A12 不存在的 DEM 输入结构化失败（不得静默成功）",
            dem_payload.get("status") == "error"
            and isinstance(dem_payload.get("error"), dict)
            and bool(dem_payload["error"].get("code")),
            {
                "status": dem_payload.get("status"),
                "code": (dem_payload.get("error") or {}).get("code"),
            },
        )

        # --- 失败之后服务仍存活 ------------------------------------------------
        cap_again = tool_result_json(await client.call_tool("get_capabilities", {}))
        checklist.check(
            "A13 连续失败后服务仍存活（再次 get_capabilities status=ok）",
            cap_again.get("status") == "ok",
            {"status": cap_again.get("status")},
        )

        # --- HTTP/stdio 层证据：协议 transcript --------------------------------
        transcript_path = evidence_dir / f"stdio-transcript-{time.strftime('%Y%m%dT%H%M%S')}.log"
        lines: list[str] = []
        lines.append("# MCP stdio transcript（CLIENT->SERVER 为客户端请求，SERVER->CLIENT 为服务端 stdout）")
        lines.append("# ===== CLIENT -> SERVER =====")
        lines.extend(session.sent_raw_lines)
        lines.append("# ===== SERVER -> CLIENT (stdout raw lines) =====")
        lines.extend(session.raw_stdout_lines)
        lines.append("# ===== SERVER stderr (logging; 非协议) =====")
        lines.extend(session.stderr_lines)
        transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        evidence["transcript_path"] = str(transcript_path)
        evidence["transcript_sha256"] = hashlib.sha256(transcript_path.read_bytes()).hexdigest()
        evidence["stdout_line_count"] = len(session.raw_stdout_lines)
        evidence["stderr_line_count"] = len(session.stderr_lines)
        evidence["client_sent_count"] = len(session.sent_raw_lines)

        # stdout 纯净性：逐行用独立 JSON-RPC 校验再核一遍
        nonprotocol = list(session.transport.nonprotocol_lines)
        invalid: list[str] = []
        kinds: dict[str, int] = {}
        for line in session.raw_stdout_lines:
            ok, kind = _parse_jsonrpc(line)
            if not ok:
                invalid.append(line[:200])
            elif kind:
                kinds[kind] = kinds.get(kind, 0) + 1
        evidence["stdout_kinds"] = kinds
        evidence["stdout_invalid_lines"] = invalid
        evidence["stdout_nonprotocol_from_harness"] = nonprotocol
        checklist.check(
            "A14 stdout 纯净：每一行都是合法 JSON-RPC 2.0（无 print/日志污染）",
            not nonprotocol and not invalid,
            {"lines": len(session.raw_stdout_lines), "kinds": kinds, "invalid": invalid[:3]},
        )
        checklist.check(
            "A15 客户端请求真实走了协议（initialize/tools 调用数 > 0）",
            len(session.sent_raw_lines) >= 5 and "initialize" in session.transport.sent_methods,
            {"sent": len(session.sent_raw_lines), "methods": sorted(set(session.transport.sent_methods))},
        )

        # --- stderr 行为 -------------------------------------------------------
        stderr_text = "\n".join(session.stderr_lines)
        evidence["stderr_head"] = [_desensitize(s, session) for s in session.stderr_lines[:6]]
        checklist.check(
            "A16 日志写 stderr（stderr 非空）且无 Traceback（可预期错误路径不得抛栈）",
            bool(session.stderr_lines) and "Traceback (most recent call last)" not in stderr_text,
            {"stderr_lines": len(session.stderr_lines), "traceback": "Traceback" in stderr_text},
        )

        # --- 凭据泄漏 ---------------------------------------------------------
        leaked = []
        if session.psk and session.psk in "\n".join(session.raw_stdout_lines + session.sent_raw_lines + session.stderr_lines):
            leaked.append("transcript")
        checklist.check(
            "A17 假 PSK 未出现在 stdout/stderr/协议帧中（红线：凭据脱敏）",
            not leaked,
            {"psk_length": len(session.psk), "leaked": leaked},
        )

        # --- 启动副作用（红线：懒连接）---------------------------------------
        joined_stderr = stderr_text.lower()
        checklist.check(
            "A18 启动阶段未自动连接奥维（stderr 无 connected/连接成功记录）",
            "ovital connected" not in joined_stderr,
            {"stderr_has_connected_log": "ovital connected" in joined_stderr},
        )
        data_root = session.run_dir / "data"
        task_entries: list[str] = []
        if (data_root / "tasks").is_dir():
            task_entries = [p.name for p in (data_root / "tasks").iterdir()]
        job_entries: list[str] = []
        if (data_root / "jobs").is_dir():
            job_entries = [p.name for p in (data_root / "jobs").iterdir()]
        evidence["startup_side_effects"] = {
            "data_root": str(data_root),
            "task_entries": task_entries,
            "job_entries": job_entries,
        }
        checklist.check(
            "A19 启动未创建任务/作业记录（无地图写入副作用）",
            not task_entries and not job_entries,
            {"task_entries": task_entries[:3], "job_entries": job_entries[:3]},
        )

    # --- 清理退出（会话已退出，finally 中记录了 returncode）------------------
    evidence["server_exit_code"] = session.exit_code
    evidence["server_session_elapsed_s"] = session.elapsed_s
    checklist.check(
        "A20 关闭 stdin 后服务端进程正常退出（exit code 0，无残留）",
        session.exit_code == 0,
        {"exit_code": session.exit_code, "session_elapsed_s": session.elapsed_s},
    )

    # ------------------------------------------------------------------
    # B. 无凭据配置：仍能启动并显式报配置错误（不得启动即崩溃）
    # ------------------------------------------------------------------
    async with open_stdio_session(include_psk=False) as session2:
        cap2 = tool_result_json(await session2.client.call_tool("get_capabilities", {}))
        conn2 = cap2.get("connection") or {}
        err2 = conn2.get("error") or {}
        evidence["no_psk_capabilities"] = {
            "status": cap2.get("status"),
            "available": conn2.get("available"),
            "error_code": err2.get("code"),
            "error_message": (err2.get("message") or "")[:160],
        }
        checklist.check(
            "B1 未配置 PSK 时服务仍可启动，get_capabilities 明确报 CONFIG_ERROR 类错误",
            cap2.get("status") == "ok"
            and conn2.get("available") is False
            and err2.get("code") in {"CONFIG_ERROR"},
            {
                "status": cap2.get("status"),
                "available": conn2.get("available"),
                "error_code": err2.get("code"),
            },
        )
    evidence["no_psk_session_exit_code"] = session2.exit_code

    # ------------------------------------------------------------------
    # C. 诊断入口 --dump-tools：stdout 只允许一行 JSON
    # ------------------------------------------------------------------
    dump_run_dir = evidence_dir / "dump-tools"
    dump_run_dir.mkdir(parents=True, exist_ok=True)
    dump_config = dump_run_dir / "ovital.yaml"
    dump_config.write_text(
        "ovital:\n"
        f'  endpoint: "ws://127.0.0.1:1"\n'
        '  psk: "verify-fake-psk-dump"\n'
        "storage:\n"
        f"  data_root: {json.dumps(str(dump_run_dir / 'data'))}\n"
        "logging:\n"
        '  level: "INFO"\n',
        encoding="utf-8",
    )
    dump_env = dict(os.environ)
    dump_env["OVITAL_MCP_CONFIG"] = str(dump_config)
    dump_proc = subprocess.run(
        [sys.executable, "-m", "ovital_mcp", "--dump-tools"],
        cwd=str(REPO_ROOT),
        env=dump_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    out_lines = [line for line in dump_proc.stdout.splitlines() if line.strip()]
    evidence["dump_tools"] = {
        "exit_code": dump_proc.returncode,
        "stdout_line_count": len(out_lines),
        "stderr_line_count": len([line for line in dump_proc.stderr.splitlines() if line.strip()]),
    }
    dump_json_ok = False
    dump_names: list[str] = []
    if len(out_lines) == 1:
        try:
            dump_payload = json.loads(out_lines[0])
            dump_json_ok = isinstance(dump_payload, dict) and isinstance(dump_payload.get("tools"), list)
            dump_names = dump_payload.get("tools", []) if dump_json_ok else []
        except json.JSONDecodeError:
            dump_json_ok = False
    evidence["dump_tools"]["tools_count"] = len(dump_names)
    checklist.check(
        "C1 --dump-tools stdout 只有一行可解析 JSON，exit=0，工具名 18 个",
        dump_proc.returncode == 0
        and len(out_lines) == 1
        and dump_json_ok
        and set(dump_names) == set(EXPECTED_TOOLS),
        {
            "exit": dump_proc.returncode,
            "stdout_lines": len(out_lines),
            "json_ok": dump_json_ok,
            "tools": len(dump_names),
        },
    )
    checklist.check(
        "C2 --dump-tools 的日志只走 stderr（stdout 无日志行）",
        " INFO " not in dump_proc.stdout and (" INFO " in dump_proc.stderr or dump_proc.stderr.strip() != ""),
        {"stdout_has_INFO": " INFO " in dump_proc.stdout, "stderr_kb": round(len(dump_proc.stderr) / 1024, 2)},
    )
    checklist.check(
        "C3 --dump-tools 不泄漏假 PSK",
        "verify-fake-psk-dump" not in dump_proc.stdout and "verify-fake-psk-dump" not in dump_proc.stderr,
        None,
    )

    return evidence


def _desensitize(text: str, session: HarnessSession) -> str:
    text = text.replace(str(session.run_dir), "<run_dir>")
    return text.replace(str(REPO_ROOT), "<repo>")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCP stdio 真实性验证（独立验收脚本）")
    parser.add_argument(
        "--evidence-dir",
        default=str(EVIDENCE_DIR_DEFAULT),
        help="证据输出目录（默认 <repo>/.cache/verify）",
    )
    args = parser.parse_args(argv)

    evidence_dir = Path(args.evidence_dir)
    if not evidence_dir.is_absolute():
        evidence_dir = REPO_ROOT / evidence_dir
    evidence_dir.mkdir(parents=True, exist_ok=True)

    if HARNESS_REPO_ROOT != REPO_ROOT:  # pragma: no cover - 防御性
        print(f"[WARN] harness REPO_ROOT={HARNESS_REPO_ROOT} != {REPO_ROOT}", file=sys.stderr)

    checklist = Checklist()
    started = time.monotonic()
    try:
        evidence = asyncio.run(run_checks(checklist, evidence_dir))
    except Exception as exc:  # noqa: BLE001 - 脚本失败必须显式
        print(f"[FAIL] 验证脚本自身异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1
    elapsed = round(time.monotonic() - started, 2)

    stamp = time.strftime("%Y%m%dT%H%M%S")
    report = {
        **evidence,
        "elapsed_s": elapsed,
        "checks_total": len(checklist.items),
        "checks_passed": len(checklist.passed),
        "checks_failed": len(checklist.failed),
        "checks": checklist.items,
        "passed": not checklist.failed,
    }
    evidence_path = evidence_dir / f"stdio-evidence-{stamp}.json"
    evidence_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path = evidence_dir / "stdio-evidence-latest.json"
    latest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"证据：{evidence_path}")
    print(f"最新：{latest_path}")
    print(f"transcript：{report.get('transcript_path')}")
    print(
        f"结果：{len(checklist.passed)}/{len(checklist.items)} 项通过，"
        f"{len(checklist.failed)} 项失败，用时 {elapsed}s"
    )
    if checklist.failed:
        print("失败项：")
        for item in checklist.failed:
            print(f"  - {item['name']}  实际={item['detail']}")
        return 1
    print("阶段 1 stdio 验证：全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
