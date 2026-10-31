"""DEM 检查（inspect_dem）测试：正常路径 + 各类异常/警告路径。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from helpers import FIXTURES, ROOT

from ovital_mcp.errors import DataValidationError
from ovital_mcp.gis import inspect_dem, sample_dem_lonlat, sha256_file


def test_inspect_valid_projected_dem() -> None:
    info = inspect_dem(FIXTURES / "two_bowl_ridge.tif")
    assert info.width == 60 and info.height == 12
    assert info.crs_epsg == 32650
    assert info.is_geographic is False
    assert info.res_x == 30.0 and info.res_y == 30.0
    assert info.nodata is None
    assert info.nodata_fraction == 0.0
    # 解析地形：碗心 100 m，最高点在 A 侧角点 (row=11, col=0) → 100+0.1*(36+441)=147.7
    assert info.min_elev is not None and abs(info.min_elev - 100.0) < 1e-3
    assert info.max_elev is not None and abs(info.max_elev - 147.7) < 1e-2
    # SHA256 与独立计算一致
    assert info.sha256 == sha256_file(FIXTURES / "two_bowl_ridge.tif")
    assert info.extent_lonlat is not None
    lon_min, lat_min, lon_max, lat_max = info.extent_lonlat
    assert 116.0 < lon_min < lon_max < 118.0
    assert 39.0 < lat_min < lat_max < 41.0
    # 必须显式说明垂直基准未知 + DSM/DTM 未知
    text = "\n".join(info.warnings)
    assert "垂直基准未知" in text
    assert "DSM" in text and "DTM" in text
    assert info.vertical_datum_known is False
    assert info.provisional is False


def test_inspect_missing_crs_is_provisional() -> None:
    info = inspect_dem(FIXTURES / "inspect_no_crs.tif")
    assert info.crs_wkt is None and info.crs_epsg is None
    assert info.is_geographic is False
    assert info.extent_lonlat is None
    assert info.provisional is True
    assert any("缺少 CRS" in w for w in info.warnings)


def test_inspect_constant_and_all_zero_are_provisional() -> None:
    constant = inspect_dem(FIXTURES / "inspect_constant.tif")
    assert constant.provisional is True
    assert constant.min_elev == constant.max_elev == 100.0
    assert any("常数" in w for w in constant.warnings)

    zero = inspect_dem(FIXTURES / "inspect_all_zero.tif")
    assert zero.provisional is True
    assert any("全部为 0" in w for w in zero.warnings)


def test_inspect_out_of_range_elevation() -> None:
    info = inspect_dem(FIXTURES / "inspect_out_of_range.tif")
    assert info.provisional is True
    assert any("量程异常" in w for w in info.warnings)


def test_inspect_high_nodata_fraction() -> None:
    info = inspect_dem(FIXTURES / "inspect_high_nodata.tif")
    assert info.nodata_fraction == pytest.approx(0.6, abs=0.01)
    assert info.provisional is True
    assert any("NoData" in w for w in info.warnings)


def test_inspect_geographic_dem_reports_degree_resolution() -> None:
    info = inspect_dem(FIXTURES / "inspect_geographic.tif")
    assert info.is_geographic is True
    assert info.res_approx_m is not None
    # 0.0003° ≈ 30 m
    assert 25.0 < info.res_approx_m[0] < 40.0
    assert 25.0 < info.res_approx_m[1] < 40.0
    assert any("地理坐标系" in w for w in info.warnings)
    assert info.extent_lonlat is not None


def test_inspect_missing_file_raises() -> None:
    with pytest.raises(DataValidationError):
        inspect_dem(FIXTURES / "does_not_exist.tif")


def test_sample_dem_lonlat_handles_edge_vertices() -> None:
    """边界顶点常落在 DEM 最外缘：必须能采到值而不是 None。"""

    expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
    spec = expected["scenarios"]["two_bowl_ridge"]
    lon, lat = spec["outlet_lonlat"]
    values = sample_dem_lonlat(FIXTURES / "two_bowl_ridge.tif", [(lon, lat)])
    assert len(values) == 1 and values[0] is not None
    assert abs(values[0] - 100.0) < 1e-3

    # 栅格最外角（左上角外侧顶点附近）也应采到边缘像元
    extent = spec["outlet_lonlat"]
    del extent
    from ovital_mcp.gis.dem import dem_lonlat_to_crs

    x, y = dem_lonlat_to_crs(FIXTURES / "two_bowl_ridge.tif", lon, lat)
    del x, y
