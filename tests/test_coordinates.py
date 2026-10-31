"""坐标语义与轴序测试：协议里四套互不相同的枚举是最高风险区。"""

from __future__ import annotations

import math

import pytest
from shapely.geometry import box

from ovital_mcp.coordinates import (
    CANONICAL_CRS,
    CoordSemantic,
    LatLng,
    area_m2,
    canonical_to_latlng,
    cgcs2000_to_gcj02,
    crs_axis_order,
    gcj02_to_cgcs2000,
    latlng_to_canonical,
    metric_crs_for,
    semantic_for_latlng_type,
    semantic_for_omap_latlng_coord_type,
    semantic_for_shape_gcj02,
    semantic_for_top_coord_type,
    transform_geometry,
    verify_canonical_lonlat,
)
from ovital_mcp.errors import CoordinateSemanticError, DataValidationError


def test_four_protocol_enums_are_not_merged() -> None:
    """同一数字在四套枚举下含义不同，必须分别解析（手册 2.2.2 vs 2.4.1 vs 2.4.8）。"""

    # 顶层 CoordType: 0=CGCS2000；而 OmapLatlng.CoordType: 0=GCJ02
    assert semantic_for_top_coord_type(0) is CoordSemantic.CGCS2000
    assert semantic_for_omap_latlng_coord_type(0) is CoordSemantic.GCJ02
    # LatlngType: 1=CGCS2000, 2=GCJ02；Shape.Gcj02: 0=CGCS2000, 1=GCJ02
    assert semantic_for_latlng_type(1) is CoordSemantic.CGCS2000
    assert semantic_for_latlng_type(2) is CoordSemantic.GCJ02
    assert semantic_for_shape_gcj02(0) is CoordSemantic.CGCS2000
    assert semantic_for_shape_gcj02(1) is CoordSemantic.GCJ02
    # Shape.Gcj02=2 是平面坐标
    assert semantic_for_shape_gcj02(2) is CoordSemantic.PLANE


@pytest.mark.parametrize("value", [7, -1, 99, True, "1", None])
def test_unknown_enum_values_raise_instead_of_defaulting(value: object) -> None:
    with pytest.raises(CoordinateSemanticError):
        semantic_for_latlng_type(value)  # type: ignore[arg-type]


def test_canonical_lonlat_is_lon_first_and_guards_axis_swap() -> None:
    assert verify_canonical_lonlat(116.39, 39.90) == (116.39, 39.90)
    with pytest.raises(DataValidationError):
        # 把纬度当经度：116 仍在范围内，但把 116 当纬度会超界
        verify_canonical_lonlat(39.90, 116.39)
    with pytest.raises(DataValidationError):
        verify_canonical_lonlat(200.0, 39.9)


def test_latlng_to_canonical_cgcs2000_and_gcj02_policy() -> None:
    point = LatLng(lat=39.9075, lng=116.3913, semantic=CoordSemantic.CGCS2000)
    assert latlng_to_canonical(point) == (116.3913, 39.9075)

    gcj = LatLng(lat=39.9089, lng=116.3975, semantic=CoordSemantic.GCJ02)
    with pytest.raises(CoordinateSemanticError):
        latlng_to_canonical(gcj)  # 默认禁止自动偏移
    lon, lat = latlng_to_canonical(gcj, allow_gcj02_conversion=True)
    assert 116.3 < lon < 116.5 and 39.8 < lat < 40.0

    system = LatLng(lat=39.9, lng=116.4, semantic=CoordSemantic.SYSTEM)
    with pytest.raises(CoordinateSemanticError):
        latlng_to_canonical(system)


def test_gcj02_roundtrip_is_centimetre_level() -> None:
    lon, lat = 116.3913, 39.9075
    gcj_lon, gcj_lat = cgcs2000_to_gcj02(lon, lat)
    back_lon, back_lat = gcj02_to_cgcs2000(gcj_lon, gcj_lat)
    assert abs(back_lon - lon) < 1e-9
    assert abs(back_lat - lat) < 1e-9
    # 偏移量应为数百米级，而不是零
    assert math.hypot((gcj_lon - lon) * 111320 * math.cos(math.radians(lat)), (gcj_lat - lat) * 110540) > 50


def test_canonical_to_latlng_roundtrip() -> None:
    lon, lat = 103.8, 30.6
    latlng = canonical_to_latlng(lon, lat, CoordSemantic.CGCS2000)
    assert latlng.lat == lat and latlng.lng == lon and latlng.semantic is CoordSemantic.CGCS2000


def test_metric_crs_for_utm_and_gk() -> None:
    assert metric_crs_for(116.4, 39.9) == "EPSG:32650"
    assert metric_crs_for(-70.0, -33.0) == "EPSG:32719"
    assert metric_crs_for(116.4, 39.9, kind="gk").startswith("EPSG:45")
    with pytest.raises(DataValidationError):
        metric_crs_for(116.4, 39.9, kind="nope")


def test_area_must_use_metric_crs() -> None:
    square = box(0, 0, 1000, 1000)
    assert area_m2(square, "EPSG:3857") == pytest.approx(1_000_000)
    with pytest.raises(DataValidationError):
        area_m2(box(116.3, 39.9, 116.4, 40.0), CANONICAL_CRS)


def test_crs_axis_order_reports_official_axis_order() -> None:
    assert crs_axis_order("EPSG:4490") == "latlon"  # 官方轴序是 lat,lon
    assert crs_axis_order("EPSG:3857") == "easting_northing"


def test_transform_geometry_uses_always_xy() -> None:
    # 4490 的官方轴序是 lat,lon；always_xy 下输入仍按 (lon,lat) 解释
    geom = box(116.3, 39.9, 116.4, 40.0)
    projected = transform_geometry(geom, "EPSG:4490", "EPSG:32650")
    minx, miny, maxx, maxy = projected.bounds
    assert minx > 400_000 and miny > 4_000_000
    assert projected.area > 0
