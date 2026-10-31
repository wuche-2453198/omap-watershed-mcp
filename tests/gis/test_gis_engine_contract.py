"""引擎契约测试：参数默认值、引擎选择、结果字段/序列化、参数校验、证据字段。"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from helpers import FIXTURES, run_scenario, scenario

from ovital_mcp.coordinates import CANONICAL_CRS
from ovital_mcp.errors import DataValidationError
from ovital_mcp.gis import (
    WatershedParams,
    WatershedResult,
    get_engine,
    load_result,
    run_watershed,
)
from ovital_mcp.gis.dem import sha256_file


def test_params_defaults_match_contract() -> None:
    params = WatershedParams(dem_path="x.tif", outlet_lonlat=(116.0, 39.0))
    assert params.engine == "auto"
    assert params.snap_radius_m == 90.0
    assert params.metric_crs is None
    assert params.target_res_m is None
    assert params.min_accum_cells == 3
    assert params.simplify_tolerance_m == 0.0
    assert params.extra == {}


def test_get_engine_selection() -> None:
    native = get_engine("native")
    assert native.name == "native"
    assert native.available()[0] is True
    auto = get_engine("auto")
    assert auto.name in {"grass", "native"}
    with pytest.raises(DataValidationError):
        get_engine("bogus")


def test_validate_params_rejects_bad_extra(tmp_path: Path) -> None:
    dem = str(FIXTURES / "two_bowl_ridge.tif")
    outlet = tuple(scenario("two_bowl_ridge")["outlet_lonlat"])
    bad_extras = [
        {"max_cells": 0},
        {"max_cells": "abc"},
        {"n_segments": 0},
        {"resampling": "magic"},
    ]
    for index, extra in enumerate(bad_extras):
        with pytest.raises(DataValidationError):
            run_watershed(
                WatershedParams(
                    dem_path=dem, outlet_lonlat=outlet, extra=extra
                ),
                tmp_path / f"bad{index}",
            )


def test_result_serialization_roundtrip(tmp_path: Path) -> None:
    result = run_scenario(tmp_path / "roundtrip", "two_bowl_ridge")
    assert result.ok
    payload = result.to_dict()
    restored = WatershedResult.from_dict(payload)
    assert restored.task_dir == result.task_dir
    assert restored.area_m2 == pytest.approx(result.area_m2)
    assert restored.outlet_used == result.outlet_used
    assert restored.analysis_extent_lonlat == result.analysis_extent_lonlat
    assert restored.boundary_crs == CANONICAL_CRS
    assert restored.outlet_vertex_index == result.outlet_vertex_index
    assert restored.truncated == result.truncated


def test_result_contract_fields_populated(tmp_path: Path) -> None:
    result = run_scenario(tmp_path / "fields", "two_bowl_ridge")
    assert result.ok
    assert result.boundary_crs == CANONICAL_CRS
    assert isinstance(result.outlet_vertex_index, int)
    assert isinstance(result.truncated, bool)
    assert result.metric_crs.startswith("EPSG:")
    assert result.cell_area_m2 == pytest.approx(900.0)
    assert result.flow_accum_cells == 360
    assert result.snap_distance_m == 0.0
    assert result.geometry_valid is True
    assert result.engine_used == "native"
    assert "native" in result.engine_version
    assert len(result.analysis_extent_lonlat) == 4
    assert result.dem_info["sha256"] == sha256_file(FIXTURES / "two_bowl_ridge.tif")
    assert result.artifacts["boundary_geojson"].endswith("boundary.geojson")
    assert result.artifacts["mask_geotiff"].endswith("mask.tif")
    assert result.artifacts["provenance_json"].endswith("provenance.json")
    assert result.artifacts["result_json"].endswith("result.json")


def test_provenance_contains_required_evidence(tmp_path: Path) -> None:
    result = run_scenario(tmp_path / "evidence", "two_bowl_ridge")
    assert result.ok
    provenance = result.provenance
    assert provenance["verified_against_real_device"] is False
    assert provenance["offline_computed"] is True
    assert provenance["simulated"] is False
    assert provenance["evidence_level"] == "offline_computed"
    assert provenance["data_sources"]
    assert provenance["params"]["engine"] == "native"
    assert provenance["params"]["outlet_lonlat"] == pytest.approx(
        list(scenario("two_bowl_ridge")["outlet_lonlat"])
    )
    assert provenance["analysis_grid"]["metric_crs"] == result.metric_crs
    assert provenance["dem"]["sha256"] == sha256_file(FIXTURES / "two_bowl_ridge.tif")
    assert provenance["engine"]["version"] == result.engine_version
    assert "libraries" in provenance and "rasterio" in provenance["libraries"]
    # 落盘的 provenance.json 与 result.provenance 一致
    on_disk = json.loads(Path(result.artifacts["provenance_json"]).read_text())
    assert on_disk["determinism"]["fingerprint_sha256"] == provenance["determinism"][
        "fingerprint_sha256"
    ]


def test_load_result_requires_artifacts(tmp_path: Path) -> None:
    with pytest.raises(DataValidationError):
        load_result(tmp_path / "empty_task")


def test_result_reconstruction_and_review_like_worker_and_tools(tmp_path: Path) -> None:
    """模拟 worker `jsonable(result)` + tools `WatershedResult(**kwargs)` 重建后复核。

    这是跨模块集成契约：worker 把成果 JSON 化，tools 再重建对象调用
    `list_segments` / `render_segment`，两条路径都必须工作。
    """

    from ovital_mcp.gis.review import render_segment
    from ovital_mcp.gis.segment import list_segments

    result = run_scenario(tmp_path / "recon", "two_bowl_ridge")
    assert result.ok
    payload = json.loads(json.dumps(dataclasses.asdict(result), ensure_ascii=False))
    field_names = {field.name for field in dataclasses.fields(WatershedResult)}
    reconstructed = WatershedResult(
        **{key: value for key, value in payload.items() if key in field_names}
    )
    assert reconstructed.area_m2 == pytest.approx(result.area_m2)
    assert reconstructed.boundary_crs == "EPSG:4490"
    segments = list_segments(
        reconstructed.boundary_geojson,
        n_segments=8,
        metric_crs=reconstructed.metric_crs,
    )
    # 该解析边界是矩形（5 个顶点）→ 段数被顶点数限制，不能超过边数
    assert 1 <= len(segments) <= 8
    item = render_segment(
        reconstructed,
        segments[0]["segment_id"],
        tmp_path / "review_segments",
        max_pixels=600,
    )
    assert item["ok"] is True, item["warnings"]
    figure = item["figures"][0]
    bbox = figure["bbox_lonlat"]
    assert -180 <= bbox[0] <= bbox[2] <= 180
    assert -90 <= bbox[1] <= bbox[3] <= 90
    assert figure["crs"] == "EPSG:4490"
