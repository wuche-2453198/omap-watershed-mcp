#!/usr/bin/env python
"""协议层端到端回放：官方夹具 + 模拟奥维服务器 + 完整读写闭环。

做什么
======
1. 回放 ``ovital_mcp/mock/fixtures/*.json``（全部来自官方 V2.7 手册/官方样例 SDK），
   逐条经过真实的编解码与解析路径；
2. 在本机启动**模拟**奥维 WebSocket 服务器，用 ``OvitalClient`` 跑通：
   登录 → 能力探测 → 写草稿（文件夹+图形+出口标签）→ 回读坐标往返 →
   20802 量算对账 → 20301 地图图片（带 SIMULATED MAP 水印）→ 20303 等高线元数据 →
   20016/20730/20212 视图 → 20107 查找 → 20103 删除 → 回读确认为空；
3. 注入故障验证防护：断线不自动重发写、超时不重试、重复 20112 响应不重复写入、
   -7 权限错误正确映射；
4. 跑两遍并比较确定性摘要（同一操作序列 → 同一对象 ID/面积/PNG sha256），
   证明可重复；
5. 输出脱敏证据与样例到 ``docs/protocol-samples/``（每条样例带 ``_source``）。

边界（必须如实标注）
====================
- 本机是 macOS，没有 Windows 奥维客户端与真实账号 → **真实联机未验证**：
  ``simulated=true``、``verified_against_real_device=false``、
  ``evidence_level="offline_simulated"``；
- 模拟通过 ≠ 真实设备通过；手册未写的行为一律列入 ``unverified``。

用法::

    .venv/bin/python scripts/replay_protocol.py
    .venv/bin/python scripts/replay_protocol.py --out docs/protocol-samples
    .venv/bin/python scripts/replay_protocol.py --no-write   # 只跑不落盘（CI 用）

退出码：0 全部通过；1 有步骤失败（会打印失败明细）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ovital_mcp.errors import OmapError, OvitalPrereqError  # noqa: E402
from ovital_mcp.mock import Faults, MockOvitalServer  # noqa: E402
from ovital_mcp.protocol import OvitalClient, commands, constants as C, objects  # noqa: E402
from ovital_mcp.protocol.codec import (  # noqa: E402
    decode_message,
    encode_request,
    redact_msg,
)
from ovital_mcp.protocol.errors import OvitalTimeoutError  # noqa: E402
from ovital_mcp.protocol.models import (  # noqa: E402
    parse_contour_response,
    parse_map_data_response,
    parse_obj_items,
    parse_search_results,
    parse_selected_list,
    parse_shape_metrics,
    parse_screen_corners,
    parse_system_crs,
    parse_viewport_rect,
    parse_vip_level,
    parse_write_results,
)

PSK = "test-psk-not-secret"  # 测试值；禁止真实凭据
FIXTURES_DIR = REPO_ROOT / "ovital_mcp" / "mock" / "fixtures"
DEFAULT_OUT = REPO_ROOT / "docs" / "protocol-samples"
MANUAL_URL = C.MANUAL_URL

#: 草稿图形（约 0.01°×0.01°，39.01N 附近 ≈ 0.86 km × 1.11 km）。
DRAFT_VERTICES = [
    (106.5700, 39.0100),
    (106.5800, 39.0100),
    (106.5800, 39.0200),
    (106.5700, 39.0200),
]
OUTLET = (106.5750, 39.0150)
DRAFT_FOLDER = "OMAP-MCP 回放草稿"

#: 样例来源映射（每条样例的 _source；全部指向官方手册章节）。
SAMPLE_SOURCES: dict[str, dict[str, Any]] = {
    "20000": {
        "manual_section": "2.3.1 连接奥维互动地图",
        "sdk": "omapInterfaceWeb_latest/js/omapinterface.js#onopen 登录",
        "note": "md5psk 已脱敏；成功时 ret 是版本号（2.3.1.2.1）",
    },
    "20006": {"manual_section": "2.3.24 获取奥维互动地图版本"},
    "20101": {
        "manual_section": "2.3.11 获取收藏夹对象",
        "note": "LatlngType=1 请求 CGCS2000（手册 2.2.2）",
    },
    "20103": {
        "manual_section": "2.3.13 删除收藏夹对象",
        "note": "NoGroup 字段；成功响应形态手册未给（待实测）",
    },
    "20104": {"manual_section": "2.3.17 获取奥维端选中的收藏夹对象基本信息"},
    "20107": {
        "manual_section": "2.3.22 查找奥维对象",
        "note": "响应只含 ObjID/ObjType，不含名称",
    },
    "20110": {"manual_section": "2.3.18 获取奥维端选中的收藏夹对象树形结构详细信息"},
    "20112": {
        "manual_section": "2.3.12 设置收藏夹对象",
        "note": "响应形态有手册扁平与官方样例嵌套两种，本样例为模拟器输出（待实测真实形态）",
    },
    "20212": {"manual_section": "2.3.28 获取当前点在地图下像素比例"},
    "20222": {
        "manual_section": "2.3.51 获取奥维系统坐标系参数",
        "note": "CoordType 是独立枚举（2=经纬度,4=横轴墨卡托）",
    },
    "20301": {
        "manual_section": "2.3.29 获取地图图片数据",
        "note": "Data 为模拟 PNG（已截断）；真实图片需 SVIP 且格式为 base64 PNG",
    },
    "20303": {
        "manual_section": "2.3.31 获取等高线",
        "note": "手册只给元数据字段，未定义等高线数据数组格式（待实测）",
    },
    "20501": {"manual_section": "2.3.19 获取VIP等级"},
    "20730": {"manual_section": "2.3.52 获取奥维可显示区域"},
    "20802": {
        "manual_section": "2.3.48 获取图形对象基本信息",
        "note": "面积 ㎡、周长 km；表格与示例字段名不一致，两种都接受",
    },
    "20015": {"manual_section": "2.3.49 监测奥维对象改变（20015 事件）"},
    "20016": {
        "manual_section": "2.3.58 获取屏幕中心点、左上角、右下角坐标",
        "note": "ReqCoordType 是独立枚举；手册示例数值错位（待实测）",
    },
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


class StepLog:
    """记录每个步骤的结果与失败，最后统一报告。"""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []

    async def run(self, name: str, coro: Callable[[], Any], *, cmdid: int | None = None) -> Any:
        started = time.monotonic()
        entry: dict[str, Any] = {"step": name, "cmdid": cmdid, "ok": True}
        try:
            result = await coro()
            entry["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            if isinstance(result, (str, int, float, bool)) or result is None:
                entry["result"] = result
            self.steps.append(entry)
            return result
        except Exception as exc:  # noqa: BLE001 - 回放需要记录而不是中断
            entry["ok"] = False
            entry["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            entry["error"] = f"{type(exc).__name__}: {exc}"
            self.steps.append(entry)
            self.failures.append(entry)
            return None


def digest(obj: Any) -> str:
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def independent_area(vertices: list[tuple[float, float]]) -> dict[str, float]:
    """独立 GIS 计算（不复用 mock 世界代码），用于 20802 对账。"""

    from pyproj import Transformer
    from shapely.geometry import Polygon

    from ovital_mcp.coordinates import metric_crs_for

    mean_lon = sum(point[0] for point in vertices) / len(vertices)
    mean_lat = sum(point[1] for point in vertices) / len(vertices)
    crs = metric_crs_for(mean_lon, mean_lat, kind="utm")
    transformer = Transformer.from_crs("EPSG:4490", crs, always_xy=True)
    polygon = Polygon([transformer.transform(lon, lat) for lon, lat in vertices])
    return {
        "area_m2": float(polygon.area),
        "perimeter_km": float(polygon.length / 1000.0),
        "metric_crs": crs,
    }


def last_exchange(server: MockOvitalServer, cmdid: int) -> tuple[dict | None, dict | None]:
    request = next(
        (item for item in reversed(server.requests) if item["cmdid"] == cmdid), None
    )
    response = next(
        (item for item in reversed(server.responses) if item["cmdid"] == cmdid), None
    )
    return request, response


def sanitize_request(request: dict | None) -> dict | None:
    if not request:
        return None
    return {
        "cmdid": request["cmdid"],
        "ret": request["ret"],
        "msg": redact_msg(request["msg"], [PSK]),
    }


def sample_payload(cmdid: int, request: dict | None, response: dict | None) -> dict[str, Any]:
    """生成一条脱敏样例（请求/响应成对，带 _source）。"""

    key = str(cmdid)
    source = {
        "manual_section": SAMPLE_SOURCES.get(key, {}).get("manual_section", "手册"),
        "manual_url": MANUAL_URL,
        "evidence_level": "official_document",
        "verified_against_real_device": False,
        "simulated": True,
    }
    source.update(
        {k: v for k, v in SAMPLE_SOURCES.get(key, {}).items() if k != "manual_section"}
    )
    payload: dict[str, Any] = {
        "_kind": "mock_exchange",
        "_source": source,
        "simulated": True,
        "verified_against_real_device": False,
        "request": sanitize_request(request),
        "response": None,
    }
    if response:
        msg = dict(response["msg"])
        data = msg.get("Data")
        if isinstance(data, str) and len(data) > 80:
            msg["Data"] = data[:80] + "…"
            msg["_data_truncated"] = True
        payload["response"] = {
            "cmdid": response["cmdid"],
            "ret": response["ret"],
            "msg": msg,
        }
    return payload


# ---------------------------------------------------------------------------
# 官方夹具回放
# ---------------------------------------------------------------------------


def replay_official_fixtures() -> dict[str, Any]:
    """把 mock/fixtures 下每条官方夹具走一遍真实编解码/解析路径。"""

    parsed = 0
    encodable = 0
    failures: list[str] = []
    by_cmdid: dict[int, str] = {}

    for path in sorted(FIXTURES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        cmdid = int(data["cmdid"])
        by_cmdid[cmdid] = path.name
        source = data.get("_source") or {}
        if not source.get("manual_section") or not source.get("manual_url"):
            failures.append(f"{path.name}: 缺少 _source")
            continue
        try:
            if data.get("_kind") == "request":
                frame = encode_request(cmdid, data.get("msg", {}))
                envelope = json.loads(frame)
                assert envelope["ret"] == 0 and envelope["cmdid"] == cmdid
                encodable += 1
            else:
                response = decode_message(json.dumps(data))
                _parse_fixture_response(response.cmdid, response.msg)
                parsed += 1
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path.name}: {type(exc).__name__}: {exc}")

    return {
        "fixtures_total": len(list(FIXTURES_DIR.glob("*.json"))),
        "requests_encoded": encodable,
        "responses_parsed": parsed,
        "failures": failures,
        "fixture_files": by_cmdid,
    }


def _parse_fixture_response(cmdid: int, msg: dict[str, Any]) -> None:
    """按 cmdid 调用真实解析器（验证官方样例能被生产代码消费）。"""

    if cmdid in (C.CMD_GET_OBJECT, C.CMD_GET_SELECTED_TREE, C.CMD_WRITE_OBJECT_TREE):
        if cmdid == C.CMD_WRITE_OBJECT_TREE:
            parse_write_results(msg)
        else:
            parse_obj_items(msg)
    elif cmdid == C.CMD_GET_SELECTED:
        parse_selected_list(msg)
    elif cmdid == C.CMD_GET_SHAPE_METRICS:
        parse_shape_metrics(msg)
    elif cmdid == C.CMD_GET_MAP_DATA:
        parse_map_data_response(msg)
    elif cmdid == C.CMD_GET_CONTOUR:
        parse_contour_response(msg)
    elif cmdid == C.CMD_GET_SCREEN_COORDS:
        parse_screen_corners(msg)
    elif cmdid == C.CMD_GET_OMAP_WND_RECT:
        parse_viewport_rect(msg)
    elif cmdid == C.CMD_GET_SYS_CRS:
        parse_system_crs(msg)
    elif cmdid == C.CMD_GET_VIP_LEVEL:
        parse_vip_level(msg)
    elif cmdid == C.CMD_SEARCH_OBJECT:
        parse_search_results(msg)
    elif cmdid in (C.CMD_LOGIN, C.CMD_GET_VERSION, C.CMD_OBJ_CHANGED):
        pass  # 外壳/事件：decode_message 已覆盖
    else:  # pragma: no cover - 夹具里没有其它命令
        raise AssertionError(f"未覆盖的夹具 cmdid={cmdid}")


# ---------------------------------------------------------------------------
# 主闭环
# ---------------------------------------------------------------------------


async def run_closure(*, write_style: str, record_samples: bool) -> dict[str, Any]:
    """跑一遍完整读写闭环，返回结构化证据。"""

    log = StepLog()
    server = MockOvitalServer(
        PSK, port=0, write_response_style=write_style, faults=Faults()
    )
    await server.start()
    client = OvitalClient(server.url, PSK, request_timeout_s=5.0)
    samples: dict[str, dict[str, Any]] = {}
    try:
        version = await log.run("login", client.connect, cmdid=C.CMD_LOGIN)
        assert version is not None, "登录失败"
        assert version.ret > 0, "登录 ret 应为版本号"
        assert client.is_simulated is True, "模拟服务器必须自标识 simulated"

        capabilities = await log.run("probe_capabilities", lambda: commands.probe_capabilities(client))
        assert capabilities and capabilities["status"] == "ok", f"能力探测失败：{capabilities}"

        await log.run("get_version", lambda: commands.get_version(client), cmdid=C.CMD_GET_VERSION)
        await log.run("get_vip_level", lambda: commands.get_vip_level(client), cmdid=C.CMD_GET_VIP_LEVEL)
        await log.run("get_system_crs", lambda: commands.get_system_crs(client), cmdid=C.CMD_GET_SYS_CRS)
        await log.run("get_selected_empty", lambda: commands.get_selected(client), cmdid=C.CMD_GET_SELECTED)

        # ---- 写草稿：文件夹 + 图形 + 出口标签 ----
        shape_node = objects.build_shape_object(
            name="集水区草稿", comment="离线回放；非真实成果", vertices_lonlat=DRAFT_VERTICES
        )
        sign_node = objects.build_sign_object(
            name="出口", comment="出口点（模拟）", lon=OUTLET[0], lat=OUTLET[1]
        )
        tree = objects.build_write_tree(
            folder_id=0, folder_name=DRAFT_FOLDER, children=[shape_node, sign_node]
        )
        results = await log.run(
            "write_draft_tree",
            lambda: commands.write_tree(client, tree),
            cmdid=C.CMD_WRITE_OBJECT_TREE,
        )
        assert results, "写入失败"
        by_type = {item.type_id: item.obj_id for item in results}
        assert C.OBJ_TYPE_GROUP in by_type, "20112 未返回文件夹 obj_id（手册示例如此，真实设备待实测）"
        folder_id = by_type[C.OBJ_TYPE_GROUP]
        shape_id = by_type[C.OBJ_TYPE_SHAPE]
        sign_id = by_type[C.OBJ_TYPE_SIGN]
        if record_samples:
            samples["20112"] = sample_payload(
                C.CMD_WRITE_OBJECT_TREE, *last_exchange(server, C.CMD_WRITE_OBJECT_TREE)
            )
            samples["20015"] = sample_payload(
                C.CMD_OBJ_CHANGED, *last_exchange(server, C.CMD_OBJ_CHANGED)
            )
            samples["20000"] = sample_payload(
                C.CMD_LOGIN, *last_exchange(server, C.CMD_LOGIN)
            )

        # 事件：写入后应有 20015（手册 2.3.49）
        event = await log.run("next_event_20015", lambda: client.next_event(C.CMD_OBJ_CHANGED, timeout=2.0))
        assert event is not None and event.msg.get("Flag") == 0

        # ---- 回读 + 坐标往返 ----
        shape = await log.run("read_back_shape", lambda: commands.get_shape(client, shape_id), cmdid=C.CMD_GET_OBJECT)
        assert shape is not None
        comparison = commands.compare_vertices(DRAFT_VERTICES, shape.vertices)
        log.steps.append({"step": "compare_vertices", "ok": comparison["within_tolerance"], "result": comparison})
        if not comparison["within_tolerance"]:
            log.failures.append({"step": "compare_vertices", "ok": False, "error": "坐标往返超差"})
        if record_samples:
            samples["20101"] = sample_payload(C.CMD_GET_OBJECT, *last_exchange(server, C.CMD_GET_OBJECT))

        # ---- 20802 量算 + 独立 GIS 对账 ----
        metrics = await log.run(
            "shape_metrics", lambda: commands.get_shape_metrics(client, shape_id), cmdid=C.CMD_GET_SHAPE_METRICS
        )
        assert metrics is not None
        independent = independent_area(DRAFT_VERTICES)
        area_diff = abs(metrics.area_m2 - independent["area_m2"])
        perimeter_diff = abs(metrics.perimeter_km - independent["perimeter_km"])
        accounting = {
            "mock_area_m2": metrics.area_m2,
            "independent_area_m2": independent["area_m2"],
            "area_diff_m2": area_diff,
            "area_rel_diff": area_diff / independent["area_m2"],
            "mock_perimeter_km": metrics.perimeter_km,
            "independent_perimeter_km": independent["perimeter_km"],
            "perimeter_diff_km": perimeter_diff,
            "metric_crs": independent["metric_crs"],
            "point_count": metrics.point_count,
            "area_count": metrics.area_count,
            "tolerance_m2": 1e-6,
            "within_tolerance": area_diff <= 1e-6 and perimeter_diff <= 1e-6,
            "note": "模拟器与回放脚本各自独立用 pyproj+shapely 计算，口径一致",
        }
        log.steps.append({"step": "metrics_accounting", "ok": accounting["within_tolerance"], "result": accounting})
        if not accounting["within_tolerance"]:
            log.failures.append({"step": "metrics_accounting", "ok": False, "error": "量算对账超差"})
        if record_samples:
            samples["20802"] = sample_payload(
                C.CMD_GET_SHAPE_METRICS, *last_exchange(server, C.CMD_GET_SHAPE_METRICS)
            )

        # ---- 20301 地图图片 ----
        png = await log.run(
            "get_map_image",
            lambda: commands.get_map_image(
                client,
                bbox_lonlat=(106.56, 39.00, 106.59, 39.03),
                max_level=12,
                max_pixel=250_000,
            ),
            cmdid=C.CMD_GET_MAP_DATA,
        )
        assert png is not None
        image_info = _inspect_png(png)
        assert image_info["watermark_ok"], "模拟地图缺少 SIMULATED MAP 水印"
        log.steps.append({"step": "map_image_inspect", "ok": True, "result": image_info})
        if record_samples:
            samples["20301"] = sample_payload(C.CMD_GET_MAP_DATA, *last_exchange(server, C.CMD_GET_MAP_DATA))

        # ---- 20303 等高线（仅元数据） ----
        contours = await log.run(
            "get_contours",
            lambda: commands.get_contours(
                client, bbox_lonlat=(106.56, 39.00, 106.59, 39.03), elev_value=50
            ),
            cmdid=C.CMD_GET_CONTOUR,
        )
        assert contours is not None and contours["geometry_format_unverified"] is True
        if record_samples:
            samples["20303"] = sample_payload(C.CMD_GET_CONTOUR, *last_exchange(server, C.CMD_GET_CONTOUR))

        # ---- 视图 ----
        corners = await log.run("screen_corners", lambda: commands.get_screen_corners(client), cmdid=C.CMD_GET_SCREEN_COORDS)
        assert corners is not None and corners["center"] is not None
        viewport = await log.run("viewport", lambda: commands.get_viewport(client))
        assert viewport is not None and viewport["status"] == "ok"
        if record_samples:
            samples["20016"] = sample_payload(C.CMD_GET_SCREEN_COORDS, *last_exchange(server, C.CMD_GET_SCREEN_COORDS))
            samples["20730"] = sample_payload(C.CMD_GET_OMAP_WND_RECT, *last_exchange(server, C.CMD_GET_OMAP_WND_RECT))
            samples["20212"] = sample_payload(C.CMD_GET_PIXEL_RATIO, *last_exchange(server, C.CMD_GET_PIXEL_RATIO))
            samples["20222"] = sample_payload(C.CMD_GET_SYS_CRS, *last_exchange(server, C.CMD_GET_SYS_CRS))
            samples["20501"] = sample_payload(C.CMD_GET_VIP_LEVEL, *last_exchange(server, C.CMD_GET_VIP_LEVEL))
            samples["20006"] = sample_payload(C.CMD_GET_VERSION, *last_exchange(server, C.CMD_GET_VERSION))

        # ---- 20107 查找 ----
        found = await log.run(
            "find_objects", lambda: commands.find_objects(client, DRAFT_FOLDER, isrh_opt=5), cmdid=C.CMD_SEARCH_OBJECT
        )
        assert found and found[0].obj_id == folder_id
        if record_samples:
            samples["20107"] = sample_payload(C.CMD_SEARCH_OBJECT, *last_exchange(server, C.CMD_SEARCH_OBJECT))

        # ---- 删除 + 回读确认为空 ----
        await log.run(
            "delete_objects",
            lambda: commands.delete_objects(client, [shape_id, sign_id], no_group=0),
            cmdid=C.CMD_DELETE_OBJECT,
        )
        remaining = await log.run("read_after_delete", lambda: commands.get_objects(client, shape_id), cmdid=C.CMD_GET_OBJECT)
        assert remaining == [], "删除后回读仍存在对象"
        await log.run(
            "delete_folder",
            lambda: commands.delete_objects(client, [folder_id], no_group=0),
            cmdid=C.CMD_DELETE_OBJECT,
        )
        assert server.world.object_count() == 0
        if record_samples:
            samples["20103"] = sample_payload(C.CMD_DELETE_OBJECT, *last_exchange(server, C.CMD_DELETE_OBJECT))

        stats = client.stats()
    finally:
        await client.close()
        await server.stop()

    core = {
        "login_ret": version.ret,
        "app_id": version.app_id,
        "simulated": client.is_simulated,
        "capabilities_status": capabilities["status"],
        "object_ids": [folder_id, shape_id, sign_id],
        "vertices_readback": [list(point) for point in shape.vertices],
        "comparison": comparison,
        "metrics": {
            "area_m2": metrics.area_m2,
            "perimeter_km": metrics.perimeter_km,
            "point_count": metrics.point_count,
        },
        "map_png_sha256": hashlib.sha256(png).hexdigest(),
        "map_png_bytes": len(png),
        "write_attempts": len(
            [item for item in server.requests if item["cmdid"] == C.CMD_WRITE_OBJECT_TREE]
        ),
    }
    return {
        "run": {
            "write_style": write_style,
            "steps": log.steps,
            "failures": log.failures,
            "core": core,
            "digest": digest(core),
            "client_stats": stats,
        },
        "samples": samples,
        "png": png,
        "image_info": image_info,
        "comparison": comparison,
        "accounting": accounting,
        "object_ids": {"folder": folder_id, "shape": shape_id, "sign": sign_id},
    }


def _count_red_pixels(image: Any) -> int:
    """统计红色水印像素（兼容 Pillow 新旧 API，避免弃用告警）。"""

    getter = getattr(image, "get_flattened_data", None) or image.getdata
    return sum(
        1 for pixel in getter() if pixel[0] > 140 and pixel[1] < 90 and pixel[2] < 90
    )


def _inspect_png(png: bytes) -> dict[str, Any]:
    from PIL import Image
    import io

    image = Image.open(io.BytesIO(png))
    description = image.info.get("Description", "")
    rgb = image.convert("RGB")
    red_pixels = _count_red_pixels(rgb)
    return {
        "bytes": len(png),
        "sha256": hashlib.sha256(png).hexdigest(),
        "size": list(image.size),
        "description": description,
        "simulated_metadata": image.info.get("Simulated"),
        "verified_against_real_device": image.info.get("verified_against_real_device"),
        "watermark_ok": "SIMULATED MAP" in description and red_pixels > 20,
        "red_watermark_pixels": red_pixels,
    }


# ---------------------------------------------------------------------------
# 故障场景（断线/超时/重复/权限）
# ---------------------------------------------------------------------------


async def run_fault_scenarios() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    async def fresh(faults: Faults) -> tuple[MockOvitalServer, OvitalClient]:
        server = MockOvitalServer(PSK, port=0, faults=faults)
        await server.start()
        client = OvitalClient(server.url, PSK, request_timeout_s=2.0)
        await client.connect()
        return server, client

    # 1) 断线：写请求 pending 时断开 → 显式失败，且只发送一次
    faults = Faults()
    faults.drop_on_write = True
    server, client = await fresh(faults)
    try:
        tree = objects.build_write_tree(folder_id=0, folder_name="断线场景", children=[])
        started = time.monotonic()
        error: str | None = None
        try:
            await commands.write_tree(client, tree)
        except OmapError as exc:
            error = type(exc).__name__
        writes = [item for item in server.requests if item["cmdid"] == C.CMD_WRITE_OBJECT_TREE]
        results.append(
            {
                "scenario": "drop_on_write",
                "ok": error == "OvitalConnectionError" and len(writes) == 1,
                "error": error,
                "write_requests": len(writes),
                "connected_after": client.connected,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "note": "断线时 pending 请求显式失败；不自动重发创建（防重复对象）",
            }
        )
    finally:
        await client.close()
        await server.stop()

    # 2) 超时：服务端不响应 → OvitalTimeoutError，且不自动重试
    faults = Faults()
    faults.timeout_on.add(C.CMD_GET_VERSION)
    server, client = await fresh(faults)
    try:
        error = None
        try:
            await client.request(C.CMD_GET_VERSION, {}, timeout=0.3)
        except OvitalTimeoutError as exc:
            error = type(exc).__name__
        received = [item for item in server.requests if item["cmdid"] == C.CMD_GET_VERSION]
        results.append(
            {
                "scenario": "timeout_on",
                "ok": error == "OvitalTimeoutError" and len(received) == 1,
                "error": error,
                "requests": len(received),
                "note": "超时显式失败且不自动重试；写请求超时后应回读确认而非重写",
            }
        )
    finally:
        await client.close()
        await server.stop()

    # 3) 重复 20112 响应：只写一次，重复帧进事件队列
    faults = Faults()
    faults.duplicate_20112 = True
    server, client = await fresh(faults)
    try:
        tree = objects.build_write_tree(folder_id=0, folder_name="重复场景", children=[])
        response = await client.request(C.CMD_WRITE_OBJECT_TREE, {"ObjTree": tree})
        duplicate = await client.next_event(C.CMD_WRITE_OBJECT_TREE, timeout=2.0)
        results.append(
            {
                "scenario": "duplicate_20112",
                "ok": response.cmdid == C.CMD_WRITE_OBJECT_TREE
                and duplicate is not None
                and server.world.object_count() == 1,
                "write_requests": len(
                    [item for item in server.requests if item["cmdid"] == C.CMD_WRITE_OBJECT_TREE]
                ),
                "objects_created": server.world.object_count(),
                "duplicate_seen_as_event": duplicate is not None,
                "note": "重复响应未被当成第二次写入结果；真实设备是否重发未知（待实测）",
            }
        )
    finally:
        await client.close()
        await server.stop()

    # 4) 权限：20301 返回 -7 → OvitalPrereqError（手册 2.1.1：地图图片需 SVIP）
    faults = Faults()
    faults.fail_on[C.CMD_GET_MAP_DATA] = -7
    server, client = await fresh(faults)
    try:
        error = None
        try:
            await commands.get_map_image(
                client, bbox_lonlat=(106.56, 39.00, 106.59, 39.03), max_level=12, max_pixel=1000
            )
        except OvitalPrereqError as exc:
            error = type(exc).__name__
        results.append(
            {
                "scenario": "permission_denied_20301",
                "ok": error == "OvitalPrereqError",
                "error": error,
                "note": "ret=-7 映射为权限不足（OvitalPrereqError），不伪装成空图片",
            }
        )
    finally:
        await client.close()
        await server.stop()

    return results


# ---------------------------------------------------------------------------
# 证据落盘
# ---------------------------------------------------------------------------


def write_evidence(
    out_dir: Path,
    *,
    runs: list[dict[str, Any]],
    faults: list[dict[str, Any]],
    fixture_replay: dict[str, Any],
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    samples_dir = out_dir / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)

    # 清理旧的样例（保持目录只包含本次生成内容）
    for stale in samples_dir.glob("*.json"):
        stale.unlink()

    all_samples: dict[str, dict[str, Any]] = {}
    for run in runs:
        all_samples.update(run["samples"])
    for cmdid_text, payload in sorted(all_samples.items()):
        path = samples_dir / f"{cmdid_text}-mock-exchange.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 模拟地图样例（带水印）
    first_run = runs[0]
    (out_dir / "simulated-map-20301.png").write_bytes(first_run["png"])

    # 官方夹具清单（指向仓库内可读的原始样例）
    fixtures_out = out_dir / "official-fixtures"
    if fixtures_out.exists():
        shutil.rmtree(fixtures_out)
    fixtures_out.mkdir(parents=True, exist_ok=True)
    for source in sorted(FIXTURES_DIR.glob("*.json")):
        (fixtures_out / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    digests_by_style: dict[str, set[str]] = {}
    for run in runs:
        digests_by_style.setdefault(run["run"]["write_style"], set()).add(run["run"]["digest"])
    repeatable = all(len(values) == 1 for values in digests_by_style.values())
    failures = [failure for run in runs for failure in run["run"]["failures"]]
    evidence = {
        "kind": "protocol-replay-evidence",
        "generated_by": "scripts/replay_protocol.py",
        "protocol": {
            "manual": f"奥维互动地图WEB开发工具接口（{C.MANUAL_VERSION}）",
            "manual_url": MANUAL_URL,
            "sdk_download_url": C.SDK_DOWNLOAD_URL,
            "sdk_local_path": ".research/ovital-scratch/zip-32/omapInterfaceWeb_latest/",
        },
        "simulated": True,
        "verified_against_real_device": False,
        "evidence_level": "offline_simulated",
        "real_device_pending": True,
        "environment": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "note": "本机为 macOS，无 Windows 奥维客户端与真实账号；全部结论来自模拟服务器",
        },
        "repeatable": repeatable,
        "run_count": len(runs),
        "runs": [run["run"] for run in runs],
        "fault_scenarios": faults,
        "official_fixture_replay": fixture_replay,
        "key_results": {
            "login_ret": first_run["run"]["core"]["login_ret"],
            "simulated": first_run["run"]["core"]["simulated"],
            "object_ids": first_run["object_ids"],
            "roundtrip": first_run["comparison"],
            "metrics_accounting": first_run["accounting"],
            "map_image": first_run["image_info"],
            "write_styles_tested": list(dict.fromkeys(run["run"]["write_style"] for run in runs)),
            "repeatable_digest": first_run["run"]["digest"],
        },
        "failures": failures,
        "unverified": list(C.UNVERIFIED_ITEMS),
        "artifacts": {
            "samples_dir": "samples/",
            "official_fixtures_dir": "official-fixtures/",
            "simulated_map_png": "simulated-map-20301.png",
            "readme": "README.md",
        },
    }
    (out_dir / "replay-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "README.md").write_text(
        _render_readme(evidence), encoding="utf-8"
    )
    return evidence


def _render_readme(evidence: dict[str, Any]) -> str:
    key = evidence["key_results"]
    lines = [
        "# 协议回放证据（模拟环境）",
        "",
        "> **本目录由 `scripts/replay_protocol.py` 生成。**",
        "> `simulated: true`、`verified_against_real_device: false`、",
        "> `evidence_level: offline_simulated`。本机没有 Windows 奥维客户端，",
        "> 因此这里是「官方文档级实现 + 官方夹具回放 + 模拟服务器闭环」，",
        "> **不是**真实奥维联机记录。真实设备待核验项见 `replay-evidence.json` 的 `unverified`。",
        "",
        "## 内容",
        "",
        "| 文件/目录 | 说明 |",
        "|---|---|",
        "| `replay-evidence.json` | 完整证据：步骤、耗时、坐标往返、量算对账、故障场景、待实测清单 |",
        "| `samples/*.json` | 脱敏的请求/响应样例，每条带 `_source`（手册章节 + 官方 URL） |",
        "| `official-fixtures/*.json` | 官方手册/样例 SDK 摘录夹具（带 `_source`） |",
        "| `simulated-map-20301.png` | 模拟 20301 地图图片（带 SIMULATED MAP 水印与元数据） |",
        "",
        "## 关键结果",
        "",
        f"- 登录：ret={key['login_ret']}（手册 2.3.1.2.1：成功 ret 是版本号，不是 0）；"
        f"模拟自标识 simulated={key['simulated']}",
        f"- 写草稿对象 ID：文件夹 {key['object_ids']['folder']}、图形 {key['object_ids']['shape']}、"
        f"出口标签 {key['object_ids']['sign']}",
        f"- 坐标往返：max_delta_deg={key['roundtrip']['max_delta_deg']:.3e} "
        f"（阈值 {key['roundtrip']['tolerance_deg']:.0e} 度），"
        f"轴序检查 axis_order={key['roundtrip']['axis_order']}，"
        f"swap_detected={key['roundtrip']['swap_detected']}",
        f"- 20802 量算对账：模拟 {key['metrics_accounting']['mock_area_m2']:.6f} ㎡ vs "
        f"独立 GIS {key['metrics_accounting']['independent_area_m2']:.6f} ㎡，"
        f"差 {key['metrics_accounting']['area_diff_m2']:.3e} ㎡（{key['metrics_accounting']['metric_crs']}）",
        f"- 20301 模拟地图：{key['map_image']['size'][0]}x{key['map_image']['size'][1]}px，"
        f"sha256={key['map_image']['sha256'][:16]}…，水印校验={key['map_image']['watermark_ok']}",
        f"- 可重复性：{evidence['repeatable']}（{evidence['run_count']} 次运行核心摘要一致，"
        f"digest={key['repeatable_digest'][:16]}…）",
        f"- 20112 响应形态：测试了 {', '.join(key['write_styles_tested'])} 两种官方写法",
        "",
        "## 故障注入验证",
        "",
        "| 场景 | 通过 | 说明 |",
        "|---|---|---|",
    ]
    for scenario in evidence["fault_scenarios"]:
        lines.append(
            f"| {scenario['scenario']} | {scenario['ok']} | {scenario.get('note', '')} |"
        )
    lines += [
        "",
        "## 证据分级",
        "",
        "- `official_document`：手册明确写到的命令/字段（见 `official-fixtures/` 与 `samples/` 的 `_source`）。",
        "- `offline_simulated`：本目录的回放结果（模拟服务器 + 官方夹具）。",
        "- `real_device_pending`：必须在 Windows 奥维实机验证（见 `replay-evidence.json` 的 `unverified`）。",
        "",
        "**禁止把本目录结果表述为真实设备验证结果。**",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="协议层端到端回放（模拟奥维；非真实联机）"
    )
    parser.add_argument(
        "--out", default=str(DEFAULT_OUT), help=f"证据输出目录（默认 {DEFAULT_OUT}）"
    )
    parser.add_argument(
        "--write-style",
        choices=("manual_flat", "sdk_nested", "both"),
        default="both",
        help="20112 响应形态；both=两种都跑（默认）",
    )
    parser.add_argument(
        "--repeat", type=int, default=2, help="重复运行次数（默认 2，用于证明可重复）"
    )
    parser.add_argument("--no-write", action="store_true", help="只跑验证，不写证据文件")
    parser.add_argument("--json", action="store_true", help="把证据摘要打印为 JSON")
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    print("== 协议端到端回放（模拟奥维；非真实联机）==", file=sys.stderr)

    fixture_replay = replay_official_fixtures()
    print(
        f"官方夹具：共 {fixture_replay['fixtures_total']} 个，"
        f"请求编码 {fixture_replay['requests_encoded']}，响应解析 {fixture_replay['responses_parsed']}，"
        f"失败 {len(fixture_replay['failures'])}",
        file=sys.stderr,
    )

    styles = (
        ["manual_flat", "sdk_nested"] if args.write_style == "both" else [args.write_style]
    )
    runs: list[dict[str, Any]] = []
    repeat = max(1, args.repeat)
    total = len(styles) * repeat
    index = 0
    for style in styles:
        for _ in range(repeat):
            index += 1
            result = await run_closure(write_style=style, record_samples=index == 1)
            runs.append(result)
            status = "ok" if not result["run"]["failures"] else "FAILED"
            print(
                f"第 {index}/{total} 次闭环（write_style={style}）：{status}，"
                f"digest={result['run']['digest'][:16]}…",
                file=sys.stderr,
            )

    fault_results = await run_fault_scenarios()
    for scenario in fault_results:
        print(
            f"故障场景 {scenario['scenario']}: {'ok' if scenario['ok'] else 'FAILED'}",
            file=sys.stderr,
        )

    evidence = write_evidence(
        Path(args.out), runs=runs, faults=fault_results, fixture_replay=fixture_replay
    ) if not args.no_write else {
        "repeatable": len({run["run"]["digest"] for run in runs}) == 1,
        "runs": [run["run"] for run in runs],
        "fault_scenarios": fault_results,
        "official_fixture_replay": fixture_replay,
        "simulated": True,
        "verified_against_real_device": False,
        "out": None,
    }

    failures = (
        [failure for run in runs for failure in run["run"]["failures"]]
        + fixture_replay["failures"]
        + [scenario for scenario in fault_results if not scenario["ok"]]
    )
    if args.json:
        print(
            json.dumps(
                {
                    "ok": not failures,
                    "simulated": True,
                    "verified_against_real_device": False,
                    "repeatable": evidence.get("repeatable"),
                    "failures": failures,
                    "out": None if args.no_write else args.out,
                    "key_results": {
                        "login_ret": runs[0]["run"]["core"]["login_ret"],
                        "roundtrip": runs[0]["comparison"],
                        "metrics": runs[0]["run"]["core"]["metrics"],
                        "map_png_sha256": runs[0]["run"]["core"]["map_png_sha256"],
                        "faults": {item["scenario"]: item["ok"] for item in fault_results},
                    },
                },
                ensure_ascii=False,
            )
        )
    else:
        print("\n== 结果摘要 ==")
        print(f"登录 ret={runs[0]['run']['core']['login_ret']}（版本号语义）")
        print(
            "坐标往返 max_delta_deg="
            f"{runs[0]['comparison']['max_delta_deg']:.3e}（阈值 1e-7），"
            f"within_tolerance={runs[0]['comparison']['within_tolerance']}"
        )
        print(
            "20802 对账 area_diff_m2="
            f"{runs[0]['accounting']['area_diff_m2']:.3e}，"
            f"within={runs[0]['accounting']['within_tolerance']}"
        )
        print(f"20301 模拟地图 sha256={runs[0]['run']['core']['map_png_sha256'][:32]}…")
        print(f"故障场景：{ {item['scenario']: item['ok'] for item in fault_results} }")
        print(f"可重复：{evidence.get('repeatable')}（{len(runs)} 次）")
        if not args.no_write:
            print(f"证据目录：{args.out}")

    if failures:
        print("\n!! 回放失败项：", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(
        "\n全部通过。注意：这是模拟环境结果，verified_against_real_device=false；"
        "真实 Windows 奥维联机仍未验证。",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
