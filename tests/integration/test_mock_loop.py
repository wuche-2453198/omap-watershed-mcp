"""阶段 2 集成测试：模拟奥维服务 + 真实 MCP stdio 的最小闭环回归。

与 ``scripts/verify_mock_loop.py``（完整证据脚本）互补：这里只保留**快、稳定、可长期回归**的断言，
避免全量证据采集拖慢 `pytest`。覆盖：
- 模拟服务器（真实 ``scripts/run_mock_ovital.py`` 子进程）连接后 capabilities 标记
  ``simulated=true / evidence_level=offline_simulated / verified_against_real_device=false``；
- 伪造 ``ovital.real_device_verified=true`` + 模拟连接仍不得声称真实设备验证（F-09 回归）；
- 草稿写入 → 幂等键复用 → 回读顶点一致 → 回滚删除，全程结构化返回且 stdout 纯净。

运行::

    .venv/bin/python -m pytest tests/integration/test_mock_loop.py -q
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Coroutine

from verify_mock_ovital import running_mock_ovital
from verify_stdio_harness import open_stdio_session, tool_result_json

GEOJSON_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[116.990, 39.740], [117.000, 39.740], [117.000, 39.750],
                     [116.990, 39.750], [116.990, 39.740]]],
}


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def test_mock_connection_marks_simulated_and_not_real_device(tmp_path: Path) -> None:
    async def scenario() -> None:
        run_dir = tmp_path / "mock"
        with running_mock_ovital(run_dir, psk="test-psk") as mock:
            async with open_stdio_session(endpoint=mock.url, psk="test-psk", run_dir=run_dir / "mcp") as session:
                capability = tool_result_json(await session.client.call_tool("get_capabilities", {}))
                connection = capability["connection"]
                provenance = capability["provenance"]
                assert capability["status"] == "ok"
                assert connection["available"] is True
                assert connection["simulated"] is True
                assert (connection.get("version") or {}).get("value") == 817
                assert provenance["simulated"] is True
                assert provenance["evidence_level"] == "offline_simulated"
                assert provenance["verified_against_real_device"] is False
                # stdout 仍然纯净
                assert session.transport.nonprotocol_lines == []

    run(scenario())


def test_forged_real_device_flag_with_mock_stays_false(tmp_path: Path) -> None:
    """红线回归：即使配置伪造 real_device_verified=true，模拟连接也不得声称真实设备验证。"""

    async def scenario() -> None:
        run_dir = tmp_path / "forged"
        with running_mock_ovital(run_dir, psk="test-psk") as mock:
            async with open_stdio_session(
                endpoint=mock.url,
                psk="test-psk",
                run_dir=run_dir / "mcp",
                ovital_extra={"real_device_verified": True},
            ) as session:
                capability = tool_result_json(await session.client.call_tool("get_capabilities", {}))
                assert capability["connection"]["simulated"] is True
                assert capability["provenance"]["verified_against_real_device"] is False

    run(scenario())


def test_draft_write_idempotent_readback_rollback(tmp_path: Path) -> None:
    async def scenario() -> None:
        run_dir = tmp_path / "draft"
        with running_mock_ovital(run_dir, psk="test-psk") as mock:
            async with open_stdio_session(endpoint=mock.url, psk="test-psk", run_dir=run_dir / "mcp") as session:
                client = session.client
                first = tool_result_json(await client.call_tool(
                    "write_draft",
                    {"geojson": GEOJSON_POLYGON, "draft_folder": "回归草稿", "name": "回归",
                     "idempotency_key": "integration-1"},
                ))
                assert first["status"] == "ok"
                obj_id = first["obj_id"]
                assert isinstance(obj_id, int) and first["change_id"]
                comparison = (first.get("readback") or {}).get("vertex_comparison") or {}
                assert comparison.get("ok") is True
                assert (comparison.get("max_delta_deg") or 0.0) <= 1e-6

                second = tool_result_json(await client.call_tool(
                    "write_draft",
                    {"geojson": GEOJSON_POLYGON, "draft_folder": "回归草稿", "name": "回归",
                     "idempotency_key": "integration-1"},
                ))
                assert second["status"] == "ok"
                assert second["obj_id"] == obj_id
                assert second.get("reused") is True

                readback = tool_result_json(await client.call_tool("read_back", {"obj_id": obj_id}))
                assert readback["status"] == "ok"
                assert readback["vertex_count"] == 4

                rolled = tool_result_json(await client.call_tool(
                    "rollback_change", {"change_id": first["change_id"]}))
                assert rolled["status"] == "ok"
                assert obj_id in (rolled.get("report") or {}).get("deleted_obj_ids", [])
                after = tool_result_json(await client.call_tool("read_back", {"obj_id": obj_id}))
                assert after["status"] == "error"
                assert (after.get("error") or {}).get("code") == "OVITAL_NOT_FOUND"
                # 全程 stdout 纯净
                assert session.transport.nonprotocol_lines == []
                # 工具返回中无伪造的真实设备验证
                dumped = json.dumps([first, second, readback, rolled], ensure_ascii=False)
                assert '"verified_against_real_device": true' not in dumped

        assert session.exit_code == 0

    run(scenario())
