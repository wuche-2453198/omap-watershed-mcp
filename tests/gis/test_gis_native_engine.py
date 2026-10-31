"""native 引擎的**解析真值**验证与异常路径测试。

核心思想：合成地形的分水岭可以由公式推导，因此测试直接断言上游掩膜的
**像元集合**（而不只是面积接近）。所有解析场景都用 ``snap_radius_m=0``
（关闭吸附）运行，把"吸附语义"留给单独的吸附测试，避免两种语义混在一起。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio

from helpers import (
    FIXTURES,
    analytic_mask,
    assert_close,
    is_two_bowl_half,
    read_mask,
    read_mask_grid,
    run_scenario,
    scenario,
    warnings_text,
)

from ovital_mcp.errors import DataValidationError
from ovital_mcp.gis import WatershedParams, load_result, run_watershed
from ovital_mcp.gis.dem import sha256_file

#: 可以逐像元断言解析真值的场景。
EXACT_SCENARIOS = [
    "two_bowl_ridge",
    "two_bowl_ridge_outlet_b",
    "single_bowl",
    "tilted_plane",
    "plane_with_pit_downstream",
    "plane_with_pit_outlet",
    "two_bowl_nodata_wall",
    "two_bowl_nodata_hole",
    "valley_converge",
    "single_bowl_fine",
]


@pytest.mark.parametrize("name", EXACT_SCENARIOS)
def test_native_analytic_mask_exact(tmp_path: Path, name: str) -> None:
    """掩膜像元集合必须与解析真值**完全一致**，面积由像元数精确复算。"""

    spec = scenario(name)
    result = run_scenario(tmp_path / name, name, engine="native")
    assert result.ok, result.error
    assert result.engine_used == "native"

    mask = read_mask(result)
    assert mask.shape == (spec["height"], spec["width"])
    expected = analytic_mask(spec["mask_rule"], *mask.shape)
    assert expected is not None
    difference = int(np.count_nonzero(mask ^ expected))
    assert difference == 0, (
        f"{name}: 掩膜与解析真值差 {difference} 个像元；"
        f"实际 {int(mask.sum())} vs 期望 {int(expected.sum())}"
    )
    assert int(mask.sum()) == spec["expected_mask_cells"]

    # 面积：解析像元面积（30 m DEM=900 m²，10 m 细网格=100 m²），1e-9 相对误差
    cell_area = float(spec["cell_area_m2"])
    assert result.cell_area_m2 == pytest.approx(cell_area)
    assert_close(result.area_m2, spec["expected_area_m2"], rel=1e-9)
    assert_close(result.area_m2, int(mask.sum()) * cell_area, rel=1e-9)

    # native D8 不变量：出口累积 = 上游像元数（含自身）
    assert result.flow_accum_cells == int(mask.sum())
    # 关闭吸附语义：距离 0、outlet_used 与输入完全一致
    assert result.snap_distance_m == 0.0
    assert result.outlet_used == tuple(spec["outlet_lonlat"])

    assert result.geometry_valid is True
    assert result.hole_count == spec["expected_hole_count"]
    assert result.truncated is spec["expected_truncated"]
    text = warnings_text(result)
    for keyword in spec["expected_warnings_any"]:
        assert keyword in text, f"{name}: 缺少警告关键词 {keyword!r}"

    # 边界 GeoJSON 属性与成果一致（tools 层依赖这些字段）
    boundary = json.loads(Path(result.boundary_geojson).read_text(encoding="utf-8"))
    properties = boundary["features"][0]["properties"]
    assert properties["area_m2"] == pytest.approx(result.area_m2)
    assert properties["mask_cell_count"] == int(mask.sum())
    assert properties["metric_crs"] == result.metric_crs
    assert properties["engine"] == "native"
    assert properties["boundary_crs"] == "EPSG:4490"
    assert properties["verified_against_real_device"] is False
    assert properties["evidence_level"] == "offline_computed"
    assert properties["outlet_vertex_index"] == result.outlet_vertex_index


def test_native_crater_basin_no_truncation(tmp_path: Path) -> None:
    """闭合内流盆地：掩膜落在解析包络内、不接触分析范围边界、无截断警告。"""

    spec = scenario("crater_basin")
    result = run_scenario(tmp_path / "crater", "crater_basin")
    assert result.ok, result.error
    mask = read_mask(result)
    cells = int(mask.sum())
    assert spec["expected_mask_min_cells"] <= cells <= spec["expected_mask_max_cells"]
    assert result.truncated is False
    assert "分析范围截断" not in warnings_text(result)
    assert not (
        mask[0, :].any() or mask[-1, :].any() or mask[:, 0].any() or mask[:, -1].any()
    ), "闭合盆地集水区不应接触分析范围边界"
    assert result.geometry_valid is True
    assert_close(result.area_m2, cells * 900.0, rel=1e-9)


def test_native_snap_offset_reports_distance_and_evidence(tmp_path: Path) -> None:
    """出口偏移 20 m：应吸附到解析像元并报告吸附距离/候选证据。"""

    spec = scenario("tilted_plane_snap_offset")
    result = run_scenario(tmp_path / "snap", "tilted_plane_snap_offset")
    assert result.ok, result.error
    assert result.snap_distance_m == pytest.approx(20.0, abs=1e-6)
    assert result.outlet_used != tuple(spec["outlet_lonlat"])
    mask = read_mask(result)
    expected = analytic_mask(spec["mask_rule"], *mask.shape)
    assert expected is not None
    assert np.array_equal(mask, expected)
    assert result.flow_accum_cells == spec["expected_flow_accum_cells"]
    assert "出口已从输入点吸附" in warnings_text(result)

    evidence = result.provenance["outlet_snap"]
    assert evidence["mode"] == "max_accum_in_radius"
    assert evidence["candidates"], "吸附必须留下候选点证据"
    assert evidence["snapped_cell"] == [7, 59]
    assert evidence["distance_m"] == pytest.approx(20.0, abs=1e-6)


def test_native_ambiguous_snap_reports_candidates(tmp_path: Path) -> None:
    """出口落在两碗分水岭上：两侧碗心累积相同，应报告候选不唯一。"""

    spec = scenario("two_bowl_ridge_ambiguous_snap")
    result = run_scenario(tmp_path / "ambiguous", "two_bowl_ridge_ambiguous_snap")
    assert result.ok, result.error
    mask = read_mask(result)
    assert is_two_bowl_half(mask), "吸附结果应落在完整的一个半区（A 或 B）"
    assert int(mask.sum()) == spec["expected_mask_cells"]
    evidence = result.provenance["outlet_snap"]
    assert evidence["ambiguous"] is True
    assert len(evidence["candidates"]) >= 2
    assert "候选栅格" in warnings_text(result)


def test_native_geographic_dem_matches_closed_form_separatrix(tmp_path: Path) -> None:
    """地理坐标 DEM 重投影到 UTM 后，分水岭仍应贴合解析分水岭（容差内）。"""

    spec = scenario("two_bowl_ridge_geo")
    result = run_scenario(tmp_path / "geo", "two_bowl_ridge_geo")
    assert result.ok, result.error
    assert result.dem_info["is_geographic"] is True
    assert any("地理坐标系" in item for item in result.warnings)

    raw, transform, _crs = read_mask_grid(result)
    mask = raw == 1
    valid = raw != 255
    height, width = mask.shape
    rows, cols = np.mgrid[0:height, 0:width]
    x = transform.c + (cols + 0.5) * transform.a
    y = transform.f + (rows + 0.5) * transform.e
    # 合成地形定义在 (ORIGIN_X, ORIGIN_TOP_Y) / 30 m 的像元索引坐标上
    col_index = (x - 499000.0) / 30.0 - 0.5
    row_index = (4400000.0 - y) / 30.0 - 0.5
    ra2 = (row_index - 5.0) ** 2 + (col_index - 21.0) ** 2
    rb2 = (row_index - 5.0) ** 2 + (col_index - 38.0) ** 2
    expected = (ra2 < rb2) & valid
    mismatch = int(np.count_nonzero(mask ^ expected))
    assert mismatch <= 4, f"重投影路径与解析分水岭差 {mismatch} 像元"
    assert abs(int(mask.sum()) - int(expected.sum())) <= 4
    # 面积口径必须与分析网格自洽（地理 DEM 的分析范围略大于原始 UTM 图幅，
    # 因此不能拿 324000 m² 硬套；用分析网格像元数 × 像元面积复算）
    cell_area = result.cell_area_m2
    assert cell_area > 0
    assert abs(result.area_m2 - int(expected.sum()) * cell_area) <= 6 * cell_area
    assert result.geometry_valid is True


def test_native_determinism_two_runs_identical(tmp_path: Path) -> None:
    """同一输入/参数两次运行：指纹、掩膜、边界、分析 DEM 的 SHA256 必须一致。"""

    first = run_scenario(tmp_path / "run1", "two_bowl_ridge")
    second = run_scenario(tmp_path / "run2", "two_bowl_ridge")
    assert first.ok and second.ok

    det_a = first.provenance["determinism"]
    det_b = second.provenance["determinism"]
    for key in (
        "fingerprint_sha256",
        "mask_sha256",
        "boundary_wkb_sha256",
        "analysis_dem_sha256",
        "params_sha256",
    ):
        assert det_a[key] == det_b[key], f"确定性指纹字段不一致：{key}"

    for artifact in ("mask_geotiff", "boundary_geojson", "analysis_dem_tif"):
        path_a = Path(first.artifacts[artifact])
        path_b = Path(second.artifacts[artifact])
        assert sha256_file(path_a) == sha256_file(path_b), (
            f"两次运行的 {artifact} 字节不一致：{path_a.name}"
        )
    assert np.array_equal(read_mask(first), read_mask(second))


def test_native_writes_stable_artifacts_and_result_json(tmp_path: Path) -> None:
    """稳定成果文件名 + result.json 可回读 + provenance 证据字段。"""

    result = run_scenario(tmp_path / "artifacts", "two_bowl_ridge", render_overview=True)
    assert result.ok, result.error
    task_dir = Path(result.task_dir)
    for filename in (
        "boundary.geojson",
        "mask.tif",
        "provenance.json",
        "result.json",
        "analysis_dem.tif",
        "flow_direction.tif",
        "flow_accum.tif",
        "segments.geojson",
        "streams.geojson",
    ):
        assert (task_dir / filename).is_file(), f"缺少稳定成果文件 {filename}"
    assert (task_dir / "review" / "overview.png").is_file()
    assert result.artifacts["overview_png"].endswith("overview.png")

    loaded = load_result(task_dir)
    assert loaded.ok is True
    assert loaded.area_m2 == pytest.approx(result.area_m2)
    assert loaded.boundary_crs == "EPSG:4490"
    assert loaded.outlet_vertex_index is not None
    assert loaded.truncated is True

    provenance = json.loads((task_dir / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["verified_against_real_device"] is False
    assert provenance["offline_computed"] is True
    assert provenance["simulated"] is False
    assert provenance["evidence_level"] == "offline_computed"
    assert provenance["dem"]["sha256"] == sha256_file(FIXTURES / "two_bowl_ridge.tif")
    assert provenance["determinism"]["mask_sha256"] == sha256_file(result.mask_geotiff)
    assert provenance["engine"]["name"] == "native"
    assert provenance["result"]["mask_cell_count"] == 360


def test_native_target_res_resamples_and_keeps_area(tmp_path: Path) -> None:
    """target_res_m=60：分析网格变为 30×6，解析半区约 90 像元、面积不变。"""

    result = run_scenario(tmp_path / "coarse", "two_bowl_ridge", target_res_m=60.0)
    assert result.ok, result.error
    mask = read_mask(result)
    # 分析范围向外对齐到 60 m 整数倍 → 31×7 栅格，其中有效像元 30×6
    assert mask.shape == (7, 31)
    cells = int(mask.sum())
    assert cells == 90, f"60 m 重采样后解析半区应精确 90 像元，实际 {cells}"
    assert result.cell_area_m2 == pytest.approx(3600.0)
    # 解析半区面积 = 90 × 3600 = 324000 m²，与原始 30 m 结果一致
    assert_close(result.area_m2, 324000.0, rel=1e-9)


def test_native_simplify_wiring_and_protected_outlet(tmp_path: Path) -> None:
    """引擎内的受控简化接线：报告完整、面积不被静默改变、内部出口不硬插。

    注意：30 m 栅格化的边界是 30 m 阶梯，允许容差（1% 等效半径 ≈3.2 m）内
    不会减少顶点；"顶点确实减少"由 test_gis_geometry 用解析多边形验证。
    """

    result = run_scenario(
        tmp_path / "simplify", "crater_basin", simplify_tolerance_m=3.0
    )
    assert result.ok, result.error
    assert result.geometry_valid is True
    assert "已简化" in warnings_text(result)
    report = result.provenance["result"]["simplify"]
    assert report["applied"] is True
    assert report["tolerance_m"] == 3.0
    assert report["max_allowed_tolerance_m"] == pytest.approx(3.198, abs=1e-2)
    assert report["hausdorff_m"] <= 2.0 * report["tolerance_m"] + 1e-6
    assert report["protected_points"] == 1
    # 该场景出口在流域内部（碗心 sink），不应被硬插成边界尖刺
    assert report["skipped_protected_points"], "内部出口保护点应被记录为跳过"
    # 允许容差内简化不改变面积（阶梯边界未被削平）
    assert_close(result.area_m2, 321300.0, rel=1e-9)


def test_native_oversized_simplify_rejected_without_silent_change(tmp_path: Path) -> None:
    """容差过大时显式拒绝简化并保留原始边界（不静默改拓扑）。"""

    result = run_scenario(tmp_path / "simplify_big", "single_bowl", simplify_tolerance_m=500.0)
    assert result.ok, result.error
    assert "简化被拒绝" in warnings_text(result)
    assert_close(result.area_m2, 1512900.0, rel=1e-9)
    assert result.provenance["result"]["simplify"] is None


def test_invalid_params_raise_data_validation_error(tmp_path: Path) -> None:
    dem = str(FIXTURES / "two_bowl_ridge.tif")
    outlet = scenario("two_bowl_ridge")["outlet_lonlat"]
    with pytest.raises(DataValidationError):
        run_watershed(
            WatershedParams(dem_path="does/not/exist.tif", outlet_lonlat=tuple(outlet)),
            tmp_path / "a",
        )
    with pytest.raises(DataValidationError):
        run_watershed(
            WatershedParams(dem_path=dem, outlet_lonlat=(999.0, 39.0)), tmp_path / "b"
        )
    with pytest.raises(DataValidationError):
        run_watershed(
            WatershedParams(dem_path=dem, outlet_lonlat=tuple(outlet), snap_radius_m=-1.0),
            tmp_path / "c",
        )
    with pytest.raises(DataValidationError):
        run_watershed(
            WatershedParams(dem_path=dem, outlet_lonlat=tuple(outlet), engine="bogus"),
            tmp_path / "d",
        )


def test_outlet_outside_dem_returns_failure_not_exception(tmp_path: Path) -> None:
    """出口在 DEM 覆盖外：显式失败（ok=False + failure.json），不抛异常。"""

    result = run_watershed(
        WatershedParams(
            dem_path=str(FIXTURES / "two_bowl_ridge.tif"),
            outlet_lonlat=(117.5, 39.0),
            engine="native",
            snap_radius_m=0.0,
            extra={"render_overview": False},
        ),
        tmp_path / "outside",
    )
    assert result.ok is False
    assert "出口不在" in (result.error or "")
    task_dir = Path(result.task_dir)
    assert (task_dir / "failure.json").is_file()
    assert not (task_dir / "boundary.geojson").exists()
    failure = json.loads((task_dir / "failure.json").read_text(encoding="utf-8"))
    assert failure["ok"] is False
    assert failure["error_code"]
    assert failure["traceback"]
    loaded = load_result(task_dir)
    assert loaded.ok is False


def test_nodata_outlet_with_snap_disabled_fails_explicitly(tmp_path: Path) -> None:
    """snap_radius_m=0 且出口落在 NoData：显式失败，不悄悄挪动出口。"""

    # NoData 块中心 (row=5, col=15) 的经纬度：由解析 DEM 像元中心换算
    from ovital_mcp.gis.crs import transform_xy_to_lonlat

    x, y = 499000.0 + (15 + 0.5) * 30.0, 4400000.0 - (5 + 0.5) * 30.0
    lon, lat = transform_xy_to_lonlat(x, y, "EPSG:32650")
    result = run_watershed(
        WatershedParams(
            dem_path=str(FIXTURES / scenario("two_bowl_nodata_wall")["dem"]),
            outlet_lonlat=(lon, lat),
            engine="native",
            snap_radius_m=0.0,
            extra={"render_overview": False},
        ),
        tmp_path / "nodata_outlet",
    )
    assert result.ok is False
    assert "NoData" in (result.error or "")
    assert (Path(result.task_dir) / "failure.json").is_file()


def test_engine_unavailable_returns_failure(tmp_path: Path) -> None:
    """显式请求 GRASS 但可执行文件不存在：ok=False + ENGINE_UNAVAILABLE。"""

    outlet = scenario("two_bowl_ridge")["outlet_lonlat"]
    result = run_watershed(
        WatershedParams(
            dem_path=str(FIXTURES / "two_bowl_ridge.tif"),
            outlet_lonlat=tuple(outlet),
            engine="grass",
            extra={
                "grass_cmd": "/nonexistent/definitely-not-grass",
                "render_overview": False,
            },
        ),
        tmp_path / "no_grass",
    )
    assert result.ok is False
    assert (result.error_code or "") == "ENGINE_UNAVAILABLE"
    assert (Path(result.task_dir) / "failure.json").is_file()
