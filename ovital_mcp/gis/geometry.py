"""集水区边界的几何校验、量算、受控简化与锚点修订。

对应契约 ``docs/architecture.md`` §5.2 的 geometry 部分。

原则（来自任务书质量红线）：

- **校验只报告、不静默改**：``validate_geometry`` 返回完整检查结果；
  修复必须由调用方显式决定，并写进 warnings/报告。
- **面积/周长只在米制 CRS 下计算**：绝不对 EPSG:4490 的度坐标直接算面积。
- **简化只在明确容差内**，且保护出口与指定锚点；任何改动写进报告。
- 禁止为了"看起来圆滑/贴近目标面积"移动分水岭。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np
import shapely
from pyproj import CRS
from shapely import is_ccw, make_valid
from shapely.geometry import (
    GeometryCollection,
    LinearRing,
    MultiPolygon,
    Point,
    Polygon,
    shape,
)
from shapely.geometry.polygon import orient
from shapely.ops import substring
from shapely.validation import explain_validity

from ..coordinates import metric_crs_for, transform_geometry
from ..errors import GeometryError

#: 紧凑度 ``4πA/P²`` 低于该值时提示"细长片"（圆为 1，细长条接近 0）。
SLIVER_COMPACTNESS_THRESHOLD = 0.05
#: 最小外接矩形短边/长边低于该值也提示细长片。
SLIVER_THINNESS_THRESHOLD = 0.02
#: 简化容差上限：等效半径（``sqrt(A/π)``）的比例。
MAX_SIMPLIFY_RADIUS_RATIO = 0.01


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class GeometryCheck:
    """几何校验结果（契约 §5.2）。"""

    is_valid: bool
    is_closed: bool
    is_simple: bool
    self_intersection: bool
    duplicate_vertex_count: int
    ring_orientation: list[str]
    rfc7946_orientation_ok: bool
    sliver_suspected: bool
    compactness: float | None
    multipart: bool
    part_count: int
    hole_count: int
    issues: list[str] = field(default_factory=list)
    metric_crs: str | None = None
    validity_explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SimplificationResult:
    """``simplify_preserving`` 的返回值（兼容两种调用风格）。

    既支持 ``geometry, report = simplify_preserving(...)`` 解包（本包内部与
    测试使用），也支持 ``result.report`` 属性访问（tools 层直接序列化时，
    ``jsonable`` 会得到 ``{"geometry": "<WKT>", "report": {...}}``）。
    """

    geometry: Polygon | MultiPolygon
    report: dict[str, Any]

    def __iter__(self):
        yield self.geometry
        yield self.report

    def __len__(self) -> int:
        return 2

    def __getitem__(self, index: int):
        return (self.geometry, self.report)[index]


@dataclass
class GeometryMeasure:
    """几何量算结果（水平投影面积，非坡面面积）。"""

    area_m2: float
    perimeter_m: float
    vertex_count: int
    part_count: int
    hole_count: int
    metric_crs: str
    method: str
    area_ha: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def as_polygonal(geom: Any) -> Polygon | MultiPolygon:
    """把输入规整成面几何；非面几何显式报错。"""

    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    if isinstance(geom, dict):
        geom = shape(geom)
    if isinstance(geom, GeometryCollection):
        polys = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon))]
        if not polys:
            raise GeometryError("几何集合中没有任何面要素")
        merged = shapely.union_all(polys)
        if not isinstance(merged, (Polygon, MultiPolygon)):  # pragma: no cover
            raise GeometryError("几何集合合并后不是面要素")
        return merged
    raise GeometryError(
        f"需要 Polygon/MultiPolygon，收到 {type(geom).__name__}；集水区边界必须是面"
    )


def iter_polygons(geom: Any) -> list[Polygon]:
    """展开为单部件 Polygon 列表。"""

    poly = as_polygonal(geom)
    if isinstance(poly, Polygon):
        return [poly]
    return [p for p in poly.geoms if isinstance(p, Polygon)]


def iter_rings(geom: Any) -> list[LinearRing]:
    """返回所有环（外环 + 内环），顺序与几何一致。"""

    rings: list[LinearRing] = []
    for poly in iter_polygons(geom):
        rings.append(poly.exterior)
        rings.extend(poly.interiors)
    return rings


def vertex_count(geom: Any) -> int:
    """顶点总数（含各环闭合点，与 OGC/GeoJSON 表示一致）。"""

    return int(sum(len(ring.coords) for ring in iter_rings(geom)))


def exterior_vertices_lonlat(geom: Any) -> list[tuple[float, float]]:
    """外环顶点 ``(lon, lat)`` 列表（去掉闭合重复点）。"""

    poly = as_polygonal(geom)
    if isinstance(poly, MultiPolygon):
        # 多部件时取面积最大部件的顶点，用于锚点定位
        poly = max(poly.geoms, key=lambda p: p.area)
    coords = list(poly.exterior.coords)
    if len(coords) >= 2 and coords[0] == coords[-1]:
        coords = coords[:-1]
    return [(float(x), float(y)) for x, y in coords]


def geometry_sha256(geom: Any) -> str:
    """几何 WKB 的 SHA256（用于可复现性指纹，与文件元数据无关）。"""

    return hashlib.sha256(shapely.to_wkb(geom, byte_order=1)).hexdigest()


def _derive_metric_crs(geom: Any) -> str | None:
    """未显式给米制 CRS 时，按几何质心自动选 UTM 分带。"""

    try:
        poly = as_polygonal(geom)
        if poly.is_empty:
            return None
        centroid = poly.centroid
        return metric_crs_for(float(centroid.x), float(centroid.y), kind="utm")
    except Exception:  # noqa: BLE001 - 自动推断失败不阻断校验
        return None


def normalize_rfc7946(geom: Any) -> tuple[Polygon | MultiPolygon, bool]:
    """按 RFC 7946 右手定则规范环方向（外环 CCW、内环 CW）。

    只改环方向，不改顶点坐标与拓扑；返回 ``(geometry, changed)``。
    """

    poly = as_polygonal(geom)
    normalized = orient(poly, sign=1.0)
    changed = shapely.to_wkb(normalized, byte_order=1) != shapely.to_wkb(
        poly, byte_order=1
    )
    return normalized, changed


def nearest_vertex_index(geom: Any, point: Point, *, metric_crs: str) -> int:
    """外环顶点中距给定点最近的索引（在米制 CRS 下度量）。

    ``point`` 是 EPSG:4490 的 ``Point``；返回去闭合点后的顶点索引。
    距离相同按索引升序，保证确定性。
    """

    poly = as_polygonal(geom)
    if isinstance(poly, MultiPolygon):
        poly = max(poly.geoms, key=lambda p: p.area)
    coords = list(poly.exterior.coords)
    if len(coords) >= 2 and coords[0] == coords[-1]:
        coords = coords[:-1]
    metric_poly = transform_geometry(poly, "EPSG:4490", metric_crs)
    metric_point = transform_geometry(point, "EPSG:4490", metric_crs)
    metric_coords = list(metric_poly.exterior.coords)
    if len(metric_coords) >= 2 and metric_coords[0] == metric_coords[-1]:
        metric_coords = metric_coords[:-1]
    if not metric_coords:  # pragma: no cover
        raise GeometryError("边界没有顶点")
    px, py = float(metric_point.x), float(metric_point.y)
    best_index = 0
    best_dist = math.inf
    for index, (x, y) in enumerate(metric_coords):
        dist = math.hypot(x - px, y - py)
        if dist < best_dist - 1e-12:
            best_dist = dist
            best_index = index
    return best_index


def pick_component_containing(
    geom: Any, point_xy: tuple[float, float]
) -> Polygon | MultiPolygon:
    """从多部件面中挑出包含（或最接近）给定点的部件。

    用于 native 引擎矢量化后"取含出口连通部件"的兜底保护。
    """

    poly = as_polygonal(geom)
    if isinstance(poly, Polygon):
        return poly
    point = Point(point_xy)
    for part in poly.geoms:
        if part.covers(point) or part.contains(point):
            return part
    # 出口点可能恰好落在像元边界：退化为"距离最近"的部件
    return min(poly.geoms, key=lambda p: p.distance(point))


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _ring_duplicates(ring: LinearRing) -> int:
    coords = list(ring.coords)
    return int(sum(1 for a, b in zip(coords, coords[1:]) if a == b))


def _ring_closed(ring: LinearRing) -> bool:
    coords = list(ring.coords)
    return bool(coords) and coords[0] == coords[-1]


def validate_geometry(geom: Any, *, metric_crs: str | None = None) -> GeometryCheck:
    """全面校验集水区边界几何。

    检查项：有效性/自交、环闭合、环方向（RFC 7946）、重复点、细长片、
    多部件、洞。所有问题写入 ``issues``（中文说明），不修改几何。
    """

    issues: list[str] = []
    poly = as_polygonal(geom)
    crs = metric_crs or _derive_metric_crs(poly)
    is_valid = bool(poly.is_valid)
    explanation = "" if is_valid else explain_validity(poly)

    rings = iter_rings(poly)
    is_closed = all(_ring_closed(r) for r in rings) if rings else False
    if not is_closed:
        issues.append("存在未闭合的环（首尾顶点不相等）")

    ring_orientations: list[str] = []
    for ring in rings:
        ring_orientations.append("ccw" if is_ccw(ring) else "cw")

    # RFC 7946：外环 CCW，内环 CW
    rfc_ok = True
    offset = 0
    for part in iter_polygons(poly):
        exterior_is_ccw = ring_orientations[offset] == "ccw"
        if not exterior_is_ccw:
            rfc_ok = False
        for hole_idx in range(len(part.interiors)):
            if ring_orientations[offset + 1 + hole_idx] != "cw":
                rfc_ok = False
        offset += 1 + len(part.interiors)
    if not rfc_ok:
        issues.append("环方向不符合 RFC 7946 右手定则（外环应 CCW、内环应 CW）")

    is_simple = all(
        LinearRing(ring.coords).is_simple for ring in rings
    ) if rings else False
    self_intersection = (not is_simple) or (
        "Self-intersection" in explanation or "self-intersection" in explanation
    )
    if self_intersection:
        issues.append(f"存在自相交：{explanation or '环不自交检查失败'}")

    duplicates = sum(_ring_duplicates(ring) for ring in rings)
    if duplicates:
        issues.append(f"存在 {duplicates} 个连续重复顶点")

    hole_count = int(sum(len(p.interiors) for p in iter_polygons(poly)))
    part_count = len(iter_polygons(poly))
    multipart = part_count > 1
    if multipart:
        issues.append(f"多部件边界（{part_count} 个部件）：集水区应连通，需人工确认")
    if hole_count:
        issues.append(
            f"边界含 {hole_count} 个洞：可能来自内部洼地/NoData 空洞，需人工确认"
        )

    compactness: float | None = None
    sliver = False
    if crs and not poly.is_empty:
        try:
            metric_geom = transform_geometry(poly, "EPSG:4490", crs)
            areas = [p.area for p in iter_polygons(metric_geom)]
            perims = [p.length for p in iter_polygons(metric_geom)]
            compact_values = [
                4.0 * math.pi * a / (p * p)
                for a, p in zip(areas, perims)
                if p > 0 and a > 0
            ]
            if compact_values:
                compactness = float(min(compact_values))
                if compactness < SLIVER_COMPACTNESS_THRESHOLD:
                    sliver = True
                    issues.append(
                        f"疑似细长片：紧凑度 {compactness:.4f} < "
                        f"{SLIVER_COMPACTNESS_THRESHOLD}"
                    )
            for part in iter_polygons(metric_geom):
                rect = part.minimum_rotated_rectangle
                if isinstance(rect, Polygon) and not rect.is_empty:
                    coords = list(rect.exterior.coords)
                    sides = [
                        math.dist(coords[i], coords[i + 1])
                        for i in range(min(4, len(coords) - 1))
                    ]
                    long_side, short_side = max(sides), min(sides)
                    if long_side > 0 and short_side / long_side < SLIVER_THINNESS_THRESHOLD:
                        sliver = True
                        issues.append(
                            f"疑似细长片：最小外接矩形短/长边比 "
                            f"{short_side / long_side:.4f}"
                        )
                        break
        except Exception as exc:  # noqa: BLE001
            issues.append(f"米制量算失败，无法判断细长片：{exc}")

    return GeometryCheck(
        is_valid=is_valid,
        is_closed=is_closed,
        is_simple=bool(is_simple),
        self_intersection=bool(self_intersection),
        duplicate_vertex_count=int(duplicates),
        ring_orientation=ring_orientations,
        rfc7946_orientation_ok=bool(rfc_ok),
        sliver_suspected=bool(sliver),
        compactness=compactness,
        multipart=multipart,
        part_count=int(part_count),
        hole_count=hole_count,
        issues=issues,
        metric_crs=crs,
        validity_explanation=explanation,
    )


def measure(
    geom: Any, metric_crs: str, *, source_crs: str = "EPSG:4490"
) -> GeometryMeasure:
    """在米制 CRS 下量算水平面积/周长/顶点/部件/洞。

    ``source_crs`` 是输入几何的 CRS（默认 EPSG:4490；引擎内部几何已是米制时
    传同一个 CRS 可避免无谓往返变换）。明确标注方法：shapely 平面多边形
    面积（投影面积），**不是**坡面面积，也不含大地水准面/椭球面修正。
    """

    parsed = CRS.from_user_input(metric_crs)
    if parsed.is_geographic:
        raise GeometryError(f"量算 CRS {metric_crs} 是地理坐标系，不能算面积")
    poly = as_polygonal(geom)
    if CRS.from_user_input(source_crs) == parsed:
        metric_geom = poly
    else:
        metric_geom = transform_geometry(poly, source_crs, metric_crs)
    area = float(metric_geom.area)
    perimeter = float(metric_geom.length)
    parts = iter_polygons(metric_geom)
    return GeometryMeasure(
        area_m2=area,
        perimeter_m=perimeter,
        vertex_count=vertex_count(poly),
        part_count=len(parts),
        hole_count=int(sum(len(p.interiors) for p in parts)),
        metric_crs=str(metric_crs),
        method=(
            "shapely 2.x 平面多边形面积/周长，在投影 CRS "
            f"{metric_crs} 下计算；口径为水平投影面积（非坡面面积）"
        ),
        area_ha=area / 10000.0,
    )


# ---------------------------------------------------------------------------
# 受控简化
# ---------------------------------------------------------------------------


def _insert_protected_points(
    poly_metric: Polygon,
    protected_metric: Sequence[tuple[float, float]],
    *,
    tolerance_m: float,
) -> tuple[Polygon, list[dict[str, Any]], list[dict[str, Any]]]:
    """把保护点插回简化后的外环（仅在偏差超过 0.5×容差时插入）。

    只处理**本来就在边界附近**的保护点（距离 ≤ 2×容差）：集水区出口可能在
    流域内部（例如碗心 sink），把内部点硬插进边界会造成 300 m 级尖刺并改变
    面积——这不是"保护锚点"，而是破坏几何。明显不在边界上的保护点会被跳过
    并写入报告，由调用方决定是否需要别的锚定方式。
    """

    inserted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if not protected_metric:
        return poly_metric, inserted, skipped
    coords = list(poly_metric.exterior.coords)
    ring = LinearRing(coords)
    for px, py in protected_metric:
        point = Point(px, py)
        distance = ring.distance(point)
        if distance > tolerance_m * 2.0:
            skipped.append({"point": [px, py], "distance_to_boundary_m": float(distance)})
            continue
        if distance <= tolerance_m * 0.5:
            continue
        # 找到最近的边，把保护点插入到该边之后
        best_index = 0
        best_dist = math.inf
        for index in range(len(coords) - 1):
            segment = shapely.geometry.LineString([coords[index], coords[index + 1]])
            d = segment.distance(point)
            if d < best_dist:
                best_dist = d
                best_index = index
        coords.insert(best_index + 1, (px, py))
        inserted.append({"point": [px, py], "offset_m": float(best_dist)})
        ring = LinearRing(coords)
    if not inserted:
        return poly_metric, inserted, skipped
    holes = [LinearRing(h.coords) for h in poly_metric.interiors]
    revised = Polygon(coords, holes)
    if not revised.is_valid:
        raise GeometryError(
            "简化后插回保护点导致几何无效；已放弃简化（不做静默拓扑修复）"
        )
    return revised, inserted, skipped


def simplify_preserving(
    geom: Any,
    tolerance_m: float,
    protected_points: Iterable[tuple[float, float]] = (),
    *,
    metric_crs: str | None = None,
) -> SimplificationResult:
    """在明确容差内简化边界，并保护出口/锚点。

    返回 :class:`SimplificationResult`（支持 ``geometry, report = ...`` 解包，
    也支持 ``result.report`` 属性访问）。报告包含简化前后顶点数、
    面积、周长、Hausdorff 偏差、保护点插入情况与 warnings。容差超过
    ``max(1.0, 1% × 等效半径)`` 时显式拒绝（``GeometryError``），避免
    "为了好看"抹掉关键转折。
    """

    poly = as_polygonal(geom)
    if tolerance_m < 0:
        raise GeometryError(f"简化容差不能为负：{tolerance_m}")
    crs = metric_crs or _derive_metric_crs(poly)
    if crs is None:
        raise GeometryError("无法确定米制 CRS，不能按米制容差简化")

    before = measure(poly, crs)
    if tolerance_m == 0:
        return SimplificationResult(
            poly,
            {
                "applied": False,
                "tolerance_m": 0.0,
                "metric_crs": crs,
                "measure_before": before.to_dict(),
                "measure_after": before.to_dict(),
                "warnings": ["simplify_tolerance_m=0，未做任何简化"],
            },
        )

    equivalent_radius = math.sqrt(max(before.area_m2, 1e-9) / math.pi)
    max_allowed = max(1.0, MAX_SIMPLIFY_RADIUS_RATIO * equivalent_radius)
    if tolerance_m > max_allowed:
        raise GeometryError(
            f"简化容差 {tolerance_m:g} m 超过允许上限 {max_allowed:.3f} m"
            f"（=max(1 m, {MAX_SIMPLIFY_RADIUS_RATIO:.0%}×等效半径 {equivalent_radius:.1f} m)）。"
            "过大的容差会抹掉关键山脊转折，请显式调小或关闭简化",
            details={
                "tolerance_m": tolerance_m,
                "max_allowed_m": max_allowed,
                "equivalent_radius_m": equivalent_radius,
            },
        )

    metric_geom = transform_geometry(poly, "EPSG:4490", crs)
    simplified = metric_geom.simplify(tolerance_m, preserve_topology=True)
    if simplified.is_empty:
        raise GeometryError("简化后几何为空，已放弃简化")
    if not simplified.is_valid:
        raise GeometryError(
            "简化后几何无效（不做静默 make_valid 修复），已放弃简化；"
            "请减小 simplify_tolerance_m"
        )

    protected_metric = [
        transform_geometry(Point(lon, lat), "EPSG:4490", crs).coords[0]
        for lon, lat in protected_points
    ]
    warnings: list[str] = []
    inserted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if isinstance(simplified, Polygon):
        simplified, inserted, skipped = _insert_protected_points(
            simplified, protected_metric, tolerance_m=tolerance_m
        )
    else:  # 多部件：逐部件插点后合并
        parts: list[Polygon] = []
        for part in iter_polygons(simplified):
            part2, ins, skp = _insert_protected_points(
                part, protected_metric, tolerance_m=tolerance_m
            )
            inserted.extend(ins)
            skipped.extend(skp)
            parts.append(part2)
        simplified = shapely.union_all(parts)
    if inserted:
        warnings.append(
            f"简化后重新插回 {len(inserted)} 个保护点（出口/锚点），以保持边界锚定"
        )
    if skipped:
        warnings.append(
            f"{len(skipped)} 个保护点距边界超过 2×容差（集水区出口在流域内部？），"
            "未插入边界；仅记录在报告中"
        )

    after_metric = simplified
    hausdorff = float(metric_geom.hausdorff_distance(after_metric))
    if hausdorff > tolerance_m * 2.0:
        warnings.append(
            f"简化后 Hausdorff 偏差 {hausdorff:.2f} m 超过 2×容差，请复核"
        )

    revised_4490 = transform_geometry(simplified, crs, "EPSG:4490")
    if not revised_4490.is_valid:
        raise GeometryError("简化结果回投 EPSG:4490 后无效，已放弃简化")
    after = measure(revised_4490, crs)
    removed = before.vertex_count - after.vertex_count
    warnings.append(
        f"已简化：顶点 {before.vertex_count}→{after.vertex_count}（减少 {removed}），"
        f"面积变化 {after.area_m2 - before.area_m2:+.1f} m²"
        f"（{(after.area_m2 - before.area_m2) / max(before.area_m2, 1e-9):+.4%}）"
    )
    report: dict[str, Any] = {
        "applied": True,
        "tolerance_m": tolerance_m,
        "max_allowed_tolerance_m": max_allowed,
        "metric_crs": crs,
        "vertices_before": before.vertex_count,
        "vertices_after": after.vertex_count,
        "vertices_removed": removed,
        "hausdorff_m": hausdorff,
        "protected_points": len(protected_metric),
        "inserted_protected_points": inserted,
        "skipped_protected_points": skipped,
        "topology_preserved": True,
        "measure_before": before.to_dict(),
        "measure_after": after.to_dict(),
        "warnings": warnings,
    }
    return SimplificationResult(revised_4490, report)


# ---------------------------------------------------------------------------
# 锚点修订
# ---------------------------------------------------------------------------


def revise_with_anchors(
    geom: Any,
    anchors: Iterable[tuple[float, float]],
    *,
    metric_crs: str | None = None,
    max_move_m: float = 50.0,
    protected_points: Iterable[tuple[float, float]] = (),
) -> tuple[Polygon | MultiPolygon, dict[str, Any]]:
    """按用户锚点（EPSG:4490 经纬度）修订边界顶点。

    对每个锚点：找外环最近的顶点，在 ``max_move_m`` 内把该顶点移动到锚点
    位置；超过距离显式报错（不允许大范围"拖动分水岭"）。返回
    ``(修订后几何, 变更报告)``，报告含每个顶点的移动量、修订前后面积/周长。
    """

    poly = as_polygonal(geom)
    crs = metric_crs or _derive_metric_crs(poly)
    if crs is None:
        raise GeometryError("无法确定米制 CRS，不能做锚点修订")
    anchors = [(float(lon), float(lat)) for lon, lat in anchors]
    if not anchors:
        return poly, {
            "applied": False,
            "anchors": [],
            "warnings": ["没有提供锚点，未做修订"],
        }
    if max_move_m <= 0:
        raise GeometryError(f"max_move_m 必须为正：{max_move_m}")

    before = measure(poly, crs)
    metric_geom = transform_geometry(poly, "EPSG:4490", crs)
    coords = list(metric_geom.exterior.coords)
    closed = len(coords) >= 2 and coords[0] == coords[-1]
    open_coords = coords[:-1] if closed else coords
    moved: list[dict[str, Any]] = []
    used: set[int] = set()
    for anchor_index, (lon, lat) in enumerate(anchors):
        ax, ay = transform_geometry(Point(lon, lat), "EPSG:4490", crs).coords[0]
        best_index = -1
        best_dist = math.inf
        for index, (x, y) in enumerate(open_coords):
            if index in used:
                continue
            dist = math.hypot(x - ax, y - ay)
            if dist < best_dist:
                best_dist = dist
                best_index = index
        if best_index < 0:
            raise GeometryError("没有可用顶点供锚点修订")
        if best_dist > max_move_m:
            raise GeometryError(
                f"锚点 {anchor_index}（{lon:.7f},{lat:.7f}）距最近边界顶点 "
                f"{best_dist:.1f} m，超过 max_move_m={max_move_m:g} m；"
                "不允许大幅移动分水岭，请检查锚点位置",
                details={
                    "anchor_index": anchor_index,
                    "distance_m": best_dist,
                    "max_move_m": max_move_m,
                },
            )
        used.add(best_index)
        old_x, old_y = open_coords[best_index]
        open_coords[best_index] = (ax, ay)
        moved.append(
            {
                "anchor_index": anchor_index,
                "vertex_index": best_index,
                "from_xy": [old_x, old_y],
                "to_xy": [ax, ay],
                "move_m": best_dist,
            }
        )
    revised_metric = Polygon(
        open_coords + [open_coords[0]] if closed else open_coords,
        [LinearRing(h.coords) for h in metric_geom.interiors],
    )
    if not revised_metric.is_valid:
        raise GeometryError("锚点修订后几何无效，已放弃修订（不做静默修复）")
    revised_4490 = transform_geometry(revised_metric, crs, "EPSG:4490")
    after = measure(revised_4490, crs)
    report = {
        "applied": True,
        "metric_crs": crs,
        "max_move_m": max_move_m,
        "moved_vertices": moved,
        "measure_before": before.to_dict(),
        "measure_after": after.to_dict(),
        "area_delta_m2": after.area_m2 - before.area_m2,
        "perimeter_delta_m": after.perimeter_m - before.perimeter_m,
        "protected_points": list(protected_points),
        "warnings": [
            f"锚点修订移动了 {len(moved)} 个顶点，"
            f"面积变化 {after.area_m2 - before.area_m2:+.1f} m²"
        ],
    }
    return revised_4490, report
