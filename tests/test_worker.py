"""worker 任务落盘/错误码测试（不依赖奥维）。

覆盖 verifier F-12：引擎不可用时 job.error.code 必须保留 ENGINE_UNAVAILABLE，
而不是退化成笼统的 ENGINE_FAILED。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ovital_mcp.config import AppConfig, RuntimePaths
from ovital_mcp.jobs import JobManager

REPO = Path(__file__).resolve().parent.parent
DEM = REPO / "tests" / "fixtures" / "synthetic" / "tilted_plane.tif"
_EXPECTED = json.loads(
    (REPO / "tests" / "fixtures" / "synthetic" / "expected.json").read_text(encoding="utf-8")
)
TILTED = _EXPECTED["scenarios"]["tilted_plane"]
OUTLET = list(TILTED["outlet_lonlat"])
EXPECTED_AREA = float(TILTED["expected_area_m2"])


@pytest.fixture()
def job_manager(tmp_path: Path) -> JobManager:
    config = AppConfig.model_validate(
        {"storage": {"data_root": str(tmp_path / "data")}, "gis": {"engine": "native"}}
    )
    paths = RuntimePaths.build(config).ensure()
    return JobManager(config, paths, repo_root=REPO)


@pytest.mark.skipif(not DEM.is_file(), reason="合成 DEM fixture 不存在")
def test_engine_unavailable_preserves_specific_error_code(job_manager: JobManager) -> None:
    params = {
        "dem_path": str(DEM),
        "outlet_lonlat": OUTLET,
        "engine": "grass",
        "grass_cmd": "/nonexistent/grass-binary-for-test",
        "snap_radius_m": 0,
        "metric_crs": None,
        "target_res_m": None,
        "min_accum_cells": 3,
        "simplify_tolerance_m": 0.0,
        "review_max_pixels": 800,
        "max_cells": 36_000_000,
    }
    job = job_manager.submit("watershed", params)
    record = job_manager.wait(job.job_id, timeout_s=90, poll_s=0.3)
    assert record.status == "failed"
    assert record.error is not None
    assert record.error["code"] == "ENGINE_UNAVAILABLE"
    assert "GRASS" in record.error["message"]
    # 失败任务不得伪装成功成果
    assert job_manager.result(job.job_id) is None or job_manager.result(job.job_id)["ok"] is False


@pytest.mark.skipif(not DEM.is_file(), reason="合成 DEM fixture 不存在")
def test_successful_job_writes_reproducible_artifacts(job_manager: JobManager) -> None:
    params = {
        "dem_path": str(DEM),
        "outlet_lonlat": OUTLET,
        "engine": "native",
        "snap_radius_m": 0,
        "metric_crs": None,
        "target_res_m": None,
        "min_accum_cells": 3,
        "simplify_tolerance_m": 0.0,
        "review_max_pixels": 800,
        "max_cells": 9_000_000,
    }
    job = job_manager.submit("watershed", params)
    record = job_manager.wait(job.job_id, timeout_s=180, poll_s=0.3)
    assert record.status == "succeeded", record.error
    result = job_manager.result(job.job_id) or {}
    inner = result.get("result") or {}
    assert inner.get("ok") is True
    assert inner.get("area_m2") == pytest.approx(EXPECTED_AREA, rel=1e-6)
    boundary = Path(inner["boundary_geojson"])
    assert boundary.is_file()
    mask = Path(inner["mask_geotiff"])
    assert mask.is_file()
    manifest = Path(record.job_dir) / "artifacts" / "review" / "manifest.json"
    assert manifest.is_file(), "复核图 manifest 未生成"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data.get("figures"), "复核图 manifest 为空"
    for figure in data["figures"]:
        assert Path(figure["path"]).is_file()
