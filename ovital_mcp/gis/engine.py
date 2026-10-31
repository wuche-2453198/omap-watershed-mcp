"""集水区计算引擎入口与成果装配（对 tools 的唯一入口）。

对应契约 ``docs/architecture.md`` §5.2，并按 Lead 补充要求实现：

- 稳定成果文件名：``boundary.geojson`` / ``mask.tif`` / ``provenance.json`` /
  ``result.json``，失败时写 ``failure.json``；
- 失败**不抛异常**（除参数非法抛 ``DataValidationError``），返回 ``ok=False``
  的 :class:`WatershedResult`，供 worker 落终态；
- ``WatershedResult`` 补充 ``boundary_crs`` / ``outlet_vertex_index`` /
  ``truncated`` 字段（只增不改，见 docs/gis-notes.md）。

本模块只做"调度 + 成果装配"：具体地形计算在 :mod:`ovital_mcp.gis.native`
（numpy/rasterio D8）与 :mod:`ovital_mcp.gis.grass`（GRASS 8.x SFD）里。
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import math
import platform
import sys
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np
import shapely
from shapely import make_valid
from shapely.geometry import Point, mapping

from ..coordinates import CANONICAL_CRS, verify_canonical_lonlat
from ..errors import (
    DataValidationError,
    EngineError,
    EngineUnavailableError,
    GeometryError,
    OmapError,
)
from . import dem as dem_module
from . import geometry as geometry_module
from . import segment as segment_module
from .crs import (
    RESAMPLING,
    AnalysisRaster,
    prepare_analysis_dem,
    resolve_metric_crs,
    transform_geometry,
    transform_lonlat_to_crs,
    transform_xy_to_lonlat,
)

# ---------------------------------------------------------------------------
# 稳定成果文件名（Lead 的 worker/tools 直接依赖，勿随意改）
# ---------------------------------------------------------------------------

BOUNDARY_GEOJSON = "boundary.geojson"
MASK_GEOTIFF = "mask.tif"
PROVENANCE_JSON = "provenance.json"
RESULT_JSON = "result.json"
FAILURE_JSON = "failure.json"
ANALYSIS_DEM_TIF = "analysis_dem.tif"
FLOW_DIRECTION_TIF = "flow_direction.tif"
FLOW_ACCUM_TIF = "flow_accum.tif"
STREAMS_GEOJSON = "streams.geojson"
SEGMENTS_GEOJSON = "segments.geojson"
REVIEW_DIR = "review"

#: 未显式配置时的分析栅格像元上限（内存保护；可用 params.extra["max_cells"] 覆盖）。
DEFAULT_MAX_CELLS = 16_000_000

#: 本包实现/验证依据的数据与算法说明（写入 provenance / 复核图）。
DATA_SOURCES = [
    "DEM: 用户提供或脚本下载的 GeoTIFF（逐像元高程；SHA256 记录在 provenance）",
    "算法: D8 单流向 + 汇流累积 + 出口上游反向 BFS（native）/ GRASS r.watershed -s + r.water.outlet",
    "坐标: 内部规范 EPSG:4490 (CGCS2000, lon/lat)；面积/吸附在米制 UTM CRS 下计算",
    "口径: 水平投影面积（shapely 平面多边形），非坡面面积；不填洼、不静默简化",
    "证据等级: offline_computed（本机确定性计算）；无独立人工审核边界",
]


# ---------------------------------------------------------------------------
# 契约数据结构
# ---------------------------------------------------------------------------


@dataclass
class WatershedParams:
    """集水区计算参数（契约 §5.2）。"""

    dem_path: str
    outlet_lonlat: tuple[float, float]
    engine: str = "auto"
    snap_radius_m: float = 90.0
    metric_crs: str | None = None
    target_res_m: float | None = None
    min_accum_cells: int = 3
    simplify_tolerance_m: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WatershedResult:
    """集水区计算成果（契约 §5.2 + 只增不改的补充字段）。"""

    task_dir: str
    ok: bool
    error: str | None = None
    error_code: str | None = None
    boundary_geojson: str | None = None
    mask_geotiff: str | None = None
    area_m2: float | None = None
    perimeter_m: float | None = None
    geometry_valid: bool = False
    multipart: bool = False
    hole_count: int = 0
    outlet_input: tuple[float, float] = (math.nan, math.nan)
    outlet_used: tuple[float, float] = (math.nan, math.nan)
    snap_distance_m: float = math.nan
    flow_accum_cells: int = 0
    cell_area_m2: float = math.nan
    metric_crs: str = ""
    engine_used: str = ""
    engine_version: str = ""
    analysis_extent_lonlat: tuple[float, float, float, float] = (
        math.nan,
        math.nan,
        math.nan,
        math.nan,
    )
    dem_info: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    # -- 补充字段（只增不改；Lead 的 worker/工具层依赖）--------------
    boundary_crs: str = CANONICAL_CRS
    outlet_vertex_index: int | None = None
    truncated: bool = False

    # -- 序列化 --------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WatershedResult":
        known = {f.name for f in dataclasses.fields(cls)}
        payload = {k: v for k, v in data.items() if k in known}
        for key in ("outlet_input", "outlet_used", "analysis_extent_lonlat"):
            value = payload.get(key)
            if isinstance(value, list) and len(value) == 2:
                payload[key] = (float(value[0]), float(value[1]))
            elif isinstance(value, list) and len(value) == 4:
                payload[key] = tuple(float(v) for v in value)
        if payload.get("boundary_crs") is None:
            payload["boundary_crs"] = CANONICAL_CRS
        return cls(**payload)


@runtime_checkable
class WatershedEngine(Protocol):
    """引擎协议（契约 §5.2）。"""

    name: str

    def available(self) -> tuple[bool, str]: ...

    def run(self, params: WatershedParams, out_dir: str | Path) -> WatershedResult: ...


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds")


def _json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


def _params_dict(params: WatershedParams) -> dict[str, Any]:
    data = asdict(params)
    data["outlet_lonlat"] = [float(params.outlet_lonlat[0]), float(params.outlet_lonlat[1])]
    return data


def _library_versions() -> dict[str, str]:
    versions = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }
    for name in ("numpy", "scipy", "rasterio", "shapely", "pyproj"):
        try:
            module = __import__(name)
            versions[name] = str(getattr(module, "__version__", "unknown"))
        except Exception:  # noqa: BLE001
            versions[name] = "unavailable"
    return versions


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fingerprint(
    *,
    mask_data: np.ndarray,
    boundary_4490: Any,
    params: WatershedParams,
    metric_crs: str,
    analysis: AnalysisRaster,
) -> str:
    """确定性指纹：只依赖输入、参数与结果几何/掩膜，不含时间戳。"""

    digest = hashlib.sha256()
    digest.update(mask_data.tobytes(order="C"))
    digest.update(shapely.to_wkb(boundary_4490, byte_order=1))
    canonical = json.dumps(
        {
            "params": _params_dict(params),
            "metric_crs": metric_crs,
            "grid": [analysis.width, analysis.height, analysis.res_x, analysis.res_y],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    digest.update(canonical.encode("utf-8"))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 两种引擎共用的准备阶段
# ---------------------------------------------------------------------------


@dataclass
class CommonContext:
    """引擎运行前的公共上下文（DEM 检查 + 米制分析网格 + 出口投影）。"""

    dem_info: dem_module.DemInfo
    metric_crs: str
    analysis: AnalysisRaster
    outlet_xy: tuple[float, float]
    warnings: list[str]
    max_cells: int


def prepare_common(params: WatershedParams, out_dir: str | Path) -> CommonContext:
    """DEM 检查 + 分析网格准备 + 出口投影（native/GRASS 共用）。"""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    dem_info = dem_module.inspect_dem(params.dem_path)
    warnings.extend(f"[DEM] {item}" for item in dem_info.warnings)

    lon, lat = verify_canonical_lonlat(*params.outlet_lonlat)
    metric_crs = resolve_metric_crs(lon, lat, params.metric_crs)

    extra = params.extra or {}
    max_cells = int(extra.get("max_cells", DEFAULT_MAX_CELLS))
    if max_cells <= 0:
        raise DataValidationError(f"extra.max_cells 必须为正整数，收到 {max_cells}")
    resampling = str(extra.get("resampling", "bilinear"))
    if resampling not in RESAMPLING:
        raise DataValidationError(
            f"extra.resampling={resampling!r} 不受支持，允许 {sorted(RESAMPLING)}"
        )

    analysis = prepare_analysis_dem(
        params.dem_path,
        out_dir / ANALYSIS_DEM_TIF,
        metric_crs=metric_crs,
        target_res_m=params.target_res_m,
        max_cells=max_cells,
        resampling=resampling,
        read_array=True,
    )
    outlet_xy = transform_lonlat_to_crs(lon, lat, metric_crs)
    warnings.append(
        f"分析投影：{metric_crs}；分析栅格 {analysis.width}×{analysis.height}，"
        f"分辨率 {analysis.res_x:g} m，像元面积 {analysis.cell_area_m2:g} m²"
    )
    if analysis.reprojected:
        warnings.append(
            f"DEM 已重投影/重采样到 {metric_crs}（{analysis.resampling}）；"
            "重采样会给高程与坡度带来小幅平滑，窄山脊/平缓分水岭需复核"
        )
    return CommonContext(
        dem_info=dem_info,
        metric_crs=metric_crs,
        analysis=analysis,
        outlet_xy=outlet_xy,
        warnings=warnings,
        max_cells=max_cells,
    )


# ---------------------------------------------------------------------------
# 成果装配（native / GRASS 共用）
# ---------------------------------------------------------------------------


def _mask_touches_edge(mask: np.ndarray) -> int:
    """统计掩膜接触分析范围边界的像元数（去重）。"""

    if mask.size == 0:
        return 0
    edges = np.zeros_like(mask, dtype=bool)
    edges[0, :] = True
    edges[-1, :] = True
    edges[:, 0] = True
    edges[:, -1] = True
    return int(np.count_nonzero(mask & edges))


def _nodata_adjacent_to_mask(mask: np.ndarray, valid: np.ndarray) -> int:
    """统计紧邻 NoData 的流域像元数（NoData 空洞可能扭曲分水岭）。"""

    invalid = ~valid
    if not invalid.any():
        return 0
    try:
        from scipy import ndimage

        dilated = ndimage.binary_dilation(invalid, structure=np.ones((3, 3), dtype=bool))
        return int(np.count_nonzero(mask & dilated))
    except Exception:  # noqa: BLE001 - scipy 异常不阻断主结果
        padded = np.pad(invalid, 1, constant_values=False)
        neighbor = np.zeros_like(invalid)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                neighbor |= padded[
                    1 + dr : 1 + dr + invalid.shape[0],
                    1 + dc : 1 + dc + invalid.shape[1],
                ]
        return int(np.count_nonzero(mask & neighbor))


def assemble_result(
    *,
    out_dir: str | Path,
    params: WatershedParams,
    dem_info: dem_module.DemInfo,
    metric_crs: str,
    analysis: AnalysisRaster,
    mask: np.ndarray,
    boundary_metric: Any,
    outlet_used_lonlat: tuple[float, float],
    snap_distance_m: float,
    flow_accum_cells: int,
    engine_name: str,
    engine_version: str,
    warnings: list[str],
    provenance_extra: dict[str, Any] | None = None,
    stream_network: Any | None = None,
    extra_artifacts: dict[str, str] | None = None,
    outlet_is_sink: bool = False,
    topology_repaired: bool = False,
    snap_evidence: dict[str, Any] | None = None,
) -> WatershedResult:
    """把引擎算出的掩膜/边界装配成契约成果并落盘。

    这是 native 与 GRASS 的公共后处理：几何校验/受控简化、量算、GeoJSON/
    GeoTIFF/分段/河网落盘、provenance 与 result.json、概览复核图。
    """

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, str] = dict(extra_artifacts or {})
    warnings = list(warnings)
    extra = params.extra or {}

    mask = np.asarray(mask, dtype=bool)
    if mask.shape != analysis.shape:
        raise EngineError(
            f"掩膜形状 {mask.shape} 与分析栅格形状 {analysis.shape} 不一致"
        )
    analysis_valid = analysis.ensure_valid_mask()
    analysis_array = analysis.ensure_array()
    cell_area = float(analysis.cell_area_m2)
    mask_cell_count = int(np.count_nonzero(mask))
    area_from_cells = mask_cell_count * cell_area

    # -- 几何：校验 -> （显式）修复 -> （可选）简化 ----------------------
    boundary_4490 = transform_geometry(
        geometry_module.as_polygonal(boundary_metric), metric_crs, CANONICAL_CRS
    )
    raw_check = geometry_module.validate_geometry(boundary_4490, metric_crs=metric_crs)
    if not raw_check.is_valid:
        warnings.append(
            "矢量化边界初始无效（%s）；已用 make_valid 做受控修复，"
            "修复前后几何均记录，请复核" % raw_check.validity_explanation
        )
        repaired = make_valid(boundary_4490)
        if not repaired.is_valid or repaired.is_empty:
            raise EngineError(
                "矢量化边界无效且 make_valid 修复失败：" + raw_check.validity_explanation
            )
        boundary_4490 = geometry_module.as_polygonal(repaired)
        topology_repaired = True

    simplify_report: dict[str, Any] | None = None
    if params.simplify_tolerance_m and params.simplify_tolerance_m > 0:
        try:
            boundary_4490, simplify_report = geometry_module.simplify_preserving(
                boundary_4490,
                float(params.simplify_tolerance_m),
                protected_points=[tuple(outlet_used_lonlat)],
                metric_crs=metric_crs,
            )
            warnings.extend(simplify_report.get("warnings", []))
        except GeometryError as exc:
            warnings.append(
                f"简化被拒绝（未做任何静默修改，保留原始矢量化边界）：{exc}"
            )

    boundary_4490, orientation_changed = geometry_module.normalize_rfc7946(boundary_4490)
    if orientation_changed:
        warnings.append(
            "边界环方向已按 RFC 7946 规范化为外环 CCW/内环 CW（只改方向，不改坐标与拓扑）"
        )

    check = geometry_module.validate_geometry(boundary_4490, metric_crs=metric_crs)
    measure = geometry_module.measure(boundary_4490, metric_crs)
    if area_from_cells > 0:
        rel = abs(measure.area_m2 - area_from_cells) / area_from_cells
        if rel > 0.005:
            warnings.append(
                f"矢量边界面积 {measure.area_m2:.1f} m² 与掩膜像元面积 "
                f"{area_from_cells:.1f} m² 相差 {rel:.3%}，请复核矢量化/简化"
            )
    if check.multipart:
        warnings.append(
            f"边界为多部件（{check.part_count} 个）：集水区通常应连通，请复核"
        )
    if check.hole_count:
        warnings.append(
            f"边界含 {check.hole_count} 个洞：可能来自内部洼地/NoData 空洞，请复核"
        )
    if check.sliver_suspected:
        warnings.append("边界疑似细长片，请确认出口是否落在坡面而非河道")

    outlet_point = Point(float(outlet_used_lonlat[0]), float(outlet_used_lonlat[1]))
    try:
        outlet_vertex_index = geometry_module.nearest_vertex_index(
            boundary_4490, outlet_point, metric_crs=metric_crs
        )
    except Exception:  # noqa: BLE001
        outlet_vertex_index = None

    # -- 截断 / NoData / 洼地 警告 ------------------------------------
    edge_cells = _mask_touches_edge(mask)
    truncated = edge_cells > 0
    if truncated:
        warnings.append(
            f"分析范围截断：流域掩膜有 {edge_cells} 个像元接触 DEM 分析范围边界，"
            "上游面积可能被截断；请使用覆盖完整上游的更大 DEM 或扩大分析范围"
        )
    nodata_adjacent = _nodata_adjacent_to_mask(mask, analysis_valid)
    if nodata_adjacent:
        warnings.append(
            f"NoData 空洞影响：{nodata_adjacent} 个流域像元紧邻 NoData，"
            "空洞会阻断汇流，边界可能失真"
        )
    if flow_accum_cells and mask_cell_count:
        if abs(flow_accum_cells - mask_cell_count) > max(
            2, int(0.001 * mask_cell_count)
        ):
            warnings.append(
                f"出口汇流累积 {flow_accum_cells} 与掩膜像元数 {mask_cell_count} "
                "不一致（>0.1%）：可能来自洼地/平地/边界效应，请复核"
            )
    if outlet_is_sink:
        warnings.append(
            "出口所在栅格没有出流方向（洼地/平地/分析范围边界）：上游范围以该栅格为终止；"
            "引擎不填洼，若该处是真实洼地/水库/人工拦蓄，请人工确认上游边界是否应另行定义"
        )

    # -- 掩膜 GeoTIFF --------------------------------------------------
    mask_data = np.full(mask.shape, 255, dtype=np.uint8)
    mask_data[analysis_valid] = 0
    mask_data[mask & analysis_valid] = 1
    mask_path = out_dir / MASK_GEOTIFF
    import rasterio

    mask_profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "count": 1,
        "width": analysis.width,
        "height": analysis.height,
        "crs": metric_crs,
        "transform": analysis.transform,
        "nodata": 255,
    }
    with rasterio.open(mask_path, "w", **mask_profile) as dst:
        dst.write(mask_data, 1)
    artifacts["mask_geotiff"] = str(mask_path)
    artifacts["mask_tif"] = str(mask_path)
    mask_sha = dem_module.sha256_file(mask_path)

    # -- 河网 GeoJSON（可选） -----------------------------------------
    streams_path: str | None = None
    if stream_network is not None and len(stream_network) > 0:
        streams_4490 = [
            transform_geometry(line, metric_crs, CANONICAL_CRS) for line in stream_network
        ]
        features = [
            {
                "type": "Feature",
                "properties": {
                    "kind": "stream_cell_to_receiver",
                    "accum_threshold_cells": int(
                        extra.get("stream_threshold_cells", params.min_accum_cells)
                    ),
                    "length_m": float(line.length),
                },
                "geometry": mapping(line),
            }
            for line in streams_4490
        ]
        streams_path = str(out_dir / STREAMS_GEOJSON)
        _json_dump(
            Path(streams_path),
            {"type": "FeatureCollection", "features": features},
        )
        artifacts["streams_geojson"] = streams_path

    # -- 分段 ----------------------------------------------------------
    n_segments = extra.get("n_segments")
    segment_max_len_m = extra.get("segment_max_len_m")
    try:
        if n_segments is not None:
            n_segments = int(n_segments)
            if n_segments < 1:
                raise DataValidationError("extra.n_segments 必须 ≥1")
        segments = segment_module.segment_boundary(
            boundary_4490,
            n_segments=n_segments,
            max_len_m=float(segment_max_len_m) if segment_max_len_m else None,
            metric_crs=metric_crs,
            outlet_lonlat=tuple(outlet_used_lonlat),
            outlet_vertex_index=outlet_vertex_index,
            dem_path=analysis.path,
        )
        segments_geojson = segment_module.segments_to_geojson(segments)
        segments_path = out_dir / SEGMENTS_GEOJSON
        _json_dump(segments_path, segments_geojson)
        artifacts["segments_geojson"] = str(segments_path)
    except (DataValidationError, GeometryError) as exc:
        warnings.append(f"边界分段失败（不影响边界成果）：{exc}")
        segments = []

    # -- 边界 GeoJSON --------------------------------------------------
    boundary_path = out_dir / BOUNDARY_GEOJSON
    boundary_properties = {
        "area_m2": measure.area_m2,
        "area_ha": measure.area_ha,
        "perimeter_m": measure.perimeter_m,
        "metric_crs": metric_crs,
        "boundary_crs": CANONICAL_CRS,
        "engine": engine_name,
        "engine_version": engine_version,
        "dem_path": dem_info.path,
        "dem_sha256": dem_info.sha256,
        "outlet_input": [float(params.outlet_lonlat[0]), float(params.outlet_lonlat[1])],
        "outlet_used": [float(outlet_used_lonlat[0]), float(outlet_used_lonlat[1])],
        "outlet_vertex_index": outlet_vertex_index,
        "snap_distance_m": float(snap_distance_m),
        "flow_accum_cells": int(flow_accum_cells),
        "mask_cell_count": mask_cell_count,
        "area_from_cells_m2": area_from_cells,
        "cell_area_m2": cell_area,
        "vertex_count": measure.vertex_count,
        "part_count": measure.part_count,
        "hole_count": measure.hole_count,
        "geometry_valid": bool(check.is_valid),
        "topology_repaired": bool(topology_repaired),
        "truncated": bool(truncated),
        "simplify_tolerance_m": float(params.simplify_tolerance_m),
        "area_method": "水平投影面积（shapely 平面多边形，投影 CRS 下计算）",
        "evidence_level": "offline_computed",
        "verified_against_real_device": False,
    }
    _json_dump(
        boundary_path,
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": boundary_properties,
                    "geometry": mapping(boundary_4490),
                }
            ],
        },
    )
    artifacts["boundary_geojson"] = str(boundary_path)

    # -- provenance ----------------------------------------------------
    boundary_wkb_sha = _sha256_bytes(shapely.to_wkb(boundary_4490, byte_order=1))
    fingerprint = _fingerprint(
        mask_data=mask_data,
        boundary_4490=boundary_4490,
        params=params,
        metric_crs=metric_crs,
        analysis=analysis,
    )
    extent_lonlat = analysis.bounds_lonlat()
    provenance: dict[str, Any] = {
        "schema": "ovital_mcp.watershed.provenance/v1",
        "created_utc": _utc_now(),
        "evidence_level": "offline_computed",
        "verified_against_real_device": False,
        "offline_computed": True,
        "simulated": False,
        "params": _params_dict(params),
        "dem": {
            "path": dem_info.path,
            "sha256": dem_info.sha256,
            "crs_wkt": dem_info.crs_wkt,
            "crs_epsg": dem_info.crs_epsg,
            "is_geographic": dem_info.is_geographic,
            "width": dem_info.width,
            "height": dem_info.height,
            "res_x": dem_info.res_x,
            "res_y": dem_info.res_y,
            "nodata": dem_info.nodata,
            "min_elev": dem_info.min_elev,
            "max_elev": dem_info.max_elev,
            "nodata_fraction": dem_info.nodata_fraction,
            "vertical_datum_known": dem_info.vertical_datum_known,
            "vertical_datum_note": dem_info.vertical_datum_note,
            "provisional": dem_info.provisional,
        },
        "analysis_grid": {
            "metric_crs": metric_crs,
            "width": analysis.width,
            "height": analysis.height,
            "res_x": analysis.res_x,
            "res_y": analysis.res_y,
            "cell_area_m2": cell_area,
            "reprojected": analysis.reprojected,
            "resampling": analysis.resampling,
            "source_crs": analysis.source_crs,
            "bounds_lonlat": list(extent_lonlat),
        },
        "engine": {
            "name": engine_name,
            "version": engine_version,
            "selection": params.engine,
        },
        "result": {
            "mask_cell_count": mask_cell_count,
            "area_from_cells_m2": area_from_cells,
            "area_m2": measure.area_m2,
            "perimeter_m": measure.perimeter_m,
            "vertex_count": measure.vertex_count,
            "part_count": measure.part_count,
            "hole_count": measure.hole_count,
            "flow_accum_cells": int(flow_accum_cells),
            "snap_distance_m": float(snap_distance_m),
            "outlet_is_sink": bool(outlet_is_sink),
            "truncated": bool(truncated),
            "outlet_vertex_index": outlet_vertex_index,
            "segment_count": len(segments),
            "simplify": simplify_report,
        },
        "determinism": {
            "fingerprint_sha256": fingerprint,
            "mask_sha256": mask_sha,
            "boundary_wkb_sha256": boundary_wkb_sha,
            "analysis_dem_sha256": dem_module.sha256_file(analysis.path),
            "params_sha256": _sha256_bytes(
                json.dumps(_params_dict(params), sort_keys=True, ensure_ascii=False).encode()
            ),
            "note": "相同输入+参数应得到相同 fingerprint/mask/boundary SHA256",
        },
        "libraries": _library_versions(),
        "data_sources": DATA_SOURCES,
        "warnings": list(warnings),
    }
    if snap_evidence:
        provenance["outlet_snap"] = snap_evidence
    if provenance_extra:
        provenance.update(provenance_extra)
    provenance_path = out_dir / PROVENANCE_JSON
    _json_dump(provenance_path, provenance)
    artifacts["provenance_json"] = str(provenance_path)

    result = WatershedResult(
        task_dir=str(out_dir),
        ok=True,
        boundary_geojson=str(boundary_path),
        mask_geotiff=str(mask_path),
        area_m2=measure.area_m2,
        perimeter_m=measure.perimeter_m,
        geometry_valid=bool(check.is_valid),
        multipart=bool(check.multipart),
        hole_count=int(check.hole_count),
        outlet_input=(float(params.outlet_lonlat[0]), float(params.outlet_lonlat[1])),
        outlet_used=(float(outlet_used_lonlat[0]), float(outlet_used_lonlat[1])),
        snap_distance_m=float(snap_distance_m),
        flow_accum_cells=int(flow_accum_cells),
        cell_area_m2=cell_area,
        metric_crs=metric_crs,
        engine_used=engine_name,
        engine_version=engine_version,
        analysis_extent_lonlat=tuple(float(v) for v in extent_lonlat),
        dem_info=dem_info.to_dict(),
        warnings=warnings,
        provenance=provenance,
        artifacts=artifacts,
        boundary_crs=CANONICAL_CRS,
        outlet_vertex_index=outlet_vertex_index,
        truncated=bool(truncated),
    )
    result_path = out_dir / RESULT_JSON
    _json_dump(result_path, result.to_dict())
    artifacts["result_json"] = str(result_path)

    # -- 概览复核图（失败只记 warning，不影响边界成果）----------------
    if extra.get("render_overview", True):
        try:
            from . import review as review_module

            figure = review_module.render_overview(
                result, out_dir=out_dir / REVIEW_DIR
            )
            artifacts["overview_png"] = figure.image_path
            _json_dump(result_path, result.to_dict())
        except Exception as exc:  # noqa: BLE001 - 图失败不阻断计算成果
            warnings.append(f"概览复核图生成失败（不影响边界成果）：{exc}")
            result.warnings = warnings
            provenance["warnings"] = list(warnings)
            _json_dump(provenance_path, provenance)
            _json_dump(result_path, result.to_dict())

    return result


# ---------------------------------------------------------------------------
# 引擎选择与运行
# ---------------------------------------------------------------------------


def get_engine(preference: str = "auto", *, grass_cmd: str | None = None) -> WatershedEngine:
    """按偏好返回引擎实例（契约 §5.2）。

    - ``native``：内置 numpy/rasterio D8 引擎（始终可用）；
    - ``grass``：GRASS 8.x 适配器；不可用抛 :class:`EngineUnavailableError`；
    - ``auto``：先 GRASS，不可用则 native（两者都不可用才报错）。
    """

    from .native import NativeEngine

    if preference not in {"auto", "native", "grass"}:
        raise DataValidationError(
            f"engine 只能是 auto/native/grass，收到 {preference!r}"
        )
    if preference == "native":
        return NativeEngine()

    from .grass import GrassEngine

    grass = GrassEngine(grass_cmd=grass_cmd)
    if preference == "grass":
        ok, reason = grass.available()
        if not ok:
            raise EngineUnavailableError(f"GRASS 引擎不可用：{reason}")
        return grass
    ok, _reason = grass.available()
    if ok:
        return grass
    return NativeEngine()


def _write_failure(
    out_dir: Path,
    params: WatershedParams | None,
    exc: BaseException,
    *,
    engine_name: str | None = None,
) -> WatershedResult:
    """写 failure.json 并返回 ok=False 的成果（worker 落终态用）。"""

    out_dir.mkdir(parents=True, exist_ok=True)
    code = getattr(exc, "code", None) or exc.__class__.__name__
    payload = {
        "schema": "ovital_mcp.watershed.failure/v1",
        "ok": False,
        "created_utc": _utc_now(),
        "error": str(exc),
        "error_code": code,
        "error_type": exc.__class__.__name__,
        "engine": engine_name,
        "params": _params_dict(params) if params is not None else None,
        "traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
        "evidence_level": "offline_computed",
        "verified_against_real_device": False,
    }
    failure_path = out_dir / FAILURE_JSON
    _json_dump(failure_path, payload)
    return WatershedResult(
        task_dir=str(out_dir),
        ok=False,
        error=str(exc),
        error_code=code,
        engine_used=engine_name or "",
        warnings=[f"计算失败：{exc}"],
        provenance={
            "schema": "ovital_mcp.watershed.provenance/v1",
            "created_utc": payload["created_utc"],
            "evidence_level": "offline_computed",
            "verified_against_real_device": False,
            "offline_computed": True,
            "simulated": False,
            "error": str(exc),
            "error_code": code,
        },
        artifacts={"failure_json": str(failure_path)},
    )


def validate_params(params: WatershedParams) -> None:
    """参数级校验：非法参数抛 ``DataValidationError``（契约要求）。"""

    if not isinstance(params, WatershedParams):
        raise DataValidationError("params 必须是 WatershedParams")
    if not params.dem_path:
        raise DataValidationError("dem_path 不能为空")
    dem_path = Path(params.dem_path)
    if not dem_path.is_file():
        raise DataValidationError(f"DEM 文件不存在：{params.dem_path}")
    lon, lat = verify_canonical_lonlat(*params.outlet_lonlat)
    del lon, lat
    if params.engine not in {"auto", "native", "grass"}:
        raise DataValidationError(
            f"engine 只能是 auto/native/grass，收到 {params.engine!r}"
        )
    if not (params.snap_radius_m >= 0):
        raise DataValidationError(
            f"snap_radius_m 必须 ≥0（0 表示关闭吸附），收到 {params.snap_radius_m!r}"
        )
    if params.target_res_m is not None and not (params.target_res_m > 0):
        raise DataValidationError(
            f"target_res_m 必须为正数或 None，收到 {params.target_res_m!r}"
        )
    if params.min_accum_cells < 1:
        raise DataValidationError(
            f"min_accum_cells 必须 ≥1，收到 {params.min_accum_cells!r}"
        )
    if params.simplify_tolerance_m < 0:
        raise DataValidationError(
            f"simplify_tolerance_m 不能为负，收到 {params.simplify_tolerance_m!r}"
        )
    if params.metric_crs:
        from .crs import require_metric_crs

        require_metric_crs(params.metric_crs, what="metric_crs")
    extra = params.extra or {}
    if "max_cells" in extra:
        try:
            value = int(extra["max_cells"])
        except (TypeError, ValueError) as exc:
            raise DataValidationError("extra.max_cells 必须是整数") from exc
        if value <= 0:
            raise DataValidationError("extra.max_cells 必须为正整数")
    if "n_segments" in extra and int(extra["n_segments"]) < 1:
        raise DataValidationError("extra.n_segments 必须 ≥1")
    if "resampling" in extra and str(extra["resampling"]) not in RESAMPLING:
        raise DataValidationError(
            f"extra.resampling={extra['resampling']!r} 不受支持，允许 {sorted(RESAMPLING)}"
        )


def run_watershed(
    params: WatershedParams, out_dir: str | Path
) -> WatershedResult:
    """运行集水区计算并返回成果（契约 §5.2 唯一入口）。

    - 参数非法：抛 ``DataValidationError``（调用方/worker 负责转终态）；
    - 计算失败（引擎不可用、DEM 无法解析、几何异常等）：写 ``failure.json``，
      返回 ``ok=False`` 的成果，**不抛异常**（Lead 的 worker 契约）。
    """

    out_dir = Path(out_dir)
    validate_params(params)
    engine: WatershedEngine | None = None
    try:
        engine = get_engine(
            params.engine, grass_cmd=(params.extra or {}).get("grass_cmd")
        )
        return engine.run(params, out_dir)
    except Exception as exc:  # noqa: BLE001 - 失败必须可观察，不伪装成功
        return _write_failure(
            out_dir,
            params,
            exc,
            engine_name=getattr(engine, "name", None),
        )


def load_result(task_dir: str | Path) -> WatershedResult:
    """从任务目录读取成果（``result.json``；失败任务回退 ``failure.json``）。"""

    task_dir = Path(task_dir)
    result_path = task_dir / RESULT_JSON
    if result_path.is_file():
        return WatershedResult.from_dict(
            json.loads(result_path.read_text(encoding="utf-8"))
        )
    failure_path = task_dir / FAILURE_JSON
    if failure_path.is_file():
        data = json.loads(failure_path.read_text(encoding="utf-8"))
        return WatershedResult(
            task_dir=str(task_dir),
            ok=False,
            error=data.get("error"),
            error_code=data.get("error_code"),
            engine_used=data.get("engine") or "",
            warnings=[f"计算失败：{data.get('error')}"],
            provenance={"failure": data},
            artifacts={"failure_json": str(failure_path)},
        )
    raise DataValidationError(f"任务目录 {task_dir} 中没有 result.json/failure.json")
