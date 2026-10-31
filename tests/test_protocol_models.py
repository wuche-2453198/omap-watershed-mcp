"""协议数据模型与响应解析测试（字段差异容错、轴序、四套坐标枚举）。

重点固定「已知/未知边界」：
- 已知：手册/官方样例明确写出的字段名与轴序（Shape.Latlng 纬度在前）；
- 未知：手册表格与示例冲突的字段名 → 两种都接受并保留 raw；
- 未知：手册示例本身错位的 20016 数值 → 解析后给出 warnings，不静默纠正。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ovital_mcp.coordinates import (
    COORD_TYPE_TOP,
    LATLNG_TYPE,
    OMAP_LATLNG_COORD_TYPE,
    SHAPE_GCJ02,
    CoordSemantic,
    semantic_for_latlng_type,
    semantic_for_omap_latlng_coord_type,
    semantic_for_shape_gcj02,
    semantic_for_top_coord_type,
)
from ovital_mcp.errors import CoordinateSemanticError, OvitalNotFoundError
from ovital_mcp.protocol import constants as C
from ovital_mcp.protocol.errors import ProtocolDecodeError
from ovital_mcp.protocol.models import (
    object_point_lonlat,
    parse_contour_response,
    parse_map_data_response,
    parse_obj_items,
    parse_search_results,
    parse_selected_list,
    parse_shape,
    parse_shape_metrics,
    parse_screen_corners,
    parse_system_crs,
    parse_version,
    parse_viewport_rect,
    parse_vip_level,
    parse_write_results,
)

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "ovital_mcp" / "mock" / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 四套（+3 套额外）坐标枚举：必须分开解析
# ---------------------------------------------------------------------------


def test_four_coordinate_enums_are_distinct():
    # 手册 2.2.2 顶层 CoordType：0=CGCS2000
    assert semantic_for_top_coord_type(0) is CoordSemantic.CGCS2000
    assert semantic_for_top_coord_type(1) is CoordSemantic.GCJ02
    assert semantic_for_top_coord_type(2) is CoordSemantic.SYSTEM
    # 手册 2.2.2 LatlngType：0=系统, 1=CGCS2000, 2=GCJ02, 3=平面, 4=墨卡托
    assert semantic_for_latlng_type(0) is CoordSemantic.SYSTEM
    assert semantic_for_latlng_type(1) is CoordSemantic.CGCS2000
    assert semantic_for_latlng_type(2) is CoordSemantic.GCJ02
    assert semantic_for_latlng_type(3) is CoordSemantic.PLANE
    assert semantic_for_latlng_type(4) is CoordSemantic.MERCATOR
    # 手册 2.4.1 OmapLatlng.CoordType：0=GCJ02（与顶层相反！）
    assert semantic_for_omap_latlng_coord_type(0) is CoordSemantic.GCJ02
    assert semantic_for_omap_latlng_coord_type(1) is CoordSemantic.CGCS2000
    assert semantic_for_omap_latlng_coord_type(2) is CoordSemantic.PLANE
    # 手册 2.4.8 Shape.Gcj02：0=CGCS2000, 1=GCJ02, 2=平面
    assert semantic_for_shape_gcj02(0) is CoordSemantic.CGCS2000
    assert semantic_for_shape_gcj02(1) is CoordSemantic.GCJ02
    assert semantic_for_shape_gcj02(2) is CoordSemantic.PLANE


def test_extra_protocol_enums():
    # 手册 2.3.58 20016 ReqCoordType：0=根据奥维决定（不是 CGCS2000！）
    assert C.SCREEN_COORD_REQ_TYPE[0] is CoordSemantic.SYSTEM
    assert C.SCREEN_COORD_REQ_TYPE[1] is CoordSemantic.CGCS2000
    assert C.SCREEN_COORD_REQ_TYPE[2] is CoordSemantic.GCJ02
    # 手册 2.3.51 20222 CoordType：2=经纬度, 4=横轴墨卡托
    assert C.SYSTEM_CRS_COORD_TYPE == {2: "geographic", 4: "transverse_mercator"}
    # 手册 2.3.50/2.3.51 iPrjType
    assert C.SYSTEM_CRS_PRJ_TYPE[0] == "WGS84"
    assert C.SYSTEM_CRS_PRJ_TYPE[4] == "CGCS2000"


@pytest.mark.parametrize("func,value", [
    (semantic_for_top_coord_type, 9),
    (semantic_for_latlng_type, 9),
    (semantic_for_omap_latlng_coord_type, 9),
    (semantic_for_shape_gcj02, 9),
])
def test_unknown_coordinate_enum_raises(func, value):
    with pytest.raises(CoordinateSemanticError):
        func(value)


# ---------------------------------------------------------------------------
# 20000 登录
# ---------------------------------------------------------------------------


def test_parse_version_success():
    version = parse_version({"AppId": 41}, 817)
    assert version.ret == 817
    assert version.app_id == "41"
    assert version.simulated is False


def test_parse_version_simulated_flag():
    version = parse_version({"AppId": "abc", "Simulated": True}, 817)
    assert version.app_id == "abc"
    assert version.simulated is True


def test_parse_version_ret_zero_is_invalid_for_success():
    # 手册 2.3.1.2.2：ret=0 是失败（带 errmsg），不能当成功版本号
    with pytest.raises(ProtocolDecodeError):
        parse_version({"errmsg": "Pre-shared key error"}, 0)


# ---------------------------------------------------------------------------
# 对象节点 / 选中对象
# ---------------------------------------------------------------------------


def test_parse_obj_items_shape_fixture():
    nodes = parse_obj_items(fixture("20101-shape-response.json")["msg"])
    assert len(nodes) == 1
    node = nodes[0]
    assert node.obj_id == 627700251
    assert node.type_id == C.OBJ_TYPE_SHAPE
    assert node.name == "Shape"
    assert node.parent_id == 0
    assert node.srv_id == "7647106613611937263"
    assert node.type_name == "shape"
    assert node.raw["Object"]["Comment"] == "Shape"


def test_parse_obj_items_nested_20110_tree():
    nodes = parse_obj_items(fixture("20110-selected-tree-response.json")["msg"])
    assert len(nodes) == 1
    root = nodes[0]
    assert root.obj_id == 9
    assert len(root.children) == 1
    folder = root.children[0]
    assert folder.obj_id == 738224536
    assert len(folder.children) == 1
    sign = folder.children[0]
    assert sign.type_id == C.OBJ_TYPE_SIGN
    assert sign.name == "标签"


def test_parse_obj_items_accepts_single_object_and_rejects_empty():
    nodes = parse_obj_items({"ObjItems": {"Type": 30, "ObjID": "1"}})
    assert len(nodes) == 1 and nodes[0].obj_id == 1
    with pytest.raises(ProtocolDecodeError):
        parse_obj_items({"something": 1})


def test_parse_selected_list_fixture():
    selected = parse_selected_list(fixture("20104-selected-response.json")["msg"])
    assert [item.obj_id for item in selected] == [738224536, 441725141]
    assert selected[0].type_id == C.OBJ_TYPE_GROUP
    assert selected[1].name == "标签"


def test_parse_selected_list_count_mismatch_raises():
    with pytest.raises(ProtocolDecodeError):
        parse_selected_list({"Count": 3, "ObjList": [{"ObjID": 1, "Type": 7}]})


def test_parse_selected_list_empty_message_is_empty():
    assert parse_selected_list({}) == []


# ---------------------------------------------------------------------------
# Shape 解析：轴序（纬度在前）、坐标语义、两点特殊图形
# ---------------------------------------------------------------------------


def test_parse_shape_fixture_requires_gcj02_opt_in():
    msg = fixture("20101-shape-response.json")["msg"]
    # 夹具是手册 2.4.8 示例：Gcj02=1（GCJ-02）→ 默认禁止自动转换
    with pytest.raises(CoordinateSemanticError):
        parse_shape(msg, obj_id=627700251)


def test_parse_shape_fixture_axis_order_and_two_point_semantics():
    msg = fixture("20101-shape-response.json")["msg"]
    shape = parse_shape(msg, obj_id=627700251, allow_gcj02_conversion=True)
    # 手册示例 Latlng=[39.01064751, 106.57287598, ...]：39.01 是纬度、106.57 是经度
    lon, lat = shape.vertices[0]
    assert 105.0 < lon < 109.0  # 经度
    assert 37.0 < lat < 41.0  # 纬度
    assert shape.source_semantic is CoordSemantic.GCJ02
    assert shape.two_point_special is True  # 手册 2.4.8：两点=圆/扇形/矩形
    assert "纬度在前" in shape.axis_order_used


def test_parse_shape_cgcs2000_polygon_keeps_lonlat_order():
    msg = {
        "ObjItems": [
            {
                "Type": 13,
                "ObjID": "1001",
                "ParentID": 0,
                "Object": {
                    "Name": "多边形",
                    "Comment": "备注",
                    "Type": 13,
                    "ObjectDetail": {
                        "Gcj02": 0,
                        "Mtp": 4,
                        # 协议顺序：lat, lng, lat, lng, ...
                        "Latlng": [39.01, 106.57, 39.01, 106.58, 39.02, 106.58, 39.02, 106.57],
                    },
                },
            }
        ]
    }
    shape = parse_shape(msg, obj_id=1001)
    assert shape.vertices == [
        (106.57, 39.01),
        (106.58, 39.01),
        (106.58, 39.02),
        (106.57, 39.02),
    ]
    assert shape.two_point_special is False
    assert shape.source_semantic is CoordSemantic.CGCS2000


def test_parse_shape_mtp_mismatch_raises_with_raw():
    msg = {
        "ObjItems": [
            {
                "Type": 13,
                "ObjID": "1",
                "Object": {"ObjectDetail": {"Gcj02": 0, "Mtp": 3, "Latlng": [39.0, 106.0]}},
            }
        ]
    }
    with pytest.raises(ProtocolDecodeError) as info:
        parse_shape(msg, obj_id=1)
    assert info.value.raw is not None


def test_parse_shape_plane_coordinates_rejected():
    msg = {
        "ObjItems": [
            {
                "Type": 13,
                "ObjID": "1",
                "Object": {
                    "ObjectDetail": {"Gcj02": 2, "Mtp": 1, "Latlng": [500000.0, 3000000.0]}
                },
            }
        ]
    }
    with pytest.raises(CoordinateSemanticError):
        parse_shape(msg, obj_id=1)


def test_parse_shape_wrong_type_raises():
    msg = {"ObjItems": [{"Type": 7, "ObjID": "1", "Object": {"ObjectDetail": {}}}]}
    with pytest.raises(ProtocolDecodeError):
        parse_shape(msg, obj_id=1)


def test_parse_shape_missing_object_raises_not_found():
    with pytest.raises(OvitalNotFoundError):
        parse_shape({"ObjItems": []}, obj_id=1)


# ---------------------------------------------------------------------------
# 20802 字段名差异
# ---------------------------------------------------------------------------


def test_parse_shape_metrics_plain_fields_from_fixture():
    metrics = parse_shape_metrics(fixture("20802-shape-metrics-response.json")["msg"])
    assert metrics.obj_id == 828960081
    assert metrics.point_count == 2
    assert metrics.area_count == 1
    assert metrics.area_m2 == pytest.approx(282735.549033)
    assert metrics.perimeter_km == pytest.approx(1.884938)


def test_parse_shape_metrics_hungarian_prefix_aliases():
    # 手册 2.3.48 表格字段名（带前缀）也必须能解析
    metrics = parse_shape_metrics(
        {
            "idObj": 828960081,
            "iPointCnt": 2,
            "iAreaCnt": 1,
            "dPerimeter": 1.884938,
            "dAreaSize": 282735.549033,
        }
    )
    assert metrics.obj_id == 828960081
    assert metrics.point_count == 2
    assert metrics.area_m2 == pytest.approx(282735.549033)


def test_parse_shape_metrics_missing_field_raises_with_raw():
    with pytest.raises(ProtocolDecodeError) as info:
        parse_shape_metrics({"ObjID": 1, "PointCnt": 2})
    assert info.value.raw == {"ObjID": 1, "PointCnt": 2}


# ---------------------------------------------------------------------------
# 20112 写入结果：扁平（手册）与嵌套（官方样例）两种形态
# ---------------------------------------------------------------------------


def test_parse_write_results_manual_flat():
    results = parse_write_results(fixture("20112-write-response-flat.json")["msg"])
    assert [item.obj_id for item in results] == [262232699, 1269695656, 1778665247, 131253234]
    assert [item.type_id for item in results] == [7, 8, 13, 11]


def test_parse_write_results_official_sdk_nested():
    results = parse_write_results(fixture("20112-write-response-nested.json")["msg"])
    ids = {(item.type_id, item.obj_id) for item in results}
    assert (30, 1778665247) in ids
    assert (13, 1778665248) in ids


def test_parse_write_results_ignores_zero_ids():
    results = parse_write_results({"ObjItems": [{"Type": 13, "ObjID": "0"}]})
    assert results == []


# ---------------------------------------------------------------------------
# 20301 / 20303
# ---------------------------------------------------------------------------


def test_parse_map_data_response_flat_fields():
    meta = parse_map_data_response(fixture("20301-map-data-response.json")["msg"])
    assert meta["data_present"] is True
    assert meta["coord_type"] == 2
    assert meta["map_level"] == 12
    assert meta["bbox"]["x_lng1"] == pytest.approx(697827.09)


def test_parse_map_data_response_reqmapdatas_array_variant():
    meta = parse_map_data_response(
        {
            "ReqMapDatas": [
                {
                    "x_lng1": 106.55,
                    "y_lat1": 38.99,
                    "x_lng2": 106.60,
                    "y_lat2": 39.03,
                    "CoordType": 0,
                    "MaxLevel": 12,
                    "MaxPixel": 1000,
                    "Data": "AAAA",
                }
            ]
        }
    )
    assert meta["bbox_lonlat"] == pytest.approx((106.55, 38.99, 106.60, 39.03))


def test_parse_contour_response_metadata_only():
    result = parse_contour_response(fixture("20303-contour-response.json")["msg"])
    assert result["ret"] == 0
    assert result["ret_meaning"] == "获取成功"
    assert result["need_vip"] == 9
    assert result["elev_value"] == 30  # 手册响应字段 EvevValue（拼写错误）
    assert result["geometry_format_unverified"] is True
    # 手册没有定义等高线数据格式：必须显式标注未验证，禁止编造几何
    assert "未给出等高线数据数组结构" in result["note"]


def test_parse_contour_response_accepts_corrected_field_name():
    result = parse_contour_response({"Ret": 0, "ElevValue": 55})
    assert result["elev_value"] == 55


# ---------------------------------------------------------------------------
# 20016 / 20730 / 20222 / 20501 / 20107
# ---------------------------------------------------------------------------


def test_parse_screen_corners_flags_scrambled_manual_example():
    result = parse_screen_corners(fixture("20016-screen-corners-response.json")["msg"])
    # 手册 2.3.58.2 示例数值错位 → 必须给 warning，不静默纠正
    assert result["warnings"], "手册示例数值错位必须产生 warning"
    assert result["req_coord_semantic"] == CoordSemantic.CGCS2000.value
    assert result["center"] == pytest.approx((115.25756836, 28.88796935))


def test_parse_screen_corners_plausible_values():
    result = parse_screen_corners(
        {
            "latc": 39.0,
            "lngc": 106.5,
            "latlt": 39.1,
            "lnglt": 106.4,
            "latrb": 38.9,
            "lngrb": 106.6,
            "ReqCoordType": 1,
            "RealLl": 1,
            "MapLevel": 12,
            "MapType": 6,
        }
    )
    assert result["warnings"] == []
    assert result["top_left"] == pytest.approx((106.4, 39.1))
    assert result["bottom_right"] == pytest.approx((106.6, 38.9))


def test_parse_screen_corners_unknown_req_coord_type_warns():
    result = parse_screen_corners({"latc": 39.0, "lngc": 106.5, "ReqCoordType": 9})
    assert any("ReqCoordType=9" in item for item in result["warnings"])


def test_parse_viewport_rect_minimized():
    rect = parse_viewport_rect(fixture("20730-window-rect-response.json")["msg"])
    assert rect["width"] == 1889 - 765
    assert rect["height"] == 1201 - 497
    assert rect["minimized"] is False
    minimized = parse_viewport_rect({"Left": 0, "Right": 100, "Top": 10, "Bottom": 200})
    assert minimized["minimized"] is True
    assert minimized["warnings"]


def test_parse_system_crs_interpretation():
    crs = parse_system_crs(fixture("20222-system-crs-response.json")["msg"])
    assert crs["coord_type"] == 4
    assert crs["coord_type_meaning"] == "transverse_mercator"
    assert crs["prj_type_meaning"] == "WGS84"
    assert crs["central_meridian"] == 105.0
    assert crs["warnings"] == []


def test_parse_system_crs_geographic_warns_projection_params():
    crs = parse_system_crs({"CoordType": 2, "iPrjType": 4})
    assert crs["coord_type_meaning"] == "geographic"
    assert crs["prj_type_meaning"] == "CGCS2000"
    assert crs["warnings"]


def test_parse_vip_level_does_not_guess_tier():
    vip = parse_vip_level(fixture("20501-vip-response.json")["msg"])
    assert vip["omap_level"] == 9
    assert vip["load_ok_bool"] is True
    # 手册 2.3.19 没有数值→等级映射：不允许猜测为 SVIP
    assert vip["tier"] == "unknown"
    assert "未给" in vip["tier_note"]


def test_parse_search_results_from_fixture():
    nodes = parse_search_results(fixture("20107-search-response.json")["msg"])
    assert [node.obj_id for node in nodes] == [1968783191, 283412523]
    assert all(node.type_id == 7 for node in nodes)
    # 20107 不返回名称（手册 2.3.22.2）
    assert all(node.name is None for node in nodes)


# ---------------------------------------------------------------------------
# object_point_lonlat（工具层「读出口」）
# ---------------------------------------------------------------------------


def test_object_point_lonlat_from_sign():
    node = {
        "Type": 7,
        "ObjID": "441725141",
        "Object": {
            "Name": "标签",
            "ObjectDetail": {"Lat": 38.461403, "Lng": 106.321734, "Gcj02": 1},
        },
    }
    lon, lat = object_point_lonlat(node, allow_gcj02_conversion=True)
    assert 105.0 < lon < 108.0 and 37.0 < lat < 40.0
    with pytest.raises(CoordinateSemanticError):
        object_point_lonlat(node)  # GCJ-02 默认禁止自动转换


def test_object_point_lonlat_from_shape_first_vertex():
    node = {
        "Type": 13,
        "ObjID": "1",
        "Object": {
            "ObjectDetail": {
                "Gcj02": 0,
                "Latlng": [39.01, 106.57, 39.02, 106.58],
            }
        },
    }
    assert object_point_lonlat(node) == (106.57, 39.01)


def test_object_point_lonlat_unsupported_type_and_missing_fields():
    with pytest.raises(ProtocolDecodeError):
        object_point_lonlat({"Type": 8, "ObjID": "1", "Object": {"ObjectDetail": {}}})
    with pytest.raises(ProtocolDecodeError):
        object_point_lonlat({"Type": 7, "ObjID": "1", "Object": {"ObjectDetail": {}}})


# ---------------------------------------------------------------------------
# 契约夹具（tests/fixtures/protocol/）：枚举表与实现必须逐项一致
# ---------------------------------------------------------------------------

CONTRACT_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "protocol"


def contract_fixture(name: str) -> dict:
    return json.loads((CONTRACT_FIXTURES / name).read_text(encoding="utf-8"))


def test_coordinate_enum_contract_fixture_matches_implementation():
    contract = contract_fixture("coordinate-enums.json")
    assert contract["_source"]["verified_against_real_device"] is False

    assert {str(k): v.value for k, v in COORD_TYPE_TOP.items()} == contract["top_coord_type"]
    assert {str(k): v.value for k, v in LATLNG_TYPE.items()} == contract["latlng_type"]
    assert {
        str(k): v.value for k, v in OMAP_LATLNG_COORD_TYPE.items()
    } == contract["omap_latlng_coord_type"]
    assert {str(k): v.value for k, v in SHAPE_GCJ02.items()} == contract["shape_gcj02"]
    assert {
        str(k): v.value for k, v in C.SCREEN_COORD_REQ_TYPE.items()
    } == contract["screen_req_coord_type"]
    assert C.SYSTEM_CRS_COORD_TYPE == {int(k): v for k, v in contract["system_crs_coord_type"].items()}
    assert C.SYSTEM_CRS_PRJ_TYPE == {int(k): v for k, v in contract["system_crs_prj_type"].items()}
    # 轴序契约：Shape.Latlng 纬度在前（手册 2.4.8 示例）
    assert "lat,lng" in contract["axis_order"]["shape_latlng"]
    assert "39.01" in contract["axis_order"]["shape_latlng_evidence"]


def test_two_point_shape_contract_is_recorded():
    contract = contract_fixture("coordinate-enums.json")
    rule = contract["two_point_shape"]["rule"]
    assert "两个点" in rule and "圆" in rule
    # 代码里必须存在该常量（手册 2.4.8 原文）
    assert "两个点" in C.SHAPE_TWO_POINT_NOTE
