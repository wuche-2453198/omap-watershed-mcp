"""草稿写入安全与 provenance 诚实性测试（Lead 回归用例）。

覆盖 Lead 在集成测试中发现并修复的真实缺陷：
1. rollback 曾误删既有草稿文件夹（20112 响应回显父文件夹）；
2. 陈旧幂等键（对象已删除/已回滚）会挡住后续写入；
3. load_job_result 曾把 worker 外层包装当引擎成果；
4. ovital_provenance 曾把“连上非模拟服务器”当成真实设备已验证。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio

from ovital_mcp.app import AppContext
from ovital_mcp.config import AppConfig
from ovital_mcp.mock.server import MockOvitalServer
from ovital_mcp.server import create_server
from ovital_mcp.tools.common import ovital_provenance, provenance

PSK = "unit-test-psk"


@pytest_asyncio.fixture()
async def mock_ovital():
    server = MockOvitalServer(PSK)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _config(tmp_path: Path, endpoint: str, **ovital_extra) -> AppConfig:
    data = {
        "ovital": {
            "endpoint": endpoint,
            "psk": PSK,
            "connect_timeout_s": 3,
            **ovital_extra,
        },
        "storage": {"data_root": str(tmp_path / "data")},
        "gis": {"engine": "native", "snap_radius_m": 0},
    }
    return AppConfig.model_validate(data)


def _tool(mcp, name: str):
    tool = mcp._tool_manager.get_tool(name)
    assert tool is not None, name
    return tool.fn


SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [
            [116.30, 39.95],
            [116.32, 39.95],
            [116.32, 39.97],
            [116.30, 39.97],
            [116.30, 39.95],
        ]
    ],
}


@pytest.mark.asyncio
async def test_write_readback_rollback_preserves_existing_folder(tmp_path: Path, mock_ovital) -> None:
    ctx = AppContext(config=_config(tmp_path, mock_ovital.url))
    mcp = create_server(ctx)
    write_draft = _tool(mcp, "write_draft")
    read_back = _tool(mcp, "read_back")
    rollback = _tool(mcp, "rollback_change")

    first = await write_draft(geojson=SQUARE, draft_folder="unit 草稿", name="unit-1")
    assert first["status"] == "ok", first
    folder_id = first["folder_id"]
    obj_id = first["obj_id"]
    assert first["reused"] is False
    assert first["readback"]["vertex_comparison"]["ok"] is True

    second = await write_draft(geojson=SQUARE, draft_folder="unit 草稿", name="unit-2")
    assert second["status"] == "ok"
    assert second["folder_id"] == folder_id  # 复用文件夹

    # 回读
    back = await read_back(obj_id=obj_id)
    assert back["status"] == "ok"
    assert back["vertex_count"] == 4

    # 回滚第二笔：只删本次创建的对象，不得删既有文件夹
    assert second["change_id"]
    rolled = await rollback(change_id=second["change_id"])
    assert rolled["status"] == "ok"
    report = rolled["report"]
    assert folder_id not in report["deleted_obj_ids"]
    assert set(report["deleted_obj_ids"]) == {second["obj_id"], second["sign_obj_id"]}

    # 文件夹仍在：第一笔对象也可继续回读
    assert (await read_back(obj_id=obj_id))["status"] == "ok"


@pytest.mark.asyncio
async def test_stale_idempotency_key_recovers(tmp_path: Path, mock_ovital) -> None:
    ctx = AppContext(config=_config(tmp_path, mock_ovital.url))
    mcp = create_server(ctx)
    write_draft = _tool(mcp, "write_draft")
    rollback = _tool(mcp, "rollback_change")

    first = await write_draft(geojson=SQUARE, draft_folder="unit 草稿", name="stale-test")
    assert first["status"] == "ok"
    await rollback(change_id=first["change_id"])

    # 回滚清除了幂等键，同一参数可重新写入
    again = await write_draft(geojson=SQUARE, draft_folder="unit 草稿", name="stale-test")
    assert again["status"] == "ok", again
    assert again["obj_id"] != first["obj_id"]

    # 手动制造陈旧幂等键（指向不存在对象）→ 应清除并继续创建，而不是卡死
    key = again["idempotency_key"]
    ctx.store.idempotency_put(
        f"draft:{key}", {"obj_id": 999_999_999, "folder_id": again["folder_id"], "change_id": "c-old"}
    )
    stale = await write_draft(geojson=SQUARE, draft_folder="unit 草稿", name="stale-test")
    assert stale["status"] == "ok", stale
    assert any("已不存在" in w for w in stale["warnings"])


def test_ovital_provenance_never_optimistic(tmp_path: Path) -> None:
    # 未连接：false
    ctx = AppContext(config=_config(tmp_path, "ws://127.0.0.1:1"))
    prov = provenance("official_document")
    assert prov["verified_against_real_device"] is False
    # 显式设置 real_device_verified=true，但仍不可在未连接时断言
    cfg = _config(tmp_path, "ws://127.0.0.1:1", real_device_verified=True)
    ctx2 = AppContext(config=cfg)
    # 模拟连接状态（不真正连接）
    ctx2._ovital = type("FakeClient", (), {"connected": True, "is_simulated": True})()
    prov = ovital_provenance(ctx2)
    assert prov["verified_against_real_device"] is False or prov["simulated"] is True
    ctx3 = AppContext(config=_config(tmp_path, "ws://127.0.0.1:1", real_device_verified=True))
    ctx3._ovital = type("FakeClient", (), {"connected": True, "is_simulated": False})()
    prov3 = ovital_provenance(ctx3)
    assert prov3["verified_against_real_device"] is True  # 只有人工显式标记才允许
    assert prov3["simulated"] is False


def test_load_job_result_unwraps_worker_payload(tmp_path: Path) -> None:
    from ovital_mcp.tools.common import load_job_result

    ctx = AppContext(config=_config(tmp_path, "ws://127.0.0.1:1"))
    job_dir = ctx.jobs.jobs_root / "j-unit"
    job_dir.mkdir(parents=True)
    (job_dir / "artifacts").mkdir()
    job = {
        "job_id": "j-unit",
        "kind": "watershed",
        "status": "succeeded",
        "created_at": "2026-01-01T00:00:00+00:00",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:00+00:00",
        "pid": None,
        "params": {},
        "progress": {"stage": "done"},
    }
    (job_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
    result = {
        "ok": True,
        "result": {
            "ok": True,
            "boundary_geojson": str(job_dir / "artifacts" / "boundary.geojson"),
            "area_m2": 123.0,
        },
        "review": {"figures": []},
    }
    (job_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    job_dict, inner = load_job_result(ctx, "j-unit")
    assert job_dict["job_id"] == "j-unit"
    assert inner["area_m2"] == 123.0


@pytest.mark.asyncio
async def test_write_draft_rejects_multipart_and_holes(tmp_path: Path, mock_ovital) -> None:
    ctx = AppContext(config=_config(tmp_path, mock_ovital.url))
    mcp = create_server(ctx)
    write_draft = _tool(mcp, "write_draft")

    multipart = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[116.30, 39.95], [116.31, 39.95], [116.31, 39.96], [116.30, 39.95]]],
            [[[116.33, 39.95], [116.34, 39.95], [116.34, 39.96], [116.33, 39.95]]],
        ],
    }
    result = await write_draft(geojson=multipart)
    assert result["status"] == "error"
    assert "多部件" in result["error"]["message"]

    with_hole = {
        "type": "Polygon",
        "coordinates": [
            [[116.30, 39.95], [116.34, 39.95], [116.34, 39.99], [116.30, 39.99], [116.30, 39.95]],
            [[116.31, 39.96], [116.32, 39.96], [116.32, 39.97], [116.31, 39.96]],
        ],
    }
    result = await write_draft(geojson=with_hole)
    assert result["status"] == "error"
    assert "洞" in result["error"]["message"]


@pytest.mark.asyncio
async def test_get_capabilities_does_not_claim_real_device_without_manual_flag(tmp_path: Path) -> None:
    """F-09 回归：连上“非模拟”服务端也不得自动标 verified_against_real_device=true。"""

    class FakeClient:
        connected = True
        is_simulated = False
        server_version = 817

        async def request(self, *args, **kwargs):
            raise RuntimeError("probe intentionally unavailable in test")

    cfg = _config(tmp_path, "ws://127.0.0.1:9")
    ctx = AppContext(config=cfg)
    ctx._ovital = FakeClient()
    mcp = create_server(ctx)
    out = await _tool(mcp, "get_capabilities")()
    assert out["status"] == "ok"
    assert out["provenance"]["verified_against_real_device"] is False

    cfg2 = _config(tmp_path, "ws://127.0.0.1:9", real_device_verified=True)
    ctx2 = AppContext(config=cfg2)
    ctx2._ovital = FakeClient()
    mcp2 = create_server(ctx2)
    out2 = await _tool(mcp2, "get_capabilities")()
    # 人工显式标记 + 非模拟连接，才允许为 true（本机实际不会发生）
    assert out2["provenance"]["verified_against_real_device"] is True


@pytest.mark.asyncio
async def test_finalize_then_rollback_restores_object(tmp_path: Path, mock_ovital) -> None:
    """finalize 走 20112 替换语义；rollback 用快照恢复原对象（模拟环境验证）。"""

    ctx = AppContext(config=_config(tmp_path, mock_ovital.url))
    mcp = create_server(ctx)
    write_draft = _tool(mcp, "write_draft")
    finalize = _tool(mcp, "finalize_draft")
    rollback = _tool(mcp, "rollback_change")
    read_object = _tool(mcp, "read_object")

    draft = await write_draft(geojson=SQUARE, draft_folder="unit 草稿", name="待定稿草稿")
    assert draft["status"] == "ok"
    obj_id = draft["obj_id"]

    fin = await finalize(obj_id=obj_id, confirm=True, new_name="已定稿-正式")
    assert fin["status"] == "ok", fin
    after = await read_object(obj_id=obj_id)
    assert after["name"] == "已定稿-正式"

    rolled = await rollback(change_id=fin["change_id"])
    assert rolled["status"] == "ok"
    restored = await read_object(obj_id=obj_id)
    assert restored["name"] == "待定稿草稿"

    # finalize 必须显式 confirm
    rejected = await finalize(obj_id=obj_id, confirm=False)
    assert rejected["status"] == "error"
    assert "confirm" in rejected["error"]["message"]
