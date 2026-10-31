"""协议数据模型与响应解析。

字段来源与容错原则
==================
1. 只使用官方手册/官方样例里出现过的字段名；不得编造。
2. 手册存在「表格字段名」与「示例字段名」不一致的地方（20802、Shape 结构体等），
   解析时**两种都接受**，并把原始响应保留在 ``raw`` 里（便于回放与实机对账）。
3. 缺必需字段时抛 ``ProtocolDecodeError``，但异常 ``details["raw"]`` 保留原始
   消息，禁止静默返回空对象。
4. 坐标一律转成内部规范 ``(lon, lat)``（EPSG:4490 语义）；轴序与语义枚举的
   解释只调用 ``ovital_mcp.coordinates``（唯一实现），本模块不另写一套。

证据分级见 ``ovital_mcp.protocol.constants`` 顶部说明；本机没有真实奥维，
所有解析逻辑属于「官方文档级实现 + 模拟回放验证」，``verified_against_real_device=False``。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..coordinates import (
    CoordSemantic,
    canonical_to_latlng,
    latlng_to_canonical,
    semantic_for_latlng_type,
    semantic_for_shape_gcj02,
    verify_canonical_lonlat,
)
from ..errors import CoordinateSemanticError, DataValidationError, OvitalNotFoundError
from . import constants as C
from .errors import ProtocolDecodeError

__all__ = [
    "OvitalVersion",
    "OvitalResponse",
    "ObjNode",
    "ShapeObject",
    "ShapeMetrics",
    "SelectedObject",
    "ObjectWriteResult",
    "MISSING",
    "field_value",
    "as_int",
    "as_float",
    "as_str",
    "parse_version",
    "parse_obj_items",
    "parse_obj_node",
    "parse_selected_list",
    "parse_shape",
    "parse_shape_metrics",
    "parse_write_results",
    "parse_map_data_response",
    "parse_contour_response",
    "parse_screen_corners",
    "parse_viewport_rect",
    "parse_system_crs",
    "parse_vip_level",
    "parse_search_results",
    "object_point_lonlat",
    "axis_order_note",
]


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "<MISSING>"


MISSING = _Missing()


# ---------------------------------------------------------------------------
# 基础数据类（签名见 docs/architecture.md §4.1）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OvitalVersion:
    """20000 登录成功返回。

    - ``ret``：奥维版本号（手册 2.3.1.2.1：成功时 ret 是版本号，不是 0）。
    - ``app_id``：手册 2.3.1.2.1 ``AppId``，每个 ws 连接一个。
    - ``simulated``：**本项目模拟协议扩展**——模拟服务器在 msg 里带
      ``Simulated: true``；真实奥维不会返回该字段，因此真实设备上恒为 False。
    """

    ret: int
    app_id: str | None
    raw: dict[str, Any]
    simulated: bool = False


@dataclass(frozen=True)
class OvitalResponse:
    """一条完整响应/事件帧（协议无请求 ID，靠 cmdid 关联）。"""

    cmdid: int
    ret: int
    msg: dict[str, Any]
    raw: dict[str, Any]
    #: 该响应由多少个分片重组而成（0/1 表示未分片）。字段来自官方样例
    #: omapinterface.js 的 FragmentNum/FragmentIndex/FragmentData（手册未写，待实测）。
    fragments: int = 0


@dataclass
class ObjNode:
    """收藏夹对象节点（20101 / 20110 / 20104 / 20112 返回）。

    ``obj_id``/``type_id`` 必有；其余字段在部分命令（如 20107 只返回 ID+类型）
    中可能为 ``None``。``children`` 递归保存 ``Object.ObjectDetail.ObjChildren``。
    """

    obj_id: int
    type_id: int
    name: str | None
    parent_id: int | None
    srv_id: str | None
    tm_modify: str | None
    raw: dict[str, Any]
    children: list["ObjNode"] = field(default_factory=list)

    @property
    def type_name(self) -> str:
        return C.OBJ_TYPE_NAMES.get(self.type_id, f"type_{self.type_id}")


@dataclass
class ShapeObject:
    """图形对象（Type=13，Shape 结构体，手册 2.4.8）。

    ``vertices`` 是内部规范 ``(lon, lat)`` 列表（EPSG:4490 语义），**不是**协议
    里的扁平 ``lat,lng`` 数组。``axis_order_used`` 记录解析时使用的轴序说明。
    ``two_point_special``：手册 2.4.8 说明两个点的图形用起止角度表示圆/扇形/
    矩形，因此当其为 True 时 ``vertices`` 不能当作多边形边界使用。
    """

    obj_id: int
    name: str
    comment: str
    parent_id: int | None
    vertices: list[tuple[float, float]]
    source_semantic: CoordSemantic
    axis_order_used: str
    raw: dict[str, Any]
    two_point_special: bool = False


@dataclass
class ShapeMetrics:
    """图形量算（20802，手册 2.3.48）。面积单位 ㎡，周长单位 km。"""

    obj_id: int
    point_count: int
    area_count: int
    area_m2: float
    perimeter_km: float
    raw: dict[str, Any]


@dataclass
class SelectedObject:
    """20104 选中的收藏夹对象基本信息。"""

    obj_id: int
    type_id: int
    name: str | None
    raw: dict[str, Any]


@dataclass
class ObjectWriteResult:
    """20112 写入结果（ObjID 已由服务端分配或替换）。"""

    obj_id: int
    type_id: int
    parent_id: int | None
    raw: dict[str, Any]



@contextlib.contextmanager
def _decode_context(raw: Any):
    """保证解析失败时 ``ProtocolDecodeError.raw`` 保留原始响应（便于回放/实机对账）。"""

    try:
        yield
    except ProtocolDecodeError as exc:
        if exc.raw is not None:
            raise
        raise ProtocolDecodeError(exc.message, details=exc.details, raw=raw) from exc


# ---------------------------------------------------------------------------
# 基础取值工具（容忍手册字段名差异与类型不一致）
# ---------------------------------------------------------------------------


def field_value(
    mapping: Mapping[str, Any],
    names: Iterable[str],
    *,
    default: Any = MISSING,
) -> Any:
    """按候选字段名依次取值（区分大小写，先到先得）。

    ``names`` 允许同一语义的多种官方写法，例如 20802 的
    ``("dAreaSize", "AreaSize")``。找不到时返回 ``default``，若 default 是
    ``MISSING`` 则抛 ``ProtocolDecodeError``。
    """

    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    if default is MISSING:
        raise ProtocolDecodeError(
            "响应缺少必需字段",
            details={"candidates": list(names), "present": sorted(mapping)[:40]},
        )
    return default


def as_int(value: Any, *, field_name: str) -> int:
    """把协议里的 int/str/float 统一转 int（ObjID 在不同命令里类型不一致）。"""

    if isinstance(value, bool):
        raise ProtocolDecodeError(f"字段 {field_name} 不能是 bool")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ProtocolDecodeError(f"字段 {field_name} 需要整数，收到 {value!r}")
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError as exc:
            raise ProtocolDecodeError(
                f"字段 {field_name} 需要整数，收到字符串 {value!r}",
                details={"field": field_name},
            ) from exc
    raise ProtocolDecodeError(f"字段 {field_name} 类型不支持：{type(value).__name__}")


def as_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ProtocolDecodeError(f"字段 {field_name} 不能是 bool")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise ProtocolDecodeError(
                f"字段 {field_name} 需要数字，收到字符串 {value!r}",
                details={"field": field_name},
            ) from exc
    raise ProtocolDecodeError(f"字段 {field_name} 类型不支持：{type(value).__name__}")


def as_str(value: Any, *, field_name: str) -> str:
    if isinstance(value, str):
        return value
    raise ProtocolDecodeError(f"字段 {field_name} 需要字符串，收到 {type(value).__name__}")


def axis_order_note(latlng_type: int | None = None) -> str:
    """人类可读的轴序说明（写进 ShapeObject.axis_order_used 与报告）。"""

    if latlng_type is None:
        return "Shape.Latlng 为扁平 lat,lng,lat,lng…（纬度在前，手册 2.4.8 示例）"
    try:
        semantic = semantic_for_latlng_type(latlng_type)
        semantic_name = semantic.value
    except Exception:  # noqa: BLE001 - 说明文本不因非法枚举失败
        semantic_name = f"unknown({latlng_type})"
    return (
        "Shape.Latlng 为扁平 lat,lng,lat,lng…（纬度在前，手册 2.4.8 示例）；"
        f"请求 LatlngType={latlng_type}（{semantic_name}，手册 2.2.2）"
    )


# ---------------------------------------------------------------------------
# 20000 登录
# ---------------------------------------------------------------------------


def parse_version(msg: Mapping[str, Any], ret: int, raw: dict[str, Any] | None = None) -> OvitalVersion:
    """解析 20000 成功响应。

    手册 2.3.1.2.1：成功 ``ret`` 是版本号，``msg.AppId`` 每个连接一个。
    模拟服务器额外返回 ``msg.Simulated``（本项目扩展，真实奥维没有）。
    """

    if ret <= 0:
        raise ProtocolDecodeError(
            f"登录成功响应 ret 应为版本号（>0），收到 {ret}",
            details={"ret": ret},
            raw=raw,
        )
    app_id_raw = msg.get(C.LOGIN_APPID_FIELD)
    app_id: str | None
    if app_id_raw is None:
        app_id = None
    elif isinstance(app_id_raw, str):
        app_id = app_id_raw
    elif isinstance(app_id_raw, (int, float)):
        app_id = str(int(app_id_raw))
    else:
        raise ProtocolDecodeError(
            f"AppId 类型不支持：{type(app_id_raw).__name__}", raw=raw
        )
    simulated = bool(msg.get(C.SIMULATED_FIELD))
    return OvitalVersion(ret=ret, app_id=app_id, raw=dict(raw or {"ret": ret, "msg": dict(msg)}), simulated=simulated)


# ---------------------------------------------------------------------------
# 对象节点
# ---------------------------------------------------------------------------


def _parse_children(
    obj_detail: Mapping[str, Any],
    *,
    raw: dict[str, Any],
    depth: int,
    max_depth: int,
) -> list["ObjNode"]:
    children_raw = obj_detail.get("ObjChildren")
    if children_raw is None:
        return []
    if depth >= max_depth:
        raise ProtocolDecodeError(
            f"对象树嵌套超过 {max_depth} 层，疑似响应异常（防止无限递归）",
            raw=raw,
        )
    if isinstance(children_raw, Mapping):
        children_raw = [children_raw]
    if not isinstance(children_raw, list):
        raise ProtocolDecodeError(
            "ObjectDetail.ObjChildren 应为数组", raw=raw
        )
    return [
        parse_obj_node(item, depth=depth + 1, max_depth=max_depth)
        for item in children_raw
        if isinstance(item, Mapping)
    ]


def parse_obj_node(
    item: Mapping[str, Any],
    *,
    depth: int = 0,
    max_depth: int = 32,
) -> ObjNode:
    """解析单个对象节点（20101/20110/20112 的 ``ObjItems[]`` 元素）。

    结构（手册 2.3.11.2 / 2.4.6 / 2.4.8 示例）::

        {"Type":13, "ObjID":"627700251", "ParentID":0, "SrvID":"...",
         "tmModify":"2019/03/07?16:08:38",
         "Object":{"Name":"Shape","Type":13,"Comment":"Shape",
                   "ObjectDetail":{"Latlng":[...], "ObjChildren":[...], ...}}}
    """

    if not isinstance(item, Mapping):
        raise ProtocolDecodeError(
            f"对象节点应为 JSON 对象，收到 {type(item).__name__}"
        )
    raw = dict(item)
    type_value = field_value(item, ("Type", "ObjType"), default=MISSING)
    type_id = as_int(type_value, field_name="Type")
    obj_id = as_int(field_value(item, ("ObjID", "idObj")), field_name="ObjID")
    parent_raw = item.get("ParentID")
    parent_id = as_int(parent_raw, field_name="ParentID") if parent_raw is not None else None
    srv_raw = item.get("SrvID")
    srv_id = str(srv_raw) if srv_raw is not None else None
    tm_raw = item.get("tmModify")
    tm_modify = str(tm_raw) if tm_raw is not None else None

    obj = item.get("Object")
    if obj is None:
        obj = {}
    if not isinstance(obj, Mapping):
        raise ProtocolDecodeError("Object 字段应为 JSON 对象", raw=raw)
    name_raw = obj.get("Name")
    name = name_raw if isinstance(name_raw, str) else None
    detail = obj.get("ObjectDetail")
    if detail is None:
        detail = {}
    if not isinstance(detail, Mapping):
        raise ProtocolDecodeError("Object.ObjectDetail 应为 JSON 对象", raw=raw)
    children = _parse_children(detail, raw=raw, depth=depth, max_depth=max_depth)

    return ObjNode(
        obj_id=obj_id,
        type_id=type_id,
        name=name,
        parent_id=parent_id,
        srv_id=srv_id,
        tm_modify=tm_modify,
        raw=raw,
        children=children,
    )


def _iter_obj_items(msg: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = msg.get("ObjItems")
    if items is None:
        # 20101 某些实现可能直接返回单个对象字段（手册未写，防御性容错）
        if "ObjID" in msg or "Object" in msg:
            return [msg]
        raise ProtocolDecodeError(
            "响应缺少 ObjItems 字段", details={"present": sorted(msg)[:40]}
        )
    if isinstance(items, Mapping):
        return [items]
    if not isinstance(items, list):
        raise ProtocolDecodeError("ObjItems 应为数组")
    return [item for item in items if isinstance(item, Mapping)]


def parse_obj_items(msg: Mapping[str, Any]) -> list[ObjNode]:
    """解析 ``msg.ObjItems`` 为节点列表（保留完整子树）。"""

    return [parse_obj_node(item) for item in _iter_obj_items(msg)]


# ---------------------------------------------------------------------------
# 20104 选中对象
# ---------------------------------------------------------------------------


def parse_selected_list(msg: Mapping[str, Any]) -> list[SelectedObject]:
    """解析 20104 响应 ``msg.Count`` / ``msg.ObjList``（手册 2.3.17.2）。"""

    with _decode_context(dict(msg)):
        return _parse_selected_list_impl(msg)


def _parse_selected_list_impl(msg: Mapping[str, Any]) -> list[SelectedObject]:
    obj_list = msg.get("ObjList")
    if obj_list is None:
        # 手册未给失败/空选择时的形态；空对象视为 0 条而不是错误。
        if not msg:
            return []
        raise ProtocolDecodeError(
            "20104 响应缺少 ObjList", details={"present": sorted(msg)[:40]}
        )
    if isinstance(obj_list, Mapping):
        obj_list = [obj_list]
    if not isinstance(obj_list, list):
        raise ProtocolDecodeError("20104 ObjList 应为数组")

    count = msg.get("Count")
    if count is not None:
        expected = as_int(count, field_name="Count")
        if expected != len(obj_list):
            raise ProtocolDecodeError(
                f"20104 Count={expected} 与 ObjList 长度 {len(obj_list)} 不一致",
                details={"count": expected, "len": len(obj_list)},
            )
    results: list[SelectedObject] = []
    for item in obj_list:
        if not isinstance(item, Mapping):
            continue
        raw = dict(item)
        obj_id = as_int(field_value(item, ("ObjID", "idObj")), field_name="ObjID")
        type_id = as_int(field_value(item, ("Type", "ObjType")), field_name="Type")
        name_raw = item.get("Name")
        name = name_raw if isinstance(name_raw, str) else None
        results.append(SelectedObject(obj_id=obj_id, type_id=type_id, name=name, raw=raw))
    return results


# ---------------------------------------------------------------------------
# Shape（手册 2.4.8）
# ---------------------------------------------------------------------------


def _latlng_pairs(latlng: Any) -> list[tuple[float, float]]:
    """把 ``Shape.Latlng`` 解析成 ``[(lat, lng), ...]``。

    手册 2.4.8：``Latlng`` 是 double 扁平数组，示例
    ``[39.01064751, 106.57287598, 38.52668162, 107.10571289]``（纬度在前）。
    同时防御性支持 ``[{"lat":..,"lng":..}, ...]``（OmapLatlng 结构体写法），
    但只在数组元素是对象时启用；两种写法都保留在 raw 里。
    """

    if not isinstance(latlng, list):
        raise ProtocolDecodeError(
            f"Shape.Latlng 应为数组，收到 {type(latlng).__name__}"
        )
    if not latlng:
        return []
    if isinstance(latlng[0], Mapping):
        pairs: list[tuple[float, float]] = []
        for element in latlng:
            if not isinstance(element, Mapping):
                raise ProtocolDecodeError("Shape.Latlng 混合了对象与数字")
            lat = as_float(field_value(element, ("lat", "Lat")), field_name="lat")
            lng = as_float(field_value(element, ("lng", "Lng")), field_name="lng")
            pairs.append((lat, lng))
        return pairs
    if len(latlng) % 2 != 0:
        raise ProtocolDecodeError(
            f"Shape.Latlng 扁平数组长度必须为偶数，收到 {len(latlng)}"
        )
    pairs = []
    for index in range(0, len(latlng), 2):
        lat = as_float(latlng[index], field_name=f"Latlng[{index}]")
        lng = as_float(latlng[index + 1], field_name=f"Latlng[{index + 1}]")
        pairs.append((lat, lng))
    return pairs


def parse_shape(
    msg: Mapping[str, Any],
    *,
    obj_id: int | None = None,
    allow_gcj02_conversion: bool = False,
    latlng_type_requested: int | None = 1,
) -> ShapeObject:
    """从 20101 响应解析图形对象（Type=13）。

    轴序：``Shape.Latlng`` 扁平数组是 ``lat, lng``（手册 2.4.8 示例
    39.01,106.57 可证），对外统一转成 ``(lon, lat)``。

    坐标语义：``ObjectDetail.Gcj02`` 按手册 2.4.8 解析
    （0=CGCS2000, 1=GCJ-02, 2=平面）。GCJ-02 默认**不**自动转换（避免重复
    偏移），除非 ``allow_gcj02_conversion=True``；平面坐标需要投影参数，直接报错。
    """

    nodes = parse_obj_items(msg)
    if not nodes:
        raise OvitalNotFoundError("20101 响应没有对象", details={"msg": sorted(msg)[:20]})
    node = None
    for candidate in _walk_nodes(nodes):
        if obj_id is not None and candidate.obj_id != obj_id:
            continue
        if candidate.type_id == C.OBJ_TYPE_SHAPE:
            node = candidate
            break
    if node is None:
        types = [n.type_id for n in _walk_nodes(nodes)]
        raise ProtocolDecodeError(
            f"对象 {obj_id} 不是图形对象（Type 应为 13，实际类型 {types}）",
            details={"obj_id": obj_id, "types": types},
            raw=node.raw if node else None,
        )

    obj = node.raw.get("Object") or {}
    detail = obj.get("ObjectDetail") or {}
    if not isinstance(detail, Mapping):
        raise ProtocolDecodeError("Shape ObjectDetail 应为对象", raw=node.raw)

    gcj02_value = field_value(detail, ("Gcj02",), default=0)
    gcj02 = as_int(gcj02_value, field_name="Gcj02")
    semantic = semantic_for_shape_gcj02(gcj02)

    pairs = _latlng_pairs(detail.get("Latlng"))
    mtp_raw = detail.get("Mtp", detail.get("nMtp"))
    if mtp_raw is not None:
        mtp = as_int(mtp_raw, field_name="Mtp")
        if mtp != len(pairs):
            raise ProtocolDecodeError(
                f"Shape.Mtp={mtp} 与 Latlng 点数 {len(pairs)} 不一致（手册 2.4.8）",
                details={"mtp": mtp, "points": len(pairs)},
                raw=node.raw,
            )

    vertices: list[tuple[float, float]] = []
    for lat, lng in pairs:
        if semantic == CoordSemantic.CGCS2000:
            vertices.append(verify_canonical_lonlat(lng, lat))
        elif semantic == CoordSemantic.GCJ02:
            canonical = latlng_to_canonical(
                _make_latlng(lat, lng, semantic),
                allow_gcj02_conversion=allow_gcj02_conversion,
            )
            vertices.append(canonical)
        else:
            raise CoordinateSemanticError(
                "图形坐标是平面坐标（Shape.Gcj02=2），适配器没有该投影参数，"
                "无法安全转换为经纬度；请改用 LatlngType=1 读取 CGCS2000",
                details={"gcj02": gcj02, "semantic": semantic.value},
            )

    comment_raw = obj.get("Comment")
    name_raw = obj.get("Name")
    return ShapeObject(
        obj_id=node.obj_id,
        name=name_raw if isinstance(name_raw, str) else "",
        comment=comment_raw if isinstance(comment_raw, str) else "",
        parent_id=node.parent_id,
        vertices=vertices,
        source_semantic=semantic,
        axis_order_used=axis_order_note(latlng_type_requested),
        raw=node.raw,
        two_point_special=len(vertices) == 2,
    )


def _make_latlng(lat: float, lng: float, semantic: CoordSemantic):
    from ..coordinates import LatLng

    return LatLng(lat=lat, lng=lng, semantic=semantic)


def _walk_nodes(nodes: Iterable[ObjNode]) -> Iterable[ObjNode]:
    for node in nodes:
        yield node
        yield from _walk_nodes(node.children)


# ---------------------------------------------------------------------------
# 20802 图形量算
# ---------------------------------------------------------------------------

#: 手册 2.3.48 表格 vs 示例/官方样例的字段名差异。
SHAPE_METRICS_ALIASES: dict[str, tuple[str, ...]] = {
    "obj_id": ("ObjID", "idObj"),
    "point_count": ("PointCnt", "iPointCnt"),
    "area_count": ("AreaCnt", "iAreaCnt"),
    "area_m2": ("AreaSize", "dAreaSize"),
    "perimeter_km": ("Perimeter", "dPerimeter"),
}


def parse_shape_metrics(msg: Mapping[str, Any]) -> ShapeMetrics:
    """解析 20802（手册 2.3.48）。

    手册表格字段名带匈牙利前缀（``idObj/iPointCnt/iAreaCnt/dPerimeter/dAreaSize``），
    示例与官方样例 omapinterface.js 用无前缀字段（``ObjID/PointCnt/AreaCnt/
    Perimeter/AreaSize``）——两种都接受。
    """

    raw = dict(msg)
    with _decode_context(raw):
        return _parse_shape_metrics_impl(msg)


def _parse_shape_metrics_impl(msg: Mapping[str, Any]) -> ShapeMetrics:
    raw = dict(msg)
    obj_id = as_int(field_value(msg, SHAPE_METRICS_ALIASES["obj_id"]), field_name="ObjID")
    point_count = as_int(
        field_value(msg, SHAPE_METRICS_ALIASES["point_count"]), field_name="PointCnt"
    )
    area_count = as_int(
        field_value(msg, SHAPE_METRICS_ALIASES["area_count"]), field_name="AreaCnt"
    )
    area_m2 = as_float(
        field_value(msg, SHAPE_METRICS_ALIASES["area_m2"]), field_name="AreaSize"
    )
    perimeter_km = as_float(
        field_value(msg, SHAPE_METRICS_ALIASES["perimeter_km"]), field_name="Perimeter"
    )
    return ShapeMetrics(
        obj_id=obj_id,
        point_count=point_count,
        area_count=area_count,
        area_m2=area_m2,
        perimeter_km=perimeter_km,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# 20112 写入结果
# ---------------------------------------------------------------------------


def parse_write_results(msg: Mapping[str, Any]) -> list[ObjectWriteResult]:
    """解析 20112 响应。

    两种官方形态都要接受（**待实测**）：
    - 手册 2.3.12.2 示例：``msg.ObjItems`` 是扁平数组
      ``[{Type, ObjID, ParentID}, ...]``；
    - 官方样例 omapinterface.js：按
      ``msg.ObjItems[0].Object.ObjectDetail.ObjChildren`` 读取，即树形嵌套。
    """

    results: list[ObjectWriteResult] = []
    seen: set[tuple[int, int]] = set()

    def add(node: ObjNode) -> None:
        key = (node.obj_id, node.type_id)
        if node.obj_id <= 0 or key in seen:
            return
        seen.add(key)
        results.append(
            ObjectWriteResult(
                obj_id=node.obj_id,
                type_id=node.type_id,
                parent_id=node.parent_id,
                raw=node.raw,
            )
        )

    def walk(nodes: Iterable[ObjNode]) -> None:
        for node in nodes:
            add(node)
            walk(node.children)

    walk(parse_obj_items(msg))
    return results


# ---------------------------------------------------------------------------
# 20301 / 20303
# ---------------------------------------------------------------------------


def parse_map_data_response(msg: Mapping[str, Any]) -> dict[str, Any]:
    """解析 20301 响应元数据（图片字节由 commands 解码 ``Data``）。

    手册 2.3.29.2 表格说响应有 ``ReqMapDatas`` 数组，示例却是扁平字段
    （``x_lng1`` 等在 msg 顶层）——两种都接受，统一成扁平的 ``bbox``。
    """

    raw = dict(msg)
    payload: Mapping[str, Any] = msg
    req_list = msg.get("ReqMapDatas")
    if isinstance(req_list, list) and req_list and isinstance(req_list[0], Mapping):
        payload = req_list[0]
    elif isinstance(req_list, Mapping):
        payload = req_list

    def opt_float(name: str) -> float | None:
        value = payload.get(name)
        if value is None:
            return None
        return as_float(value, field_name=name)

    bbox = {
        "x_lng1": opt_float("x_lng1"),
        "y_lat1": opt_float("y_lat1"),
        "x_lng2": opt_float("x_lng2"),
        "y_lat2": opt_float("y_lat2"),
    }
    result: dict[str, Any] = {
        "bbox": bbox,
        "coord_type": payload.get("CoordType"),
        "map_level": payload.get("MaxLevel", payload.get("MapLevel")),
        "max_pixel": payload.get("MaxPixel"),
        "data_present": bool(payload.get("Data")),
        "raw": raw,
    }
    if bbox["x_lng1"] is not None and bbox["x_lng2"] is not None:
        result["bbox_lonlat"] = (
            min(bbox["x_lng1"], bbox["x_lng2"]),
            min(bbox["y_lat1"], bbox["y_lat2"]),
            max(bbox["x_lng1"], bbox["x_lng2"]),
            max(bbox["y_lat1"], bbox["y_lat2"]),
        )
    return result


def parse_contour_response(msg: Mapping[str, Any]) -> dict[str, Any]:
    """解析 20303 响应元数据（手册 2.3.31.2）。

    手册只给了元数据字段，**没有**给出等高线几何/高程数组的结构，因此这里
    只返回元数据并显式标注 ``geometry_format_unverified=True``；禁止编造几何。
    """

    raw = dict(msg)
    payload: Mapping[str, Any] = msg
    req_list = msg.get("ReqMapDatas")
    if isinstance(req_list, list) and req_list and isinstance(req_list[0], Mapping):
        payload = req_list[0]
    ret = payload.get("Ret")
    elev = None
    for name in C.CONTOUR_ELEV_FIELDS:
        if payload.get(name) is not None:
            elev = as_int(payload[name], field_name=name)
            break
    result = {
        "ret": as_int(ret, field_name="Ret") if ret is not None else None,
        "ret_meaning": C.CONTOUR_RET_MESSAGES.get(
            as_int(ret, field_name="Ret") if ret is not None else 0
        ),
        "need_vip": payload.get("NeedVip"),
        "elev_value": elev,
        "check_flag": payload.get("CheckFlag"),
        "check_result": payload.get("CheckResult"),
        "part": payload.get("Part"),
        "map_level": payload.get("MapLevel"),
        "coord_type": payload.get("CoordType"),
        "raw": raw,
        "geometry_format_unverified": True,
        "note": "手册 2.3.31 只给出元数据字段，未给出等高线数据数组结构；待实测",
    }
    return result


# ---------------------------------------------------------------------------
# 20016 屏幕角点 / 20730 窗体区域
# ---------------------------------------------------------------------------


def parse_screen_corners(msg: Mapping[str, Any]) -> dict[str, Any]:
    """解析 20016（手册 2.3.58）。

    字段语义（手册表）：latc/lngc 中心点；latlt/lnglt 左上角；latrb/lngrb 右下角。
    ``ReqCoordType`` 是**第 5 套坐标枚举**（0=根据奥维决定, 1=真实经纬度,
    2=GCJ-02），不能套用顶层 CoordType。

    手册 2.3.58.2 的示例数值明显错位（``latlt=30.595`` / ``lnglt=27.152`` /
    ``latrb=111.418``），因此这里做合理性检查并把可疑项写进 ``warnings``，
    不静默纠正。真实轴序/含义「待实测」。
    """

    warnings: list[str] = []

    def opt(name: str) -> float | None:
        value = msg.get(name)
        if value is None:
            return None
        return as_float(value, field_name=name)

    latc, lngc = opt("latc"), opt("lngc")
    latlt, lnglt = opt("latlt"), opt("lnglt")
    latrb, lngrb = opt("latrb"), opt("lngrb")

    def plausible(lat: float | None, lng: float | None, label: str) -> None:
        if lat is None or lng is None:
            return
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
            warnings.append(
                f"{label} 数值超出经纬度合理范围（lat={lat}, lng={lng}），"
                "手册 2.3.58.2 示例本身也存在字段错位，轴序待实测"
            )

    plausible(latc, lngc, "中心点")
    plausible(latlt, lnglt, "左上角")
    plausible(latrb, lngrb, "右下角")

    req_type_raw = msg.get("ReqCoordType")
    req_type = as_int(req_type_raw, field_name="ReqCoordType") if req_type_raw is not None else None
    semantic = C.SCREEN_COORD_REQ_TYPE.get(req_type) if req_type is not None else None
    if req_type is not None and semantic is None:
        warnings.append(f"20016 ReqCoordType={req_type} 不在手册 2.3.58 的 0/1/2 枚举内")

    result: dict[str, Any] = {
        "center": (lngc, latc) if lngc is not None and latc is not None else None,
        "top_left": (lnglt, latlt) if lnglt is not None and latlt is not None else None,
        "bottom_right": (lngrb, latrb) if lngrb is not None and latrb is not None else None,
        "center_lat": latc,
        "center_lng": lngc,
        "top_left_lat": latlt,
        "top_left_lng": lnglt,
        "bottom_right_lat": latrb,
        "bottom_right_lng": lngrb,
        "req_coord_type": req_type,
        "req_coord_semantic": semantic.value if semantic else None,
        "real_ll": msg.get("RealLl"),
        "map_level": msg.get("MapLevel"),
        "map_type": msg.get("MapType"),
        "warnings": warnings,
        "raw": dict(msg),
        "axis_order_note": (
            "字段名为 lat*/lng* 命名对，不存在扁平数组轴序问题；"
            "手册 2.3.58.2 示例数值疑似错位（待实测）"
        ),
    }
    return result


def parse_viewport_rect(msg: Mapping[str, Any]) -> dict[str, Any]:
    """解析 20730（手册 2.3.52）：窗口可显示区域，<=0 表示最小化。"""

    def opt(name: str) -> int | None:
        value = msg.get(name)
        if value is None:
            return None
        return as_int(value, field_name=name)

    left, right, top, bottom = opt("Left"), opt("Right"), opt("Top"), opt("Bottom")
    minimized = any(v is not None and v <= 0 for v in (left, right, top, bottom))
    width = right - left if left is not None and right is not None else None
    height = bottom - top if top is not None and bottom is not None else None
    return {
        "left": left,
        "right": right,
        "top": top,
        "bottom": bottom,
        "width": width,
        "height": height,
        "minimized": minimized,
        "warnings": (
            ["奥维窗体被最小化或区域参数<=0（手册 2.3.52.2），无法用于配准"]
            if minimized
            else []
        ),
        "raw": dict(msg),
    }


# ---------------------------------------------------------------------------
# 20222 系统坐标系
# ---------------------------------------------------------------------------


def parse_system_crs(msg: Mapping[str, Any]) -> dict[str, Any]:
    """解析 20222（手册 2.3.51）。

    注意 ``CoordType`` 是第 6 套枚举（2=经纬度, 4=横轴墨卡托），
    与顶层 CoordType（0=CGCS2000/1=GCJ02/2=当前坐标系）完全不同。
    """

    coord_type_raw = msg.get("CoordType")
    coord_type = as_int(coord_type_raw, field_name="CoordType") if coord_type_raw is not None else None
    prj_raw = msg.get("iPrjType")
    prj_type = as_int(prj_raw, field_name="iPrjType") if prj_raw is not None else None

    warnings: list[str] = []
    coord_meaning = C.SYSTEM_CRS_COORD_TYPE.get(coord_type) if coord_type is not None else None
    if coord_type is not None and coord_meaning is None:
        warnings.append(f"20222 CoordType={coord_type} 不在手册 2.3.51 的 2/4 枚举内")
    prj_meaning = C.SYSTEM_CRS_PRJ_TYPE.get(prj_type) if prj_type is not None else None
    if prj_type is not None and prj_meaning is None:
        warnings.append(f"20222 iPrjType={prj_type} 不在手册 2.3.51 的 0..5 枚举内")
    if coord_meaning == "geographic" and prj_type is not None:
        warnings.append(
            "系统坐标系是经纬度（CoordType=2）时 iPrjType 等投影参数可能无意义"
        )

    return {
        "coord_type": coord_type,
        "coord_type_meaning": coord_meaning,
        "prj_type": prj_type,
        "prj_type_meaning": prj_meaning,
        "false_easting": msg.get("FalseEasting"),
        "false_northing": msg.get("FalseNorthing"),
        "scale_factor": msg.get("ScaleFactor"),
        "central_meridian": msg.get("CentralMeridian"),
        "angular_unit": msg.get("AngularUnit"),
        "linear_unit": msg.get("LinearUnit"),
        "semimajor_axis": msg.get("SemimajorAxis"),
        "inverse_flattening": msg.get("InverseFlattening"),
        "prj_name": msg.get("PrjName"),
        "datum": msg.get("DATUM"),
        "projection": msg.get("PROJECTION"),
        "warnings": warnings,
        "raw": dict(msg),
        "note": (
            "手册 2.3.51 的 CoordType 是独立枚举（2=经纬度,4=横轴墨卡托），"
            "不能套用 2.2.2 顶层 CoordType"
        ),
    }


# ---------------------------------------------------------------------------
# 20501 VIP
# ---------------------------------------------------------------------------


def parse_vip_level(msg: Mapping[str, Any]) -> dict[str, Any]:
    """解析 20501（手册 2.3.19）：``OmapLevel`` + ``LoadOK``。

    手册未给 OmapLevel 数值与 VIP/SVIP 名称的映射，因此只透传数值，不做等级
    断言；权限以具体命令的 ret=-5/-7 为准。
    """

    level_raw = msg.get("OmapLevel")
    load_ok_raw = msg.get("LoadOK")
    level = as_int(level_raw, field_name="OmapLevel") if level_raw is not None else None
    load_ok = as_int(load_ok_raw, field_name="LoadOK") if load_ok_raw is not None else None
    return {
        "omap_level": level,
        "load_ok": load_ok,
        "load_ok_bool": bool(load_ok) if load_ok is not None else None,
        "tier": C.tier_for_omap_level(level).value,
        "tier_note": C.VIP_LEVEL_NOTE,
        "raw": dict(msg),
    }


# ---------------------------------------------------------------------------
# 20107 查找对象
# ---------------------------------------------------------------------------


def parse_search_results(msg: Mapping[str, Any]) -> list[ObjNode]:
    """解析 20107 响应（手册 2.3.22.2）。

    响应 ``ListID`` 只含 ``ObjID`` + ``ObjType``，**不含名称**；要名称/坐标需
    再调用 20101（该限制写进 protocol-notes「待实测」）。
    """

    with _decode_context(dict(msg)):
        return _parse_search_results_impl(msg)


def _parse_search_results_impl(msg: Mapping[str, Any]) -> list[ObjNode]:
    list_id = msg.get("ListID")
    if list_id is None:
        raise ProtocolDecodeError("20107 响应缺少 ListID", raw=dict(msg))
    if isinstance(list_id, Mapping):
        list_id = [list_id]
    if not isinstance(list_id, list):
        raise ProtocolDecodeError("20107 ListID 应为数组", raw=dict(msg))

    results: list[ObjNode] = []
    for item in list_id:
        if not isinstance(item, Mapping):
            raise ProtocolDecodeError("20107 ListID 元素应为对象", raw=dict(msg))
        raw = dict(item)
        obj_id = as_int(field_value(item, ("ObjID", "idObj")), field_name="ObjID")
        type_id = as_int(field_value(item, ("ObjType", "Type")), field_name="ObjType")
        results.append(
            ObjNode(
                obj_id=obj_id,
                type_id=type_id,
                name=None,
                parent_id=None,
                srv_id=None,
                tm_modify=None,
                raw=raw,
                children=[],
            )
        )
    return results


# ---------------------------------------------------------------------------
# 从节点提取点坐标（工具层「读出口」用）
# ---------------------------------------------------------------------------


def object_point_lonlat(
    node: Mapping[str, Any] | ObjNode,
    *,
    allow_gcj02_conversion: bool = False,
) -> tuple[float, float]:
    """从 20101 返回节点提取一个点坐标，返回规范 ``(lon, lat)``。

    支持：
    - Type=7 标签（手册 2.4.6）：``Object.ObjectDetail.Lat`` / ``Lng`` /
      ``Gcj02``（2.4.6：0=CGCS2000, 1=GCJ-02；与 2.4.8 Shape.Gcj02 相同映射）。
    - Type=13 图形（手册 2.4.8）：取 ``Shape.Latlng`` 前两个点（即第一个点）
      的坐标；若有两个点（圆/扇形/矩形）取其首点。**注意**：这只是「图形上的
      一个点」，不是几何中心；工具层如需中心应显式计算。

    缺字段/类型不支持时抛 ``ProtocolDecodeError``（显式失败，不返回猜测值）。
    """

    raw = node.raw if isinstance(node, ObjNode) else dict(node)
    item: Mapping[str, Any] = raw
    # 允许直接传 20101 响应的整条 msg（含 ObjItems）以方便调用。
    if "ObjItems" in item:
        nodes = parse_obj_items(item)
        if not nodes:
            raise ProtocolDecodeError("20101 响应没有对象", raw=dict(item))
        node_obj = nodes[0]
        raw = node_obj.raw
        item = raw
    obj = item.get("Object") or {}
    if not isinstance(obj, Mapping):
        raise ProtocolDecodeError("Object 字段应为对象", raw=dict(raw))
    detail = obj.get("ObjectDetail") or {}
    if not isinstance(detail, Mapping):
        raise ProtocolDecodeError("ObjectDetail 字段应为对象", raw=dict(raw))
    type_id = as_int(
        field_value(item, ("Type", "ObjType"), default=obj.get("Type")),
        field_name="Type",
    )

    if type_id == C.OBJ_TYPE_SIGN:
        lat = as_float(field_value(detail, ("Lat",)), field_name="Lat")
        lng = as_float(field_value(detail, ("Lng", "Lon")), field_name="Lng")
        gcj02 = as_int(detail.get("Gcj02", 0), field_name="Gcj02")
        semantic = semantic_for_shape_gcj02(gcj02)
        return _to_canonical(lng, lat, semantic, allow_gcj02_conversion, raw)

    if type_id == C.OBJ_TYPE_SHAPE:
        gcj02 = as_int(detail.get("Gcj02", 0), field_name="Gcj02")
        semantic = semantic_for_shape_gcj02(gcj02)
        pairs = _latlng_pairs(detail.get("Latlng"))
        if not pairs:
            raise ProtocolDecodeError("图形对象没有 Latlng 顶点", raw=dict(raw))
        lat, lng = pairs[0]
        return _to_canonical(lng, lat, semantic, allow_gcj02_conversion, raw)

    raise ProtocolDecodeError(
        f"对象类型 {type_id} 不支持提取点坐标（仅支持 7 标签 / 13 图形）",
        details={"type_id": type_id},
        raw=dict(raw),
    )


def _to_canonical(
    lng: float,
    lat: float,
    semantic: CoordSemantic,
    allow_gcj02_conversion: bool,
    raw: Mapping[str, Any],
) -> tuple[float, float]:
    if semantic == CoordSemantic.CGCS2000:
        try:
            return verify_canonical_lonlat(lng, lat)
        except DataValidationError as exc:
            raise ProtocolDecodeError(
                f"坐标超出合理范围（疑似轴序错误）：lat={lat}, lng={lng}", raw=dict(raw)
            ) from exc
    if semantic == CoordSemantic.GCJ02:
        if not allow_gcj02_conversion:
            raise CoordinateSemanticError(
                "对象坐标是 GCJ-02。默认不自动转换以避免重复偏移；"
                "请改用 LatlngType=1 读取 CGCS2000，或在配置显式开启 "
                "allow_gcj02_conversion（公开近似算法）。",
                details={"semantic": semantic.value},
            )
        from ..coordinates import LatLng

        return latlng_to_canonical(
            LatLng(lat=lat, lng=lng, semantic=semantic),
            allow_gcj02_conversion=True,
        )
    raise CoordinateSemanticError(
        f"坐标语义 {semantic.value} 需要投影参数，适配器未实现该转换",
        details={"semantic": semantic.value},
    )


# 保留导出以便测试/工具层直接使用
_ = canonical_to_latlng
