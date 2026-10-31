"""MCP 服务层测试：工具注册、启动无副作用、诊断输出不污染 stdout。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ovital_mcp.app import AppContext
from ovital_mcp.config import AppConfig, load_config
from ovital_mcp.server import create_server

EXPECTED_TOOLS = {
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
}


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "ovital.yaml"
    path.write_text(
        "\n".join(
            [
                "ovital:",
                "  endpoint: ws://127.0.0.1:1",  # 故意不可连接
                "  connect_timeout_s: 0.3",
                f'  psk: "test-secret-psk"',
                "storage:",
                f"  data_root: {json.dumps(str(tmp_path / 'data'))}",
                "gis:",
                "  engine: native",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_create_server_registers_all_tools(tmp_path: Path) -> None:
    ctx = AppContext(config=load_config(_config_file(tmp_path)))
    mcp = create_server(ctx)
    names = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert names == EXPECTED_TOOLS
    assert len(names) == 18


def test_startup_does_not_connect_or_write_map(tmp_path: Path) -> None:
    ctx = AppContext(config=load_config(_config_file(tmp_path)))
    create_server(ctx)
    # 启动后不应建立奥维连接（根因：本机没有奥维客户端，连接会失败/超时）
    assert ctx._ovital is None
    # 不应创建任务/变更记录
    assert list(ctx.paths.tasks.iterdir()) == []
    assert list((ctx.paths.data_root / "jobs").iterdir()) == []


def test_instructions_contain_quality_redlines(tmp_path: Path) -> None:
    from ovital_mcp.server import INSTRUCTIONS

    assert "不得为了凑目标面积而修改边界" in INSTRUCTIONS
    assert "review_segment" in INSTRUCTIONS
    assert "verified_against_real_device" in INSTRUCTIONS


def test_dump_tools_stdout_is_pure_json(tmp_path: Path) -> None:
    config_path = _config_file(tmp_path)
    env = dict(os.environ)
    env["OVITAL_MCP_CONFIG"] = str(config_path)
    proc = subprocess.run(
        [sys.executable, "-m", "ovital_mcp", "--dump-tools"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert proc.returncode == 0, proc.stderr
    # stdout 只允许一行 JSON（日志必须在 stderr）
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, proc.stdout
    payload = json.loads(lines[0])
    assert set(payload["tools"]) == EXPECTED_TOOLS
    assert payload["server"]["name"] == "ovital-watershed-mcp"


def test_ovital_dependent_tool_returns_structured_error_when_unreachable(tmp_path: Path) -> None:
    """无奥维时，需要奥维的工具必须返回结构化 error（不崩溃、不伪造成功）。"""

    import asyncio

    ctx = AppContext(config=load_config(_config_file(tmp_path)))
    mcp = create_server(ctx)
    tool = mcp._tool_manager.get_tool("get_selection")
    assert tool is not None
    result = asyncio.run(tool.fn())
    assert isinstance(result, dict)
    assert result["status"] == "error"
    assert result["error"]["code"] in {"OVITAL_CONNECTION_ERROR", "OVITAL_AUTH_ERROR", "CONFIG_ERROR"}
    assert result["provenance"]["verified_against_real_device"] is False
    # 启动阶段仍未建立连接
    assert ctx._ovital is None
