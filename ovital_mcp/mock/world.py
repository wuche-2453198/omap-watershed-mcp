"""模拟奥维收藏夹世界（内存对象树 + 图形量算 + 模拟地图渲染）。

用途
====
在没有 Windows 奥维客户端、没有真实账号的机器上，为协议适配器提供**可重复**
的读写闭环：

- 20112 写入后 20101 可回读（对象树、顶点、名称、备注）；
- 20802 用局部米制投影**真算**面积/周长/顶点数（与 GIS 层口径一致）；
- 20301 渲染带 ``SIMULATED MAP`` 水印的 PNG（绝不伪装成奥维真实地图）；
- 20016/20730/20212/20222/20501/20006 返回手册示例量级的模拟值。

所有行为都只是**模拟**：``simulated=True``、``verified_against_real_device=False``。
对象 ID 由确定性计数器生成（同一操作序列 → 同一 ID），便于回放证据复现。

坐标：内部统一 ``(lon, lat)``（EPSG:4490 语义）；``Shape.Latlng`` 按手册 2.4.8
存成 ``lat, lng`` 扁平数组。
"""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..coordinates import (
    CoordSemantic,
    LatLng,
    cgcs2000_to_gcj02,
    gcj02_to_cgcs2000,
    latlng_to_canonical,
    metric_crs_for,
    semantic_for_shape_gcj02,
)
from ..protocol import constants as C

__all__ = ["MockWorld", "MockWorldError", "render_simulated_map_png"]

#: 手册 2.3.48 示例量级的默认视口（只用于模拟，不代表用户真实视图）。
DEFAULT_VIEWPORT = {
    "center_lon": 106.57287598,
    "center_lat": 39.01064751,
    "half_span_lon": 0.02,
    "half_span_lat": 0.015,
    "map_level": 12,
    "map_type": 6,
}

DEFAULT_WINDOW_RECT = {"Left": 765, "Right": 1889, "Top": 497, "Bottom": 1201}

#: 手册 2.3.51.2 示例（横轴墨卡托，Krasovsky 椭球）。仅用于模拟回放。
DEFAULT_SYSTEM_CRS = {
    "CoordType": 4,
    "iPrjType": 2,
    "PrjName": "",
    "strPrjType": "",
    "Geogcs": "",
    "DATUM": "",
    "SPHEROID": "",
    "PRIMEM": "",
    "PROJECTION": "",
    "SemimajorAxis": 6378245.0,
    "SemiminorAxis": 0.0,
    "InverseFlattening": 298.5,
    "PrimeMeridian": 0.0,
    "AngularUnit": 0.0,
    "LinearUnit": 0.0,
    "FalseEasting": 500000.0,
    "FalseNorthing": 0.0,
    "CentralMeridian": 105.0,
    "ScaleFactor": 1.0,
    "LatitudeOfOrigin": 0.0,
    "LatitudeOfCenter": 0.0,
    "LongitudeOfCenter": 0.0,
    "StandardParallel_1": 0.0,
    "StandardParallel_2": 0.0,
    "Latitude_Of_1st_Point": 0.0,
    "Latitude_Of_2nd_Point": 0.0,
    "Longitude_Of_1st_Point": 0.0,
    "Longitude_Of_2nd_Point": 0.0,
    "Azimuth": 0.0,
}

#: 手册 2.3.28.2 示例的像素比例表（MeterX 前若干项）。模拟值，仅用于回放。
DEFAULT_METER_X = [
    71118.546663,
    35869.274714,
    17968.896038,
    8994.048468,
    4498.893496,
    2249.849208,
    1124.932563,
    562.467276,
    281.233763,
    140.618219,
    70.309442,
    35.154804,
    17.577423,
    8.788716,
    4.394358,
    2.197179,
    1.09859,
    0.549295,
    0.274647,
    0.137324,
    0.068662,
    0.034331,
    0.017165,
    0.008583,
    0.004291,
    0.002146,
    0.001073,
    0.000536,
]
DEFAULT_METER_Y = [
    72663.041127,
    35869.274714,
    18841.98996,
    9240.311342,
    4563.627696,
    2266.405999,
    1129.118394,
    563.519465,
    281.49752,
    140.684244,
    70.325959,
    35.158934,
    17.578455,
    8.788975,
    4.394423,
    2.197195,
    1.098594,
    0.549296,
    0.274648,
    0.137324,
    0.068662,
    0.034331,
    0.017165,
    0.008583,
    0.004291,
    0.002146,
    0.001073,
    0.000536,
]


class MockWorldError(Exception):
    """模拟世界无法完成该操作（server 会映射成 ret<0）。"""

    def __init__(self, message: str, *, ret: int = -4) -> None:
        super().__init__(message)
        self.message = message
        self.ret = ret


@dataclass
class _StoredObject:
    obj_id: int
    type_id: int
    parent_id: int
    name: str
    comment: str
    detail: dict[str, Any]
    children: list[int] = field(default_factory=list)
    srv_id: str = ""
    tm_modify: str = "2024/01/01 00:00:00"


class MockWorld:
    """内存收藏夹对象树。"""

    def __init__(
        self,
        *,
        version: int = 817,
        vip_level: int = 9,
        load_ok: int = 1,
        start_obj_id: int = 1_000_000_001,
        viewport: Mapping[str, Any] | None = None,
        window_rect: Mapping[str, Any] | None = None,
        system_crs: Mapping[str, Any] | None = None,
    ) -> None:
        self.version = int(version)
        self.vip_level = int(vip_level)
        self.load_ok = int(load_ok)
        self._next_obj_id = int(start_obj_id)
        self._objects: dict[int, _StoredObject] = {}
        self._root_children: list[int] = []
        self.viewport: dict[str, Any] = dict(viewport or DEFAULT_VIEWPORT)
        self.window_rect: dict[str, Any] = dict(window_rect or DEFAULT_WINDOW_RECT)
        self.system_crs: dict[str, Any] = dict(system_crs or DEFAULT_SYSTEM_CRS)
        self.meter_x: list[float] = list(DEFAULT_METER_X)
        self.meter_y: list[float] = list(DEFAULT_METER_Y)
        #: 20104 选中的对象 ID 列表（测试/回放可设置）。
        self.selected_ids: list[int] = []
        #: 记录最后一次 20301/20303 请求，便于测试断言。
        self.last_map_request: dict[str, Any] | None = None
        self.last_contour_request: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------

    def _new_obj_id(self) -> int:
        obj_id = self._next_obj_id
        self._next_obj_id += 1
        return obj_id

    @staticmethod
    def _srv_id_for(obj_id: int) -> str:
        digest = hashlib.sha256(f"ovital-mock-{obj_id}".encode("utf-8")).hexdigest()
        return str(int(digest[:15], 16) + 4_000_000_000_000_000_000)

    def reset(self) -> None:
        """清空对象树（保留计数器，保证后续 ID 不重复）。"""

        self._objects.clear()
        self._root_children.clear()
        self.selected_ids.clear()

    def object_count(self) -> int:
        return len(self._objects)

    def object_ids(self) -> list[int]:
        return list(self._objects)

    # ------------------------------------------------------------------
    # 写入（20112）
    # ------------------------------------------------------------------

    def create_tree(self, tree: Mapping[str, Any]) -> list[dict[str, Any]]:
        """处理 20112 的 ``ObjTree``，返回创建的线上节点列表（扁平、创建顺序）。

        根节点按手册 2.3.12.1 是「外层文件夹对象」；其
        ``Object.ObjectDetail.ObjChildren`` 递归创建，子对象挂到根节点下
        （新建文件夹的 ObjID 事先未知，因此子节点 ``ParentID`` 由树结构决定）。
        """

        if not isinstance(tree, Mapping):
            raise MockWorldError("ObjTree 必须是对象")
        created_ids = self._create_node(tree, parent_id=None)
        return [
            self._wire_node(obj_id, include_children=False) for obj_id in created_ids
        ]

    def _create_node(
        self,
        node: Mapping[str, Any],
        *,
        parent_id: int | None,
    ) -> list[int]:
        if not isinstance(node, Mapping):
            raise MockWorldError("对象节点必须是 JSON 对象")
        type_id = _as_int(node.get("Type"), "Type")
        obj_id_raw = node.get("ObjID", 0)
        obj_id = _as_int(obj_id_raw, "ObjID")
        obj = node.get("Object") or {}
        if not isinstance(obj, Mapping):
            raise MockWorldError("Object 必须是对象")
        name = obj.get("Name")
        name = name if isinstance(name, str) else ""
        comment = obj.get("Comment")
        comment = comment if isinstance(comment, str) else ""
        detail = obj.get("ObjectDetail") or {}
        if not isinstance(detail, Mapping):
            raise MockWorldError("ObjectDetail 必须是对象")
        detail_copy = _strip_obj_children(dict(detail))

        if parent_id is None:
            parent = node.get("ParentID", 0)
            parent_id = _as_int(parent, "ParentID")

        existing = self._objects.get(obj_id) if obj_id else None
        if existing is not None:
            # 手册 2.3.12.1：收藏夹中已存在相同 id → 替换。
            existing.type_id = type_id
            existing.parent_id = int(parent_id)
            existing.name = name
            existing.comment = comment
            existing.detail = detail_copy
            stored = existing
        else:
            new_id = obj_id if obj_id else self._new_obj_id()
            stored = _StoredObject(
                obj_id=new_id,
                type_id=type_id,
                parent_id=int(parent_id),
                name=name,
                comment=comment,
                detail=detail_copy,
                srv_id=self._srv_id_for(new_id),
            )
            self._objects[new_id] = stored
            if int(parent_id) == 0:
                self._root_children.append(new_id)
            elif int(parent_id) in self._objects:
                self._objects[int(parent_id)].children.append(new_id)
            else:
                # 父节点不存在：手册未说明；模拟器拒绝而不是静默丢对象。
                del self._objects[new_id]
                raise MockWorldError(
                    f"ParentID={parent_id} 不存在（模拟器不静默挂接）", ret=-4
                )

        created: list[int] = [stored.obj_id]
        # 递归处理嵌套子对象（文件夹的 ObjChildren / 其它对象的树形写法）
        for child in _children_of(node):
            created.extend(self._create_node(child, parent_id=stored.obj_id))
        return created

    # ------------------------------------------------------------------
    # 读取（20101 / 20110 / 20104 / 20100）
    # ------------------------------------------------------------------

    def _wire_node(self, obj_id: int, *, include_children: bool = True) -> dict[str, Any]:
        stored = self._objects[obj_id]
        detail = dict(stored.detail)
        if stored.type_id == C.OBJ_TYPE_GROUP:
            detail["Child"] = len(stored.children)
            if include_children:
                detail["ObjChildren"] = [
                    self._wire_node(child_id, include_children=True)
                    for child_id in stored.children
                ]
        return {
            "Type": stored.type_id,
            "ObjID": str(stored.obj_id),
            "ParentID": stored.parent_id,
            "SrvID": stored.srv_id,
            "tmModify": stored.tm_modify,
            "Object": {
                "Name": stored.name,
                "Comment": stored.comment,
                "Type": stored.type_id,
                "ObjectDetail": detail,
            },
        }

    def read_obj(
        self,
        obj_id: int,
        *,
        include_children: bool = True,
        latlng_type: int = 1,
    ) -> dict[str, Any] | None:
        """按 20101 返回节点；``latlng_type`` 按手册 2.2.2 转换坐标。

        模拟器支持 0/1（经纬度，按 CGCS2000 处理）与 2（GCJ-02）；3/4 需要投影
        参数，模拟器不实现（返回 -4，手册 2.2.2 允许但模拟器未实现）。
        """

        if obj_id not in self._objects:
            return None
        node = self._wire_node(obj_id, include_children=include_children)
        return self._convert_node_latlng(node, latlng_type)

    def read_children(self, folder_id: int) -> list[dict[str, Any]]:
        stored = self._objects.get(folder_id)
        if stored is None:
            return []
        return [self._wire_node(child_id) for child_id in stored.children]

    def list_root(self) -> list[dict[str, Any]]:
        return [self._wire_node(obj_id, include_children=False) for obj_id in self._root_children]

    def selected_objects(self) -> list[dict[str, Any]]:
        result = []
        for obj_id in self.selected_ids:
            stored = self._objects.get(obj_id)
            if stored is None:
                continue
            result.append(
                {
                    "ObjID": stored.obj_id,  # 手册 2.3.17.2 示例：int
                    "Type": stored.type_id,
                    "Name": stored.name,
                }
            )
        return result

    def selected_tree(self, *, latlng_type: int = 1) -> dict[str, Any]:
        """构造 20110 响应：根容器 + 选中对象树（仿手册 2.3.18.2 示例）。"""

        children = [
            self._wire_node(obj_id, include_children=True)
            for obj_id in self.selected_ids
            if obj_id in self._objects
        ]
        root = {
            "Type": C.OBJ_TYPE_GROUP,
            "ObjID": "9",  # 手册 2.3.18.2 示例根节点 ObjID="9"
            "ParentID": 0,
            "Object": {
                "Name": "",
                "Type": C.OBJ_TYPE_GROUP,
                "ObjectDetail": {
                    "Child": len(children),
                    "LoadOk": 1,
                    "SaveMerge": 0,
                    "Group": 0,
                    "AutoLoad": 1,
                    "ShowLevel": 1,
                    "ShowLevelMax": 0,
                    "Crypt": 0,
                    "Share": 0,
                    "ReadOnly": 0,
                    "NotHotId": 0,
                    "Relate": 0,
                    "ObjChildren": children,
                },
            },
        }
        return {"ObjItems": [self._convert_node_latlng(root, latlng_type)]}

    # ------------------------------------------------------------------
    # 删除（20103）
    # ------------------------------------------------------------------

    def delete(self, obj_ids: Iterable[int], *, no_group: int = 1) -> dict[str, Any]:
        deleted: list[int] = []
        skipped: list[int] = []
        for obj_id in obj_ids:
            stored = self._objects.get(int(obj_id))
            if stored is None:
                skipped.append(int(obj_id))
                continue
            if stored.type_id == C.OBJ_TYPE_GROUP and no_group == 1:
                # 手册 2.3.13.1：NoGroup=「不删除文件夹」；模拟器按字面实现。
                skipped.append(stored.obj_id)
                continue
            self._remove_recursive(stored.obj_id)
            deleted.append(stored.obj_id)
        return {"deleted": deleted, "skipped": skipped}

    def _remove_recursive(self, obj_id: int) -> None:
        stored = self._objects.get(obj_id)
        if stored is None:
            return
        for child_id in list(stored.children):
            self._remove_recursive(child_id)
        self._objects.pop(obj_id, None)
        if stored.parent_id == 0:
            if obj_id in self._root_children:
                self._root_children.remove(obj_id)
        else:
            parent = self._objects.get(stored.parent_id)
            if parent and obj_id in parent.children:
                parent.children.remove(obj_id)
        if obj_id in self.selected_ids:
            self.selected_ids.remove(obj_id)

    # ------------------------------------------------------------------
    # 查找（20107）
    # ------------------------------------------------------------------

    def search(
        self,
        *,
        name: str,
        srh_opt: int = 5,
        obj_type: int = 0,
        parent_id: int = 0,
        no_child: int = 0,
    ) -> list[dict[str, Any]]:
        """按手册 2.3.22 / 2.2.4 的 SrhOpt 做简单匹配（模拟实现）。"""

        text = name or ""

        def match(stored: _StoredObject) -> bool:
            if srh_opt in (0,):
                return text in stored.name
            if srh_opt in (1,):
                return stored.name.startswith(text)
            if srh_opt in (2,):
                return text in stored.comment
            if srh_opt in (3,):
                return stored.comment.startswith(text)
            if srh_opt in (4,):
                return stored.name == text
            if srh_opt in (5,):
                return stored.name == text
            if srh_opt in (6,):
                return stored.comment == text
            # 11/12/21/22/23 依赖样式/图标/线型，模拟器没有这些属性
            raise MockWorldError(
                f"SrhOpt={srh_opt} 需要样式/图标属性，模拟器未实现（手册 2.2.4）",
                ret=-4,
            )

        candidates: Iterable[_StoredObject]
        if parent_id == 0:
            candidates = self._objects.values()
        elif no_child == 1:
            parent = self._objects.get(int(parent_id))
            candidates = (
                [self._objects[cid] for cid in parent.children if cid in self._objects]
                if parent
                else []
            )
        else:
            candidates = self._objects.values()

        results: list[dict[str, Any]] = []
        for stored in candidates:
            if obj_type and stored.type_id != obj_type:
                continue
            if not match(stored):
                continue
            results.append({"ObjID": stored.obj_id, "ObjType": stored.type_id})
        return results

    # ------------------------------------------------------------------
    # 图形量算（20802）
    # ------------------------------------------------------------------

    def shape_metrics(self, obj_id: int) -> dict[str, Any]:
        """返回 20802 msg（手册 2.3.48）。ObjID=0 表示没找到图形对象。"""

        stored = self._objects.get(int(obj_id))
        if stored is None or stored.type_id != C.OBJ_TYPE_SHAPE:
            return {
                "ObjID": 0,
                "PointCnt": 0,
                "AreaCnt": 0,
                "Perimeter": 0.0,
                "AreaSize": 0.0,
            }
        detail = stored.detail
        gcj02 = _as_int(detail.get("Gcj02", 0), "Gcj02")
        semantic = semantic_for_shape_gcj02(gcj02)
        latlng = detail.get("Latlng")
        if not isinstance(latlng, list):
            raise MockWorldError("Shape.Latlng 缺失", ret=-4)
        pairs = _flat_pairs(latlng)
        vertices = [self._canonical(lat, lng, semantic) for lat, lng in pairs]
        point_count = len(vertices)
        if point_count < 3:
            # 手册 2.4.8：两个点的图形是圆/扇形/矩形，面积依赖起止角度，
            # 模拟器没有实现该几何，返回 0 并标注（待实测真实语义）。
            return {
                "ObjID": stored.obj_id,
                "PointCnt": point_count,
                "AreaCnt": 0 if point_count < 3 else 1,
                "Perimeter": 0.0,
                "AreaSize": 0.0,
                "_note": "两点图形（圆/扇形/矩形）的模拟量算未实现（手册 2.4.8 待实测）",
            }

        area_m2, perimeter_km = _polygon_metrics(vertices)
        return {
            "ObjID": stored.obj_id,
            "PointCnt": point_count,
            "AreaCnt": 1,  # 单环单区域；多区域/孔洞表示待实测
            "Perimeter": round(perimeter_km, 6),
            "AreaSize": round(area_m2, 6),
        }

    def shape_vertices_canonical(self, obj_id: int) -> list[tuple[float, float]]:
        """取图形的规范 ``(lon, lat)`` 顶点（测试/回放用于对比）。"""

        stored = self._objects.get(int(obj_id))
        if stored is None or stored.type_id != C.OBJ_TYPE_SHAPE:
            raise MockWorldError(f"对象 {obj_id} 不是图形", ret=-4)
        detail = stored.detail
        semantic = semantic_for_shape_gcj02(_as_int(detail.get("Gcj02", 0), "Gcj02"))
        pairs = _flat_pairs(detail.get("Latlng"))
        return [self._canonical(lat, lng, semantic) for lat, lng in pairs]

    # ------------------------------------------------------------------
    # LatlngType 坐标转换（模拟 20101/20110 的返回口径）
    # ------------------------------------------------------------------

    def _convert_node_latlng(self, node: dict[str, Any], latlng_type: int) -> dict[str, Any]:
        """把节点（含子树）的坐标转换成请求的 LatlngType 口径（手册 2.2.2）。"""

        if latlng_type not in (0, 1, 2):
            raise MockWorldError(
                f"LatlngType={latlng_type} 模拟器未实现（手册 2.2.2 允许 0-4，"
                "但模拟器只支持 0/1/2）",
                ret=-4,
            )
        target_gcj = 1 if latlng_type == 2 else 0
        obj = node.get("Object")
        if not isinstance(obj, dict):
            return node
        detail = obj.get("ObjectDetail")
        if isinstance(detail, dict):
            type_id = node.get("Type")
            if type_id == C.OBJ_TYPE_SHAPE:
                self._convert_shape_detail(detail, target_gcj)
            elif type_id == C.OBJ_TYPE_SIGN:
                self._convert_sign_detail(detail, target_gcj)
            children = detail.get("ObjChildren")
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        self._convert_node_latlng(child, latlng_type)
        return node

    def _convert_shape_detail(self, detail: dict[str, Any], target_gcj: int) -> None:
        current = _as_int(detail.get("Gcj02", 0), "Gcj02")
        if current == target_gcj:
            return
        if current not in (0, 1):
            raise MockWorldError("平面坐标图形无法在模拟器中转换（手册 2.4.8）", ret=-4)
        pairs = _flat_pairs(detail.get("Latlng"))
        converted = [self._convert_pair(lat, lng, current, target_gcj) for lat, lng in pairs]
        flat: list[float] = []
        for lat, lng in converted:
            flat.extend((lat, lng))
        detail["Latlng"] = flat
        detail["Gcj02"] = target_gcj

    def _convert_sign_detail(self, detail: dict[str, Any], target_gcj: int) -> None:
        current = _as_int(detail.get("Gcj02", 0), "Gcj02")
        if current == target_gcj:
            return
        if current not in (0, 1):
            raise MockWorldError("平面坐标标签无法在模拟器中转换（手册 2.4.6）", ret=-4)
        lat = float(detail.get("Lat", 0.0))
        lng = float(detail.get("Lng", 0.0))
        new_lat, new_lng = self._convert_pair(lat, lng, current, target_gcj)
        detail["Lat"] = new_lat
        detail["Lng"] = new_lng
        detail["Gcj02"] = target_gcj

    @staticmethod
    def _convert_pair(
        lat: float, lng: float, source_gcj: int, target_gcj: int
    ) -> tuple[float, float]:
        """``(lat,lng)`` 在 CGCS2000(0) 与 GCJ-02(1) 之间转换（公开近似算法）。"""

        if source_gcj == 0:
            lon, lat_c = lng, lat
        else:
            lon, lat_c = gcj02_to_cgcs2000(lng, lat)
        if target_gcj == 0:
            return (lat_c, lon)
        g_lon, g_lat = cgcs2000_to_gcj02(lon, lat_c)
        return (g_lat, g_lon)

    @staticmethod
    def _canonical(lat: float, lng: float, semantic: CoordSemantic) -> tuple[float, float]:
        if semantic == CoordSemantic.CGCS2000:
            return (lng, lat)
        if semantic == CoordSemantic.GCJ02:
            # 模拟器内部用公开近似算法转换；真实奥维如何返回 GCJ-02 待实测。
            return latlng_to_canonical(
                LatLng(lat=lat, lng=lng, semantic=semantic),
                allow_gcj02_conversion=True,
            )
        raise MockWorldError(
            "平面坐标图形的量算需要投影参数，模拟器未实现（手册 2.4.8 Gcj02=2）",
            ret=-4,
        )

    # ------------------------------------------------------------------
    # 视图/坐标系（20016/20730/20212/20222/20501）
    # ------------------------------------------------------------------

    def screen_corners(self, req_coord_type: int = 1) -> dict[str, Any]:
        view = self.viewport
        center_lon = float(view["center_lon"])
        center_lat = float(view["center_lat"])
        half_lon = float(view["half_span_lon"])
        half_lat = float(view["half_span_lat"])
        return {
            "latc": center_lat,
            "lngc": center_lon,
            "latlt": center_lat + half_lat,
            "lnglt": center_lon - half_lon,
            "latrb": center_lat - half_lat,
            "lngrb": center_lon + half_lon,
            "ReqCoordType": int(req_coord_type),
            "RealLl": 1 if req_coord_type == 1 else 0,
            "MapLevel": view.get("map_level", 12),
            "MapType": view.get("map_type", 6),
        }

    def pixel_ratio(self, x: int = 0, y: int = 0) -> dict[str, Any]:
        return {"x": int(x), "y": int(y), "MeterX": list(self.meter_x), "MeterY": list(self.meter_y)}

    # ------------------------------------------------------------------
    # 模拟地图渲染（20301）
    # ------------------------------------------------------------------

    def render_map_png(
        self,
        *,
        bbox: tuple[float, float, float, float],
        coord_type: int,
        max_level: int,
        max_pixel: int,
    ) -> bytes:
        """渲染带 SIMULATED MAP 水印的 PNG（绝不冒充奥维真实地图）。"""

        self.last_map_request = {
            "bbox": bbox,
            "coord_type": coord_type,
            "max_level": max_level,
            "max_pixel": max_pixel,
        }
        return render_simulated_map_png(
            bbox=bbox,
            coord_type=coord_type,
            max_level=max_level,
            max_pixel=max_pixel,
        )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise MockWorldError(f"{field_name} 不能是 bool")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise MockWorldError(f"{field_name} 需要整数，收到 {value!r}")


def _children_of(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    obj = node.get("Object") or {}
    if not isinstance(obj, Mapping):
        return []
    detail = obj.get("ObjectDetail") or {}
    if not isinstance(detail, Mapping):
        return []
    children = detail.get("ObjChildren")
    if children is None:
        return []
    if isinstance(children, Mapping):
        return [children]
    if isinstance(children, list):
        return [child for child in children if isinstance(child, Mapping)]
    raise MockWorldError("ObjChildren 必须是数组")


def _strip_obj_children(detail: dict[str, Any]) -> dict[str, Any]:
    detail.pop("ObjChildren", None)
    return detail


def _flat_pairs(latlng: Sequence[Any]) -> list[tuple[float, float]]:
    if not latlng:
        return []
    if isinstance(latlng[0], Mapping):
        pairs = []
        for element in latlng:
            if not isinstance(element, Mapping):
                raise MockWorldError("Shape.Latlng 混合了对象与数字")
            pairs.append((float(element.get("lat", 0.0)), float(element.get("lng", 0.0))))
        return pairs
    if len(latlng) % 2 != 0:
        raise MockWorldError(f"Shape.Latlng 长度必须为偶数，收到 {len(latlng)}")
    return [
        (float(latlng[i]), float(latlng[i + 1])) for i in range(0, len(latlng), 2)
    ]


def _polygon_metrics(vertices: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """用局部米制投影计算面积（㎡）与周长（km）。

    与 ``ovital_mcp.coordinates.metric_crs_for`` 同一口径；真实奥维 20802 的
    面积/周长算法未公开（待实测），模拟器只是用标准 GIS 口径给出可对账值。
    """

    from pyproj import Transformer
    from shapely.geometry import Polygon

    mean_lon = sum(point[0] for point in vertices) / len(vertices)
    mean_lat = sum(point[1] for point in vertices) / len(vertices)
    metric_crs = metric_crs_for(mean_lon, mean_lat, kind="utm")
    transformer = Transformer.from_crs("EPSG:4490", metric_crs, always_xy=True)
    projected = [transformer.transform(lon, lat) for lon, lat in vertices]
    polygon = Polygon(projected)
    if not polygon.is_valid:
        # 模拟器不做几何修复：无效多边形显式报错，避免给出假面积。
        raise MockWorldError(
            "模拟器计算的图形无效（自交等），拒绝返回面积（真实奥维行为待实测）",
            ret=-4,
        )
    return float(polygon.area), float(polygon.length / 1000.0)


def render_simulated_map_png(
    *,
    bbox: tuple[float, float, float, float],
    coord_type: int,
    max_level: int,
    max_pixel: int,
) -> bytes:
    """生成带水印/经纬网/范围文字的模拟地图 PNG。

    - 图上明确写 ``SIMULATED MAP`` 与 ``NOT REAL OVITAL DATA``；
    - PNG 元数据（tEXt）里也写 ``Simulated=True``、``verified_against_real_device=False``，
      便于程序化校验（防止把模拟图误当真实影像）。
    """

    from PIL import Image, ImageDraw, ImageFont
    from PIL.PngImagePlugin import PngInfo

    min_lon, min_lat, max_lon, max_lat = bbox
    lon_span = max(max_lon - min_lon, 1e-9)
    lat_span = max(max_lat - min_lat, 1e-9)
    # max_pixel 是像素总数预算（手册 2.3.29 只写「最大像素」）：按平方根取边长，
    # 再按 bbox 长宽比决定高，最终保证 width*height 不超过预算。
    pixel_budget = max(64, min(int(max_pixel), 4096 * 4096))
    base = int(math.sqrt(pixel_budget))
    width = max(64, min(base, 1024))
    aspect = lat_span / lon_span
    height = int(width * aspect)
    height = max(48, min(height, 1024))
    if width * height > pixel_budget:
        scale = math.sqrt(pixel_budget / (width * height))
        width = max(48, int(width * scale))
        height = max(48, int(height * scale))

    image = Image.new("RGB", (width, height), (245, 246, 240))
    draw = ImageDraw.Draw(image)

    def font(size: int):
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # pragma: no cover - 老版本 Pillow
            return ImageFont.load_default()

    # 经纬网（6x6）与坐标标注
    for index in range(1, 6):
        x = int(width * index / 6)
        y = int(height * index / 6)
        draw.line([(x, 0), (x, height)], fill=(200, 203, 196), width=1)
        draw.line([(0, y), (width, y)], fill=(200, 203, 196), width=1)
        lon_label = min_lon + lon_span * index / 6
        lat_label = max_lat - lat_span * index / 6
        draw.text((x + 2, 2), f"{lon_label:.4f}", fill=(120, 120, 120), font=font(10))
        draw.text((2, y + 1), f"{lat_label:.4f}", fill=(120, 120, 120), font=font(10))

    draw.rectangle([0, 0, width - 1, height - 1], outline=(40, 40, 40), width=2)
    draw.text((6, 6), "SIMULATED MAP", fill=(180, 30, 30), font=font(20))
    draw.text(
        (6, 30),
        "NOT REAL OVITAL DATA - mock server for offline tests",
        fill=(120, 30, 30),
        font=font(11),
    )
    lines = [
        f"bbox lon/lat: {min_lon:.6f},{min_lat:.6f} - {max_lon:.6f},{max_lat:.6f}",
        f"CoordType={coord_type}  MaxLevel={max_level}  MaxPixel={max_pixel}",
        f"image: {width}x{height}px  simulated=true",
        "verified_against_real_device=false",
    ]
    y_text = height - 12 * len(lines) - 6
    for offset, line in enumerate(lines):
        draw.text((6, y_text + offset * 12), line, fill=(60, 60, 60), font=font(10))

    metadata = PngInfo()
    metadata.add_text("Description", "SIMULATED MAP - NOT REAL OVITAL DATA")
    metadata.add_text("Software", "ovital_mcp.mock.MockWorld")
    metadata.add_text("Simulated", "True")
    metadata.add_text("verified_against_real_device", "False")
    metadata.add_text(
        "bbox_lonlat", f"{min_lon:.6f},{min_lat:.6f},{max_lon:.6f},{max_lat:.6f}"
    )
    metadata.add_text("CoordType", str(coord_type))
    metadata.add_text("MaxLevel", str(max_level))
    metadata.add_text("MaxPixel", str(max_pixel))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=metadata)
    return buffer.getvalue()
