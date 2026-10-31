"""几何校验、量算、受控简化与锚点修订测试（解析多边形，米制基准点贴近真实 UTM 坐标）。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Point, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import transform as shapely_transform

from ovital_mcp.errors import GeometryError
from ovital_mcp.gis.geometry import (
    geometry_sha256,
    measure,
    normalize_rfc7946,
    revise_with_anchors,
    simplify_preserving,
    validate_geometry,
)

METRIC_CRS = "EPSG:32650"
BASE_X = 499000.0
BASE_Y = 4400000.0


def _to_lonlat(geom):
    return shapely_transform(
        Transformer.from_crs(METRIC_CRS, "EPSG:4490", always_xy=True).transform, geom
    )


def _to_metric(geom):
    return shapely_transform(
        Transformer.from_crs("EPSG:4490", METRIC_CRS, always_xy=True).transform, geom
    )


def _shifted(coords):
    return [(BASE_X + x, BASE_Y + y) for x, y in coords]


def _wiggly_circle(radius_m: float = 300.0, n: int = 360) -> Polygon:
    """解析圆多边形（米制，半径 300 m，360 个顶点 + 2% 确定性起伏）。"""

    angles = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
    radii = radius_m * (1.0 + 0.02 * np.sin(angles * 12.0))
    coords = [
        (float(r * math.cos(a)), float(r * math.sin(a)))
        for a, r in zip(angles, radii)
    ]
    return Polygon(_shifted(coords))


def test_validate_regular_polygon() -> None:
    geom = _to_lonlat(_wiggly_circle())
    check = validate_geometry(geom, metric_crs=METRIC_CRS)
    assert check.is_valid is True
    assert check.is_closed is True
    assert check.is_simple is True
    assert check.self_intersection is False
    assert check.duplicate_vertex_count == 0
    assert check.multipart is False
    assert check.part_count == 1
    assert check.hole_count == 0
    assert check.sliver_suspected is False
    assert check.compactness is not None and check.compactness > 0.5
    normalized, _changed = normalize_rfc7946(geom)
    assert validate_geometry(normalized, metric_crs=METRIC_CRS).rfc7946_orientation_ok is True


def test_validate_self_intersection_detected() -> None:
    bowtie = Polygon(_shifted([(0, 0), (100, 100), (100, 0), (0, 100), (0, 0)]))
    check = validate_geometry(_to_lonlat(bowtie), metric_crs=METRIC_CRS)
    assert check.is_valid is False
    assert check.self_intersection is True
    assert check.issues
    assert any("自相交" in item for item in check.issues)


def test_validate_duplicate_and_multipart_and_holes() -> None:
    square = Polygon(
        _shifted([(0, 0), (100, 0), (100, 0), (100, 100), (0, 100), (0, 0)])
    )
    check = validate_geometry(_to_lonlat(square), metric_crs=METRIC_CRS)
    assert check.duplicate_vertex_count == 1

    multi = MultiPolygon(
        [
            box(BASE_X, BASE_Y, BASE_X + 100, BASE_Y + 100),
            box(BASE_X + 200, BASE_Y + 200, BASE_X + 300, BASE_Y + 300),
        ]
    )
    multi_check = validate_geometry(_to_lonlat(multi), metric_crs=METRIC_CRS)
    assert multi_check.multipart is True
    assert multi_check.part_count == 2
    assert any("多部件" in item for item in multi_check.issues)

    with_hole = Polygon(
        _shifted([(0, 0), (300, 0), (300, 300), (0, 300)]),
        [_shifted([(100, 100), (200, 100), (200, 200), (100, 200)])],
    )
    hole_check = validate_geometry(_to_lonlat(with_hole), metric_crs=METRIC_CRS)
    assert hole_check.hole_count == 1
    assert any("洞" in item for item in hole_check.issues)


def test_validate_sliver_detected() -> None:
    thin = box(BASE_X, BASE_Y, BASE_X + 1000, BASE_Y + 4)
    check = validate_geometry(_to_lonlat(thin), metric_crs=METRIC_CRS)
    assert check.sliver_suspected is True
    assert check.compactness is not None and check.compactness < 0.05
    assert any("细长片" in item for item in check.issues)


def test_measure_matches_analytic_circle() -> None:
    metric_geom = _wiggly_circle()
    geom = _to_lonlat(metric_geom)
    result = measure(geom, METRIC_CRS)
    assert result.area_m2 == pytest.approx(metric_geom.area, rel=1e-9)
    assert result.perimeter_m == pytest.approx(metric_geom.length, rel=1e-9)
    assert result.area_ha == pytest.approx(result.area_m2 / 10000.0)
    assert result.part_count == 1 and result.hole_count == 0
    assert result.vertex_count == 361  # 360 顶点 + 闭合点
    assert "水平投影面积" in result.method
    assert result.area_m2 == pytest.approx(math.pi * 300.0**2, rel=0.01)


def test_measure_rejects_geographic_crs() -> None:
    with pytest.raises(GeometryError):
        measure(_wiggly_circle(), "EPSG:4490")


def test_simplify_preserving_reduces_vertices_and_protects_outlet() -> None:
    metric_geom = _wiggly_circle()
    geom = _to_lonlat(metric_geom)
    protected_metric = list(metric_geom.exterior.coords)[0]
    protected_lonlat = _to_lonlat(Point(*protected_metric)).coords[0]
    simplified, report = simplify_preserving(
        geom, 3.0, [protected_lonlat], metric_crs=METRIC_CRS
    )
    assert report["applied"] is True
    assert report["vertices_after"] < report["vertices_before"]
    assert report["vertices_removed"] > 100
    assert report["hausdorff_m"] <= 2.0 * report["tolerance_m"]
    assert report["topology_preserved"] is True
    assert report["protected_points"] == 1
    back = _to_metric(simplified)
    assert back.exterior.distance(Point(*protected_metric)) <= 0.5 * 3.0 + 1e-6
    assert report["measure_after"]["area_m2"] == pytest.approx(
        report["measure_before"]["area_m2"], rel=0.01
    )
    assert validate_geometry(simplified, metric_crs=METRIC_CRS).is_valid is True


def test_simplify_rejects_oversized_tolerance() -> None:
    geom = _to_lonlat(_wiggly_circle())
    with pytest.raises(GeometryError) as excinfo:
        simplify_preserving(geom, 100.0, [], metric_crs=METRIC_CRS)
    assert "超过允许上限" in str(excinfo.value)


def test_simplify_zero_tolerance_is_noop() -> None:
    geom = _to_lonlat(_wiggly_circle())
    simplified, report = simplify_preserving(geom, 0.0, [], metric_crs=METRIC_CRS)
    assert report["applied"] is False
    assert geometry_sha256(simplified) == geometry_sha256(geom)


def test_simplify_interior_protected_point_is_skipped() -> None:
    """出口在流域内部时不得硬插成边界尖刺（否则面积被静默改变）。"""

    geom = _to_lonlat(_wiggly_circle())
    center_lonlat = _to_lonlat(Point(BASE_X, BASE_Y)).coords[0]
    simplified, report = simplify_preserving(
        geom, 3.0, [center_lonlat], metric_crs=METRIC_CRS
    )
    assert report["applied"] is True
    assert report["inserted_protected_points"] == []
    assert len(report["skipped_protected_points"]) == 1
    assert report["skipped_protected_points"][0][
        "distance_to_boundary_m"
    ] == pytest.approx(300.0, rel=0.05)
    assert report["measure_after"]["area_m2"] == pytest.approx(
        report["measure_before"]["area_m2"], rel=0.01
    )


def test_revise_with_anchors_moves_vertex_and_reports() -> None:
    square_metric = box(BASE_X, BASE_Y, BASE_X + 1000, BASE_Y + 1000)
    geom = _to_lonlat(square_metric)
    anchor = _to_lonlat(Point(BASE_X + 20.0, BASE_Y + 20.0)).coords[0]
    revised, report = revise_with_anchors(
        geom, [anchor], metric_crs=METRIC_CRS, max_move_m=50.0
    )
    assert report["applied"] is True
    assert len(report["moved_vertices"]) == 1
    assert report["moved_vertices"][0]["move_m"] <= 50.0
    assert report["area_delta_m2"] != 0.0
    assert validate_geometry(revised, metric_crs=METRIC_CRS).is_valid is True


def test_revise_with_anchors_rejects_far_anchor() -> None:
    geom = _to_lonlat(box(BASE_X, BASE_Y, BASE_X + 1000, BASE_Y + 1000))
    anchor = _to_lonlat(Point(BASE_X + 500.0, BASE_Y + 500.0)).coords[0]
    with pytest.raises(GeometryError) as excinfo:
        revise_with_anchors(geom, [anchor], metric_crs=METRIC_CRS, max_move_m=50.0)
    assert "超过 max_move_m" in str(excinfo.value)


def test_normalize_rfc7946_orientation() -> None:
    clockwise = orient(box(BASE_X, BASE_Y, BASE_X + 100, BASE_Y + 100), sign=-1.0)
    from shapely import is_ccw

    assert not is_ccw(clockwise.exterior)
    normalized, changed = normalize_rfc7946(clockwise)
    assert changed is True
    assert is_ccw(normalized.exterior)
    check = validate_geometry(_to_lonlat(normalized), metric_crs=METRIC_CRS)
    assert check.rfc7946_orientation_ok is True
