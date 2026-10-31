"""边界分段测试：段数/长度/锚点稳定/高程统计/GeoJSON 属性。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
from pyproj import Transformer
from shapely.geometry import Point
from shapely.ops import transform as shapely_transform

from helpers import FIXTURES, run_scenario, scenario

from ovital_mcp.gis.segment import (
    choose_segment_count,
    list_segments,
    segment_boundary,
    segments_to_geojson,
)

METRIC_CRS = "EPSG:32650"


def _circle_polygon_lonlat(radius_m: float = 300.0, n: int = 120):
    angles = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
    coords = [
        (radius_m * math.cos(a), radius_m * math.sin(a)) for a in angles
    ]
    from shapely.geometry import Polygon

    metric = Polygon(coords)
    return shapely_transform(
        Transformer.from_crs(METRIC_CRS, "EPSG:4490", always_xy=True).transform,
        metric,
    )


def test_segment_boundary_counts_lengths_and_continuity() -> None:
    boundary = _circle_polygon_lonlat()
    outlet = (116.99, 39.74)  # 大致圆心，用于确定 1 号段起点
    segments = segment_boundary(
        boundary, n_segments=4, metric_crs=METRIC_CRS, outlet_lonlat=outlet
    )
    assert len(segments) == 4
    assert [seg.segment_id for seg in segments] == [1, 2, 3, 4]
    # 长度之和 = 周长（米制折线），误差 ≤1e-6 相对
    total = sum(seg.length_m for seg in segments)
    from ovital_mcp.gis.geometry import measure

    perimeter = measure(boundary, METRIC_CRS).perimeter_m
    assert total == pytest.approx(perimeter, rel=1e-6)
    # 相邻段首尾锚点必须接上（几何连续）
    for index in range(len(segments) - 1):
        assert segments[index].end_anchor.lonlat == pytest.approx(
            segments[index + 1].start_anchor.lonlat, abs=1e-12
        )
    # 最后一段终点 = 第一段起点（闭合）
    assert segments[-1].end_anchor.lonlat == pytest.approx(
        segments[0].start_anchor.lonlat, abs=1e-12
    )
    # 锚点 ID 格式 A-<段号:02d>-<原始顶点索引:05d>
    for seg in segments:
        assert seg.start_anchor.anchor_id.startswith(f"A-{seg.segment_id:02d}-")
        assert seg.end_anchor.anchor_id.startswith(f"A-{seg.segment_id:02d}-")
    # 1 号段起点应是距出口最近的顶点
    from ovital_mcp.gis.geometry import nearest_vertex_index

    expected_start = nearest_vertex_index(
        boundary, Point(*outlet), metric_crs=METRIC_CRS
    )
    assert segments[0].start_vertex_index == expected_start


def test_segment_boundary_is_deterministic() -> None:
    boundary = _circle_polygon_lonlat()
    outlet = (116.99, 39.74)
    first = segment_boundary(
        boundary, n_segments=6, metric_crs=METRIC_CRS, outlet_lonlat=outlet
    )
    second = segment_boundary(
        boundary, n_segments=6, metric_crs=METRIC_CRS, outlet_lonlat=outlet
    )
    assert [
        (seg.segment_id, seg.start_anchor.anchor_id, seg.end_anchor.anchor_id, seg.length_m)
        for seg in first
    ] == [
        (seg.segment_id, seg.start_anchor.anchor_id, seg.end_anchor.anchor_id, seg.length_m)
        for seg in second
    ]


def test_segment_boundary_max_len_converts_to_count() -> None:
    boundary = _circle_polygon_lonlat(radius_m=600.0)
    perimeter = 2.0 * math.pi * 600.0
    segments = segment_boundary(
        boundary, max_len_m=500.0, metric_crs=METRIC_CRS, outlet_lonlat=(116.99, 39.74)
    )
    assert len(segments) == math.ceil(perimeter / 500.0)
    assert all(seg.length_m <= 500.0 * 1.35 for seg in segments)


def test_choose_segment_count_bounds() -> None:
    assert choose_segment_count(0.0) == 4
    assert choose_segment_count(1000.0) == 4
    assert choose_segment_count(6000.0) == 4
    assert choose_segment_count(9000.0) == 6
    assert choose_segment_count(100000.0) == 8


def test_list_segments_from_engine_boundary(tmp_path: Path) -> None:
    """集成：从 native 成果的 boundary.geojson 读取分段（1 号段起点=出口）。"""

    result = run_scenario(tmp_path / "segments", "two_bowl_ridge")
    assert result.ok, result.error
    boundary_path = result.boundary_geojson
    assert boundary_path is not None
    segments = list_segments(
        boundary_path,
        n_segments=4,
        metric_crs=result.metric_crs,
        dem_path=result.artifacts["analysis_dem_tif"],
    )
    assert len(segments) == 4
    for index, item in enumerate(segments, start=1):
        assert item["segment_id"] == index
        assert item["start_anchor_id"].startswith(f"A-{index:02d}-")
        assert item["end_anchor_id"].startswith(f"A-{index:02d}-")
        assert item["length_m"] > 0
        assert item["vertex_count"] >= 2
        assert item["mean_elev_m"] is not None
        assert item["min_elev_m"] <= item["mean_elev_m"] <= item["max_elev_m"]
        bbox = item["bbox_lonlat"]
        assert len(bbox) == 4
        assert -180 <= bbox[0] <= bbox[2] <= 180
        assert -90 <= bbox[1] <= bbox[3] <= 90
        # 顶点是经纬度（不是米制）
        assert all(-180 <= lon <= 180 and -90 <= lat <= 90 for lon, lat in item["vertices"])
    # 1 号段起点 = 出口顶点（boundary 属性里的 outlet_vertex_index）
    boundary_data = json.loads(Path(boundary_path).read_text(encoding="utf-8"))
    props = boundary_data["features"][0]["properties"]
    assert segments[0]["start_vertex_index"] == props["outlet_vertex_index"]
    # 长度之和 ≈ 周长
    assert sum(item["length_m"] for item in segments) == pytest.approx(
        result.perimeter_m, rel=1e-6
    )


def test_segments_to_geojson_carries_lonlat_bbox() -> None:
    boundary = _circle_polygon_lonlat()
    segments = segment_boundary(
        boundary, n_segments=3, metric_crs=METRIC_CRS, outlet_lonlat=(116.99, 39.74)
    )
    collection = segments_to_geojson(segments)
    assert collection["type"] == "FeatureCollection"
    assert len(collection["features"]) == 3
    for feature in collection["features"]:
        properties = feature["properties"]
        bbox = properties["bbox_lonlat"]
        assert len(bbox) == 4
        # 显式经纬度口径（F-11 回归：不能是米制）
        assert -180 <= bbox[0] <= bbox[2] <= 180
        assert -90 <= bbox[1] <= bbox[3] <= 90
        assert feature["geometry"]["type"] == "LineString"
        coordinates = feature["geometry"]["coordinates"]
        assert all(-180 <= lon <= 180 and -90 <= lat <= 90 for lon, lat in coordinates)


def test_list_segments_without_dem_returns_none_elevation() -> None:
    boundary = _circle_polygon_lonlat()
    segments = list_segments(boundary, n_segments=2, metric_crs=METRIC_CRS)
    assert len(segments) == 2
    assert all(item["mean_elev_m"] is None for item in segments)
