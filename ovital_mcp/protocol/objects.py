"""奥维对象（Shape / Sign / 文件夹树）的请求构造器。

写草稿的工具层用这些构造器生成 20112 的 ``msg.ObjTree``，**不要手写长顶点列表**。

字段依据
========
- 手册 2.3.12.1「设置收藏夹对象」请求示例（ObjTree 外层是文件夹对象，
  ``Object.ObjectDetail.ObjChildren`` 内层是待添加对象）。
- 手册 2.4.6 ``Sign`` 结构体、手册 2.4.8 ``Shape`` 结构体。
- 官方样例 ``omapobj.js``：``BuildShape`` / ``BuildSign`` / ``BuildTree`` /
  ``BuildGroup``（这些构造器才是官方实际发出去的消息形状）。

已知的官方内部不一致（全部写入 docs/protocol-notes.md 的「待实测」）
------------------------------------------------------------------
1. 手册 2.4.8 **表格**字段名带匈牙利前缀（``nMtp``/``dStartAngle``/``iFillType``/
   ``fFillRotate``/``Edit``），而**示例**与官方 ``BuildShape`` 用无前缀名
   （``Mtp``/``StartAngle``/``FillType``/``FillRotate``/``EditMode``）。
   本模块按「示例 + 官方样例」发送，因为那是官方实际发出去的格式。
2. 手册 2.4.6 ``Sign`` 表格把 ``Lat`` 写了两遍（应为 Lat/Lng），示例有 ``Lng``。
3. ``ObjTree`` 根：手册示例是「根节点 ParentID=0、ObjID=0」；官方样例
   ``BuildTree`` 的根是**合成容器**（``Object.Name=""``，``ParentID`` = 目标父
   文件夹 id），子对象的 ``ParentID`` 也是目标父文件夹 id。本实现按任务书/
   工具层需要：根节点即草稿文件夹（有名字），``ParentID`` 由 ``parent_id``
   参数决定（默认 0=收藏夹根），子对象 ``ParentID`` 设为该文件夹的
   ``folder_id``（新建时为 0，由奥维按树结构挂接）。**该细节待实测。**
4. 颜色整数编码（``LineClr``/``AreaClr``）手册只给示例值，未说明是
   0xRRGGBB / 0xAARRGGBB / 0xBBGGRR → 原样透传，编码语义待实测。
"""

from __future__ import annotations

import copy
import time
from typing import Any, Iterable, Mapping, Sequence

from ..coordinates import (
    CoordSemantic,
    SHAPE_GCJ02,
    canonical_to_latlng,
    verify_canonical_lonlat,
)
from ..errors import CoordinateSemanticError, DataValidationError
from . import constants as C
from .codec import build_delete_request, build_search_request
from .errors import ProtocolDecodeError
from .models import object_point_lonlat

__all__ = [
    "build_shape_object",
    "build_sign_object",
    "build_write_tree",
    "build_delete_request",
    "build_search_request",
    "object_point_lonlat",
    "format_tm_modify",
    "SEMANTIC_TO_SHAPE_GCJ02",
    "DEFAULT_SHAPE_STYLE",
]

#: 语义 -> ``Shape.ObjectDetail.Gcj02``（手册 2.4.8：0=CGCS2000, 1=GCJ-02, 2=平面）。
SEMANTIC_TO_SHAPE_GCJ02: dict[CoordSemantic, int] = {v: k for k, v in SHAPE_GCJ02.items()}

#: 默认样式（颜色值透传；编码语义待实测）。
DEFAULT_SHAPE_STYLE: dict[str, int] = {
    "line_color": 0x00FF8000,
    "line_width": 3,
    "line_alpha": 50,  # 官方样例 BuildShape: LineAlpha=50
    "area_color": 0x40FF8000,
    "area_alpha": 60,
    "show_flag": 2,  # 手册 2.4.8：0 不显示 / 1 名称 / 2 面积 / 3 名称+面积
}


def format_tm_modify(epoch: float | None = None) -> str:
    """生成手册示例风格的时间字符串 ``YYYY/MM/DD HH:MM:SS``。

    手册示例里显示为 ``2019/09/09?11:11:59``，其中 ``?`` 是 HTML 把空格转义后
    的残留；官方样例 ``GetNowFormatDate`` 用 ``YYYY/MM/DD HH:MM:SS``。
    """

    timestamp = time.localtime(epoch) if epoch is not None else time.localtime()
    return time.strftime("%Y/%m/%d %H:%M:%S", timestamp)


def _shape_gcj02_for(semantic: CoordSemantic) -> int:
    try:
        return SEMANTIC_TO_SHAPE_GCJ02[semantic]
    except KeyError as exc:
        raise CoordinateSemanticError(
            f"Shape 不支持坐标语义 {semantic.value}（手册 2.4.8 只有 "
            "0=CGCS2000 / 1=GCJ-02 / 2=平面）",
            details={"semantic": semantic.value},
        ) from exc


def _latlng_flat(
    vertices_lonlat: Sequence[tuple[float, float]],
    semantic: CoordSemantic,
    *,
    allow_gcj02_conversion: bool,
) -> list[float]:
    """把 ``[(lon,lat), ...]`` 转成 ``Shape.Latlng`` 扁平数组（**纬度在前**）。

    手册 2.4.8 示例 ``[39.01064751, 106.57287598, 38.52668162, 107.10571289]``：
    39.01 是北纬、106.57 是东经 → 扁平数组顺序是 ``lat, lng``。
    """

    flat: list[float] = []
    for index, point in enumerate(vertices_lonlat):
        if not (isinstance(point, (tuple, list)) and len(point) == 2):
            raise ProtocolDecodeError(
                f"vertices_lonlat[{index}] 必须是 (lon, lat) 二元组，收到 {point!r}"
            )
        lon, lat = verify_canonical_lonlat(float(point[0]), float(point[1]))
        if semantic == CoordSemantic.CGCS2000:
            flat.extend((lat, lon))
        elif semantic == CoordSemantic.GCJ02:
            if not allow_gcj02_conversion:
                raise CoordinateSemanticError(
                    "写 GCJ-02 坐标需要显式 allow_gcj02_conversion=True"
                    "（公开近似算法；默认应写 CGCS2000/Gcj02=0）",
                    details={"semantic": semantic.value},
                )
            latlng = canonical_to_latlng(
                lon, lat, semantic, allow_gcj02_conversion=True
            )
            flat.extend((latlng.lat, latlng.lng))
        else:
            raise CoordinateSemanticError(
                f"写图形不支持坐标语义 {semantic.value}；手册 2.4.8 的平面坐标"
                "需要投影参数，适配器未实现",
                details={"semantic": semantic.value},
            )
    return flat


def build_shape_object(
    *,
    name: str,
    comment: str,
    vertices_lonlat: Sequence[tuple[float, float]],
    semantic: CoordSemantic = CoordSemantic.CGCS2000,
    line_color: int = DEFAULT_SHAPE_STYLE["line_color"],
    line_width: int = DEFAULT_SHAPE_STYLE["line_width"],
    area_color: int = DEFAULT_SHAPE_STYLE["area_color"],
    area_alpha: int = DEFAULT_SHAPE_STYLE["area_alpha"],
    show_flag: int = DEFAULT_SHAPE_STYLE["show_flag"],
    allow_gcj02_conversion: bool = False,
    parent_id: int = 0,
) -> dict[str, Any]:
    """构造 Type=13 图形节点（20112 的 ``ObjChildren[]`` 元素）。

    参数
    ----
    vertices_lonlat
        内部规范 ``(lon, lat)`` 顶点序列（EPSG:4490 语义，**经度在前**）。
        至少 2 个点；2 个点在手册 2.4.8 里表示圆/扇形/矩形（用起止角度描述），
        **不是**多边形——写集水区边界时请传 >=3 个点。
    semantic
        写入坐标语义；默认 ``CGCS2000`` → ``Gcj02=0``（推荐，避免 GCJ-02 偏移）。
    line_color / area_color
        ARGB/RGB 整数原样透传（编码语义待实测，见模块 docstring 第 4 条）。

    返回节点结构::

        {"Type":13, "ObjID":0, "ParentID":0,
         "Object":{"Name":..., "Comment":..., "Type":13,
                   "ObjectDetail":{"Gcj02":0, "Mtp":N, "Latlng":[lat,lng,...], ...}}}
    """

    if not isinstance(name, str) or not name:
        raise ProtocolDecodeError("图形名称不能为空")
    vertices = list(vertices_lonlat)
    if len(vertices) < 2:
        raise ProtocolDecodeError(
            f"图形至少需要 2 个顶点（手册 2.4.8），收到 {len(vertices)}"
        )
    if len(vertices) == 2:
        # 允许，但明确提醒：两个点是圆/扇形/矩形语义，不是多边形。
        pass
    if len(vertices) > 100_000:
        raise ProtocolDecodeError(
            "顶点数超过 100000，拒绝构造（防止意外把整条轨迹写进图形）"
        )
    gcj02 = _shape_gcj02_for(semantic)
    flat = _latlng_flat(
        vertices, semantic, allow_gcj02_conversion=allow_gcj02_conversion
    )

    detail: dict[str, Any] = {
        # 手册 2.4.8 示例 / 官方样例 omapobj.js BuildShape 实际字段名
        "Gcj02": gcj02,
        "OverlayIdx": 0,
        # 官方样例 BuildShape 带 TrackType=0（手册 2.4.8 未列；贴近官方样例）
        "TrackType": 0,
        "ShowLevel": 1,
        "ShowLevelMax": 0,
        "ShowLevelBak": 0,
        "OuterRgnIdx": 0,
        "EditMode": 0,
        "ShowFlag": int(show_flag),
        "LineClr": int(line_color),
        "LineWidth": int(line_width),
        "LineAlpha": int(DEFAULT_SHAPE_STYLE["line_alpha"]),
        "AreaClr": int(area_color),
        "AreaAlpha": int(area_alpha),
        "StartAngle": 0,
        "EndAngle": 0,
        "FillType": 0,
        "FillSubType": 0,
        "FillPattern": 0,
        "FillChgClr": 0,
        "FillRotate": 0,
        "FillScale": 0,
        "FillOffsetX": 0,
        "FillOffsetY": 0,
        "Mtp": len(vertices),
        "Latlng": flat,
    }
    return {
        "Type": C.OBJ_TYPE_SHAPE,
        "ObjID": 0,  # 0 = 新建，由奥维自动生成（手册 2.3.12.1 ObjID 说明）
        "ParentID": int(parent_id),
        "Object": {
            "Name": name,
            "Comment": comment or "",
            "Type": C.OBJ_TYPE_SHAPE,
            "ObjectDetail": detail,
        },
    }


def build_sign_object(
    *,
    name: str,
    comment: str,
    lon: float,
    lat: float,
    semantic: CoordSemantic = CoordSemantic.CGCS2000,
    sign_pic: int = 1,
    allow_gcj02_conversion: bool = False,
    parent_id: int = 0,
) -> dict[str, Any]:
    """构造 Type=7 标签节点（出口点/锚点用）。

    字段依据：手册 2.4.6 ``Sign`` 结构体 + 官方样例 ``BuildSign``。
    ``ObjectDetail`` 用 ``Lat`` / ``Lng`` 命名字段（不是扁平数组），``Gcj02``
    映射同 Shape（手册 2.4.6：0=CGCS2000, 1=GCJ-02）。
    """

    if not isinstance(name, str) or not name:
        raise ProtocolDecodeError("标签名称不能为空")
    lon_v, lat_v = verify_canonical_lonlat(float(lon), float(lat))
    gcj02 = _shape_gcj02_for(semantic)
    if semantic == CoordSemantic.CGCS2000:
        lat_written, lng_written = lat_v, lon_v
    elif semantic == CoordSemantic.GCJ02:
        if not allow_gcj02_conversion:
            raise CoordinateSemanticError(
                "写 GCJ-02 标签需要显式 allow_gcj02_conversion=True"
            )
        latlng = canonical_to_latlng(lon_v, lat_v, semantic, allow_gcj02_conversion=True)
        lat_written, lng_written = latlng.lat, latlng.lng
    else:
        raise CoordinateSemanticError(
            f"写标签不支持坐标语义 {semantic.value}", details={"semantic": semantic.value}
        )

    detail: dict[str, Any] = {
        "Lat": lat_written,
        "Lng": lng_written,
        "Gcj02": gcj02,
        "Altitude": 0,
        "EditMode": 0,
        "OverlayIdx": 0,
        "TxtType": 1,
        "ShowLevel": 1,
        "ShowLevelBak": 0,
        "ShowLevelMax": 0,
        "TimeUncertain": 0,
        "TxtShowSta": 0,
        "TxtShowStaSet": 0,
        "SignEvent": {"Radius": 0, "ShowClr": 0},
        "SignPic": {
            "SignPic": int(sign_pic),
            "AlignFlag": 0,
            "SignClr": 0,
            "PicScale": 0,
            "SignPicNum": 0,
            "SignPicNumOffx": 0,
            "SignPicNumOffy": 0,
            "SignPicNumClr": 0,
            "SignPicNumSize": 0,
        },
    }
    return {
        "Type": C.OBJ_TYPE_SIGN,
        "ObjID": 0,
        "ParentID": int(parent_id),
        "Object": {
            "Name": name,
            "Comment": comment or "",
            "Type": C.OBJ_TYPE_SIGN,
            "ObjectDetail": detail,
        },
    }


def _folder_detail(child_count: int) -> dict[str, Any]:
    """文件夹 ``ObjectDetail``（手册 2.3.12.1 请求示例 / 官方样例 BuildGroup）。"""

    return {
        "Child": int(child_count),
        "LoadOk": 1,
        "SaveMerge": 0,
        "Group": 0,
        "AutoLoad": 1,
        "ShowLevel": 0,
        "ShowLevelMax": 0,
        "ShowLevelBak": 0,
        "Crypt": 0,
        "Share": 0,
        "ReadOnly": 0,
        "NotHotId": 0,
        "Bind": 0,
        "BindCheck": 0,
        "Link": 0,
        "LinkAutoCheck": 0,
        "ChildiOverlay": 0,
        "LinkUrl": "",
        "Relate": 0,
    }


def build_write_tree(
    *,
    folder_id: int,
    folder_name: str,
    children: Sequence[Mapping[str, Any]],
    parent_id: int = 0,
    tm_modify: str | None = None,
) -> dict[str, Any]:
    """构造 20112 请求的 ``msg.ObjTree``（外层文件夹 + 内层对象树）。

    结构严格按手册 2.3.12.1 示例（外层为文件夹对象，``ObjectDetail.ObjChildren``
    为待添加对象）::

        {"Type":30, "ObjID":<folder_id>, "ParentID":<parent_id>,
         "tmModify":"YYYY/MM/DD HH:MM:SS",
         "Object":{"Name":<folder_name>, "Type":30,
                   "ObjectDetail":{... "Child":N, "ObjChildren":[children]}}}

    参数
    ----
    folder_id
        草稿文件夹的 ObjID；**0 表示新建**（手册 2.3.12.1：ObjID 为 0 时自动
        生成，非 0 时替换已有对象）。新建时无法预先知道 id，因此子对象
        ``ParentID`` 也设为 0，由奥维按树结构挂接（**该细节待实测**）。
    parent_id
        文件夹挂到哪个父节点下；0 = 收藏夹根节点（手册 2.2.6）。
    children
        ``build_shape_object`` / ``build_sign_object`` 等构造的节点；本函数会
        复制一份并把它们的 ``ParentID`` 改成 ``folder_id``，不修改调用方对象。
    """

    if not isinstance(folder_name, str) or not folder_name:
        raise ProtocolDecodeError("草稿文件夹名称不能为空")
    if folder_id < 0 or parent_id < 0:
        raise ProtocolDecodeError("folder_id / parent_id 不能为负数")
    child_list = [copy.deepcopy(dict(child)) for child in children]
    for index, child in enumerate(child_list):
        if "Type" not in child or "Object" not in child:
            raise ProtocolDecodeError(
                f"children[{index}] 不是合法的奥维对象节点（缺 Type/Object）；"
                "请用 build_shape_object / build_sign_object 构造"
            )
        child["ParentID"] = int(folder_id)

    detail = _folder_detail(len(child_list))
    detail["ObjChildren"] = child_list
    tree = {
        "Type": C.OBJ_TYPE_GROUP,
        "ObjID": int(folder_id),
        "ParentID": int(parent_id),
        "tmModify": tm_modify or format_tm_modify(),
        "Object": {
            "Name": folder_name,
            "Type": C.OBJ_TYPE_GROUP,
            "ObjectDetail": detail,
        },
    }
    return tree


def ensure_valid_vertices(vertices: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """校验顶点序列（数值合理、无 NaN）；返回规范化副本。

    仅做数值合理性检查（``coordinates.verify_canonical_lonlat``），不做几何
    拓扑校验——拓扑校验属于 GIS 层（``gis.geometry``），不在这里重复实现。
    """

    result: list[tuple[float, float]] = []
    for index, point in enumerate(vertices):
        if not (isinstance(point, (tuple, list)) and len(point) == 2):
            raise ProtocolDecodeError(f"顶点 {index} 不是 (lon, lat)：{point!r}")
        try:
            result.append(verify_canonical_lonlat(float(point[0]), float(point[1])))
        except DataValidationError as exc:
            raise ProtocolDecodeError(
                f"顶点 {index} 坐标非法：{exc.message}", details={"index": index}
            ) from exc
    return result
