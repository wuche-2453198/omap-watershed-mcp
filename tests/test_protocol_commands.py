"""commands.py 高层命令测试：读写闭环、量算、地图图片、能力探测、回读对比。

全部基于本机模拟服务器；模拟通过 ≠ 真实联机通过（``verified_against_real_device=False``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import math
import pathlib

import pytest

from ovital_mcp.coordinates import CoordSemantic, metric_crs_for
from ovital_mcp.errors import (
    CoordinateSemanticError,
    OmapError,
    OvitalNotFoundError,
    OvitalPrereqError,
)
from ovital_mcp.mock import Faults, MockOvitalServer
from ovital_mcp.protocol import OvitalClient, commands, constants as C, objects

PSK = "test-psk-not-secret"

SQUARE = [(106.57, 39.01), (106.58, 39.01), (106.58, 39.02), (106.57, 39.02)]


def run(coro):
    return asyncio.run(coro)


@contextlib.asynccontextmanager
async def environment(**server_kwargs):
    server = MockOvitalServer(PSK, port=0, **server_kwargs)
    await server.start()
    client = OvitalClient(server.url, PSK, request_timeout_s=3.0)
    try:
        await client.connect()
        yield server, client
    finally:
        await client.close()
        await server.stop()


async def write_demo_tree(client: OvitalClient, *, name: str = "OMAP-MCP 测试草稿") -> dict:
    shape = objects.build_shape_object(
        name="集水区草稿", comment="离线回放", vertices_lonlat=SQUARE
    )
    sign = objects.build_sign_object(name="出口", comment="测试出口", lon=106.575, lat=39.015)
    tree = objects.build_write_tree(folder_id=0, folder_name=name, children=[shape, sign])
    results = await commands.write_tree(client, tree)
    by_type = {item.type_id: item.obj_id for item in results}
    return {"results": results, "folder_id": by_type[C.OBJ_TYPE_GROUP], "shape_id": by_type[C.OBJ_TYPE_SHAPE], "sign_id": by_type[C.OBJ_TYPE_SIGN]}


# ---------------------------------------------------------------------------
# 读命令
# ---------------------------------------------------------------------------


def test_get_version_and_vip_and_system_crs():
    async def scenario():
        async with environment() as (server, client):
            assert await commands.get_version(client) == 817
            vip = await commands.get_vip_level(client)
            assert vip["omap_level"] == 9
            assert vip["load_ok"] == 1
            crs = await commands.get_system_crs(client)
            assert crs["coord_type"] == 4
            assert crs["coord_type_meaning"] == "transverse_mercator"
            assert crs["prj_type_meaning"] == "Krasovsky_1940"

    run(scenario())


def test_get_selected_and_selected_tree():
    async def scenario():
        async with environment() as (server, client):
            demo = await write_demo_tree(client)
            server.world.selected_ids = [demo["folder_id"]]
            selected = await commands.get_selected(client, flag=1)
            assert [item.obj_id for item in selected] == [demo["folder_id"]]
            assert selected[0].type_id == C.OBJ_TYPE_GROUP
            tree = await commands.get_selected_tree(client, latlng_type=1)
            assert len(tree) == 1
            # 20110 响应根节点是容器（手册 2.3.18.2 示例 ObjID="9"），
            # 选中的文件夹在 ObjChildren 里，文件夹的子对象再下一层。
            container = tree[0]
            assert container.obj_id == 9
            selected_folder = container.children[0]
            assert selected_folder.obj_id == demo["folder_id"]
            child_types = {node.type_id for node in selected_folder.children}
            assert child_types == {C.OBJ_TYPE_SHAPE, C.OBJ_TYPE_SIGN}

    run(scenario())


def test_get_objects_and_folder_children():
    async def scenario():
        async with environment() as (server, client):
            demo = await write_demo_tree(client)
            nodes = await commands.get_objects(client, demo["folder_id"])
            assert nodes and nodes[0].obj_id == demo["folder_id"]
            children = await commands.get_folder_children(client, demo["folder_id"])
            assert {child.type_id for child in children} == {C.OBJ_TYPE_SHAPE, C.OBJ_TYPE_SIGN}
            missing = await commands.get_objects(client, 999999999)
            assert missing == []

    run(scenario())


# ---------------------------------------------------------------------------
# 图形读回：坐标往返 ≤1e-7、轴序、GCJ-02 语义
# ---------------------------------------------------------------------------


def test_shape_write_read_roundtrip_within_tolerance():
    async def scenario():
        async with environment() as (server, client):
            demo = await write_demo_tree(client)
            shape = await commands.get_shape(client, demo["shape_id"])
            assert shape.source_semantic is CoordSemantic.CGCS2000
            assert shape.axis_order_used
            comparison = commands.compare_vertices(SQUARE, shape.vertices)
            assert comparison["point_count_local"] == comparison["point_count_readback"] == 4
            assert comparison["max_delta_deg"] <= 1e-7
            assert comparison["within_tolerance"] is True
            assert comparison["axis_order_checked"] is True
            assert comparison["swap_detected"] is False
            # 轴序：回读必须是 (lon, lat)，不能是 (lat, lon)
            assert shape.vertices[0][0] == pytest.approx(106.57, abs=1e-7)
            assert shape.vertices[0][1] == pytest.approx(39.01, abs=1e-7)

    run(scenario())


def test_get_shape_gcj02_requires_explicit_conversion():
    async def scenario():
        async with environment() as (server, client):
            demo = await write_demo_tree(client)
            # 请求 LatlngType=2（GCJ-02，手册 2.2.2）→ 默认禁止自动转换
            with pytest.raises(CoordinateSemanticError):
                await commands.get_shape(client, demo["shape_id"], latlng_type=2)
            shape = await commands.get_shape(
                client,
                demo["shape_id"],
                latlng_type=2,
                allow_gcj02_conversion=True,
            )
            assert shape.source_semantic is CoordSemantic.GCJ02
            comparison = commands.compare_vertices(SQUARE, shape.vertices, tolerance_deg=1e-6)
            assert comparison["max_delta_deg"] <= 1e-6

    run(scenario())


def test_get_shape_not_found_and_not_shape():
    async def scenario():
        async with environment() as (server, client):
            with pytest.raises(OvitalNotFoundError):
                await commands.get_shape(client, 123456789)
            demo = await write_demo_tree(client)
            with pytest.raises(OmapError):
                await commands.get_shape(client, demo["sign_id"])

    run(scenario())


# ---------------------------------------------------------------------------
# 20802 量算：与独立 GIS 计算对账
# ---------------------------------------------------------------------------


def _independent_area_and_perimeter(vertices):
    from pyproj import Transformer
    from shapely.geometry import Polygon

    mean_lon = sum(p[0] for p in vertices) / len(vertices)
    mean_lat = sum(p[1] for p in vertices) / len(vertices)
    crs = metric_crs_for(mean_lon, mean_lat, kind="utm")
    transformer = Transformer.from_crs("EPSG:4490", crs, always_xy=True)
    polygon = Polygon([transformer.transform(lon, lat) for lon, lat in vertices])
    return polygon.area, polygon.length / 1000.0, crs


def test_shape_metrics_match_independent_gis_computation():
    async def scenario():
        async with environment() as (server, client):
            demo = await write_demo_tree(client)
            metrics = await commands.get_shape_metrics(client, demo["shape_id"])
            area, perimeter_km, crs = _independent_area_and_perimeter(SQUARE)
            assert metrics.obj_id == demo["shape_id"]
            assert metrics.point_count == 4
            assert metrics.area_count == 1
            assert metrics.area_m2 == pytest.approx(area, rel=1e-9)
            assert metrics.perimeter_km == pytest.approx(perimeter_km, abs=1e-6)  # 模拟器保留 6 位小数
            assert metrics.area_m2 > 0
            assert "dAreaSize" not in metrics.raw  # 模拟器用示例字段名

    run(scenario())


def test_shape_metrics_not_found_raises_not_returns_zero():
    async def scenario():
        async with environment() as (server, client):
            with pytest.raises(OvitalNotFoundError):
                await commands.get_shape_metrics(client, 42)
            demo = await write_demo_tree(client)
            with pytest.raises(OvitalNotFoundError):
                await commands.get_shape_metrics(client, demo["sign_id"])

    run(scenario())


# ---------------------------------------------------------------------------
# 写入：两种官方响应形态 / 大小守卫 / 删除
# ---------------------------------------------------------------------------


def test_write_objects_wrapper_returns_expected_types():
    async def scenario():
        async with environment() as (server, client):
            shape = objects.build_shape_object(name="图形", comment="", vertices_lonlat=SQUARE)
            sign = objects.build_sign_object(name="出口", comment="", lon=106.57, lat=39.01)
            results = await commands.write_objects(
                client, parent_id=0, name="包装测试", children=[shape, sign]
            )
            assert {item.type_id for item in results} == {
                C.OBJ_TYPE_GROUP,
                C.OBJ_TYPE_SHAPE,
                C.OBJ_TYPE_SIGN,
            }
            for item in results:
                assert item.obj_id > 0

    run(scenario())


def test_write_response_sdk_nested_style_is_parsed():
    async def scenario():
        async with environment(write_response_style="sdk_nested") as (server, client):
            demo = await write_demo_tree(client, name="嵌套响应")
            assert demo["folder_id"] and demo["shape_id"] and demo["sign_id"]
            shape = await commands.get_shape(client, demo["shape_id"])
            assert len(shape.vertices) == 4

    run(scenario())


def test_write_tree_rejects_oversize_payload(monkeypatch):
    async def scenario():
        async with environment() as (server, client):
            monkeypatch.setattr(C, "DEFAULT_WRITE_MAX_BYTES", 10)
            tree = objects.build_write_tree(folder_id=0, folder_name="太大", children=[])
            with pytest.raises(OmapError) as info:
                await commands.write_tree(client, tree)
            assert "20112" in str(info.value) or "字节" in str(info.value)
            assert server.world.object_count() == 0  # 没有产生写入

    run(scenario())


def test_delete_objects_removes_and_readback_is_empty():
    async def scenario():
        async with environment() as (server, client):
            demo = await write_demo_tree(client)
            await commands.delete_objects(client, [demo["shape_id"], demo["sign_id"]])
            assert await commands.get_objects(client, demo["shape_id"]) == []
            with pytest.raises(OvitalNotFoundError):
                await commands.get_shape(client, demo["shape_id"])

    run(scenario())


def test_delete_folder_requires_no_group_zero():
    async def scenario():
        async with environment() as (server, client):
            demo = await write_demo_tree(client)
            # 手册 2.3.13.1：NoGroup=1 表示「不删除文件夹」→ 文件夹被跳过
            await commands.delete_objects(client, [demo["folder_id"]], no_group=1)
            assert await commands.get_objects(client, demo["folder_id"])
            await commands.delete_objects(client, [demo["folder_id"]], no_group=0)
            assert await commands.get_objects(client, demo["folder_id"]) == []

    run(scenario())


# ---------------------------------------------------------------------------
# 地图图片 / 等高线
# ---------------------------------------------------------------------------


def test_get_map_image_returns_png_with_simulated_watermark():
    async def scenario():
        from PIL import Image

        async with environment() as (server, client):
            png = await commands.get_map_image(
                client,
                bbox_lonlat=(106.55, 38.99, 106.60, 39.03),
                max_level=12,
                max_pixel=250_000,
            )
            assert png.startswith(commands.PNG_SIGNATURE)
            image = Image.open(io.BytesIO(png))
            assert image.size[0] * image.size[1] <= 250_000
            description = image.info.get("Description", "")
            assert "SIMULATED MAP" in description
            assert image.info.get("Simulated") == "True"
            assert image.info.get("verified_against_real_device") == "False"
            # 画面上确实有红色水印文字（不是空白图）
            rgb = image.convert("RGB")
            red_pixels = sum(
                1
                for pixel in rgb.getdata()
                if pixel[0] > 140 and pixel[1] < 90 and pixel[2] < 90
            )
            assert red_pixels > 20
            assert server.world.last_map_request["max_pixel"] == 250_000

    run(scenario())


def test_get_map_image_permission_error_maps_to_prereq():
    async def scenario():
        faults = Faults()
        faults.fail_on[C.CMD_GET_MAP_DATA] = -7  # 手册 2.3.29.2：不是 SVIP
        async with environment(faults=faults) as (server, client):
            with pytest.raises(OvitalPrereqError) as info:
                await commands.get_map_image(
                    client, bbox_lonlat=(106.55, 38.99, 106.60, 39.03), max_level=12, max_pixel=1000
                )
            assert "-7" in str(info.value)
            assert "SVIP" in str(info.value)

    run(scenario())


def test_get_contours_returns_metadata_and_marks_format_unverified():
    async def scenario():
        async with environment() as (server, client):
            result = await commands.get_contours(
                client, bbox_lonlat=(106.55, 38.99, 106.60, 39.03), elev_value=50
            )
            assert result["ret"] == 0
            assert result["need_vip"] == 9
            assert result["elev_value"] == 50
            assert result["geometry_format_unverified"] is True

    run(scenario())


def test_get_contours_permission_error():
    async def scenario():
        faults = Faults()
        faults.fail_on[C.CMD_GET_CONTOUR] = -7
        async with environment(faults=faults) as (server, client):
            with pytest.raises(OvitalPrereqError):
                await commands.get_contours(client, bbox_lonlat=(106.55, 38.99, 106.60, 39.03))

    run(scenario())


# ---------------------------------------------------------------------------
# 视图 / 屏幕
# ---------------------------------------------------------------------------


def test_get_screen_corners_uses_viewport():
    async def scenario():
        async with environment() as (server, client):
            corners = await commands.get_screen_corners(client)
            assert corners["center"] == pytest.approx((106.57287598, 39.01064751))
            assert corners["top_left"][0] < corners["center"][0]
            assert corners["top_left"][1] > corners["center"][1]
            assert corners["warnings"] == []
            assert corners["req_coord_semantic"] == CoordSemantic.CGCS2000.value

    run(scenario())


def test_get_viewport_combines_three_commands():
    async def scenario():
        async with environment() as (server, client):
            viewport = await commands.get_viewport(client)
            assert viewport["status"] == "ok"
            assert viewport["window_rect"]["width"] == 1889 - 765
            assert viewport["screen"]["center"] is not None
            assert viewport["pixel_ratio"]["meter_x"]
            assert viewport["simulated"] is True
            assert viewport["verified_against_real_device"] is False

    run(scenario())


def test_get_viewport_partial_failure_is_reported_not_raised():
    async def scenario():
        faults = Faults()
        faults.fail_on[C.CMD_GET_OMAP_WND_RECT] = -4
        async with environment(faults=faults) as (server, client):
            viewport = await commands.get_viewport(client)
            assert viewport["status"] == "partial"
            probes = {error["probe"] for error in viewport["errors"]}
            assert "20730_window_rect" in probes
            assert viewport["screen"]["center"] is not None  # 其它项仍成功

    run(scenario())


# ---------------------------------------------------------------------------
# 能力探测
# ---------------------------------------------------------------------------


def test_probe_capabilities_reports_simulated_and_unverified():
    async def scenario():
        async with environment() as (server, client):
            capabilities = await commands.probe_capabilities(client)
            assert capabilities["connected"] is True
            assert capabilities["simulated"] is True
            assert capabilities["verified_against_real_device"] is False
            assert capabilities["evidence_level"] == "offline_simulated"
            assert capabilities["version"]["value"] == 817
            assert capabilities["vip"]["omap_level"] == 9
            assert capabilities["system_crs"]["coord_type"] == 4
            assert capabilities["selection"]["ok"] is True
            assert capabilities["permissions"]["map_image"]["ok"] is True
            assert capabilities["permissions"]["contour"]["ok"] is True
            assert capabilities["errors"] == []
            assert capabilities["unverified"], "必须列出待实测项"
            assert capabilities["limits"]["documented"]["map_image_min_tier"].startswith("SVIP")
            # 探测过程绝不能写地图
            assert server.world.object_count() == 0
            assert not [r for r in server.requests if r["cmdid"] in (
                C.CMD_WRITE_OBJECT_TREE,
                C.CMD_DELETE_OBJECT,
            )]

    run(scenario())


def test_probe_capabilities_records_single_probe_failure_without_raising():
    async def scenario():
        faults = Faults()
        faults.fail_on[C.CMD_GET_VIP_LEVEL] = -5  # 手册 2.2.7：不是 VIP
        async with environment(faults=faults) as (server, client):
            capabilities = await commands.probe_capabilities(client)
            assert capabilities["status"] == "partial"
            assert capabilities["vip"]["ok"] is False
            assert any(error.get("code") == "OVITAL_PREREQ_ERROR" for error in capabilities["errors"])
            # 其它探测项仍然成功
            assert capabilities["version"]["value"] == 817
            assert capabilities["system_crs"]["coord_type"] == 4

    run(scenario())


def test_probe_capabilities_requires_connection():
    async def scenario():
        client = OvitalClient("ws://127.0.0.1:9", PSK)
        with pytest.raises(OmapError):
            await commands.probe_capabilities(client)

    run(scenario())


# ---------------------------------------------------------------------------
# 查找对象
# ---------------------------------------------------------------------------


def test_find_objects_by_name():
    async def scenario():
        async with environment() as (server, client):
            demo = await write_demo_tree(client, name="查找测试草稿")
            results = await commands.find_objects(client, "查找测试草稿", isrh_opt=5)
            assert [item.obj_id for item in results] == [demo["folder_id"]]
            assert results[0].type_id == C.OBJ_TYPE_GROUP
            assert results[0].name is None  # 20107 不返回名称（手册 2.3.22.2）
            fuzzy = await commands.find_objects(client, "查找", isrh_opt=0)
            assert demo["folder_id"] in [item.obj_id for item in fuzzy]
            none = await commands.find_objects(client, "不存在的名字", isrh_opt=5)
            assert none == []

    run(scenario())


# ---------------------------------------------------------------------------
# compare_vertices 边界
# ---------------------------------------------------------------------------


def test_compare_vertices_detects_swap_and_count_mismatch():
    identical = commands.compare_vertices(SQUARE, SQUARE)
    assert identical["max_delta_deg"] == 0.0
    assert identical["within_tolerance"] is True
    assert identical["swap_detected"] is False

    swapped = [(lat, lon) for lon, lat in SQUARE]
    mismatch = commands.compare_vertices(SQUARE, swapped)
    assert mismatch["swap_detected"] is True
    assert mismatch["within_tolerance"] is False
    assert mismatch["max_delta_deg"] > 1.0

    short = commands.compare_vertices(SQUARE, SQUARE[:2])
    assert short["point_count_readback"] == 2
    assert short["within_tolerance"] is False

    empty = commands.compare_vertices([], [])
    assert math.isinf(empty["max_delta_deg"])
    assert empty["within_tolerance"] is False


def test_compare_vertices_tolerance_boundary():
    slightly_off = [(lon + 5e-8, lat - 5e-8) for lon, lat in SQUARE]
    result = commands.compare_vertices(SQUARE, slightly_off)
    assert result["max_delta_deg"] <= 1e-7
    assert result["within_tolerance"] is True
    off = [(lon + 2e-7, lat) for lon, lat in SQUARE]
    assert commands.compare_vertices(SQUARE, off)["within_tolerance"] is False


# ---------------------------------------------------------------------------
# 两点图形：不得当作多边形
# ---------------------------------------------------------------------------


def test_two_point_shape_is_flagged_special():
    async def scenario():
        async with environment() as (server, client):
            two_point = objects.build_shape_object(
                name="圆/矩形占位", comment="两点", vertices_lonlat=[(106.57, 39.01), (106.58, 39.02)]
            )
            tree = objects.build_write_tree(folder_id=0, folder_name="两点", children=[two_point])
            results = await commands.write_tree(client, tree)
            shape_id = next(item.obj_id for item in results if item.type_id == C.OBJ_TYPE_SHAPE)
            shape = await commands.get_shape(client, shape_id)
            assert shape.two_point_special is True
            assert len(shape.vertices) == 2
            # 量算返回 0 面积并带说明（模拟器未实现圆/扇形/矩形几何；待实测）
            metrics = await commands.get_shape_metrics(client, shape_id)
            assert metrics.area_m2 == 0.0
            assert metrics.point_count == 2

    run(scenario())


# ---------------------------------------------------------------------------
# 夹具回放：官方样例请求/响应能被真实解析路径消费
# ---------------------------------------------------------------------------


def test_official_fixtures_parse_through_models():
    from ovital_mcp.protocol.models import (
        parse_map_data_response,
        parse_obj_items,
        parse_shape_metrics,
        parse_write_results,
    )

    fixtures_dir = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "ovital_mcp"
        / "mock"
        / "fixtures"
    )

    def fixture(name: str) -> dict:
        return json.loads((fixtures_dir / name).read_text(encoding="utf-8"))

    assert parse_obj_items(fixture("20101-shape-response.json")["msg"])[0].obj_id == 627700251
    assert parse_write_results(fixture("20112-write-response-flat.json")["msg"])
    assert parse_write_results(fixture("20112-write-response-nested.json")["msg"])
    metrics = parse_shape_metrics(fixture("20802-shape-metrics-response.json")["msg"])
    assert metrics.perimeter_km == pytest.approx(1.884938)
    assert parse_map_data_response(fixture("20301-map-data-response.json")["msg"])["data_present"]


# ---------------------------------------------------------------------------
# 契约夹具（tests/fixtures/protocol/）：回放计划与命令矩阵
# ---------------------------------------------------------------------------

CONTRACT_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "protocol"


def contract_fixture(name: str) -> dict:
    import json as _json

    return _json.loads((CONTRACT_FIXTURES / name).read_text(encoding="utf-8"))


def test_replay_plan_fixture_is_consistent_with_implementation():
    plan = contract_fixture("replay-plan.json")
    assert plan["simulated"] is True
    assert plan["verified_against_real_device"] is False
    assert plan["_source"]["manual_url"].startswith("https://")
    # 回放计划里的每个 cmdid 都必须有官方命令名
    for step in plan["steps"]:
        cmdid = step["cmdid"]
        if cmdid is not None:
            assert cmdid in C.CMD_NAMES, f"{step['name']} 的 cmdid={cmdid} 不在手册 2.2.1 命令号表"
    # 回放脚本必须存在
    script = CONTRACT_FIXTURES.parents[2] / plan["script"]
    assert script.is_file(), f"回放脚本不存在：{script}"
    # 容差必须是正的有限数
    for name, value in plan["tolerances"].items():
        assert isinstance(value, (int, float)) and value > 0, name
    # 证据清单必须包含核心产物
    for required in ("replay-evidence.json", "samples/", "simulated-map-20301.png"):
        assert required in plan["required_evidence"]


def test_command_matrix_fixture_matches_constants():
    matrix = contract_fixture("command-matrix.json")
    implemented = {item["cmdid"] for item in matrix["implemented"]}
    not_implemented = set(matrix["not_implemented"])
    assert implemented, "命令矩阵不能为空"
    assert implemented.isdisjoint(not_implemented), "实现/未实现集合不能重叠"
    for cmdid in implemented:
        assert cmdid in C.CMD_NAMES, f"cmdid={cmdid} 不在手册 2.2.1 命令号表"
        assert matrix["_source"]["manual_url"].startswith("https://")
    # 手册明确的两条会员限制必须在矩阵来源里可追溯
    assert "2.3.29" in {item["section"] for item in matrix["implemented"]}
    assert "2.3.31" in {item["section"] for item in matrix["implemented"]}


# ---------------------------------------------------------------------------
# finalize_draft 依赖的替换语义（commands 层集成）
# ---------------------------------------------------------------------------


def test_finalize_like_replace_updates_object_in_place():
    """读 raw → 改 Name/Comment → build_write_tree(folder_id=父ID, children=[raw]) → 替换。

    这是 `tools/draft.py::finalize_draft` 的协议路径；手册 2.3.12.1 规定
    「已存在相同 id 则替换」。真实设备替换是否会丢字段「待实测」，故必须回读核对。
    """

    async def scenario():
        async with environment() as (server, client):
            demo = await write_demo_tree(client)
            nodes = await commands.get_objects(client, demo["shape_id"])
            raw = json.loads(json.dumps(nodes[0].raw, ensure_ascii=False))
            raw["Object"]["Name"] = "集水区草稿-定稿"
            raw["Object"]["Comment"] = "finalize 测试"
            tree = objects.build_write_tree(
                folder_id=demo["folder_id"],
                folder_name="OMAP-MCP 测试草稿",
                children=[raw],
            )
            results = await commands.write_tree(client, tree)
            assert {item.obj_id for item in results} == {
                demo["folder_id"],
                demo["shape_id"],
            }
            assert server.world.object_count() == 3  # 文件夹+图形+标签，未新增
            shape = await commands.get_shape(client, demo["shape_id"])
            assert shape.name == "集水区草稿-定稿"
            assert shape.comment == "finalize 测试"
            # 顶点未被破坏（raw 原样回写）
            comparison = commands.compare_vertices(SQUARE, shape.vertices)
            assert comparison["within_tolerance"] is True

    run(scenario())
