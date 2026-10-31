"""CRS 与栅格网格工具（GIS 包内部共用）。

职责：

- 校验 DEM 的 CRS，并选定/校验**米制分析 CRS**；
- 把 DEM 重投影（必要时重采样）到统一的米制分析网格。native 与 GRASS
  两种引擎共用这一函数，保证分析网格一致，便于交叉校验与复核图对齐；
- 提供像元中心、行列号、最近邻采样等纯网格小工具。

坐标契约（docs/architecture.md §3）：对外规范坐标是 ``EPSG:4490`` 的
``(lon, lat)``；面积、距离、吸附半径一律在米制 CRS 下计算。本模块**不**
做任何坐标语义转换（GCJ-02 等），那属于 :mod:`ovital_mcp.coordinates`。
"""

from __future__ import annotations

import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.warp import calculate_default_transform, reproject, transform_bounds

from ..coordinates import CANONICAL_CRS, metric_crs_for, transform_geometry
from ..errors import DataValidationError, EngineError

#: 支持的重采样方式（DEM 默认双线性；NoData/分类栅格用 nearest）。
RESAMPLING: dict[str, Resampling] = {
    "bilinear": Resampling.bilinear,
    "nearest": Resampling.nearest,
    "cubic": Resampling.cubic,
}

#: 没有 NoData 声明时写 GeoTIFF 用的兜底 NoData 值。
FALLBACK_NODATA = -9999.0


def crs_from_user_input(crs: Any, *, what: str = "CRS") -> CRS:
    """把用户输入的 CRS 解析成 :class:`pyproj.CRS`，失败时给明确错误。"""

    if crs is None:
        raise DataValidationError(f"{what} 缺失")
    try:
        return CRS.from_user_input(crs)
    except Exception as exc:  # noqa: BLE001 - pyproj 抛的异常类型不稳定
        raise DataValidationError(f"无法解析{what} {crs!r}: {exc}") from exc


def require_metric_crs(crs: Any, *, what: str = "分析 CRS") -> CRS:
    """要求给定 CRS 是**投影**坐标系（米制），否则显式报错。"""

    parsed = crs_from_user_input(crs, what=what)
    if parsed.is_geographic:
        raise DataValidationError(
            f"{what} {crs} 是地理坐标系，不能用于面积/距离计算；"
            "请传入米制投影 CRS（例如 EPSG:32650），或留空由出口自动选择 UTM 分带"
        )
    return parsed


def resolve_metric_crs(
    lon: float,
    lat: float,
    explicit: str | None = None,
) -> str:
    """确定分析用的米制 CRS 字符串。

    ``explicit`` 为空时按出口经纬度自动选择 UTM 分带
    （复用 :func:`ovital_mcp.coordinates.metric_crs_for`，不另写一套）。
    """

    if explicit:
        require_metric_crs(explicit)
        return str(explicit)
    return metric_crs_for(lon, lat, kind="utm")


def transform_lonlat_to_crs(lon: float, lat: float, dst_crs: Any) -> tuple[float, float]:
    """EPSG:4490 ``(lon, lat)`` -> 目标 CRS ``(x, y)``（始终 ``always_xy``）。"""

    transformer = Transformer.from_crs(CANONICAL_CRS, dst_crs, always_xy=True)
    x, y = transformer.transform(lon, lat)
    return float(x), float(y)


def transform_xy_to_lonlat(x: float, y: float, src_crs: Any) -> tuple[float, float]:
    """目标 CRS ``(x, y)`` -> EPSG:4490 ``(lon, lat)``（始终 ``always_xy``）。"""

    transformer = Transformer.from_crs(src_crs, CANONICAL_CRS, always_xy=True)
    lon, lat = transformer.transform(x, y)
    return float(lon), float(lat)


def transform_lines_batch(
    lines: list[Any], src_crs: Any, dst_crs: Any
) -> list[Any]:
    """批量转换折线坐标（一次 pyproj 数组变换，避免逐要素新建 Transformer）。

    河网可能有成千上万条折线；逐条调用 ``transform_geometry`` 会重复建
    Transformer 并产生大量小对象。这里把所有顶点拼成一个数组一次性变换。
    """

    from shapely.geometry import LineString

    items = list(lines)
    if not items:
        return []
    transformer = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    arrays = [np.asarray(line.coords, dtype=np.float64) for line in items]
    lengths = [int(arr.shape[0]) for arr in arrays]
    all_coords = np.concatenate(arrays, axis=0)
    xs, ys = transformer.transform(all_coords[:, 0], all_coords[:, 1])
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    result: list[Any] = []
    offset = 0
    for count in lengths:
        segment = np.stack(
            [xs[offset : offset + count], ys[offset : offset + count]], axis=1
        )
        result.append(LineString(segment))
        offset += count
    return result


def bounds_to_lonlat(
    bounds: tuple[float, float, float, float],
    src_crs: Any,
) -> tuple[float, float, float, float]:
    """把 ``(minx, miny, maxx, maxy)`` 从源 CRS 转到 EPSG:4490。"""

    minx, miny, maxx, maxy = transform_bounds(
        src_crs, CANONICAL_CRS, *bounds, densify_pts=21
    )
    return (float(minx), float(miny), float(maxx), float(maxy))


def _align_outward(value: float, res: float, *, up: bool) -> float:
    """把坐标对齐到 ``res`` 的整数倍：``up=True`` 向上取整，否则向下。"""

    scaled = value / res
    aligned = math.ceil(scaled) if up else math.floor(scaled)
    return aligned * res


def _suggest_target_res(area_m2: float, max_cells: int) -> float:
    """当分析栅格超限时，给出一个「好看」的建议分辨率（米）。"""

    raw = math.sqrt(max(area_m2, 1.0) / max(max_cells, 1))
    # 取 1/2/5×10^k 的向上整值，便于用户理解
    exp = math.floor(math.log10(max(raw, 1e-6)))
    base = 10.0**exp
    for step in (1.0, 2.0, 5.0, 10.0):
        candidate = step * base
        if candidate >= raw:
            return candidate
    return 10.0 * base


@dataclass
class AnalysisRaster:
    """统一分析网格上的 DEM（米制 CRS）。

    ``array`` 是 ``float64`` 二维数组，NoData 位置为 ``nan``；``valid_mask``
    是同一形状的布尔数组。数组为 ``None`` 表示调用方只要文件、不要内存
    数据（GRASS 引擎走这条路径以省内存）。
    """

    path: str
    crs: str
    width: int
    height: int
    res_x: float
    res_y: float
    transform: Affine
    nodata: float | None
    array: np.ndarray | None = None
    valid_mask: np.ndarray | None = None
    reprojected: bool = False
    resampling: str = "none"
    source_path: str | None = None
    source_crs: str | None = None

    # -- 基本属性 -----------------------------------------------------
    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)

    @property
    def cell_area_m2(self) -> float:
        """单个像元的水平面积（平方米，按分析网格分辨率的绝对值）。"""

        return abs(float(self.res_x) * float(self.res_y))

    def bounds(self) -> tuple[float, float, float, float]:
        left = self.transform.c
        top = self.transform.f
        return (
            float(left),
            float(top + self.transform.e * self.height),
            float(left + self.transform.a * self.width),
            float(top),
        )

    def bounds_lonlat(self) -> tuple[float, float, float, float]:
        return bounds_to_lonlat(self.bounds(), self.crs)

    def cell_center(self, row: int, col: int) -> tuple[float, float]:
        """像元中心在分析 CRS 下的 ``(x, y)``。"""

        x, y = self.transform * (float(col) + 0.5, float(row) + 0.5)
        return float(x), float(y)

    def rowcol_of_xy(self, x: float, y: float) -> tuple[float, float]:
        """分析 CRS 坐标 -> 连续行列号（``floor`` 即像元索引）。"""

        return xy_to_rowcol(self.transform, x, y)

    def cell_of_xy(self, x: float, y: float) -> tuple[int, int] | None:
        """分析 CRS 坐标 -> 像元索引；越界返回 ``None``。

        允许半个像元的容差：落在栅格最外缘上的点按最近像元处理（边界顶点
        常常正好落在 DEM 外缘，不应被当成"越界"）。
        """

        row_f, col_f = xy_to_rowcol(self.transform, x, y)
        if row_f < -0.5 or row_f > self.height + 0.5:
            return None
        if col_f < -0.5 or col_f > self.width + 0.5:
            return None
        row = min(max(int(math.floor(row_f)), 0), self.height - 1)
        col = min(max(int(math.floor(col_f)), 0), self.width - 1)
        return row, col

    def xy_of_cell(self, row: int, col: int) -> tuple[float, float]:
        return self.cell_center(row, col)

    def ensure_array(self) -> np.ndarray:
        """惰性读取 DEM 数组（``float64``，NoData 为 ``nan``）。"""

        if self.array is None:
            with rasterio.open(self.path) as ds:
                band = ds.read(1).astype(np.float64)
                nodata = ds.nodata
            if nodata is not None and not math.isnan(nodata):
                band[band == nodata] = np.nan
            band[~np.isfinite(band)] = np.nan
            self.array = band
            self.valid_mask = np.isfinite(band)
        return self.array

    def ensure_valid_mask(self) -> np.ndarray:
        if self.valid_mask is None:
            self.ensure_array()
        assert self.valid_mask is not None
        return self.valid_mask

    def sample_nearest(self, x: float, y: float) -> float:
        """最近邻采样；越界/NoData 返回 ``nan``。"""

        cell = self.cell_of_xy(x, y)
        if cell is None:
            return math.nan
        array = self.ensure_array()
        value = float(array[cell])
        return value if math.isfinite(value) else math.nan


def xy_to_rowcol(transform: Affine, x: float, y: float) -> tuple[float, float]:
    """世界坐标 -> 连续行列号（显式逆仿射，避免 ``~transform * tuple`` 的
    numpy matmul 弃用告警，也避免多余对象创建）。返回 ``(row_f, col_f)``。"""

    inverse = ~transform
    col_f = inverse.a * float(x) + inverse.b * float(y) + inverse.c
    row_f = inverse.d * float(x) + inverse.e * float(y) + inverse.f
    return float(row_f), float(col_f)


def _check_rotation(transform: Affine, path: str) -> None:
    """本实现只支持 north-up 栅格（无旋转项）。"""

    if abs(transform.b) > 1e-12 or abs(transform.d) > 1e-12:
        raise EngineError(
            f"DEM {path} 的仿射变换含旋转项（b={transform.b}, d={transform.d}），"
            "当前 D8 实现不支持旋转栅格；请先重采样为 north-up 栅格"
        )


def prepare_analysis_dem(
    src_path: str | Path,
    dst_path: str | Path,
    *,
    metric_crs: str,
    target_res_m: float | None = None,
    max_cells: int | None = None,
    resampling: str = "bilinear",
    read_array: bool = True,
) -> AnalysisRaster:
    """把 DEM 准备成统一米制分析网格（必要时重投影/重采样）。

    规则：

    - 源 CRS 已经是目标米制 CRS 且分辨率一致、未要求重采样时，**直接复制
      文件**，不重采样——这样合成 DEM 的像元值保持逐位一致，解析真值测试
      才有意义；
    - 否则用 ``rasterio.warp.reproject`` 重投影到目标 CRS，并把分析范围向
      外对齐到目标分辨率的整数倍（GRASS ``g.region -a`` 因此不会移动网格）；
    - 超过 ``max_cells`` 时显式报错并给出建议分辨率，绝不静默降采样。
    """

    src_path = Path(src_path)
    dst_path = Path(dst_path)
    if not src_path.is_file():
        raise DataValidationError(f"DEM 文件不存在：{src_path}")
    if resampling not in RESAMPLING:
        raise DataValidationError(
            f"未知重采样方式 {resampling!r}，允许 {sorted(RESAMPLING)}"
        )
    dst_crs = require_metric_crs(metric_crs)

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(src_path) as src:
        if src.crs is None:
            raise DataValidationError(
                f"DEM {src_path} 缺少 CRS，无法确定分析投影；"
                "请提供带坐标系统的 GeoTIFF（或先用 gdalwarp 指定 CRS）"
            )
        src_crs = CRS.from_user_input(src.crs)
        _check_rotation(src.transform, str(src_path))
        src_res_x = abs(float(src.transform.a))
        src_res_y = abs(float(src.transform.e))

        same_crs = src_crs == dst_crs
        same_res = target_res_m is None or (
            abs(src_res_x - float(target_res_m)) <= 1e-9
            and abs(src_res_y - float(target_res_m)) <= 1e-9
        )

        if same_crs and same_res:
            # 恒等路径：不重采样，保持像元值逐位一致。
            if src_path.resolve() != dst_path.resolve():
                shutil.copyfile(src_path, dst_path)
            analysis = AnalysisRaster(
                path=str(dst_path),
                crs=str(src_crs.to_string()),
                width=src.width,
                height=src.height,
                res_x=src_res_x,
                res_y=src_res_y,
                transform=src.transform,
                nodata=src.nodata,
                reprojected=False,
                resampling="none",
                source_path=str(src_path),
                source_crs=str(src_crs.to_string()),
            )
        else:
            if same_crs:
                left, bottom, right, top = (
                    float(src.bounds.left),
                    float(src.bounds.bottom),
                    float(src.bounds.right),
                    float(src.bounds.top),
                )
                if target_res_m is not None:
                    res_x = res_y = float(target_res_m)
                else:  # pragma: no cover - same_crs 且 same_res 已走恒等路径
                    res_x, res_y = src_res_x, src_res_y
            else:
                dst_transform, dst_w, dst_h = calculate_default_transform(
                    src_crs,
                    dst_crs,
                    src.width,
                    src.height,
                    *src.bounds,
                )
                left, bottom, right, top = transform_bounds(
                    src_crs, dst_crs, *src.bounds, densify_pts=21
                )
                if target_res_m is not None:
                    res_x = res_y = float(target_res_m)
                else:
                    res_x = abs(float(dst_transform.a))
                    res_y = abs(float(dst_transform.e))
                    # 用 GDAL 建议的格网尺寸，避免重投影后范围抖动
                    if dst_w > 0 and dst_h > 0:
                        res_x = (right - left) / dst_w
                        res_y = (top - bottom) / dst_h

            res = max(res_x, 1e-6)
            left = _align_outward(left, res, up=False)
            bottom = _align_outward(bottom, res, up=False)
            right = _align_outward(right, res, up=True)
            top = _align_outward(top, res, up=True)
            width = int(round((right - left) / res))
            height = int(round((top - bottom) / res))
            if width < 1 or height < 1:
                raise EngineError(
                    f"重投影后分析栅格为空（width={width}, height={height}）"
                )
            dst_transform = Affine(res, 0.0, left, 0.0, -res, top)

            if max_cells is not None and width * height > int(max_cells):
                area = float((right - left) * (top - bottom))
                suggestion = _suggest_target_res(area, int(max_cells))
                raise DataValidationError(
                    f"分析栅格 {width}×{height}={width * height} 个像元，"
                    f"超过上限 max_cells={max_cells}。请设置 target_res_m≥{suggestion:g} "
                    "以降低分辨率，或改用覆盖范围更小的 DEM",
                    details={
                        "width": width,
                        "height": height,
                        "cells": width * height,
                        "max_cells": int(max_cells),
                        "suggested_target_res_m": suggestion,
                    },
                )

            src_nodata = src.nodata
            if src_nodata is not None and isinstance(src_nodata, float) and math.isnan(
                src_nodata
            ):
                dst_nodata: float = math.nan
            elif src_nodata is not None:
                dst_nodata = float(src_nodata)
            else:
                dst_nodata = FALLBACK_NODATA

            profile = {
                "driver": "GTiff",
                "dtype": "float32",
                "count": 1,
                "width": width,
                "height": height,
                "crs": dst_crs,
                "transform": dst_transform,
                "nodata": dst_nodata,
            }
            with rasterio.open(dst_path, "w", **profile) as dst:
                reproject(
                    source=rasterio.band(src, 1),
                    destination=rasterio.band(dst, 1),
                    src_transform=src.transform,
                    src_crs=src_crs,
                    src_nodata=src.nodata,
                    dst_transform=dst_transform,
                    dst_crs=dst_crs,
                    dst_nodata=dst_nodata,
                    resampling=RESAMPLING[resampling],
                )
            analysis = AnalysisRaster(
                path=str(dst_path),
                crs=str(dst_crs.to_string()),
                width=width,
                height=height,
                res_x=res,
                res_y=res,
                transform=dst_transform,
                nodata=dst_nodata,
                reprojected=True,
                resampling=resampling,
                source_path=str(src_path),
                source_crs=str(src_crs.to_string()),
            )

    if read_array:
        analysis.ensure_array()
        if analysis.valid_mask is None:
            analysis.valid_mask = np.isfinite(analysis.ensure_array())
    return analysis


def sample_nearest(
    array: np.ndarray,
    valid_mask: np.ndarray,
    transform: Affine,
    x: float,
    y: float,
) -> float:
    """在给定网格上按最近邻采样；越界/无效返回 ``nan``。"""

    row_f, col_f = xy_to_rowcol(transform, x, y)
    if row_f < -0.5 or row_f > array.shape[0] + 0.5:
        return math.nan
    if col_f < -0.5 or col_f > array.shape[1] + 0.5:
        return math.nan
    row = min(max(int(math.floor(row_f)), 0), array.shape[0] - 1)
    col = min(max(int(math.floor(col_f)), 0), array.shape[1] - 1)
    if not bool(valid_mask[row, col]):
        return math.nan
    value = float(array[row, col])
    return value if math.isfinite(value) else math.nan
