"""边界分段：把集水区边界切成可编号、可复现复核的若干段。

对应契约 ``docs/architecture.md`` §5.2 的 segment 部分，并实现 Lead 补充的
``list_segments``（worker/工具层按段复核用）。

关键约定：

- **段号从 1 开始，1 号段起点为出口**（出口顶点索引来自边界 GeoJSON 属性
  ``outlet_vertex_index``，或按 ``outlet_used`` 距离最近顶点现算）。
- **锚点 ID 稳定可复现**：``A-<段号:02d>-<原始顶点索引:05d>``，直接引用
  边界环上的顶点序号，不依赖浮点累计误差。
- 分段在米制 CRS 下按长度进行；段端点吸附到**原始顶点**，因此每段的
  ``length_m`` 是沿边界的折线长度，可逐段求和复算。
- 高程统计需要 DEM；没有 DEM 时 ``mean_elev_m=None``（显式缺失，不猜）。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from shapely.geometry import LineString, shape

from ..coordinates import metric_crs_for, transform_geometry
from ..errors import DataValidationError, GeometryError
from . import dem as dem_module
from .geometry import as_polygonal


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Anchor:
    """分段锚点（稳定 ID + 原始顶点索引）。"""

    anchor_id: str
    lonlat: tuple[float, float]
    vertex_index: int
    distance_along_m: float


@dataclass
class BoundarySegment:
    """一段边界（契约 §5.2）。"""

    segment_id: int
    start_anchor: Anchor
    end_anchor: Anchor
    vertices: list[tuple[float, float]]
    length_m: float
    mean_elev_m: float | None
    min_elev_m: float | None
    max_elev_m: float | None
    vertex_count: int
    bbox_lonlat: tuple[float, float, float, float]
    start_vertex_index: int = 0
    end_vertex_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


# ---------------------------------------------------------------------------
# 内部：读取边界与构建环路径
# ---------------------------------------------------------------------------


def _load_boundary(
    boundary_geojson: str | Path | Any,
) -> tuple[Any, dict[str, Any]]:
    """读取边界几何与属性。

    支持：shapely 几何、GeoJSON 字符串、GeoJSON 文件路径
    （Feature / FeatureCollection / 裸 geometry）。返回 ``(geom, properties)``。
    """

    if isinstance(boundary_geojson, (str, Path)):
        raw = str(boundary_geojson)
        if raw.lstrip().startswith("{"):
            data = json.loads(raw)
        else:
            path = Path(raw)
            if not path.is_file():
                raise DataValidationError(f"边界 GeoJSON 不存在：{path}")
            data = json.loads(path.read_text(encoding="utf-8"))
    elif isinstance(boundary_geojson, dict):
        data = boundary_geojson
    else:
        return as_polygonal(boundary_geojson), {}

    properties: dict[str, Any] = {}
    if data.get("type") == "FeatureCollection":
        features = data.get("features") or []
        if not features:
            raise DataValidationError("GeoJSON FeatureCollection 为空")
        feature = features[0]
        properties = dict(feature.get("properties") or {})
        geom = shape(feature["geometry"])
    elif data.get("type") == "Feature":
        properties = dict(data.get("properties") or {})
        geom = shape(data["geometry"])
    elif data.get("type") in {"Polygon", "MultiPolygon"}:
        geom = shape(data)
    else:
        raise DataValidationError(
            f"不支持的 GeoJSON 类型：{data.get('type')!r}（需要 Polygon/MultiPolygon）"
        )
    return as_polygonal(geom), properties


def _main_ring(boundary: Any) -> list[tuple[float, float]]:
    """取面积最大部件的**外环顶点**（不含闭合点），顺序保持原始环方向。"""

    poly = as_polygonal(boundary)
    if hasattr(poly, "geoms"):
        poly = max(poly.geoms, key=lambda p: p.area)
    coords = list(poly.exterior.coords)
    if len(coords) >= 2 and coords[0] == coords[-1]:
        coords = coords[:-1]
    if len(coords) < 3:
        raise GeometryError("边界顶点不足 3 个，无法分段")
    return [(float(x), float(y)) for x, y in coords]


def _metric_vertices(
    lonlat_vertices: Sequence[tuple[float, float]], metric_crs: str
) -> list[tuple[float, float]]:
    ring = LineString(list(lonlat_vertices) + [lonlat_vertices[0]])
    metric = transform_geometry(ring, "EPSG:4490", metric_crs)
    coords = list(metric.coords)
    return [(float(x), float(y)) for x, y in coords[:-1]]


# ---------------------------------------------------------------------------
# 分段核心
# ---------------------------------------------------------------------------


def _split_indices(
    cumulative: Sequence[float],
    total: float,
    n_segments: int,
) -> list[int]:
    """按等长目标把环切成 ``n_segments`` 段，端点吸附到原始顶点索引。"""

    vertex_count = len(cumulative)
    if n_segments < 1:
        raise DataValidationError(f"n_segments 必须 ≥1，收到 {n_segments}")
    n = min(n_segments, vertex_count - 1) if vertex_count > 1 else 1
    if n < 1:  # pragma: no cover
        raise GeometryError("边界顶点不足，无法分段")
    # 目标分割距离（沿环累计长度）；vertex_count = 顶点数 + 1（闭合点）
    targets = [total * k / n for k in range(1, n)]
    split_indices: list[int] = []
    cursor = 0
    last_allowed = vertex_count - 2  # 分割点不能是闭合点本身
    for target in targets:
        # 找第一个累计长度 ≥ target 的顶点
        index = cursor
        while index < vertex_count and cumulative[index] < target:
            index += 1
        index = min(index, last_allowed)
        # 保证严格递增，且每段至少 1 条边
        index = max(index, cursor + 1)
        if index > last_allowed:
            break
        split_indices.append(index)
        cursor = index
    return split_indices


def _elevation_stats(
    lonlat_points: Sequence[tuple[float, float]],
    dem_path: str | Path | None,
) -> tuple[float | None, float | None, float | None]:
    """采样一段边界的高程统计（无 DEM 时返回三个 None）。"""

    if not dem_path:
        return None, None, None
    values = [
        value
        for value in dem_module.sample_dem_lonlat(dem_path, lonlat_points)
        if value is not None
    ]
    if not values:
        return None, None, None
    return (
        float(sum(values) / len(values)),
        float(min(values)),
        float(max(values)),
    )


def _build_segments(
    lonlat_vertices: Sequence[tuple[float, float]],
    metric_vertices: Sequence[tuple[float, float]],
    *,
    metric_crs: str,
    n_segments: int,
    start_index: int,
    dem_path: str | Path | None,
) -> list[BoundarySegment]:
    """从顶点序列构建分段列表（含锚点 ID、长度、高程）。"""

    count = len(lonlat_vertices)
    if count < 3:
        raise GeometryError("边界顶点不足 3 个，无法分段")

    # 环的累计长度：从 start_index 开始绕一圈（末尾补回起点，便于切片到闭合边）
    ordered_lonlat = [lonlat_vertices[(start_index + i) % count] for i in range(count)]
    ordered_metric = [metric_vertices[(start_index + i) % count] for i in range(count)]
    ordered_lonlat_closed = ordered_lonlat + [ordered_lonlat[0]]
    ordered_metric_closed = ordered_metric + [ordered_metric[0]]
    cumulative = [0.0]
    for i in range(1, len(ordered_metric_closed)):
        cumulative.append(
            cumulative[-1]
            + math.dist(ordered_metric_closed[i - 1], ordered_metric_closed[i])
        )
    total = cumulative[-1]
    if total <= 0:
        raise GeometryError("边界周长为 0，无法分段")

    split_indices = _split_indices(cumulative, total, n_segments)
    # 段的端点（有序序列中的索引，取值 0..count）
    boundaries = [0, *split_indices, count]
    segments: list[BoundarySegment] = []
    for seg_index in range(len(boundaries) - 1):
        begin = boundaries[seg_index]
        end = boundaries[seg_index + 1]
        if end <= begin:  # pragma: no cover - _split_indices 已保证递增
            continue
        seg_vertices = ordered_lonlat_closed[begin : end + 1]
        if len(seg_vertices) < 2:  # pragma: no cover
            continue
        start_original_index = (start_index + begin) % count
        end_original_index = (start_index + end) % count
        segment_id = seg_index + 1
        start_anchor = Anchor(
            anchor_id=f"A-{segment_id:02d}-{start_original_index:05d}",
            lonlat=ordered_lonlat[begin],
            vertex_index=start_original_index,
            distance_along_m=cumulative[begin],
        )
        end_anchor = Anchor(
            anchor_id=f"A-{segment_id:02d}-{end_original_index:05d}",
            lonlat=ordered_lonlat[end % count],
            vertex_index=end_original_index,
            distance_along_m=cumulative[end],
        )
        length_m = cumulative[end] - cumulative[begin]
        # 高程采样点：顶点 + 边中点（边较长时补充，避免漏掉谷底）
        sample_points: list[tuple[float, float]] = list(seg_vertices)
        for i in range(len(seg_vertices) - 1):
            p, q = seg_vertices[i], seg_vertices[i + 1]
            sample_points.append(((p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0))
        mean_elev, min_elev, max_elev = _elevation_stats(sample_points, dem_path)
        xs = [p[0] for p in seg_vertices]
        ys = [p[1] for p in seg_vertices]
        segments.append(
            BoundarySegment(
                segment_id=segment_id,
                start_anchor=start_anchor,
                end_anchor=end_anchor,
                vertices=[(float(x), float(y)) for x, y in seg_vertices],
                length_m=float(length_m),
                mean_elev_m=mean_elev,
                min_elev_m=min_elev,
                max_elev_m=max_elev,
                vertex_count=len(seg_vertices),
                bbox_lonlat=(min(xs), min(ys), max(xs), max(ys)),
                start_vertex_index=start_original_index,
                end_vertex_index=end_original_index,
            )
        )
    return segments


def segment_boundary(
    boundary: str | Path | Any,
    *,
    n_segments: int | None = None,
    max_len_m: float | None = None,
    metric_crs: str | None = None,
    outlet_lonlat: tuple[float, float] | None = None,
    outlet_vertex_index: int | None = None,
    dem_path: str | Path | None = None,
) -> list[BoundarySegment]:
    """把集水区边界切成编号段（契约 §5.2）。

    - ``n_segments``：指定段数；``max_len_m``：按最大段长换算段数；
      两者都为空时默认 8 段。两者都给时以 ``n_segments`` 为准并忽略
      ``max_len_m``（显式优先级）。
    - 1 号段起点优先用 ``outlet_lonlat``，其次 ``outlet_vertex_index``，
      最后回退到顶点 0；边界来自 GeoJSON 时自动读取其属性。
    - 段端点吸附到原始顶点，锚点 ID 形如 ``A-01-00042``。
    """

    geom, properties = _load_boundary(boundary)
    lonlat_vertices = _main_ring(geom)
    if metric_crs is None:
        metric_crs = metric_crs_for(
            float(lonlat_vertices[0][0]), float(lonlat_vertices[0][1]), kind="utm"
        )
    metric_vertices = _metric_vertices(lonlat_vertices, metric_crs)

    if outlet_lonlat is None:
        raw = properties.get("outlet_used")
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            outlet_lonlat = (float(raw[0]), float(raw[1]))
    if outlet_vertex_index is None:
        raw_index = properties.get("outlet_vertex_index")
        if isinstance(raw_index, int):
            outlet_vertex_index = raw_index
    if dem_path is None:
        raw_dem = properties.get("dem_path")
        if isinstance(raw_dem, str) and raw_dem:
            dem_path = raw_dem

    start_index = _resolve_start_index(
        lonlat_vertices,
        metric_vertices,
        metric_crs,
        outlet_lonlat=outlet_lonlat,
        outlet_vertex_index=outlet_vertex_index,
    )

    total_length = _ring_perimeter_m(metric_vertices)
    if n_segments is None:
        if max_len_m is not None and max_len_m > 0:
            n_segments = max(1, int(math.ceil(total_length / float(max_len_m))))
        else:
            n_segments = 8

    return _build_segments(
        lonlat_vertices,
        metric_vertices,
        metric_crs=metric_crs,
        n_segments=int(n_segments),
        start_index=start_index,
        dem_path=dem_path,
    )


def _ring_perimeter_m(metric_vertices: Sequence[tuple[float, float]]) -> float:
    closed = list(metric_vertices) + [metric_vertices[0]]
    return float(
        sum(math.dist(closed[i - 1], closed[i]) for i in range(1, len(closed)))
    )


def _resolve_start_index(
    lonlat_vertices: Sequence[tuple[float, float]],
    metric_vertices: Sequence[tuple[float, float]],
    metric_crs: str,
    *,
    outlet_lonlat: tuple[float, float] | None,
    outlet_vertex_index: int | None,
) -> int:
    """确定 1 号段起点顶点索引（出口优先）。"""

    if outlet_lonlat is not None:
        from pyproj import Transformer

        transformer = Transformer.from_crs("EPSG:4490", metric_crs, always_xy=True)
        ox, oy = transformer.transform(float(outlet_lonlat[0]), float(outlet_lonlat[1]))
        best_index = 0
        best_dist = math.inf
        for index, (x, y) in enumerate(metric_vertices):
            dist = math.hypot(x - ox, y - oy)
            if dist < best_dist - 1e-12:
                best_dist = dist
                best_index = index
        return best_index
    if outlet_vertex_index is not None and 0 <= int(outlet_vertex_index) < len(
        lonlat_vertices
    ):
        return int(outlet_vertex_index)
    return 0


def list_segments(
    boundary_geojson: str | Path | Any,
    *,
    n_segments: int = 8,
    metric_crs: str | None = None,
    dem_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """worker/工具层接口：返回分段字典列表（``segment_id`` 从 1 开始）。

    每个 dict 至少包含：``segment_id``、``start_anchor_id``、
    ``end_anchor_id``、``length_m``、``vertex_count``、``mean_elev_m``、
    ``bbox_lonlat``；另附顶点坐标、高程 min/max、原始顶点索引等，供复核图
    与报告直接使用。1 号段起点为出口（从边界 GeoJSON 属性读取）。
    """

    segments = segment_boundary(
        boundary_geojson,
        n_segments=n_segments,
        metric_crs=metric_crs,
        dem_path=dem_path,
    )
    return [
        {
            "segment_id": seg.segment_id,
            "start_anchor_id": seg.start_anchor.anchor_id,
            "end_anchor_id": seg.end_anchor.anchor_id,
            "length_m": seg.length_m,
            "vertex_count": seg.vertex_count,
            "mean_elev_m": seg.mean_elev_m,
            "min_elev_m": seg.min_elev_m,
            "max_elev_m": seg.max_elev_m,
            "bbox_lonlat": list(seg.bbox_lonlat),
            "vertices": [list(v) for v in seg.vertices],
            "start_vertex_index": seg.start_vertex_index,
            "end_vertex_index": seg.end_vertex_index,
        }
        for seg in segments
    ]


def choose_segment_count(
    perimeter_m: float,
    *,
    min_segments: int = 4,
    max_segments: int = 8,
    target_length_m: float = 1500.0,
) -> int:
    """按周长给出复核用的段数（默认 4–8 段，每段约 1.5 km）。"""

    if perimeter_m <= 0:
        return min_segments
    count = int(round(perimeter_m / max(target_length_m, 1e-6)))
    return max(min_segments, min(max_segments, count))


def segments_to_geojson(segments: Sequence[BoundarySegment]) -> dict[str, Any]:
    """把分段转成 GeoJSON FeatureCollection（EPSG:4490 LineString）。"""

    features = []
    for seg in segments:
        xs = [vertex[0] for vertex in seg.vertices]
        ys = [vertex[1] for vertex in seg.vertices]
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "segment_id": seg.segment_id,
                    # 顶点 bbox（EPSG:4490 经纬度，字段名显式带 lonlat，避免与
                    # 米制 bbox 混用；F-11 回归项）
                    "bbox_lonlat": [min(xs), min(ys), max(xs), max(ys)],
                    "start_anchor_id": seg.start_anchor.anchor_id,
                    "end_anchor_id": seg.end_anchor.anchor_id,
                    "length_m": seg.length_m,
                    "mean_elev_m": seg.mean_elev_m,
                    "min_elev_m": seg.min_elev_m,
                    "max_elev_m": seg.max_elev_m,
                    "vertex_count": seg.vertex_count,
                    "start_vertex_index": seg.start_vertex_index,
                    "end_vertex_index": seg.end_vertex_index,
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [list(v) for v in seg.vertices],
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}
