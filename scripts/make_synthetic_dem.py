"""生成带**解析真值**的合成 DEM 与测试 fixture。

为什么需要合成地形
------------------
真实 DEM 没有独立审核边界时，只能证明"可复现/合理"，不能证明"分水岭正确"。
本脚本构造若干可以**数学推导分水岭**的地形，把上游掩膜的像元集合与面积
写成 fixture，让测试逐像元断言（而不是只看面积接近）：

``two_bowl_ridge``
    两个对称抛物面流域，曲率相同。两碗之间的严格分水岭是两条抛物线
    ``min(rA², rB²)`` 的等值线，即**两个碗心的垂直平分线**；本文件把该线
    放在像元列 29 与 30 之间（像元中心为整数索引），因此没有任何像元中心
    落在分水岭上，D8 流向唯一。A 碗心在 (行5, 列21)，B 碗心 (行5, 列38)，
    分水岭 x=29.5。碗心曲率足够大（D=8.5 像元）保证斜向跨越分水岭一定是
    上坡，所以 A 碗心的集水区**精确等于**列 0..29 的全部像元 = 30×12 = 360。
    出口吸附到 A 碗心（汇流累积最大）后，掩膜像元数应 = 360，面积 = 360×900。

``single_bowl``
    单个抛物面碗，碗心是唯一汇（sink）。所有像元都流向碗心，集水区 = 全部
    41×41 = 1681 个像元；面积 = 1 512 900 m²。用于验证"全图掩膜"与边界
    接触分析范围时的截断警告。

``tilted_plane``
    严格东向下降的平面（无横向分量）。每一行的像元只沿行向东流，因此
    出口 (行7, 列59) 的上游精确等于**行 7 的全部 60 个像元**（r.water.outlet
    文档所说的 "thin sliver of land" 情形）。面积 = 54 000 m²。

``plane_with_pit``
    东向下降平面 + (行7, 列20) 处深 15 m 的洼地（不填洼）。洼地是 sink：
    列 0..20 流入洼地；列 21 因为紧邻洼地而向西流入洼地；列 22..59 继续
    向东。因此洼地出口的集水区 = 列 0..20（21 个像元），下游出口 (行7,列59)
    的集水区 = 列 22..59（38 个像元）。用于验证"不填洼"的真实后果。

``two_bowl_nodata``
    在 ``two_bowl_ridge`` 的 A 碗内部挖掉 3×3 NoData 块（行4..6, 列14..16）。
    NoData 像元不参与汇流，也不进入掩膜，因此 A 碗集水区 = 360 − 9 = 351 个
    像元，且边界多边形出现 1 个洞（内部空洞）。

``crater_basin``
    内部为向心凹陷（中心 sink）、外侧向外倾斜的环形山地形。中心集水区被
    环形山脊围住，**不接触分析范围边界**，用于验证"无截断"路径。由于离散
    D8 在环脊附近有方向歧义，本场景只断言掩膜落在半径 9–11 像元的解析
    包络内，且不触发截断警告。

``two_bowl_ridge_geo``
    把 ``two_bowl_ridge`` 的地形按 EPSG:4490 经纬度格网采样，用于验证
    "地理坐标 DEM → 米制重投影 → D8" 路径；掩膜像元数允许 ±10 的离散误差，
    面积允许 ±3%（重采样 + 格网错位）。

所有 DEM 的像元面积都精确为 900 m²（EPSG:32650，30 m），因此面积可以
用解析像元数精确复算。脚本同时生成 ``expected.json`` 记录每个场景的
出口、解析掩膜规则、期望像元数与面积，供测试直接断言。

用法::

    .venv/bin/python scripts/make_synthetic_dem.py --out-dir tests/fixtures/synthetic
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

#: 合成 DEM 的统一参数：30 m 分辨率、UTM 50N（北京附近真实坐标带）。
RES_M = 30.0
UTM_CRS = "EPSG:32650"
CANONICAL_CRS = "EPSG:4490"
ORIGIN_X = 499000.0
ORIGIN_TOP_Y = 4400000.0
CELL_AREA_M2 = RES_M * RES_M


# ---------------------------------------------------------------------------
# 地形函数（全部在"像元索引坐标"下定义，与 GeoTIFF 仿射无关）
# ---------------------------------------------------------------------------


def _index_grid(width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    rows, cols = np.mgrid[0:height, 0:width]
    return rows.astype(np.float64), cols.astype(np.float64)


def two_bowl_elevation(
    width: int = 60,
    height: int = 12,
    *,
    k: float = 0.1,
    base: float = 100.0,
    center_a: tuple[float, float] = (5.0, 21.0),
    center_b: tuple[float, float] = (5.0, 38.0),
) -> np.ndarray:
    """两个同曲率抛物面的下包络：严格分水岭 = 碗心垂直平分线。

    ``z = base + k·min(rA², rB²)``。A 碗心 (5,21)，B 碗心 (5,38)，分水岭
    x = 29.5；像元中心为整数索引，故没有像元中心落在分水岭上。
    """

    rows, cols = _index_grid(width, height)
    ra2 = (rows - center_a[0]) ** 2 + (cols - center_a[1]) ** 2
    rb2 = (rows - center_b[0]) ** 2 + (cols - center_b[1]) ** 2
    return base + k * np.minimum(ra2, rb2)


def single_bowl_elevation(
    width: int = 41,
    height: int = 41,
    *,
    k: float = 0.1,
    base: float = 100.0,
    center: tuple[float, float] = (20.0, 20.0),
) -> np.ndarray:
    """单个抛物面碗；碗心为唯一 sink，所有像元流向它。"""

    rows, cols = _index_grid(width, height)
    return base + k * ((rows - center[0]) ** 2 + (cols - center[1]) ** 2)


def tilted_plane_elevation(
    width: int = 60,
    height: int = 20,
    *,
    base: float = 100.0,
    drop_per_cell: float = 0.05,
) -> np.ndarray:
    """严格东向下降平面：每行像元只沿行向东流（无横向分量）。"""

    rows, cols = _index_grid(width, height)
    del rows
    return base + drop_per_cell * ((width - 1) - cols)


def plane_with_pit_elevation(
    width: int = 60,
    height: int = 20,
    *,
    pit: tuple[int, int] = (7, 20),
    pit_depth: float = 15.0,
    base: float = 100.0,
    drop_per_cell: float = 0.05,
) -> np.ndarray:
    """东向下降平面 + 一个不填平的深洼地（真实洼地/水库的类比）。"""

    z = tilted_plane_elevation(
        width, height, base=base, drop_per_cell=drop_per_cell
    )
    z[pit[0], pit[1]] -= pit_depth
    return z


def valley_converge_elevation(
    width: int = 60,
    height: int = 20,
    *,
    base: float = 100.0,
    south_drop: float = 0.05,
    channel_pull: float = 0.1,
    channel_col: float = 30.0,
) -> np.ndarray:
    """南向倾斜 + 向中央河道收敛的 V 形谷（无并列坡度，全图汇入出口）。

    ``z = base − south_drop·row + channel_pull·|col − channel_col|``。
    因为 ``channel_pull/√2 > south_drop``，每个像元的最陡下降都是"朝河道 +
    向南"的斜向步；到河道后严格向南。出口取南边界河道像元 (19, 30)，
    解析集水区 = 全部 60×20 = 1200 个像元（闭合于分析范围边界）。
    该地形没有任何并列坡度，是 GRASS SFD 与 native D8 都应当精确复现的
    二维收敛场景。
    """

    rows, cols = _index_grid(width, height)
    return (
        base
        - south_drop * rows
        + channel_pull * np.abs(cols - channel_col)
    )


def crater_elevation(
    width: int = 41,
    height: int = 41,
    *,
    center: tuple[float, float] = (20.0, 20.0),
    rim_radius: float = 10.0,
    k_in: float = 0.5,
    k_out: float = 0.05,
    base: float = 100.0,
) -> np.ndarray:
    """环形山：内部向心凹陷（中心 sink），外侧向外倾斜（闭合内流盆地）。"""

    rows, cols = _index_grid(width, height)
    radius = np.hypot(rows - center[0], cols - center[1])
    inside = base - k_in * (rim_radius - radius) ** 2
    outside = base - k_out * (radius - rim_radius) ** 2
    return np.where(radius <= rim_radius, inside, outside)


def _nodata_block(shape: tuple[int, int], r0: int, c0: int, r1: int, c1: int) -> np.ndarray:
    """返回布尔掩膜：行 r0..r1、列 c0..c1（含端点）为 True。"""

    mask = np.zeros(shape, dtype=bool)
    mask[r0 : r1 + 1, c0 : c1 + 1] = True
    return mask


# ---------------------------------------------------------------------------
# 网格 / 写文件 / 坐标换算
# ---------------------------------------------------------------------------


def _metric_transform(width: int, height: int, res_m: float = RES_M) -> rasterio.Affine:
    del width, height
    return from_origin(ORIGIN_X, ORIGIN_TOP_Y, res_m, res_m)


def cell_center_metric(
    row: float, col: float, res_m: float = RES_M
) -> tuple[float, float]:
    """像元中心（UTM 坐标）。"""

    x = ORIGIN_X + (float(col) + 0.5) * res_m
    y = ORIGIN_TOP_Y - (float(row) + 0.5) * res_m
    return x, y


def xy_to_lonlat(x: float, y: float) -> tuple[float, float]:
    transformer = Transformer.from_crs(UTM_CRS, CANONICAL_CRS, always_xy=True)
    lon, lat = transformer.transform(float(x), float(y))
    return float(lon), float(lat)


def write_dem(
    path: Path,
    array: np.ndarray,
    *,
    crs: str | None,
    transform: rasterio.Affine,
    nodata: float | None = None,
    dtype: str = "float32",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "dtype": dtype,
        "count": 1,
        "width": array.shape[1],
        "height": array.shape[0],
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(array.astype(dtype), 1)


# ---------------------------------------------------------------------------
# 地理坐标变体
# ---------------------------------------------------------------------------


def two_bowl_geographic(
    *, width: int = 60, height: int = 12
) -> tuple[np.ndarray, rasterio.Affine]:
    """把 ``two_bowl_ridge`` 地形按 EPSG:4490 经纬度格网采样。

    先在 UTM 下计算该 DEM 的四角经纬度，再构造一个约 30 m 的经纬度格网，
    逐像元中心反算 UTM 坐标并代入解析地形。重投影回 UTM 后应与原地形近似。
    """

    metric_transform = _metric_transform(width, height)
    to_lonlat = Transformer.from_crs(UTM_CRS, CANONICAL_CRS, always_xy=True)
    left = metric_transform.c
    top = metric_transform.f
    right = left + width * RES_M
    bottom = top - height * RES_M
    corners = [
        to_lonlat.transform(x, y) for x in (left, right) for y in (bottom, top)
    ]
    lons = [p[0] for p in corners]
    lats = [p[1] for p in corners]
    lon_min, lon_max = min(lons), max(lons)
    lat_min, lat_max = min(lats), max(lats)
    center_lat = (lat_min + lat_max) / 2.0
    dlat = RES_M / 110_574.0
    dlon = RES_M / (111_320.0 * max(math.cos(math.radians(center_lat)), 1e-6))
    geo_width = int(math.ceil((lon_max - lon_min) / dlon)) + 2
    geo_height = int(math.ceil((lat_max - lat_min) / dlat)) + 2
    geo_transform = from_origin(lon_min, lat_max + dlat, dlon, dlat)

    to_utm = Transformer.from_crs(CANONICAL_CRS, UTM_CRS, always_xy=True)
    rows, cols = _index_grid(geo_width, geo_height)
    lons_grid = geo_transform.c + (cols + 0.5) * geo_transform.a
    lats_grid = geo_transform.f + (rows + 0.5) * geo_transform.e
    # pyproj 向量化变换
    xs, ys = to_utm.transform(lons_grid, lats_grid)
    # 换算回"像元索引坐标"（像元中心为整数）
    col_index = (np.asarray(xs) - ORIGIN_X) / RES_M - 0.5
    row_index = (ORIGIN_TOP_Y - np.asarray(ys)) / RES_M - 0.5
    center_a = (5.0, 21.0)
    center_b = (5.0, 38.0)
    ra2 = (row_index - center_a[0]) ** 2 + (col_index - center_a[1]) ** 2
    rb2 = (row_index - center_b[0]) ** 2 + (col_index - center_b[1]) ** 2
    elevation = 100.0 + 0.1 * np.minimum(ra2, rb2)
    return elevation.astype(np.float64), geo_transform


# ---------------------------------------------------------------------------
# DEM 规格与场景规格
# ---------------------------------------------------------------------------

#: 需要生成的 DEM 文件（名字 -> 生成参数）。
DEM_SPECS: dict[str, dict[str, Any]] = {
    "two_bowl_ridge": {"kind": "two_bowl", "width": 60, "height": 12},
    "single_bowl": {"kind": "single_bowl", "width": 41, "height": 41},
    # 10 m 细网格单碗：用于受控简化测试（30 m 栅格化边界只有 45 个阶梯顶点，
    # 任何允许容差内都无法显著简化；10 m 边界有数百顶点，才能验证简化生效）
    "single_bowl_fine": {
        "kind": "single_bowl",
        "width": 121,
        "height": 121,
        "res_m": 10.0,
        "center": (60.0, 60.0),
    },
    "tilted_plane": {"kind": "tilted_plane", "width": 60, "height": 20},
    "plane_with_pit": {
        "kind": "plane_with_pit",
        "width": 60,
        "height": 20,
        "pit": (7, 20),
        "pit_depth": 15.0,
    },
    "two_bowl_nodata": {
        "kind": "two_bowl_nodata",
        "width": 60,
        "height": 12,
        "nodata_block": (4, 14, 6, 16),
    },
    "two_bowl_nodata_hole": {
        "kind": "two_bowl_nodata",
        "width": 60,
        "height": 12,
        "nodata_cells": [(5, 15)],
    },
    "crater_basin": {"kind": "crater", "width": 41, "height": 41},
    "valley_converge": {"kind": "valley_converge", "width": 60, "height": 20},
    # 南北向长条 DEM（600 m × 1800 m）：用于竖版分段图/窄图刻度测试
    "valley_converge_tall": {
        "kind": "valley_converge",
        "width": 20,
        "height": 60,
        "channel_col": 10.0,
    },
    "two_bowl_ridge_geo": {"kind": "two_bowl_geo", "width": 60, "height": 12},
}

#: 检查类 DEM（用于 inspect_dem 的异常路径）。
INSPECT_SPECS: dict[str, dict[str, Any]] = {
    "inspect_no_crs": {"kind": "constant", "value": 100.0, "crs": None, "size": 10},
    "inspect_constant": {"kind": "constant", "value": 100.0, "crs": UTM_CRS, "size": 10},
    "inspect_all_zero": {"kind": "constant", "value": 0.0, "crs": UTM_CRS, "size": 10},
    "inspect_out_of_range": {
        "kind": "constant",
        "value": 12000.0,
        "crs": UTM_CRS,
        "size": 10,
    },
    "inspect_high_nodata": {
        "kind": "nodata",
        "crs": UTM_CRS,
        "size": 20,
        "nodata_fraction": 0.6,
    },
    "inspect_geographic": {"kind": "geo", "size": 10},
}


def build_dem_arrays(out_dir: Path) -> dict[str, dict[str, Any]]:
    """生成所有 DEM 文件，返回名字 -> 元数据（宽高/CRS/分辨率/transform）。"""

    out_dir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, dict[str, Any]] = {}
    for name, spec in DEM_SPECS.items():
        kind = spec["kind"]
        width = int(spec["width"])
        height = int(spec["height"])
        res_m = float(spec.get("res_m", RES_M))
        transform = _metric_transform(width, height, res_m)
        nodata: float | None = None
        if kind == "two_bowl":
            elevation = two_bowl_elevation(width, height)
        elif kind == "two_bowl_nodata":
            elevation = two_bowl_elevation(width, height).copy()
            if "nodata_block" in spec:
                block = _nodata_block(elevation.shape, *spec["nodata_block"])
                elevation[block] = -9999.0
            for row, col in spec.get("nodata_cells", []):
                elevation[row, col] = -9999.0
            nodata = -9999.0
        elif kind == "single_bowl":
            elevation = single_bowl_elevation(
                width, height, center=spec.get("center", (20.0, 20.0))
            )
        elif kind == "tilted_plane":
            elevation = tilted_plane_elevation(width, height)
        elif kind == "plane_with_pit":
            elevation = plane_with_pit_elevation(
                width, height, pit=spec["pit"], pit_depth=spec["pit_depth"]
            )
        elif kind == "crater":
            elevation = crater_elevation(width, height)
        elif kind == "valley_converge":
            elevation = valley_converge_elevation(
                width, height, channel_col=float(spec.get("channel_col", 30.0))
            )
        elif kind == "two_bowl_geo":
            elevation, transform = two_bowl_geographic(width=width, height=height)
            width, height = elevation.shape[1], elevation.shape[0]
        else:  # pragma: no cover - 规格表内部错误
            raise ValueError(f"未知 DEM 类型 {kind!r}")

        path = out_dir / f"{name}.tif"
        if kind == "two_bowl_geo":
            write_dem(path, elevation, crs=CANONICAL_CRS, transform=transform)
            meta[name] = {
                "path": path.name,
                "width": width,
                "height": height,
                "crs": CANONICAL_CRS,
                "res_x": float(transform.a),
                "res_y": float(abs(transform.e)),
                "kind": kind,
            }
        else:
            write_dem(path, elevation, crs=UTM_CRS, transform=transform, nodata=nodata)
            meta[name] = {
                "path": path.name,
                "width": width,
                "height": height,
                "crs": UTM_CRS,
                "res_x": res_m,
                "res_y": res_m,
                "kind": kind,
            }
    return meta


def build_inspect_dems(out_dir: Path) -> dict[str, dict[str, Any]]:
    """生成用于 inspect_dem 的检查类 DEM。"""

    out_dir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, dict[str, Any]] = {}
    for name, spec in INSPECT_SPECS.items():
        size = int(spec["size"])
        crs = spec.get("crs")
        if spec["kind"] == "constant":
            array = np.full((size, size), float(spec["value"]), dtype=np.float64)
            transform = from_origin(ORIGIN_X, ORIGIN_TOP_Y, RES_M, RES_M)
            nodata = None
        elif spec["kind"] == "nodata":
            rows, cols = _index_grid(size, size)
            array = 100.0 + 0.1 * (rows**2 + cols**2)
            flat_count = int(array.size * float(spec["nodata_fraction"]))
            flat = array.ravel().copy()
            flat[:flat_count] = -9999.0
            array = flat.reshape(array.shape)
            transform = from_origin(ORIGIN_X, ORIGIN_TOP_Y, RES_M, RES_M)
            nodata = -9999.0
        elif spec["kind"] == "geo":
            rows, cols = _index_grid(size, size)
            array = 100.0 + 0.1 * (rows**2 + cols**2)
            transform = from_origin(116.0, 40.0, 0.0003, 0.0003)
            crs = CANONICAL_CRS
            nodata = None
        else:  # pragma: no cover
            raise ValueError(f"未知检查 DEM 类型 {spec['kind']!r}")
        path = out_dir / f"{name}.tif"
        write_dem(path, array, crs=crs, transform=transform, nodata=nodata)
        meta[name] = {"path": path.name, "crs": crs}
    return meta


#: 场景：每个场景 = 一个 DEM + 一个出口 + 解析期望。
SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "two_bowl_ridge",
        "dem": "two_bowl_ridge",
        "snap_radius_m": 0.0,
        "outlet_cell": (5, 21),
        "mask_rule": {"type": "columns_le", "value": 29},
        "expected_truncated": True,
        "expected_warnings_any": ["分析范围截断"],
        "expected_hole_count": 0,
        "note": (
            "两个同曲率抛物面碗；分水岭为碗心垂直平分线 x=29.5（像元中心为整数，"
            "无像元落在分水岭上）。A 碗心集水区精确 = 列0..29 全部像元 = 360。"
        ),
    },
    {
        "name": "two_bowl_ridge_outlet_b",
        "dem": "two_bowl_ridge",
        "snap_radius_m": 0.0,
        "outlet_cell": (5, 38),
        "mask_rule": {"type": "columns_ge", "value": 30},
        "expected_truncated": True,
        "expected_warnings_any": ["分析范围截断"],
        "expected_hole_count": 0,
        "note": "B 碗心集水区精确 = 列30..59 全部像元 = 360（对称性验证）。",
    },
    {
        "name": "two_bowl_ridge_ambiguous_snap",
        "dem": "two_bowl_ridge",
        "outlet_xy": (cell_center_metric(5, 29.5)[0], cell_center_metric(5, 29.5)[1]),
        "snap_radius_m": 600.0,
        "mask_rule": {"type": "two_bowl_half"},
        "expected_truncated": True,
        "expected_warnings_any": ["候选栅格", "出口已从输入点吸附"],
        "expected_ambiguous_snap": True,
        "note": (
            "出口放在分水岭上（两侧碗心汇流累积都是 360），半径 600 m 覆盖两碗心："
            "吸附应选最近/索引最小的 A 碗心并报告候选点不唯一。"
        ),
    },
    {
        "name": "single_bowl",
        "dem": "single_bowl",
        "snap_radius_m": 0.0,
        "outlet_cell": (20, 20),
        "mask_rule": {"type": "all"},
        "expected_truncated": True,
        "expected_warnings_any": ["分析范围截断"],
        "expected_hole_count": 0,
        "note": "单碗中心是唯一 sink，集水区 = 全部 41×41 = 1681 个像元。",
    },
    {
        "name": "single_bowl_fine",
        "dem": "single_bowl_fine",
        "snap_radius_m": 0.0,
        "outlet_cell": (60, 60),
        "mask_rule": {"type": "all"},
        "expected_truncated": True,
        "expected_warnings_any": ["分析范围截断"],
        "expected_hole_count": 0,
        "note": (
            "10 m 细网格单碗（121×121）：碗心唯一 sink，集水区 = 全部 14641 个像元，"
            "面积 1,464,100 m²。30 m 栅格化的边界只有 45 个阶梯顶点，无法在允许"
            "容差内简化；本场景用细网格边界（数百顶点）验证受控简化确实生效。"
        ),
    },
    {
        "name": "tilted_plane",
        "dem": "tilted_plane",
        "snap_radius_m": 0.0,
        "outlet_cell": (7, 59),
        "mask_rule": {"type": "row_eq", "value": 7},
        "expected_truncated": True,
        "expected_warnings_any": ["分析范围截断"],
        "expected_hole_count": 0,
        "note": "严格东向平面：出口(7,59)上游精确 = 行7 的 60 个像元（细长条）。",
    },
    {
        "name": "tilted_plane_snap_offset",
        "dem": "tilted_plane",
        "outlet_xy": (
            cell_center_metric(7, 59)[0] - 20.0,
            cell_center_metric(7, 59)[1],
        ),
        "snap_radius_m": 90.0,
        "mask_rule": {"type": "row_eq", "value": 7},
        "expected_truncated": True,
        "expected_warnings_any": ["出口已从输入点吸附"],
        "expected_snap_distance_m": 20.0,
        "expected_flow_accum_cells": 60,
        "expected_hole_count": 0,
        "note": (
            "出口西偏 20 m：应吸附到 (7,59)，吸附距离精确 20 m，累积 60 像元，"
            "掩膜仍为行7（吸附报告验证）。"
        ),
    },
    {
        "name": "plane_with_pit_downstream",
        "dem": "plane_with_pit",
        "outlet_cell": (7, 59),
        "snap_radius_m": 0.0,
        "mask_rule": {"type": "row_columns_range", "row": 7, "min": 22, "max": 59},
        "expected_truncated": True,
        "expected_warnings_any": ["分析范围截断"],
        "expected_hole_count": 0,
        "expected_flow_accum_cells": 38,
        "note": (
            "不填洼：深洼地(7,20)会截留行6/7/8 列0..21 的来水；下游出口(7,59)"
            "的上游精确 = 行7 列22..59 = 38 个像元。吸附半径取 5 m，避免吸附到"
            "相邻行的更大累积像元（那正是现实中的误吸附风险，另由吸附测试覆盖）。"
        ),
    },
    {
        "name": "plane_with_pit_outlet",
        "dem": "plane_with_pit",
        "outlet_cell": (7, 20),
        "snap_radius_m": 0.0,
        "mask_rule": {"type": "rows_columns_le", "rows": [6, 7, 8], "value": 21},
        "expected_truncated": True,
        "expected_warnings_any": ["没有出流方向", "分析范围截断"],
        "expected_hole_count": 0,
        "expected_flow_accum_cells": 66,
        "note": (
            "出口就是洼地：深洼地对相邻行也有吸引力，集水区精确 = 行6/7/8 × "
            "列0..21 = 66 个像元，并应警告出口无出流（不填洼的直接后果）。"
        ),
    },
    {
        "name": "two_bowl_nodata_wall",
        "dem": "two_bowl_nodata",
        "snap_radius_m": 0.0,
        "outlet_cell": (5, 21),
        "mask_rule": {
            "type": "columns_le",
            "value": 29,
            "exclude_block": (4, 14, 6, 16),
            "exclude_strips": [[5, 0, 13]],
        },
        "expected_truncated": True,
        "expected_warnings_any": ["NoData", "分析范围截断"],
        "expected_hole_count": 0,
        "note": (
            "A 碗内部 3×3 NoData 墙：NoData 阻断汇流，(5,13) 失去所有更低邻居成为"
            "局部洼地，行5 列0..13 的来水在此终止（解析推导）。掩膜 = 列0..29 "
            "去掉 NoData 块与行5 列0..13 = 360−9−14 = 337 个像元；被排除区域"
            "经行5 与图幅西边界连通，因此不是洞。"
        ),
    },
    {
        "name": "two_bowl_nodata_hole",
        "dem": "two_bowl_nodata_hole",
        "snap_radius_m": 0.0,
        "outlet_cell": (5, 21),
        "mask_rule": {"type": "columns_le", "value": 29, "exclude_cells": [[5, 15]]},
        "expected_truncated": True,
        "expected_warnings_any": ["NoData", "分析范围截断"],
        "expected_hole_count": 1,
        "note": (
            "A 碗内部单个 NoData 像元 (5,15)：四周像元都能绕行到碗心，掩膜 = "
            "列0..29 去掉该像元 = 359，且该像元被掩膜包围形成 1 个洞。"
        ),
    },
    {
        "name": "valley_converge",
        "dem": "valley_converge",
        "snap_radius_m": 0.0,
        "outlet_cell": (19, 30),
        "mask_rule": {"type": "all"},
        "expected_truncated": True,
        "expected_warnings_any": ["分析范围截断"],
        "expected_hole_count": 0,
        "note": (
            "南向倾斜 + 向中央河道收敛的 V 形谷，无并列坡度：全图 60×20=1200 "
            "个像元都汇入南边界河道出口 (19,30)。用于 GRASS SFD 与 native D8 "
            "的二维交叉验证（两者都应精确复现 1200 像元 / 1,080,000 m²）。"
        ),
    },
    {
        "name": "valley_converge_tall",
        "dem": "valley_converge_tall",
        "snap_radius_m": 0.0,
        "outlet_cell": (59, 10),
        "mask_rule": {"type": "all"},
        "expected_truncated": True,
        "expected_warnings_any": ["分析范围截断"],
        "expected_hole_count": 0,
        "note": (
            "南北向长条收敛谷（20×60 像元 = 600 m×1800 m），无并列坡度：全部 "
            "1200 个像元汇入南边界河道出口 (59,10)。用于复核图竖版分段布局与"
            "窄图 x 轴刻度防粘连测试。"
        ),
    },
    {
        "name": "crater_basin",
        "dem": "crater_basin",
        "snap_radius_m": 0.0,
        "outlet_cell": (20, 20),
        "mask_rule": {"type": "radius_bounds", "center": (20, 20), "r_min": 9.0, "r_max": 11.0},
        "expected_truncated": False,
        "expected_warnings_any": [],
        "expected_no_truncation_warning": True,
        "expected_hole_count": 0,
        "note": (
            "环形山内部闭合盆地：中心集水区不接触分析范围边界（无截断）；"
            "离散 D8 在环脊附近有方向歧义，故掩膜只要求落在半径 9–11 像元包络内。"
        ),
    },
    {
        "name": "two_bowl_ridge_geo",
        "dem": "two_bowl_ridge_geo",
        "snap_radius_m": 0.0,
        "outlet_cell": (5, 21),
        "mask_rule": {"type": "expect_cells", "value": 360},
        "tolerance": {"mask_cells": 12, "area_rel": 0.03},
        "expected_truncated": True,
        "expected_warnings_any": ["分析范围截断", "重投影"],
        "expected_hole_count": 0,
        "note": (
            "同一地形写成 EPSG:4490 地理坐标：验证地理 DEM → UTM 重投影路径；"
            "允许 ±12 像元 / ±3% 面积误差（重采样与格网错位）。"
        ),
    },
]


def _mask_from_rule(
    rule: dict[str, Any], width: int, height: int
) -> np.ndarray | None:
    """按解析规则生成期望掩膜（返回 None 表示只约束像元数范围）。"""

    rows, cols = _index_grid(width, height)
    kind = rule["type"]
    if kind == "all":
        return np.ones((height, width), dtype=bool)
    if kind == "columns_le":
        mask = cols <= float(rule["value"])
    elif kind == "columns_ge":
        mask = cols >= float(rule["value"])
    elif kind == "columns_range":
        mask = (cols >= float(rule["min"])) & (cols <= float(rule["max"]))
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
    elif kind == "two_bowl_half":
        return None
    elif kind == "expect_cells":
        return None
    elif kind == "radius_bounds":
        center_r, center_c = rule["center"]
        radius = np.hypot(rows - center_r, cols - center_c)
        return None  # 由测试按上下界分别断言
    else:  # pragma: no cover
        raise ValueError(f"未知掩膜规则 {kind!r}")
    block = rule.get("exclude_block")
    if block:
        r0, c0, r1, c1 = block
        mask = mask & ~_nodata_block((height, width), r0, c0, r1, c1)
    for row, col_min, col_max in rule.get("exclude_strips", []):
        mask = mask & ~((rows == float(row)) & (cols >= col_min) & (cols <= col_max))
    for row, col in rule.get("exclude_cells", []):
        mask = mask & ~((rows == float(row)) & (cols == float(col)))
    return mask


def build_expected(
    dem_meta: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """生成 ``expected.json``（解析真值 fixture）。"""

    scenarios: dict[str, Any] = {}
    for spec in SCENARIOS:
        dem_name = spec["dem"]
        meta = dem_meta[dem_name]
        width, height = int(meta["width"]), int(meta["height"])
        entry: dict[str, Any] = {
            "dem": meta["path"],
            "dem_crs": meta["crs"],
            "width": width,
            "height": height,
            "note": spec["note"],
            "mask_rule": spec["mask_rule"],
        }
        dem_res = (
            float(meta.get("res_x", RES_M))
            if meta["crs"] == UTM_CRS
            else RES_M  # 地理 DEM 的像元尺寸是「度」，解析地形仍定义在 30 m UTM 网格上
        )
        if "outlet_cell" in spec:
            row, col = spec["outlet_cell"]
            entry["outlet_cell"] = [row, col]
            x, y = cell_center_metric(row, col, dem_res)
            lon, lat = xy_to_lonlat(x, y)
            entry["outlet_xy"] = [x, y]
            entry["outlet_lonlat"] = [lon, lat]
        else:
            entry["outlet_xy"] = list(spec["outlet_xy"])
            entry["outlet_lonlat"] = list(xy_to_lonlat(*spec["outlet_xy"]))
        entry["snap_radius_m"] = float(spec.get("snap_radius_m", 0.0))
        entry["expected_snap_disabled"] = entry["snap_radius_m"] == 0.0
        rule = spec["mask_rule"]
        if rule["type"] == "expect_cells":
            entry["expected_mask_cells"] = int(rule["value"])
        elif rule["type"] == "two_bowl_half":
            entry["expected_mask_cells"] = (width // 2) * height
        elif rule["type"] == "radius_bounds":
            rows, cols = _index_grid(width, height)
            center_r, center_c = rule["center"]
            radius = np.hypot(rows - center_r, cols - center_c)
            entry["expected_mask_min_cells"] = int(
                np.count_nonzero(radius <= float(rule["r_min"]))
            )
            entry["expected_mask_max_cells"] = int(
                np.count_nonzero(radius <= float(rule["r_max"]))
            )
            entry["expected_mask_cells"] = None
        else:
            mask = _mask_from_rule(rule, width, height)
            assert mask is not None
            entry["expected_mask_cells"] = int(np.count_nonzero(mask))
        if meta["crs"] == UTM_CRS:
            dem_cell_area = float(meta["res_x"]) * float(meta["res_y"])
            entry["cell_area_m2"] = dem_cell_area
        else:
            dem_cell_area = None
            entry["cell_area_m2"] = None
        if entry.get("expected_mask_cells") is not None:
            entry["expected_area_m2"] = (
                float(entry["expected_mask_cells"]) * dem_cell_area
                if dem_cell_area is not None
                else None
            )
        entry["expected_truncated"] = bool(spec.get("expected_truncated", False))
        entry["expected_warnings_any"] = list(spec.get("expected_warnings_any", []))
        entry["expected_no_truncation_warning"] = bool(
            spec.get("expected_no_truncation_warning", False)
        )
        entry["expected_hole_count"] = spec.get("expected_hole_count")
        entry["expected_ambiguous_snap"] = bool(
            spec.get("expected_ambiguous_snap", False)
        )
        if "expected_snap_distance_m" in spec:
            entry["expected_snap_distance_m"] = spec["expected_snap_distance_m"]
        if "expected_flow_accum_cells" in spec:
            entry["expected_flow_accum_cells"] = spec["expected_flow_accum_cells"]
        if "tolerance" in spec:
            entry["tolerance"] = spec["tolerance"]
        scenarios[spec["name"]] = entry
    return {
        "generated_by": "scripts/make_synthetic_dem.py",
        "description": (
            "解析真值合成 DEM fixture。掩膜像元集合与面积由地形公式推导，"
            "不是由被测实现生成；面积 = 像元数 × 900 m²（EPSG:32650，30 m）。"
        ),
        "crs": UTM_CRS,
        "canonical_crs": CANONICAL_CRS,
        "res_m": RES_M,
        "cell_area_m2": CELL_AREA_M2,
        "origin_xy": [ORIGIN_X, ORIGIN_TOP_Y],
        "scenarios": scenarios,
    }


def generate(out_dir: str | Path) -> dict[str, Any]:
    """生成全部 fixture 文件与 ``expected.json``，返回 expected 字典。"""

    out_dir = Path(out_dir)
    dem_meta = build_dem_arrays(out_dir)
    build_inspect_dems(out_dir)
    expected = build_expected(dem_meta)
    (out_dir / "expected.json").write_text(
        json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return expected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out-dir",
        default=str(Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "synthetic"),
        help="fixture 输出目录（默认 tests/fixtures/synthetic）",
    )
    args = parser.parse_args(argv)
    expected = generate(args.out_dir)
    print(f"已生成 {len(expected['scenarios'])} 个解析真值场景到 {args.out_dir}")
    for name, entry in expected["scenarios"].items():
        cells = entry.get("expected_mask_cells")
        if cells is None:
            cells = (
                f"{entry['expected_mask_min_cells']}–"
                f"{entry['expected_mask_max_cells']}（包络）"
            )
        print(f"  - {name}: 期望掩膜 {cells} 像元")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
