"""复核图渲染（matplotlib 非交互后端）。

对应契约 ``docs/architecture.md`` §5.2 的 review 部分，并实现 Lead 的 worker
接口：

- ``render_overview(result, extra_layers=None) -> ReviewFigure``（契约）；
- ``render_review_pack(result, out_dir, max_pixels=...) -> dict``
  （worker：概览 + 带编号分段的局部图，返回 ``{"ok", "warnings", "figures"}``）；
- ``render_segment(result, segment_id, out_dir, max_pixels=...) -> dict``
  （工具层：单段局部图）；
- ``render_hillshade_contours(...)``（通用地形阴影 + 等高线面板）。

每张图包含：hillshade 底图、带标注等高距的等高线、河网、出口、候选边界、
编号分段、比例尺、指北针、范围/CRS/像元尺寸文字、数据来源与
``offline computed / verified_against_real_device=false`` 标识。PNG 用 PIL
真实解码校验，且每张 ≤ 4 MB（超限自动降 dpi，仍超则显式报错）。

matplotlib 必须在导入 pyplot 之前设置 ``MPLCONFIGDIR``（避免写
``~/.matplotlib``）；优先使用项目内 ``.cache/mpl``。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..errors import ReviewFigureError


def _ensure_mpl_config_dir() -> Path:
    """在导入 matplotlib 之前确定可写的 MPLCONFIGDIR。"""

    env = os.environ.get("MPLCONFIGDIR")
    if env:
        try:
            path = Path(env)
            path.mkdir(parents=True, exist_ok=True)
            return path
        except OSError:  # pragma: no cover - 环境异常时继续尝试兜底
            pass
    candidates = [
        Path(__file__).resolve().parents[2] / ".cache" / "mpl",
        Path(tempfile.gettempdir()) / "omap-mpl",
    ]
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            os.environ["MPLCONFIGDIR"] = str(candidate)
            return candidate
        except OSError:  # pragma: no cover
            continue
    return Path(tempfile.mkdtemp(prefix="omap-mpl-"))  # pragma: no cover


_MPL_DIR = _ensure_mpl_config_dir()

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402
from PIL import Image  # noqa: E402
from rasterio.windows import Window, from_bounds  # noqa: E402
from shapely.geometry import LineString, Polygon, shape  # noqa: E402

from ..coordinates import CANONICAL_CRS, transform_geometry  # noqa: E402
from . import engine as engine_module  # noqa: E402
from . import segment as segment_module  # noqa: E402
from .crs import transform_lines_batch  # noqa: E402

# ---------------------------------------------------------------------------
# 常量与字体
# ---------------------------------------------------------------------------

#: PNG 上限 4 MB（MCP Image 直接返回的约束）。
MAX_PNG_BYTES = 4 * 1024 * 1024
#: 依次尝试的 dpi；超限就降 dpi 重画。
DPI_CANDIDATES = (130, 110, 90, 75, 60, 50)
#: 等高线目标条数（用于自动选等高距）。
CONTOUR_TARGET_LINES = 12
#: hillshade 光源方位角/高度角（ESRI 惯例）。
HILLSHADE_AZIMUTH_DEG = 315.0
HILLSHADE_ALTITUDE_DEG = 45.0

_CJK_CANDIDATES = (
    "PingFang SC",
    "Heiti TC",
    "Songti SC",
    "STHeiti",
    "Hiragino Sans GB",
    "Arial Unicode MS",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "Microsoft YaHei",
    "SimHei",
)


def _configure_cjk_font() -> str | None:
    """优先选择系统中文字体，保证图上的中文不出现方块。"""

    try:
        available = {font.name for font in font_manager.fontManager.ttflist}
    except Exception:  # noqa: BLE001 - 字体枚举失败不阻断出图
        return None
    for name in _CJK_CANDIDATES:
        if name in available:
            matplotlib.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            matplotlib.rcParams["font.monospace"] = [name, "DejaVu Sans Mono"]
            matplotlib.rcParams["axes.unicode_minus"] = False
            return name
    return None


_CJK_FONT = _configure_cjk_font()


def _t(zh: str, en: str) -> str:
    """有中文字体时用中文，否则退回英文（避免方块字）。"""

    return zh if _CJK_FONT else en


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ReviewFigure:
    """一张复核图（契约 §5.2 字段 + 只增不改的补充字段）。"""

    image_path: str
    title: str
    bbox_lonlat: tuple[float, float, float, float]
    crs: str
    pixel_size_m: float | None
    caption: str
    segment_id: int | None
    data_sources: list[str] = field(default_factory=list)
    figure_id: str = "overview"
    width_px: int = 0
    height_px: int = 0
    #: 实际出图显示范围（EPSG:4490）。分段图会外扩以带地形上下文，
    #: 因此可能大于 ``bbox_lonlat``（后者始终是段的真实 bbox，口径不混用）。
    view_bbox_lonlat: tuple[float, float, float, float] | None = None
    #: 布局元数据（面板矩形、地图面板高度占比等，供测试与可读性回归）。
    layout: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转成 Lead 的 worker/tools 约定的 figure dict。"""

        return {
            "figure_id": self.figure_id,
            "path": self.image_path,
            "title": self.title,
            "segment_id": self.segment_id,
            "bbox_lonlat": [float(v) for v in self.bbox_lonlat],
            "crs": self.crs,
            "pixel_size_m": self.pixel_size_m,
            "caption": self.caption,
            "data_sources": list(self.data_sources),
            "width_px": self.width_px,
            "height_px": self.height_px,
            "view_bbox_lonlat": (
                [float(v) for v in self.view_bbox_lonlat]
                if self.view_bbox_lonlat is not None
                else None
            ),
            "layout": dict(self.layout),
        }


# ---------------------------------------------------------------------------
# 数值 / 地形工具
# ---------------------------------------------------------------------------


def nice_number(value: float) -> float:
    """把数值向上取整到 1/2/5×10^k（比例尺、等高距用）。"""

    if not math.isfinite(value) or value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    base = 10.0**exponent
    fraction = value / base
    for step in (1.0, 2.0, 5.0, 10.0):
        if fraction <= step:
            return step * base
    return 10.0 * base


def contour_interval(zmin: float, zmax: float, *, target_lines: int = CONTOUR_TARGET_LINES) -> float:
    """按高程范围自动选"好看"的等高距（米）。"""

    span = float(zmax) - float(zmin)
    if not math.isfinite(span) or span <= 0:
        return 1.0
    interval = nice_number(span / max(target_lines, 1))
    return max(interval, 0.5)


def compute_hillshade(
    z: np.ndarray,
    valid: np.ndarray,
    res_x: float,
    res_y: float,
    *,
    azimuth_deg: float = HILLSHADE_AZIMUTH_DEG,
    altitude_deg: float = HILLSHADE_ALTITUDE_DEG,
) -> np.ndarray:
    """由 DEM 计算 hillshade（0..1，NoData 为 NaN）。

    使用 ESRI 惯例公式；north-up 栅格下行号增大向南，因此 ``dz/dy`` 取负。
    """

    z = np.asarray(z, dtype=np.float64)
    valid = np.asarray(valid, dtype=bool)
    if z.size == 0:
        return np.zeros_like(z)
    fill = float(np.nanmin(z[valid])) if valid.any() else 0.0
    filled = np.where(valid, z, fill)
    grad_row, grad_col = np.gradient(filled, float(res_y), float(res_x))
    dzdx = grad_col
    dzdy = -grad_row
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(dzdy, -dzdx)
    zenith = math.radians(90.0 - altitude_deg)
    azimuth = math.radians(azimuth_deg)
    shade = np.cos(zenith) * np.cos(slope) + np.sin(zenith) * np.sin(slope) * np.cos(
        azimuth - aspect
    )
    shade = np.clip(shade, 0.0, 1.0)
    shade[~valid] = np.nan
    return shade


def _read_dem_window(
    dem_path: str | Path,
    *,
    bounds_xy: tuple[float, float, float, float] | None = None,
    pad_frac: float = 0.15,
) -> tuple[np.ndarray, rasterio.Affine]:
    """读取 DEM 窗口（NoData → NaN）；``bounds_xy=None`` 表示整幅。"""

    dem_path = Path(dem_path)
    if not dem_path.is_file():
        raise ReviewFigureError(f"分析 DEM 不存在：{dem_path}")
    with rasterio.open(dem_path) as ds:
        nodata = ds.nodata
        if bounds_xy is None:
            window = Window(0, 0, ds.width, ds.height)
        else:
            left, bottom, right, top = (float(v) for v in bounds_xy)
            # pad_frac>0 时才自动外扩（退化段需要最小外扩）；显式传入的
            # bounds_xy 已由调用方算好，不再额外放大，否则会改变预期长宽比。
            pad_x = (right - left) * pad_frac
            pad_y = (top - bottom) * pad_frac
            if pad_frac > 0:
                pad_x = max(pad_x, abs(float(ds.res[0])) * 3.0)
                pad_y = max(pad_y, abs(float(ds.res[1])) * 3.0)
            rb = ds.bounds
            left = max(left - pad_x, rb.left)
            right = min(right + pad_x, rb.right)
            bottom = max(bottom - pad_y, rb.bottom)
            top = min(top + pad_y, rb.top)
            if right <= left or top <= bottom:
                raise ReviewFigureError(
                    "复核范围与 DEM 无交集（可能坐标 CRS 不一致）"
                )
            float_window = from_bounds(left, bottom, right, top, ds.transform)
            col0 = int(math.floor(float_window.col_off))
            row0 = int(math.floor(float_window.row_off))
            width = int(math.ceil(float_window.width)) + 1
            height = int(math.ceil(float_window.height)) + 1
            col0 = max(0, min(col0, ds.width - 1))
            row0 = max(0, min(row0, ds.height - 1))
            width = max(1, min(width, ds.width - col0))
            height = max(1, min(height, ds.height - row0))
            window = Window(col0, row0, width, height)
        data = ds.read(1, window=window).astype(np.float64)
        transform = ds.window_transform(window)
    if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
        data[data == nodata] = np.nan
    data[~np.isfinite(data)] = np.nan
    return data, transform


def _extent_of(transform: rasterio.Affine, shape_hw: tuple[int, int]) -> tuple[float, float, float, float]:
    height, width = shape_hw
    left = float(transform.c)
    top = float(transform.f)
    right = left + float(transform.a) * width
    bottom = top + float(transform.e) * height
    return left, bottom, right, top


def _stride_for(shape_hw: tuple[int, int], max_pixels: int) -> int:
    longest = max(shape_hw)
    if longest <= max_pixels:
        return 1
    return int(math.ceil(longest / max(max_pixels, 1)))


def _cell_centers(
    transform: rasterio.Affine, shape_hw: tuple[int, int], stride: int
) -> tuple[np.ndarray, np.ndarray]:
    height, width = shape_hw
    cols = np.arange(0, width, stride, dtype=np.float64)
    rows = np.arange(0, height, stride, dtype=np.float64)
    x = transform.c + (cols + 0.5) * transform.a
    y = transform.f + (rows + 0.5) * transform.e
    return x, y


def _nice_scale_length(width_m: float) -> float:
    if not math.isfinite(width_m) or width_m <= 0:
        return 1000.0
    return nice_number(width_m / 5.0)


# ---------------------------------------------------------------------------
# 图层读取
# ---------------------------------------------------------------------------


def _read_geojson_geometry(path: str | Path) -> Any:
    path = Path(path)
    if not path.is_file():
        raise ReviewFigureError(f"GeoJSON 不存在：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("type") == "FeatureCollection":
        features = data.get("features") or []
        if not features:
            raise ReviewFigureError(f"GeoJSON 没有要素：{path}")
        data = features[0]
    if data.get("type") == "Feature":
        data = data["geometry"]
    return shape(data)


def _read_geojson_lines(path: str | Path) -> list[LineString]:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    features = data.get("features") if data.get("type") == "FeatureCollection" else [data]
    lines: list[LineString] = []
    for feature in features or []:
        geom = feature.get("geometry") if feature.get("type") == "Feature" else feature
        if not geom:
            continue
        geom_type = geom.get("type")
        if geom_type == "LineString":
            lines.append(LineString(geom["coordinates"]))
        elif geom_type == "MultiLineString":
            lines.extend(LineString(part) for part in geom["coordinates"])
    return lines


def _load_segments(
    result: engine_module.WatershedResult,
    *,
    metric_crs: str,
    n_segments: int | None = None,
) -> list[dict[str, Any]]:
    """读取/生成分段（lonlat 顶点 + 米制折线）。"""

    segments_path = result.artifacts.get("segments_geojson")
    raw: list[dict[str, Any]] = []
    if segments_path and Path(segments_path).is_file():
        data = json.loads(Path(segments_path).read_text(encoding="utf-8"))
        for feature in data.get("features") or []:
            props = dict(feature.get("properties") or {})
            geom = feature.get("geometry") or {}
            if geom.get("type") != "LineString":
                continue
            props["vertices"] = [list(v) for v in geom["coordinates"]]
            raw.append(props)
    if not raw:
        boundary_path = result.boundary_geojson or result.artifacts.get("boundary_geojson")
        if not boundary_path or not Path(boundary_path).is_file():
            raise ReviewFigureError("缺少边界/分段成果，无法生成分段复核图")
        count = n_segments or segment_module.choose_segment_count(
            result.perimeter_m or 0.0
        )
        raw = segment_module.list_segments(
            boundary_path,
            n_segments=count,
            metric_crs=metric_crs,
            dem_path=result.artifacts.get("analysis_dem_tif"),
        )
    segments: list[dict[str, Any]] = []
    for props in raw:
        vertices = props.get("vertices") or []
        if len(vertices) < 2:
            continue
        line = transform_geometry(LineString(vertices), CANONICAL_CRS, metric_crs)
        # 经纬度 bbox 必须来自顶点本身（EPSG:4490），不能拿米制 bounds 冒充
        bbox_lonlat = props.get("bbox_lonlat")
        if not (isinstance(bbox_lonlat, (list, tuple)) and len(bbox_lonlat) == 4):
            xs = [float(vertex[0]) for vertex in vertices]
            ys = [float(vertex[1]) for vertex in vertices]
            bbox_lonlat = [min(xs), min(ys), max(xs), max(ys)]
        segments.append(
            {
                "segment_id": int(props.get("segment_id", len(segments) + 1)),
                "line": line,
                "vertices": vertices,
                "bbox_lonlat": [float(v) for v in bbox_lonlat],
                "bbox_metric": list(line.bounds),
                "length_m": props.get("length_m"),
                "mean_elev_m": props.get("mean_elev_m"),
                "min_elev_m": props.get("min_elev_m"),
                "max_elev_m": props.get("max_elev_m"),
                "start_anchor_id": props.get("start_anchor_id"),
                "end_anchor_id": props.get("end_anchor_id"),
                "bbox_lonlat": props.get("bbox_lonlat"),
            }
        )
    if not segments:
        raise ReviewFigureError("没有可用的边界分段")
    segments.sort(key=lambda item: item["segment_id"])
    return segments


def _load_result_layers(result: engine_module.WatershedResult) -> dict[str, Any]:
    """加载复核图所需的分析 DEM / 边界 / 河网 / 出口。"""

    if not result.metric_crs:
        raise ReviewFigureError("成果缺少 metric_crs，无法生成复核图")
    analysis_path = result.artifacts.get("analysis_dem_tif")
    if not analysis_path or not Path(analysis_path).is_file():
        raise ReviewFigureError(
            "缺少分析 DEM 成果（analysis_dem.tif），请先运行 run_watershed"
        )
    boundary_path = result.boundary_geojson or result.artifacts.get("boundary_geojson")
    if not boundary_path or not Path(boundary_path).is_file():
        raise ReviewFigureError("缺少边界 GeoJSON，无法生成复核图")
    boundary_metric = transform_geometry(
        _read_geojson_geometry(boundary_path), CANONICAL_CRS, result.metric_crs
    )
    streams_path = result.artifacts.get("streams_geojson")
    streams_metric: list[LineString] = []
    if streams_path and Path(streams_path).is_file():
        streams_lonlat = _read_geojson_lines(streams_path)
        if streams_lonlat:
            streams_metric = transform_lines_batch(
                streams_lonlat, CANONICAL_CRS, result.metric_crs
            )
    outlet_xy = None
    if all(math.isfinite(v) for v in result.outlet_used):
        from .crs import transform_lonlat_to_crs

        outlet_xy = transform_lonlat_to_crs(
            result.outlet_used[0], result.outlet_used[1], result.metric_crs
        )
    with rasterio.open(analysis_path) as ds:
        analysis_bounds = tuple(float(v) for v in ds.bounds)
    return {
        "analysis_path": str(analysis_path),
        "analysis_bounds": analysis_bounds,
        "boundary_metric": boundary_metric,
        "streams_metric": streams_metric,
        "outlet_xy": outlet_xy,
    }


# ---------------------------------------------------------------------------
# 绘图核心
# ---------------------------------------------------------------------------


def _draw_scale_bar(ax: Any, width_m: float) -> None:
    length = _nice_scale_length(width_m)
    if not math.isfinite(width_m) or width_m <= 0:  # pragma: no cover
        return
    fraction = min(length / width_m, 0.45)
    half = fraction / 2.0
    x0, y0, height = 0.06, 0.055, 0.008
    ax.add_patch(
        Rectangle(
            (x0, y0),
            half,
            height,
            transform=ax.transAxes,
            facecolor="black",
            edgecolor="black",
            linewidth=0.6,
            zorder=25,
        )
    )
    ax.add_patch(
        Rectangle(
            (x0 + half, y0),
            half,
            height,
            transform=ax.transAxes,
            facecolor="white",
            edgecolor="black",
            linewidth=0.6,
            zorder=25,
        )
    )
    label = f"{length / 1000.0:g} km" if length >= 1000 else f"{length:g} m"
    ax.text(
        x0 + fraction / 2.0,
        y0 + height + 0.006,
        label,
        transform=ax.transAxes,
        ha="center",
        va="bottom",
        fontsize=6.4,
        zorder=25,
        bbox=dict(facecolor="white", alpha=0.75, edgecolor="none", pad=1.0),
    )


def _draw_north_arrow(ax: Any) -> None:
    ax.annotate(
        "",
        xy=(0.955, 0.955),
        xytext=(0.955, 0.885),
        xycoords="axes fraction",
        arrowprops=dict(
            facecolor="#1a1a1a",
            edgecolor="#1a1a1a",
            width=2.0,
            headwidth=8.0,
            headlength=10.0,
        ),
        zorder=25,
    )
    ax.text(
        0.955,
        0.962,
        "N",
        transform=ax.transAxes,
        ha="center",
        va="bottom",
        fontsize=8.5,
        fontweight="bold",
        zorder=25,
    )
    ax.text(
        0.955,
        0.872,
        _t("网格北", "grid N"),
        transform=ax.transAxes,
        ha="center",
        va="top",
        fontsize=5.2,
        color="#333333",
        zorder=25,
    )


def _draw_info_box(ax: Any, lines: Sequence[str], *, fontsize: float = 6.2) -> None:
    ax.text(
        0.012,
        0.985,
        "\n".join(lines),
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=fontsize,
        family="monospace",
        zorder=25,
        bbox=dict(
            boxstyle="round,pad=0.32",
            facecolor="white",
            alpha=0.82,
            edgecolor="#9a9a9a",
            linewidth=0.5,
        ),
    )


def _draw_scale_and_info(
    ax: Any,
    *,
    extent: tuple[float, float, float, float],
    info_lines: Sequence[str],
    info_fontsize: float = 6.2,
) -> None:
    width_m = extent[2] - extent[0]
    _draw_scale_bar(ax, width_m)
    _draw_north_arrow(ax)
    _draw_info_box(ax, info_lines, fontsize=info_fontsize)


def _save_png_under_limit(fig: Any, out_path: Path) -> tuple[int, int]:
    """按 dpi 逐级尝试保存 PNG，确保 ≤4MB 且可被 PIL 解码。"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    last_size = 0
    for dpi in DPI_CANDIDATES:
        fig.savefig(
            out_path,
            dpi=dpi,
            bbox_inches="tight",
            facecolor="white",
            format="png",
        )
        last_size = out_path.stat().st_size
        if last_size <= MAX_PNG_BYTES:
            break
    else:
        raise ReviewFigureError(
            f"复核图 {out_path.name} 在最低 dpi 下仍超过 {MAX_PNG_BYTES / 1024 / 1024:.1f} MB"
            f"（{last_size / 1024 / 1024:.2f} MB）"
        )
    try:
        with Image.open(out_path) as image:
            image.verify()
        with Image.open(out_path) as image:
            width, height = image.size
            if image.format != "PNG":
                raise ReviewFigureError(f"复核图不是 PNG：{image.format}")
    except ReviewFigureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ReviewFigureError(f"复核图无法被 PIL 解码：{exc}") from exc
    return int(width), int(height)


def _fit_interval(
    low: float, high: float, dem_low: float, dem_high: float
) -> tuple[float, float]:
    """把区间平移/裁剪到 DEM 覆盖范围内（尽量保留原宽度）。"""

    width = high - low
    dem_width = dem_high - dem_low
    if width >= dem_width:
        return dem_low, dem_high
    if low < dem_low:
        low = dem_low
        high = low + width
    if high > dem_high:
        high = dem_high
        low = high - width
    return low, high


def _layout_friendly_view(
    seg_bounds: tuple[float, float, float, float],
    dem_bounds: tuple[float, float, float, float],
    *,
    target_aspect: float = 0.37,
    min_context_m: float = 300.0,
    min_side_m: float = 600.0,
) -> tuple[float, float, float, float]:
    """为分段图构造"带地形上下文且便于排版"的显示范围（米制）。

    - 每侧外扩 ``max(min_context_m, 0.25×段长)``，并保证最小边长 ``min_side_m``；
    - 平移到 DEM 覆盖范围内（不裁剪掉段本身）；
    - 若显示范围过扁（高/宽 < target_aspect），在仍包含整段的前提下收窄宽度，
      使图幅既能放下"地图主体"又不出现大面积白带；
    - 过窄（高/宽 > 2.2）时同理收窄高度。
    """

    minx, miny, maxx, maxy = seg_bounds
    seg_w = maxx - minx
    seg_h = maxy - miny
    length = max(seg_w, seg_h)
    pad = max(min_context_m, 0.25 * length)
    left, right = minx - pad, maxx + pad
    bottom, top = miny - pad, maxy + pad
    if right - left < min_side_m:
        center = (left + right) / 2.0
        left, right = center - min_side_m / 2.0, center + min_side_m / 2.0
    if top - bottom < min_side_m:
        center = (bottom + top) / 2.0
        bottom, top = center - min_side_m / 2.0, center + min_side_m / 2.0
    left, right = _fit_interval(left, right, dem_bounds[0], dem_bounds[2])
    bottom, top = _fit_interval(bottom, top, dem_bounds[1], dem_bounds[3])

    view_w = right - left
    view_h = top - bottom
    if view_w <= 0 or view_h <= 0:
        return (left, bottom, right, top)
    aspect = view_h / view_w
    if aspect < target_aspect:
        new_w = max(seg_w * 1.02, view_h / target_aspect)
        center_x = (minx + maxx) / 2.0
        left, right = _fit_interval(
            center_x - new_w / 2.0, center_x + new_w / 2.0, dem_bounds[0], dem_bounds[2]
        )
    elif aspect > 2.2:
        new_h = max(seg_h * 1.02, view_w * 2.2)
        center_y = (miny + maxy) / 2.0
        bottom, top = _fit_interval(
            center_y - new_h / 2.0,
            center_y + new_h / 2.0,
            dem_bounds[1],
            dem_bounds[3],
        )
    return (left, bottom, right, top)


def _create_figure_layout(
    map_aspect: float,
    *,
    enforce_aspect: bool = True,
) -> tuple[Any, Any, Any | None, dict[str, Any], str]:
    """按地图面板长宽比创建图幅布局。

    - ``map_aspect <= 0.9``（宽/横版）：``stack`` 布局，地图在上、紧凑信息条在下；
    - ``map_aspect > 0.9``（窄/竖版）：``side`` 布局，地图在左、信息在右，
      避免窄地图上 x 轴刻度粘连。

    两种布局都保证：图幅纵横比 ∈[0.5, 2.2]、地图面板高度占比 ≥60%、信息面板
    与比例尺/指北针矩形不相交。返回 ``(fig, ax, info_ax, layout, mode)``。
    """

    if map_aspect <= 0.9:
        mode = "stack"
        axes_width = 8.2
        axes_height = axes_width * map_aspect
        min_axes_height = 2.15
        if axes_height < min_axes_height:
            axes_height = min_axes_height
            axes_width = axes_height / max(map_aspect, 1e-6)
        max_axes_width = 8.2
        max_axes_height = 6.6
        if axes_width > max_axes_width:
            axes_width = max_axes_width
            axes_height = axes_width * map_aspect
        if axes_height > max_axes_height:
            axes_height = max_axes_height
            axes_width = axes_height / max(map_aspect, 1e-6)
        title_h, xmargin_h, info_h, bottom_h = 0.28, 0.40, 0.55, 0.06
        overhead = title_h + xmargin_h + info_h + bottom_h
        fig_width = max(axes_width + 0.80, 4.6)
        fig_height = axes_height + overhead
        if enforce_aspect:
            # bbox_inches="tight" 会裁掉约 0.6in 纵向空白：这里预留余量，
            # 保证最终 PNG 纵横比 ≤2.2，同时地图面板高度占比 ≥55%。
            fig_height = max(fig_height, fig_width / 2.2 + 0.62)
        if fig_width / fig_height < 0.5:
            fig_width = 0.5 * fig_height
        x0 = (fig_width - axes_width) / 2.0
        map_bottom = (bottom_h + info_h + xmargin_h) / fig_height
        fig = plt.figure(figsize=(fig_width, fig_height))
        ax = fig.add_axes(
            [
                x0 / fig_width,
                map_bottom,
                axes_width / fig_width,
                axes_height / fig_height,
            ]
        )
        info_ax = fig.add_axes([0.04, bottom_h / fig_height, 0.92, info_h / fig_height])
        info_ax.set_axis_off()
        scale_bar_axes = (0.055, 0.040, 0.42, 0.045)
        north_arrow_axes = (0.900, 0.820, 0.085, 0.165)

        def _to_figure(rect: tuple[float, float, float, float]) -> list[float]:
            return [
                x0 / fig_width + rect[0] * axes_width / fig_width,
                map_bottom + rect[1] * axes_height / fig_height,
                rect[2] * axes_width / fig_width,
                rect[3] * axes_height / fig_height,
            ]

        layout = {
            "mode": mode,
            "figure_size_in": [round(fig_width, 3), round(fig_height, 3)],
            "map_panel_rect": [
                x0 / fig_width,
                map_bottom,
                axes_width / fig_width,
                axes_height / fig_height,
            ],
            "info_panel_rect": [0.04, bottom_h / fig_height, 0.92, info_h / fig_height],
            "map_panel_height_fraction": axes_height / fig_height,
            "scale_bar_rect": _to_figure(scale_bar_axes),
            "north_arrow_rect": _to_figure(north_arrow_axes),
            "map_panel_width_in": axes_width,
        }
        return fig, ax, info_ax, layout, mode

    mode = "side"
    axes_height = 5.9
    axes_width = axes_height / map_aspect
    max_axes_width = 7.2
    if axes_width > max_axes_width:
        axes_width = max_axes_width
        axes_height = axes_width * map_aspect
    title_h, xmargin_h, caption_h, bottom_h = 0.30, 0.42, 0.34, 0.10
    info_width = 2.55
    left_margin, gap, right_margin = 0.55, 0.12, 0.20
    content_width = axes_width + gap + info_width
    fig_width = left_margin + content_width + right_margin
    fig_height = title_h + axes_height + xmargin_h + caption_h + bottom_h
    if enforce_aspect:
        fig_height = max(fig_height, fig_width / 2.2 + 0.62)
    if fig_width / fig_height < 0.5:
        fig_width = 0.5 * fig_height
    x0 = (fig_width - content_width) / 2.0
    map_bottom = (bottom_h + caption_h + xmargin_h) / fig_height
    fig = plt.figure(figsize=(fig_width, fig_height))
    ax = fig.add_axes(
        [
            x0 / fig_width,
            map_bottom,
            axes_width / fig_width,
            axes_height / fig_height,
        ]
    )
    info_x = x0 + axes_width + gap
    info_bottom = (bottom_h + caption_h) / fig_height
    info_height = fig_height - title_h - bottom_h - caption_h
    info_ax = fig.add_axes(
        [
            info_x / fig_width,
            info_bottom,
            info_width / fig_width,
            info_height / fig_height,
        ]
    )
    info_ax.set_axis_off()
    scale_bar_axes = (0.055, 0.040, 0.52, 0.045)
    north_arrow_axes = (0.870, 0.830, 0.110, 0.155)

    def _to_figure(rect: tuple[float, float, float, float]) -> list[float]:
        return [
            x0 / fig_width + rect[0] * axes_width / fig_width,
            map_bottom + rect[1] * axes_height / fig_height,
            rect[2] * axes_width / fig_width,
            rect[3] * axes_height / fig_height,
        ]

    layout = {
        "mode": mode,
        "figure_size_in": [round(fig_width, 3), round(fig_height, 3)],
        "map_panel_rect": [
            x0 / fig_width,
            map_bottom,
            axes_width / fig_width,
            axes_height / fig_height,
        ],
        "info_panel_rect": [
            info_x / fig_width,
            info_bottom,
            info_width / fig_width,
            info_height / fig_height,
        ],
        "map_panel_height_fraction": axes_height / fig_height,
        "scale_bar_rect": _to_figure(scale_bar_axes),
        "north_arrow_rect": _to_figure(north_arrow_axes),
        "map_panel_width_in": axes_width,
    }
    return fig, ax, info_ax, layout, mode


def _wrap_caption(caption: str, *, width: int = 150) -> str:
    """把图注按字符数折行（matplotlib text 不会自动折行）。"""

    import textwrap

    return "\n".join(textwrap.wrap(caption, width=width)) or caption


def _draw_info_panel(
    ax: Any,
    info_lines: Sequence[str],
    caption: str,
    *,
    mode: str,
    fontsize: float = 6.2,
) -> None:
    """在图外信息面板里画文本（stack 两列 + 图注内置；side 单列）。"""

    ax.add_patch(
        Rectangle(
            (0.0, 0.0),
            1.0,
            1.0,
            transform=ax.transAxes,
            facecolor="#fbfbfb",
            edgecolor="#9a9a9a",
            linewidth=0.5,
            zorder=0,
        )
    )
    if mode == "stack":
        lines = list(info_lines)
        split = (len(lines) + 1) // 2
        columns = [lines[:split], lines[split:]]
        for column_index, column in enumerate(columns):
            if not column:
                continue
            ax.text(
                0.006 + column_index * 0.50,
                0.985,
                "\n".join(column),
                transform=ax.transAxes,
                ha="left",
                va="top",
                fontsize=fontsize,
                family="monospace",
                zorder=1,
            )
        ax.text(
            0.006,
            0.015,
            _wrap_caption(caption, width=168),
            transform=ax.transAxes,
            ha="left",
            va="bottom",
            fontsize=4.9,
            color="#444444",
            zorder=1,
        )
    else:
        ax.text(
            0.03,
            0.985,
            "\n".join(_wrap_caption(line, width=34) for line in info_lines),
            transform=ax.transAxes,
            ha="left",
            va="top",
            fontsize=fontsize,
            family="monospace",
            zorder=1,
        )


def _apply_narrow_tick_fix(
    ax: Any,
    *,
    figure_aspect: float,
    axes_width_in: float,
    layout: dict[str, Any],
    dpi: float = 130.0,
) -> None:
    """窄图 x 轴刻度防粘连：主刻度 ≤4、必要时旋转 35°。"""

    from matplotlib.ticker import MaxNLocator

    narrow = figure_aspect < 0.9 or axes_width_in * dpi < 420.0
    if narrow:
        ax.xaxis.set_major_locator(MaxNLocator(nbins=4))
        for label in ax.get_xticklabels():
            label.set_rotation(35)
            label.set_horizontalalignment("right")
            label.set_fontsize(5.2)
        layout["x_tick_rotation_deg"] = 35
    else:
        layout["x_tick_rotation_deg"] = 0
    layout["x_major_tick_count"] = int(len(ax.get_xticks()))
    layout["narrow"] = bool(narrow)


def _render_panel(
    *,
    out_path: Path,
    dem_path: str | Path,
    metric_crs: str,
    bounds_xy: tuple[float, float, float, float] | None,
    boundary_metric: Any | None,
    segments: Sequence[dict[str, Any]],
    highlight_segment_id: int | None,
    outlet_xy: tuple[float, float] | None,
    streams_metric: Sequence[LineString],
    extra_layers: dict[str, str] | None,
    title: str,
    info_lines: Sequence[str],
    caption: str,
    data_sources: list[str],
    bbox_lonlat: tuple[float, float, float, float],
    figure_id: str,
    segment_id: int | None,
    max_pixels: int,
    layout_mode: str = "inside",
    view_bbox_lonlat: tuple[float, float, float, float] | None = None,
    uniform_segment_color: bool = False,
    enforce_aspect: bool = True,
) -> ReviewFigure:
    """渲染一张地形复核图（overview 与 segment 共用）。

    ``layout_mode="inside"``：信息框叠加在地图左上角（概览图）；
    ``layout_mode="auto"``：按地图面板长宽比自动选 stack（宽/横版：地图在上、
    紧凑信息条在下）或 side（窄/竖版：地图在左、信息在右）布局，信息面板与
    比例尺/指北针互不遮挡，窄图自动减少并旋转 x 轴刻度。
    """

    z, transform = _read_dem_window(dem_path, bounds_xy=bounds_xy, pad_frac=0.0)
    valid = np.isfinite(z)
    if not valid.any():
        raise ReviewFigureError("复核范围内没有有效高程像元")
    res_x = abs(float(transform.a))
    res_y = abs(float(transform.e))
    extent = _extent_of(transform, z.shape)
    width_m = extent[2] - extent[0]
    pixel_size_m = float(res_x)

    # 实际显示范围：请求窗口 ∪ 数据窗口（DEM 覆盖不足时被裁剪）
    if bounds_xy is not None:
        from shapely.geometry import box as _box_union

        union_extent = (
            min(float(bounds_xy[0]), extent[0]),
            min(float(bounds_xy[1]), extent[1]),
            max(float(bounds_xy[2]), extent[2]),
            max(float(bounds_xy[3]), extent[3]),
        )
        computed_view_bbox = transform_geometry(
            _box_union(*union_extent), metric_crs, CANONICAL_CRS
        ).bounds
    else:
        computed_view_bbox = view_bbox_lonlat or bbox_lonlat

    # 显示与等高线分别降采样：显示到 max_pixels，等高线到 ≤700 列以保证速度
    display_stride = _stride_for(z.shape, max_pixels)
    z_display = z[::display_stride, ::display_stride]
    valid_display = valid[::display_stride, ::display_stride]
    shade = compute_hillshade(
        z_display, valid_display, res_x * display_stride, res_y * display_stride
    )
    contour_stride = _stride_for(z.shape, 700)
    z_contour = z[::contour_stride, ::contour_stride]
    valid_contour = valid[::contour_stride, ::contour_stride]
    x_contour, y_contour = _cell_centers(transform, z.shape, contour_stride)
    z_contour_filled = np.where(valid_contour, z_contour, np.nan)

    zmin = float(np.nanmin(z[valid]))
    zmax = float(np.nanmax(z[valid]))
    interval = contour_interval(zmin, zmax)

    # 图幅布局：分段图自动选 stack/side，信息面板在图外，避免遮挡比例尺/坐标轴
    height_m = extent[3] - extent[1]
    aspect = (height_m / width_m) if width_m > 0 else 1.0
    info_ax = None
    layout_meta: dict[str, Any] = {"mode": "inside"}
    resolved_layout = layout_mode
    if layout_mode == "auto":
        fig, ax, info_ax, layout_meta, resolved_layout = _create_figure_layout(
            aspect, enforce_aspect=enforce_aspect
        )
        info_fontsize = 6.1 if layout_meta["mode"] == "stack" else 6.0
    else:
        # 概览/通用地形图：信息框放图内左上角（长宽比通常接近方形，不遮挡）
        fig_width = 9.4
        axes_width = fig_width - 2.0
        axes_height = max(axes_width * aspect, 1.15)
        fig_height = axes_height + 2.1
        if fig_height > 9.0:
            fig_height = 9.0
            axes_height = fig_height - 2.1
            axes_width = max(axes_height / max(aspect, 1e-6), 1.6)
            fig_width = max(axes_width + 2.0, 3.8)
        elif fig_height < 3.8:
            fig_height = 3.8
        info_fontsize = 6.2 if fig_width >= 6.0 else 5.2
        fig, ax = plt.subplots(figsize=(fig_width, fig_height))
        layout_meta.update(
            {
                "figure_size_in": [round(fig_width, 3), round(fig_height, 3)],
                "map_panel_height_fraction": None,
            }
        )
    try:
        ax.imshow(
            shade,
            extent=extent,
            origin="upper",
            cmap="gray",
            vmin=0.0,
            vmax=1.0,
            interpolation="bilinear",
            zorder=1,
        )
        ax.set_xlim(extent[0], extent[2])
        ax.set_ylim(extent[1], extent[3])
        ax.set_aspect("equal", adjustable="box")

        # 等高线（标注等高距）
        if math.isfinite(zmin) and math.isfinite(zmax) and zmax > zmin:
            first = math.ceil(zmin / interval) * interval
            levels = np.arange(first, zmax + interval * 0.5, interval)
            if levels.size >= 2:
                contours = ax.contour(
                    x_contour,
                    y_contour,
                    z_contour_filled,
                    levels=levels,
                    colors="#2b2b2b",
                    linewidths=0.32,
                    alpha=0.55,
                    zorder=2,
                )
                try:
                    ax.clabel(
                        contours,
                        inline=True,
                        fontsize=4.6,
                        fmt="%g",
                        colors="#2b2b2b",
                    )
                except Exception:  # noqa: BLE001 - 标注失败不阻断出图
                    pass

        # 河网
        if streams_metric:
            segments_coords = [np.asarray(line.coords) for line in streams_metric]
            ax.add_collection(
                LineCollection(
                    segments_coords,
                    colors="#1f6fd0",
                    linewidths=0.7,
                    alpha=0.85,
                    zorder=3,
                )
            )

        # 候选边界
        if boundary_metric is not None:
            polygons = (
                [boundary_metric]
                if isinstance(boundary_metric, Polygon)
                else list(getattr(boundary_metric, "geoms", []))
            )
            for polygon in polygons:
                if not isinstance(polygon, Polygon):
                    continue
                ax.plot(
                    *polygon.exterior.xy,
                    color="#e01818",
                    linewidth=1.9,
                    zorder=6,
                    solid_capstyle="round",
                )
                for interior in polygon.interiors:
                    ax.plot(
                        *interior.xy,
                        color="#f08a00",
                        linewidth=1.1,
                        linestyle="--",
                        zorder=6,
                    )

        # 编号分段（颜色语义统一：橙色=分段，高亮段更粗；非目标段灰色弱化）
        if segments:
            for segment in segments:
                line: LineString = segment["line"]
                is_highlight = (
                    highlight_segment_id is not None
                    and segment["segment_id"] == highlight_segment_id
                )
                if uniform_segment_color:
                    color, line_width, alpha = "#e8882a", 1.25, 0.95
                elif is_highlight:
                    color, line_width, alpha = "#ff8c00", 3.0, 0.98
                else:
                    color, line_width, alpha = "#8a8a8a", 1.0, 0.8
                ax.plot(
                    *line.xy,
                    color=color,
                    linewidth=line_width,
                    alpha=alpha,
                    zorder=8 if is_highlight else 7,
                )
                midpoint = line.interpolate(0.5, normalized=True)
                label = str(segment["segment_id"])
                ax.annotate(
                    label,
                    xy=(midpoint.x, midpoint.y),
                    xytext=(4, 4),
                    textcoords="offset points",
                    fontsize=6.4,
                    fontweight="bold" if is_highlight else "normal",
                    color="#111111",
                    zorder=9,
                    bbox=dict(
                        boxstyle="round,pad=0.18",
                        facecolor="#ffe9b0" if is_highlight else "white",
                        alpha=0.9,
                        edgecolor="#888888",
                        linewidth=0.4,
                    ),
                )
                if is_highlight:
                    coords = list(line.coords)
                    start_x, start_y = coords[0]
                    end_x, end_y = coords[-1]
                    ax.plot(
                        [start_x, end_x],
                        [start_y, end_y],
                        linestyle="none",
                        marker="o",
                        markersize=4.6,
                        markerfacecolor="white",
                        markeredgecolor="#d2691e",
                        markeredgewidth=1.0,
                        zorder=10,
                    )
                    if segment.get("start_anchor_id"):
                        ax.annotate(
                            str(segment["start_anchor_id"]),
                            xy=(start_x, start_y),
                            xytext=(5, -9),
                            textcoords="offset points",
                            fontsize=5.0,
                            color="#7a3b00",
                            zorder=10,
                        )
                    if segment.get("end_anchor_id"):
                        ax.annotate(
                            str(segment["end_anchor_id"]),
                            xy=(end_x, end_y),
                            xytext=(5, 4),
                            textcoords="offset points",
                            fontsize=5.0,
                            color="#7a3b00",
                            zorder=10,
                        )

        # 额外图层（例如用户参考边界）
        for path, label in (extra_layers or {}).items():
            try:
                geom = _read_geojson_geometry(path)
                geom_metric = transform_geometry(geom, CANONICAL_CRS, metric_crs)
            except Exception:  # noqa: BLE001 - 额外图层失败不阻断
                continue
            geoms = (
                [geom_metric]
                if not hasattr(geom_metric, "geoms")
                else list(geom_metric.geoms)
            )
            for item in geoms:
                if isinstance(item, Polygon):
                    ax.plot(
                        *item.exterior.xy,
                        color="#9b30ff",
                        linewidth=1.2,
                        linestyle="-.",
                        zorder=7,
                        label=label,
                    )
                elif isinstance(item, LineString):
                    ax.plot(
                        *item.xy,
                        color="#9b30ff",
                        linewidth=1.2,
                        linestyle="-.",
                        zorder=7,
                        label=label,
                    )

        # 出口
        if outlet_xy is not None:
            ax.plot(
                outlet_xy[0],
                outlet_xy[1],
                marker="*",
                markersize=15,
                markerfacecolor="#ff2d2d",
                markeredgecolor="white",
                markeredgewidth=1.2,
                zorder=12,
            )
            ax.annotate(
                _t("出口", "outlet"),
                xy=outlet_xy,
                xytext=(7, 7),
                textcoords="offset points",
                fontsize=6.6,
                fontweight="bold",
                color="#b00000",
                zorder=12,
                bbox=dict(
                    boxstyle="round,pad=0.2",
                    facecolor="white",
                    alpha=0.85,
                    edgecolor="#b00000",
                    linewidth=0.5,
                ),
            )

        ax.set_title(title, fontsize=10.2, fontweight="bold", pad=9)
        ax.set_xlabel(_t("东坐标 / Easting (m)", "Easting (m)"), fontsize=6.6)
        ax.set_ylabel(_t("北坐标 / Northing (m)", "Northing (m)"), fontsize=6.6)
        ax.tick_params(labelsize=5.8)
        # 纯数值刻度 + 隐藏 matplotlib 的 1e6 偏移文字（否则会与标题重叠）
        plain = FuncFormatter(lambda value, _pos: f"{value:.0f}")
        ax.xaxis.set_major_formatter(plain)
        ax.yaxis.set_major_formatter(plain)
        ax.xaxis.get_offset_text().set_visible(False)
        ax.yaxis.get_offset_text().set_visible(False)
        ax.grid(color="#888888", alpha=0.18, linewidth=0.4)
        if layout_mode == "auto":
            # 比例尺/指北针固定在地图面板角落；信息面板在图外，互不遮挡
            _draw_scale_bar(ax, extent[2] - extent[0])
            _draw_north_arrow(ax)
            _apply_narrow_tick_fix(
                ax,
                figure_aspect=(
                    layout_meta["figure_size_in"][0] / layout_meta["figure_size_in"][1]
                ),
                axes_width_in=float(layout_meta.get("map_panel_width_in", 7.2)),
                layout=layout_meta,
            )
            if info_ax is not None:
                if layout_meta["mode"] == "stack":
                    _draw_info_panel(
                        info_ax,
                        info_lines,
                        caption,
                        mode="stack",
                        fontsize=info_fontsize,
                    )
                else:
                    _draw_info_panel(
                        info_ax,
                        info_lines,
                        caption,
                        mode="side",
                        fontsize=info_fontsize,
                    )
                    fig.text(
                        0.5,
                        0.008,
                        _wrap_caption(caption, width=190),
                        ha="center",
                        va="bottom",
                        fontsize=4.9,
                        color="#444444",
                    )
        else:
            _draw_scale_and_info(
                ax,
                extent=extent,
                info_lines=info_lines,
                info_fontsize=info_fontsize,
            )
            fig.text(
                0.5,
                0.006,
                caption,
                ha="center",
                va="bottom",
                fontsize=5.6,
                color="#333333",
                wrap=True,
            )
        width_px, height_px = _save_png_under_limit(fig, out_path)
    finally:
        plt.close(fig)

    return ReviewFigure(
        image_path=str(out_path),
        title=title,
        bbox_lonlat=tuple(float(v) for v in bbox_lonlat),
        crs=CANONICAL_CRS,
        pixel_size_m=pixel_size_m,
        caption=caption,
        segment_id=segment_id,
        data_sources=list(data_sources),
        figure_id=figure_id,
        width_px=width_px,
        height_px=height_px,
        view_bbox_lonlat=tuple(float(v) for v in computed_view_bbox),
        layout=layout_meta,
    )


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def _format_elev(value: Any) -> str:
    """高程显示格式：保留 1 位小数（避免 106.0999984741211 这类浮点噪声）。"""

    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return _t("未知", "n/a")


def _data_sources(result: engine_module.WatershedResult) -> list[str]:
    sources = result.provenance.get("data_sources") if result.provenance else None
    if isinstance(sources, list) and sources:
        return [str(item) for item in sources]
    return list(engine_module.DATA_SOURCES)


def _base_caption(result: engine_module.WatershedResult) -> str:
    extent = result.analysis_extent_lonlat
    return (
        f"{_t('水平投影面积', 'planar area')} {result.area_m2:,.1f} m²；"
        f"{_t('边界 CRS', 'boundary CRS')} {CANONICAL_CRS}；"
        f"{_t('分析 CRS', 'analysis CRS')} {result.metric_crs}；"
        f"{_t('范围', 'bbox')} [{extent[0]:.6f},{extent[1]:.6f},{extent[2]:.6f},{extent[3]:.6f}]；"
        f"{_t('引擎', 'engine')} {result.engine_used} {result.engine_version}；"
        f"offline computed；verified_against_real_device=false。"
        f"{_t('本图用于人工复核，不能替代实测边界', 'For visual review only; not a surveyed boundary')}"
    )


def _overview_warning_flags(result: engine_module.WatershedResult) -> list[str]:
    """概览图"关键警告"行：截断 / 出口无出流 / 垂直基准未知 / DSM-DTM 未知。"""

    flags: list[str] = []
    if result.truncated:
        flags.append(_t("truncated=true（分析范围截断）", "truncated=true"))
    result_meta = (result.provenance or {}).get("result", {})
    if result_meta.get("outlet_is_sink"):
        flags.append(_t("出口无出流方向", "outlet has no outflow"))
    dem_info = result.dem_info or {}
    if not dem_info.get("vertical_datum_known", False):
        flags.append(_t("垂直基准未知", "vertical datum unknown"))
    flags.append(_t("DSM/DTM 未知", "DSM/DTM unknown"))
    return flags


def _overview_info_lines(
    result: engine_module.WatershedResult,
    *,
    interval_m: float,
    stream_count: int,
    warning_flags: Sequence[str] | None = None,
) -> list[str]:
    extent = result.analysis_extent_lonlat
    dem_name = Path(str(result.dem_info.get("path", ""))).name
    sha = str(result.dem_info.get("sha256", ""))[:12]
    area_ha = (result.area_m2 or 0.0) / 10000.0
    area_km2 = (result.area_m2 or 0.0) / 1e6
    return [
        _t(f"CRS: {result.metric_crs}（分析） / {CANONICAL_CRS}（边界）",
           f"CRS: {result.metric_crs} (analysis) / {CANONICAL_CRS} (boundary)"),
        _t(f"范围: {extent[0]:.6f},{extent[1]:.6f} → {extent[2]:.6f},{extent[3]:.6f}",
           f"bbox: {extent[0]:.6f},{extent[1]:.6f} -> {extent[2]:.6f},{extent[3]:.6f}"),
        _t(f"像元: {result.cell_area_m2 ** 0.5:.2f} m（面积 {result.cell_area_m2:g} m²）",
           f"pixel: {result.cell_area_m2 ** 0.5:.2f} m (area {result.cell_area_m2:g} m²)"),
        _t(f"面积: {result.area_m2:,.1f} m² = {area_ha:,.2f} ha = {area_km2:,.4f} km²",
           f"area: {result.area_m2:,.1f} m² = {area_ha:,.2f} ha"),
        _t(f"周长: {result.perimeter_m:,.1f} m；顶点: {result.provenance.get('result', {}).get('vertex_count', '?')}",
           f"perimeter: {result.perimeter_m:,.1f} m; vertices: {result.provenance.get('result', {}).get('vertex_count', '?')}"),
        _t(f"等高距: {interval_m:g} m；河网链: {stream_count}",
           f"contour interval: {interval_m:g} m; stream chains: {stream_count}"),
        _t(f"出口: {result.outlet_used[0]:.7f}, {result.outlet_used[1]:.7f}",
           f"outlet: {result.outlet_used[0]:.7f}, {result.outlet_used[1]:.7f}"),
        _t(f"吸附距离: {result.snap_distance_m:.2f} m；累积 {result.flow_accum_cells} 像元",
           f"snap: {result.snap_distance_m:.2f} m; accum {result.flow_accum_cells} cells"),
        _t(f"引擎: {result.engine_used} / {result.engine_version}",
           f"engine: {result.engine_used} / {result.engine_version}"),
        _t(f"DEM: {dem_name} sha256={sha}…",
           f"DEM: {dem_name} sha256={sha}..."),
        _t(f"警告: {len(result.warnings)} 条（见 provenance.json）；offline computed",
           f"warnings: {len(result.warnings)} (see provenance.json); offline computed"),
        _t(
            "图例: 红=候选边界；橙=编号分段；蓝=河网；红星=出口",
            "legend: red=candidate boundary; orange=numbered segments; blue=streams; star=outlet",
        ),
        _t(
            "关键警告: " + "；".join(warning_flags or ["无"]),
            "key warnings: " + "; ".join(warning_flags or ["none"]),
        ),
    ]


def render_overview(
    result: engine_module.WatershedResult,
    extra_layers: dict[str, str] | None = None,
    *,
    out_dir: str | Path | None = None,
    max_pixels: int = 1600,
) -> ReviewFigure:
    """生成全流域概览复核图（契约 §5.2）。"""

    layers = _load_result_layers(result)
    review_dir = Path(out_dir) if out_dir is not None else Path(result.task_dir) / engine_module.REVIEW_DIR
    review_dir.mkdir(parents=True, exist_ok=True)
    segments = _load_segments(result, metric_crs=result.metric_crs)
    z, _transform = _read_dem_window(layers["analysis_path"])
    valid = np.isfinite(z)
    interval = contour_interval(float(np.nanmin(z[valid])), float(np.nanmax(z[valid])))
    area_km2 = (result.area_m2 or 0.0) / 1e6
    # 概览：显示范围外扩 4%（让信息框落在边缘地形上）；接近方形时信息框叠加，
    # 细长 DEM 用 auto 布局（图外信息面板），避免遮挡地形。
    extent = result.analysis_extent_lonlat
    center_lat = (extent[1] + extent[3]) / 2.0
    width_m = (extent[2] - extent[0]) * 111320.0 * max(
        math.cos(math.radians(center_lat)), 1e-6
    )
    height_m = (extent[3] - extent[1]) * 110574.0
    overview_aspect = (height_m / width_m) if width_m > 0 else 1.0
    overview_layout = "auto" if overview_aspect < 0.45 else "inside"
    from shapely.geometry import box as _box_extent

    extent_metric = transform_geometry(
        _box_extent(*extent), CANONICAL_CRS, result.metric_crs
    ).bounds
    pad_x = (extent_metric[2] - extent_metric[0]) * 0.04
    pad_y = (extent_metric[3] - extent_metric[1]) * 0.04
    overview_view = (
        extent_metric[0] - pad_x,
        extent_metric[1] - pad_y,
        extent_metric[2] + pad_x,
        extent_metric[3] + pad_y,
    )
    warning_flags = _overview_warning_flags(result)
    title = _t(
        f"集水区概览 — 水平面积 {result.area_m2:,.1f} m²（{area_km2:.3f} km²）",
        f"Watershed overview - planar area {result.area_m2:,.1f} m² ({area_km2:.3f} km²)",
    )
    info_lines = _overview_info_lines(
        result,
        interval_m=interval,
        stream_count=len(layers["streams_metric"]),
        warning_flags=warning_flags,
    )
    figure = _render_panel(
        out_path=review_dir / "overview.png",
        dem_path=layers["analysis_path"],
        metric_crs=result.metric_crs,
        bounds_xy=overview_view,
        boundary_metric=layers["boundary_metric"],
        segments=segments,
        highlight_segment_id=None,
        outlet_xy=layers["outlet_xy"],
        streams_metric=layers["streams_metric"],
        extra_layers=extra_layers,
        title=title,
        info_lines=info_lines,
        caption=_base_caption(result),
        data_sources=_data_sources(result),
        bbox_lonlat=tuple(result.analysis_extent_lonlat),
        figure_id="overview",
        segment_id=None,
        max_pixels=max_pixels,
        layout_mode=overview_layout,
        uniform_segment_color=True,
        enforce_aspect=overview_aspect >= 0.3,
    )
    figure.layout["view_padding_fraction"] = 0.04
    figure.layout["key_warnings"] = list(warning_flags)
    return figure


def render_segment(
    result: engine_module.WatershedResult,
    segment_id: int,
    out_dir: str | Path | None = None,
    *,
    max_pixels: int = 1600,
) -> dict[str, Any]:
    """生成单段局部复核图（Lead 的 tools 接口）。

    返回 ``{"ok": bool, "warnings": [...], "figures": [figure_dict]}``；
    分段不存在或渲染失败时显式 ``ok=False``，不伪造图片。
    """

    warnings: list[str] = []
    try:
        layers = _load_result_layers(result)
        segments = _load_segments(result, metric_crs=result.metric_crs)
        target = None
        for segment in segments:
            if segment["segment_id"] == int(segment_id):
                target = segment
                break
        if target is None:
            return {
                "ok": False,
                "warnings": [
                    f"分段 {segment_id} 不存在（可用分段 1..{len(segments)}）"
                ],
                "figures": [],
            }
        review_dir = (
            Path(out_dir) if out_dir is not None else Path(result.task_dir) / engine_module.REVIEW_DIR
        )
        review_dir.mkdir(parents=True, exist_ok=True)
        line: LineString = target["line"]
        minx, miny, maxx, maxy = (float(v) for v in line.bounds)
        seg_bbox = [float(v) for v in target["bbox_lonlat"]]  # EPSG:4490，段真实 bbox
        # 带地形上下文且便于排版的显示范围（含段本身；近水平/近竖直退化段
        # 也能看到两侧地形与邻接分段，同时避免图幅出现大面积白带）
        requested_view = _layout_friendly_view(
            (minx, miny, maxx, maxy), layers["analysis_bounds"]
        )
        z, _transform = _read_dem_window(
            layers["analysis_path"], bounds_xy=requested_view, pad_frac=0.0
        )
        valid = np.isfinite(z)
        if not valid.any():
            return {
                "ok": False,
                "warnings": [f"分段 {segment_id} 范围内没有有效高程像元"],
                "figures": [],
            }
        # 显示范围：请求的外扩窗口 ∪ 实际数据窗口（DEM 覆盖不足时会被裁剪），
        # 取并集保证 view_bbox 一定包含段真实 bbox，且如实反映"打算显示的范围"。
        data_extent = _extent_of(_transform, z.shape)
        union_extent = (
            min(requested_view[0], data_extent[0]),
            min(requested_view[1], data_extent[1]),
            max(requested_view[2], data_extent[2]),
            max(requested_view[3], data_extent[3]),
        )
        from shapely.geometry import box as _box

        view_bbox = transform_geometry(
            _box(*union_extent), result.metric_crs, CANONICAL_CRS
        ).bounds
        interval = contour_interval(
            float(np.nanmin(z[valid])), float(np.nanmax(z[valid]))
        )
        mean_elev = target.get("mean_elev_m")
        mean_text = f"{mean_elev:.1f} m" if isinstance(mean_elev, (int, float)) else _t("未知", "n/a")
        title = _t(
            f"分段 {segment_id} 局部复核 — 长度 {target['length_m']:,.0f} m，平均高程 {mean_text}",
            f"Segment {segment_id} review - length {target['length_m']:,.0f} m, mean elev {mean_text}",
        )
        source_short = "；".join(
            item.split("（")[0].split(":")[0][:24] for item in _data_sources(result)
        )[:110]
        info_lines = [
            _t(
                f"分段 {segment_id}/{len(segments)}；锚点 "
                f"{target.get('start_anchor_id')} → {target.get('end_anchor_id')}",
                f"segment {segment_id}/{len(segments)}; anchors "
                f"{target.get('start_anchor_id')} -> {target.get('end_anchor_id')}",
            ),
            _t(
                f"长度 {target['length_m']:,.1f} m；平均高程 {mean_text}；"
                f"高程 {_format_elev(target.get('min_elev_m'))}"
                f"~{_format_elev(target.get('max_elev_m'))} m",
                f"length {target['length_m']:,.1f} m; mean elev {mean_text}; "
                f"elev {_format_elev(target.get('min_elev_m'))}"
                f"~{_format_elev(target.get('max_elev_m'))} m",
            ),
            _t(
                f"段范围 bbox_lonlat: {seg_bbox[0]:.6f},{seg_bbox[1]:.6f} → "
                f"{seg_bbox[2]:.6f},{seg_bbox[3]:.6f}",
                f"segment bbox_lonlat: {seg_bbox[0]:.6f},{seg_bbox[1]:.6f} -> "
                f"{seg_bbox[2]:.6f},{seg_bbox[3]:.6f}",
            ),
            _t(
                f"显示范围 view_bbox_lonlat（已外扩）: {view_bbox[0]:.6f},"
                f"{view_bbox[1]:.6f} → {view_bbox[2]:.6f},{view_bbox[3]:.6f}",
                f"view bbox (padded): {view_bbox[0]:.6f},{view_bbox[1]:.6f} -> "
                f"{view_bbox[2]:.6f},{view_bbox[3]:.6f}",
            ),
            _t(
                f"CRS {result.metric_crs}（分析）/{CANONICAL_CRS}（边界）；"
                f"等高距 {interval:g} m；像元 {result.cell_area_m2 ** 0.5:.2f} m",
                f"CRS {result.metric_crs} / {CANONICAL_CRS}; contour interval "
                f"{interval:g} m; pixel {result.cell_area_m2 ** 0.5:.2f} m",
            ),
            _t(
                f"出口 {result.outlet_used[0]:.7f},{result.outlet_used[1]:.7f}；"
                f"引擎 {result.engine_used}；offline computed",
                f"outlet {result.outlet_used[0]:.7f},{result.outlet_used[1]:.7f}; "
                f"engine {result.engine_used}; offline computed",
            ),
            _t(f"数据来源: {source_short}", f"sources: {source_short}"),
        ]
        caption = (
            f"{_t('分段', 'segment')} {segment_id}："
            f"{_t('沿边界长度', 'boundary length')} {target['length_m']:,.1f} m；"
            f"{_t('局部高程', 'local elevation')} {mean_text}；"
            f"{_t('等高距', 'contour interval')} {interval:g} m；"
            f"{_t('显示范围已外扩用于地形上下文（段真实 bbox 见 bbox_lonlat）', 'view padded for terrain context; true segment bbox in bbox_lonlat')}；"
            "offline computed；verified_against_real_device=false。"
        )
        figure = _render_panel(
            out_path=review_dir / f"seg-{int(segment_id):02d}.png",
            dem_path=layers["analysis_path"],
            metric_crs=result.metric_crs,
            bounds_xy=requested_view,
            boundary_metric=layers["boundary_metric"],
            segments=segments,
            highlight_segment_id=int(segment_id),
            outlet_xy=layers["outlet_xy"],
            streams_metric=layers["streams_metric"],
            extra_layers=None,
            title=title,
            info_lines=info_lines,
            caption=caption,
            data_sources=_data_sources(result),
            bbox_lonlat=tuple(seg_bbox),
            figure_id=f"seg-{int(segment_id):02d}",
            segment_id=int(segment_id),
            max_pixels=max_pixels,
            layout_mode="auto",
        )
        return {"ok": True, "warnings": warnings, "figures": [figure.to_dict()]}
    except Exception as exc:  # noqa: BLE001 - 显式失败，不伪造图片
        warnings.append(f"分段 {segment_id} 复核图生成失败：{exc}")
        return {"ok": False, "warnings": warnings, "figures": []}


def render_review_pack(
    result: engine_module.WatershedResult,
    out_dir: str | Path,
    *,
    max_pixels: int = 1600,
    n_segments: int | None = None,
) -> dict[str, Any]:
    """生成概览 + 带编号分段的局部复核图包（Lead 的 worker 接口）。

    默认按边界周长切 4–8 段（:func:`ovital_mcp.gis.segment.choose_segment_count`）。
    返回 ``{"ok": bool, "warnings": [...], "figures": [...]}``；单张失败只记
    warning，不会伪造图片；概览图失败时 ``ok=False``。
    """

    out_dir = Path(out_dir)
    review_dir = out_dir / engine_module.REVIEW_DIR
    review_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    figures: list[dict[str, Any]] = []

    try:
        segments = _load_segments(
            result,
            metric_crs=result.metric_crs,
            n_segments=n_segments
            or segment_module.choose_segment_count(result.perimeter_m or 0.0),
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"分段读取失败：{exc}")
        segments = []

    overview_ok = False
    try:
        overview = render_overview(result, out_dir=review_dir, max_pixels=max_pixels)
        figures.append(overview.to_dict())
        overview_ok = True
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"概览复核图生成失败：{exc}")

    for segment in segments:
        segment_id = int(segment["segment_id"])
        try:
            item = render_segment(
                result, segment_id, out_dir=review_dir, max_pixels=max_pixels
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"分段 {segment_id} 复核图生成失败：{exc}")
            continue
        if item.get("ok") and item.get("figures"):
            figures.extend(item["figures"])
        else:
            warnings.extend(item.get("warnings") or [])

    manifest = {
        "ok": bool(overview_ok and len(figures) >= 1),
        "warnings": warnings,
        "figures": figures,
        "result_task_dir": result.task_dir,
        "metric_crs": result.metric_crs,
        "boundary_crs": CANONICAL_CRS,
        "evidence_level": "offline_computed",
        "verified_against_real_device": False,
    }
    try:
        (review_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - 磁盘异常只告警
        warnings.append(f"复核图 manifest 写入失败：{exc}")
    return manifest


def render_hillshade_contours(
    dem_path: str | Path,
    out_path: str | Path,
    *,
    crs: str | None = None,
    bbox_lonlat: tuple[float, float, float, float] | None = None,
    title: str | None = None,
    boundary_geojson: str | Path | None = None,
    data_sources: Sequence[str] | None = None,
    max_pixels: int = 1600,
) -> ReviewFigure:
    """通用地形阴影 + 等高线面板（契约 §5.2 的 ``render_hillshade_contours``）。"""

    with rasterio.open(dem_path) as ds:
        dem_crs = crs or (ds.crs.to_string() if ds.crs else None)
        if dem_crs is None:
            raise ReviewFigureError(f"DEM {dem_path} 缺少 CRS")
        dem_bounds = ds.bounds
    bounds_xy = None
    if bbox_lonlat is not None:
        from ..coordinates import transform_geometry as _transform_geom
        from shapely.geometry import box as _box

        bbox_geom = _transform_geom(_box(*bbox_lonlat), CANONICAL_CRS, dem_crs)
        bounds_xy = bbox_geom.bounds
    else:
        bounds_xy = (dem_bounds.left, dem_bounds.bottom, dem_bounds.right, dem_bounds.top)
    z, transform = _read_dem_window(dem_path, bounds_xy=bounds_xy, pad_frac=0.0)
    valid = np.isfinite(z)
    if not valid.any():
        raise ReviewFigureError("指定范围内没有有效高程")
    interval = contour_interval(float(np.nanmin(z[valid])), float(np.nanmax(z[valid])))
    extent = _extent_of(transform, z.shape)
    width_m = extent[2] - extent[0]
    height_m = extent[3] - extent[1]
    layout_mode = "auto" if width_m > 0 and height_m / width_m < 0.45 else "inside"
    bbox_ll = bbox_lonlat or _extent_to_lonlat(extent, dem_crs)
    boundary_metric = None
    if boundary_geojson:
        boundary_metric = transform_geometry(
            _read_geojson_geometry(boundary_geojson), CANONICAL_CRS, dem_crs
        )
    figure = _render_panel(
        out_path=Path(out_path),
        dem_path=dem_path,
        metric_crs=dem_crs,
        bounds_xy=bounds_xy,
        boundary_metric=boundary_metric,
        segments=[],
        highlight_segment_id=None,
        outlet_xy=None,
        streams_metric=[],
        extra_layers=None,
        title=title or _t("地形阴影与等高线", "Hillshade and contours"),
        info_lines=[
            _t(f"CRS: {dem_crs}", f"CRS: {dem_crs}"),
            _t(f"等高距: {interval:g} m", f"contour interval: {interval:g} m"),
            _t(
                f"范围: {extent[0]:.1f},{extent[1]:.1f} → {extent[2]:.1f},{extent[3]:.1f}",
                f"extent: {extent[0]:.1f},{extent[1]:.1f} -> {extent[2]:.1f},{extent[3]:.1f}",
            ),
            _t("offline computed", "offline computed"),
        ],
        caption=_t(
            "地形阴影与等高线，仅用于人工判读；等高线来自 DEM 插值，不能作为独立精度证据。",
            "Hillshade/contours for visual interpretation only; derived from the same DEM.",
        ),
        data_sources=list(data_sources or []),
        bbox_lonlat=tuple(bbox_ll),
        figure_id="hillshade_contours",
        segment_id=None,
        max_pixels=max_pixels,
        layout_mode=layout_mode,
        view_bbox_lonlat=tuple(bbox_ll),
    )
    return figure


def _extent_to_lonlat(
    extent: tuple[float, float, float, float], src_crs: str
) -> tuple[float, float, float, float]:
    from shapely.geometry import box as _box

    geom = transform_geometry(_box(*extent), src_crs, CANONICAL_CRS)
    return tuple(float(v) for v in geom.bounds)  # type: ignore[return-value]


__all__ = [
    "MAX_PNG_BYTES",
    "ReviewFigure",
    "compute_hillshade",
    "contour_interval",
    "nice_number",
    "render_hillshade_contours",
    "render_overview",
    "render_review_pack",
    "render_segment",
]
