"""DEM 检查与元数据提取。

对应契约 ``docs/architecture.md`` §5.1。本模块只做**只读检查**，不改 DEM、
不填洼、不裁剪；任何异常都写进 ``DemInfo.warnings`` 并尽可能保留元数据，
让上层决定是否继续（``provisional`` 表示"数据可用性存疑，只能临时使用"）。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import rasterio
from pyproj import CRS, Transformer
from rasterio.windows import Window

from ..coordinates import CANONICAL_CRS, crs_axis_order
from ..errors import DataValidationError
from .crs import bounds_to_lonlat, transform_lonlat_to_crs, xy_to_rowcol

#: 高于该值的高程视为量程异常（地球陆地最高约 8849 m）。
MAX_PLAUSIBLE_ELEV_M = 9000.0
#: 低于该值的高程视为量程异常（死海约 -430 m，留出余量）。
MIN_PLAUSIBLE_ELEV_M = -500.0
#: 采样统计时每次读取的行数（控制大文件内存）。
_STATS_BLOCK_ROWS = 1024


@dataclass
class DemInfo:
    """DEM 检查结果（契约 §5.1 字段 + 若干只增不改的补充字段）。"""

    path: str
    sha256: str
    crs_wkt: str | None
    crs_epsg: int | None
    is_geographic: bool
    width: int
    height: int
    res_x: float
    res_y: float
    nodata: float | None
    min_elev: float | None
    max_elev: float | None
    nodata_fraction: float
    extent_lonlat: tuple[float, float, float, float] | None
    vertical_datum_known: bool
    vertical_datum_note: str
    warnings: list[str] = field(default_factory=list)
    provisional: bool = False
    # -- 补充字段（只增不改，不改变契约既有字段含义）------------------
    crs_label: str | None = None
    dtype: str | None = None
    res_approx_m: tuple[float, float] | None = None
    vertical_datum: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """分块计算文件 SHA256（大文件不占内存）。"""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _windowed_stats(
    ds: rasterio.DatasetReader,
) -> tuple[float | None, float | None, float, int, bool]:
    """分块统计有效高程的最小/最大/NoData 比例。

    返回 ``(min, max, nodata_fraction, nodata_count, has_nan)``。NoData 既
    包括数据集声明的 ``nodata`` 值，也包括浮点 ``NaN``。
    """

    nodata = ds.nodata
    nodata_count = 0
    valid_count = 0
    total = 0
    min_elev = math.inf
    max_elev = -math.inf
    has_nan = False
    for row_off in range(0, ds.height, _STATS_BLOCK_ROWS):
        rows = min(_STATS_BLOCK_ROWS, ds.height - row_off)
        block = ds.read(1, window=Window(0, row_off, ds.width, rows)).astype(
            np.float64, copy=False
        )
        total += block.size
        nan_mask = np.isnan(block)
        if bool(nan_mask.any()):
            has_nan = True
        invalid = nan_mask.copy()
        if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
            invalid |= block == nodata
        nodata_count += int(invalid.sum())
        good = block[~invalid]
        if good.size:
            valid_count += int(good.size)
            block_min = float(good.min())
            block_max = float(good.max())
            if block_min < min_elev:
                min_elev = block_min
            if block_max > max_elev:
                max_elev = block_max
    if valid_count == 0:
        return None, None, 1.0 if total else 0.0, nodata_count, has_nan
    return (
        min_elev,
        max_elev,
        nodata_count / total if total else 0.0,
        nodata_count,
        has_nan,
    )


def _vertical_datum_note(
    ds: rasterio.DatasetReader, crs: CRS | None
) -> tuple[bool, str, str | None]:
    """尽力判断垂直基准；GeoTIFF 普遍不声明，未知时要明确警告。"""

    # 1) 复合 CRS（水平 + 垂直）可提供垂直基准
    if crs is not None:
        try:
            is_compound = bool(getattr(crs, "is_compound", False))
        except Exception:  # noqa: BLE001 - 不同 pyproj 版本属性行为不一致
            is_compound = False
        if is_compound:
            sub = [c for c in crs.sub_crs_list if c.is_vertical]
            if sub:
                name = sub[0].name
                return True, f"文件声明了复合 CRS，垂直基准：{name}", name

    # 2) 常见 GeoTIFF/元数据标签
    tags: dict[str, str] = {}
    try:
        tags.update(ds.tags())
        tags.update(ds.tags(1))
    except Exception:  # noqa: BLE001
        pass
    for key in ("VERTICAL_DATUM", "VERT_DATUM", "vertical_datum", "GEO_VERTICAL_CS"):
        value = tags.get(key)
        if value:
            return True, f"文件标签 {key}={value}", value

    # 3) 无法判断：明确说明「未知」，不做任何假设
    note = (
        "文件未声明垂直基准，无法判断高程是大地高（椭球高）还是正高/正常高；"
        "同一区域内混用不同基准会带来米级到数十米级系统偏差。"
        "Copernicus GLO-30 官方为 EGM2008 正高，但 GeoTIFF 本身不声明，需按数据源文档另行确认。"
    )
    return False, note, None


def inspect_dem(path: str | Path) -> DemInfo:
    """检查 DEM 文件并返回完整元数据 + 警告。

    覆盖契约要求的检查项：CRS 缺失、地理/投影、轴序、NoData、量程异常、
    分辨率、覆盖范围、高程基准未知（DSM/DTM 无法从文件判断同样警告）、
    文件 SHA256。检查过程只读，不修改源文件。
    """

    path = Path(path)
    if not path.is_file():
        raise DataValidationError(f"DEM 文件不存在：{path}")

    warnings: list[str] = []
    provisional = False

    with rasterio.open(path) as ds:
        if ds.count < 1:
            raise DataValidationError(f"DEM {path} 不含任何波段")
        width, height = ds.width, ds.height
        transform = ds.transform
        res_x = abs(float(transform.a))
        res_y = abs(float(transform.e))
        nodata = ds.nodata
        dtype = str(ds.dtypes[0])
        crs_wkt: str | None = None
        crs_epsg: int | None = None
        crs_label: str | None = None
        crs: CRS | None = None
        is_geographic = False

        if ds.crs is None:
            warnings.append(
                "DEM 缺少 CRS：无法确定坐标语义，不能用于工程计算；"
                "必须先确认坐标系并写入 CRS（provisional=true）"
            )
            provisional = True
        else:
            crs = CRS.from_user_input(ds.crs)
            crs_wkt = ds.crs.to_wkt()
            try:
                crs_epsg = ds.crs.to_epsg()
            except Exception:  # noqa: BLE001
                crs_epsg = None
            crs_label = crs.to_string()
            is_geographic = bool(crs.is_geographic)
            try:
                axis = crs_axis_order(crs)
            except Exception:  # noqa: BLE001
                axis = "unknown"
            crs_display = (
                crs_label
                if (crs_epsg is not None and f"{crs_epsg}" in (crs_label or ""))
                else f"{crs_label}（EPSG:{crs_epsg}）"
            )
            warnings.append(
                f"CRS={crs_display}，"
                f"{'地理坐标系' if is_geographic else '投影坐标系'}，"
                f"官方轴序={axis}；栅格读取按 x=经度/easting、y=纬度/northing（rasterio 约定）"
            )
            if is_geographic:
                warnings.append(
                    "DEM 是地理坐标系，分辨率以「度」为单位；"
                    "面积/距离/吸附必须投影到米制 CRS（引擎会自动处理并记录）"
                )
            if abs(transform.b) > 1e-12 or abs(transform.d) > 1e-12:
                warnings.append(
                    f"仿射变换含旋转项（b={transform.b:g}, d={transform.d:g}），"
                    "当前 D8/GRASS 分析不支持旋转栅格"
                )
                provisional = True

        min_elev, max_elev, nodata_fraction, nodata_count, has_nan = _windowed_stats(ds)
        vertical_known, vertical_note, vertical_datum = _vertical_datum_note(ds, crs)

        extent_lonlat: tuple[float, float, float, float] | None = None
        if crs is not None:
            try:
                extent_lonlat = bounds_to_lonlat(
                    (ds.bounds.left, ds.bounds.bottom, ds.bounds.right, ds.bounds.top),
                    crs,
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"无法把 DEM 范围转换到 EPSG:4490：{exc}")
                extent_lonlat = None

    # -- NoData --------------------------------------------------------
    if nodata is None:
        if has_nan:
            warnings.append(
                "DEM 未声明 NoData 值，但数据中存在 NaN；已把 NaN 当作无效像元处理"
            )
        else:
            warnings.append("DEM 未声明 NoData 值（未发现 NaN）")
    elif nodata_fraction > 0:
        warnings.append(
            f"NoData 像元 {nodata_count} 个，占 {nodata_fraction:.3%}"
            f"（nodata={nodata!r}）；NoData 空洞会阻断汇流，需人工确认是否影响分水岭"
        )
        if nodata_fraction > 0.2:
            warnings.append("NoData 比例超过 20%，数据可用性存疑（provisional=true）")
            provisional = True

    # -- 高程量程 ------------------------------------------------------
    if min_elev is None or max_elev is None:
        warnings.append("DEM 没有有效高程像元（全部为 NoData）")
        provisional = True
    else:
        if abs(min_elev) < 1e-12 and abs(max_elev) < 1e-12:
            warnings.append("DEM 高程全部为 0：不是有效地形数据（provisional=true）")
            provisional = True
        elif abs(max_elev - min_elev) < 1e-9:
            warnings.append(
                f"DEM 高程为常数 {min_elev:g} m（无起伏），无法划分分水岭（provisional=true）"
            )
            provisional = True
        if min_elev < MIN_PLAUSIBLE_ELEV_M:
            warnings.append(
                f"最小高程 {min_elev:g} m 低于合理范围（<{MIN_PLAUSIBLE_ELEV_M:g} m），"
                "可能是 NoData 未声明或单位错误"
            )
            provisional = True
        if max_elev > MAX_PLAUSIBLE_ELEV_M:
            warnings.append(
                f"最大高程 {max_elev:g} m 高于地球陆地最高值（约 8849 m），量程异常"
            )
            provisional = True
        if max_elev - min_elev < 1.0 and not provisional:
            warnings.append(
                f"高程起伏仅 {max_elev - min_elev:g} m，地形过于平缓，分水岭结果不可靠"
            )

    # -- 分辨率 --------------------------------------------------------
    res_approx_m: tuple[float, float] | None = None
    if is_geographic:
        if extent_lonlat is not None:
            center_lat = (extent_lonlat[1] + extent_lonlat[3]) / 2.0
        else:  # pragma: no cover - 有 CRS 时总能算出来
            center_lat = 0.0
        approx_y = res_y * 110574.0
        approx_x = res_x * 111320.0 * max(math.cos(math.radians(center_lat)), 1e-6)
        res_approx_m = (approx_x, approx_y)
        warnings.append(
            f"分辨率 {res_x:g}°×{res_y:g}°，在中心纬度约 {approx_x:.1f} m×{approx_y:.1f} m"
        )
        if min(res_approx_m) > 100.0:
            warnings.append(
                f"DEM 分辨率约 {min(res_approx_m):.0f} m，超过 100 m，"
                "小流域/窄山脊的分水岭精度有限"
            )
    else:
        res_approx_m = (res_x, res_y)
        warnings.append(f"分辨率 {res_x:g} m×{res_y:g} m")
        if min(res_x, res_y) > 100.0:
            warnings.append(
                f"DEM 分辨率 {min(res_x, res_y):g} m，超过 100 m，"
                "小流域/窄山脊的分水岭精度有限"
            )
    if res_x > 0 and res_y > 0:
        ratio = max(res_x, res_y) / min(res_x, res_y)
        if ratio > 2.0:
            warnings.append(f"像元长宽比 {ratio:.2f}，各向异性明显")

    # -- 高程基准 / DSM-DTM -------------------------------------------
    if not vertical_known:
        warnings.append(f"垂直基准未知：{vertical_note}")
    else:
        warnings.append(f"垂直基准：{vertical_note}")
    warnings.append(
        "无法从文件判断数据是 DSM 还是 DTM：若为 DSM（如 Copernicus GLO-30），"
        "树高/建筑会抬高地表，分水岭可能偏移，工程边界应结合实测/等高线复核"
    )

    sha = sha256_file(path)
    return DemInfo(
        path=str(path),
        sha256=sha,
        crs_wkt=crs_wkt,
        crs_epsg=crs_epsg,
        is_geographic=is_geographic,
        width=width,
        height=height,
        res_x=res_x,
        res_y=res_y,
        nodata=nodata,
        min_elev=min_elev,
        max_elev=max_elev,
        nodata_fraction=nodata_fraction,
        extent_lonlat=extent_lonlat,
        vertical_datum_known=vertical_known,
        vertical_datum_note=vertical_note,
        warnings=warnings,
        provisional=provisional,
        crs_label=crs_label,
        dtype=dtype,
        res_approx_m=res_approx_m,
        vertical_datum=vertical_datum,
    )


def sample_dem_lonlat(
    dem_path: str | Path,
    points: Iterable[tuple[float, float]],
) -> list[float | None]:
    """在 DEM 上按 EPSG:4490 ``(lon, lat)`` 采样最近邻高程。

    只读取每个点所在的 1×1 窗口，适合少量点（分段平均高程、出口高程等）。
    越界或 NoData 返回 ``None``（显式缺失，不猜值）。
    """

    points = list(points)
    if not points:
        return []
    values: list[float | None] = []
    with rasterio.open(dem_path) as ds:
        if ds.crs is None:
            raise DataValidationError(f"DEM {dem_path} 缺少 CRS，无法按经纬度采样")
        transformer = Transformer.from_crs(CANONICAL_CRS, ds.crs, always_xy=True)
        nodata = ds.nodata
        for lon, lat in points:
            x, y = transformer.transform(float(lon), float(lat))
            row_f, col_f = xy_to_rowcol(ds.transform, x, y)
            # 边界上的点（例如边界顶点恰好落在栅格外缘）按最近像元处理：
            # 允许半个像元的容差，真正越界才返回 None。
            if row_f < -0.5 or row_f > ds.height + 0.5:
                values.append(None)
                continue
            if col_f < -0.5 or col_f > ds.width + 0.5:
                values.append(None)
                continue
            row = min(max(int(math.floor(row_f)), 0), ds.height - 1)
            col = min(max(int(math.floor(col_f)), 0), ds.width - 1)
            block = ds.read(1, window=Window(col, row, 1, 1))
            value = float(block[0, 0])
            if not math.isfinite(value):
                values.append(None)
            elif nodata is not None and value == nodata:
                values.append(None)
            else:
                values.append(value)
    return values


def dem_lonlat_to_crs(
    dem_path: str | Path, lon: float, lat: float
) -> tuple[float, float]:
    """把 EPSG:4490 经纬度转成 DEM 自身 CRS 的 ``(x, y)``。"""

    with rasterio.open(dem_path) as ds:
        if ds.crs is None:
            raise DataValidationError(f"DEM {dem_path} 缺少 CRS")
        return transform_lonlat_to_crs(lon, lat, ds.crs)
