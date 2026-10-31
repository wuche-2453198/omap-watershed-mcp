"""GIS 测试公用工具（fixture 读取、解析真值掩膜、引擎运行封装）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import rasterio

from ovital_mcp.gis import WatershedParams, WatershedResult, run_watershed

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "synthetic"


def load_expected() -> dict[str, Any]:
    """读取合成 DEM 的解析真值 fixture。"""

    return json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))


def scenario(name: str) -> dict[str, Any]:
    return load_expected()["scenarios"][name]


def analytic_mask(rule: dict[str, Any], height: int, width: int) -> np.ndarray | None:
    """按解析规则构造期望掩膜；无法逐像元表达时返回 ``None``。

    规则来自 ``scripts/make_synthetic_dem.py`` 的地形推导，独立于被测实现。
    """

    rows, cols = np.mgrid[0:height, 0:width]
    rows = rows.astype(np.float64)
    cols = cols.astype(np.float64)
    kind = rule["type"]
    if kind == "all":
        mask = np.ones((height, width), dtype=bool)
    elif kind == "columns_le":
        mask = cols <= float(rule["value"])
    elif kind == "columns_ge":
        mask = cols >= float(rule["value"])
    elif kind == "row_eq":
        mask = rows == float(rule["value"])
    elif kind == "row_columns_le":
        mask = (rows == float(rule["row"])) & (cols <= float(rule["value"]))
    elif kind == "row_columns_range":
        mask = (
            (rows == float(rule["row"]))
            & (cols >= float(rule["min"]))
            & (cols <= float(rule["max"]))
        )
    elif kind == "rows_columns_le":
        mask = np.isin(rows, [float(v) for v in rule["rows"]]) & (
            cols <= float(rule["value"])
        )
    else:
        # two_bowl_half / radius_bounds / expect_cells：不是单一像元集合
        return None
    block = rule.get("exclude_block")
    if block:
        r0, c0, r1, c1 = block
        mask = mask & ~((rows >= r0) & (rows <= r1) & (cols >= c0) & (cols <= c1))
    for row, col_min, col_max in rule.get("exclude_strips", []):
        mask = mask & ~((rows == row) & (cols >= col_min) & (cols <= col_max))
    for row, col in rule.get("exclude_cells", []):
        mask = mask & ~((rows == row) & (cols == col))
    return mask


def run_scenario(
    out_dir: Path,
    name: str,
    *,
    engine: str = "native",
    render_overview: bool = False,
    **param_overrides: Any,
) -> WatershedResult:
    """按 fixture 场景运行引擎（默认关闭概览图以加快测试）。"""

    spec = scenario(name)
    extra: dict[str, Any] = {"render_overview": render_overview}
    extra.update(param_overrides.pop("extra", {}) or {})
    params = WatershedParams(
        dem_path=str(FIXTURES / spec["dem"]),
        outlet_lonlat=tuple(spec["outlet_lonlat"]),
        engine=engine,
        snap_radius_m=float(spec.get("snap_radius_m", 0.0)),
        **param_overrides,
        extra=extra,
    )
    return run_watershed(params, out_dir)


def read_mask(result_or_path: WatershedResult | str | Path) -> np.ndarray:
    """读二值掩膜（True = 流域内）。"""

    path = (
        result_or_path.mask_geotiff
        if isinstance(result_or_path, WatershedResult)
        else str(result_or_path)
    )
    assert path, "成果没有 mask_geotiff"
    with rasterio.open(path) as ds:
        data = ds.read(1)
    return data == 1


def read_mask_grid(
    result_or_path: WatershedResult | str | Path,
) -> tuple[np.ndarray, rasterio.Affine, str]:
    path = (
        result_or_path.mask_geotiff
        if isinstance(result_or_path, WatershedResult)
        else str(result_or_path)
    )
    with rasterio.open(path) as ds:
        return ds.read(1), ds.transform, str(ds.crs)


def warnings_text(result: WatershedResult) -> str:
    return "\n".join(result.warnings)


def is_two_bowl_half(mask: np.ndarray) -> bool:
    """判定掩膜是否正好是两碗地形的一个完整半区（列0..29 或 列30..59）。"""

    height, width = mask.shape
    if width % 2:
        return False
    left = np.zeros_like(mask)
    left[:, : width // 2] = True
    right = np.zeros_like(mask)
    right[:, width // 2 :] = True
    return bool(np.array_equal(mask, left) or np.array_equal(mask, right))


def assert_close(actual: float, expected: float, *, rel: float = 1e-6, abs_: float = 1e-6) -> None:
    tolerance = max(abs_, rel * abs(expected))
    assert abs(actual - expected) <= tolerance, (
        f"数值不匹配：actual={actual!r} expected={expected!r} tolerance={tolerance!r}"
    )
