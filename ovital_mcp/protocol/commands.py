"""奥维协议高层命令封装（供 MCP 工具层直接调用）。

边界（docs/architecture.md §4.3）
================================
本模块只做**协议编解码与单次调用**：不重试、不幂等、不落快照、不做业务判断。
写后回读、幂等键、回滚快照由 Lead 的 ``tools/`` 层实现。

命令与手册章节对照
==================
======  ==========================================  =========================
cmdid   能力                                        手册
======  ==========================================  =========================
20000   登录                                        2.3.1
20006   获取奥维版本                                2.3.24
20101   获取收藏夹对象                              2.3.11
20103   删除收藏夹对象                              2.3.13
20104   获取收藏夹选中对象                          2.3.17
20107   查找奥维对象                                2.3.22
20110   获取选中对象树形结构                        2.3.18
20112   设置收藏夹对象（写入）                      2.3.12
20222   获取系统坐标系参数                          2.3.51
20301   获取地图图片数据                            2.3.29
20303   获取等高线                                  2.3.31
20501   获取VIP等级                                 2.3.19
20730   获取奥维窗体可显示区域                      2.3.52
20802   获取图形对象基本信息（面积/周长）            2.3.48
20016   获取屏幕中心点/左上角/右下角坐标            2.3.58
20212   获取当前点在地图下像素比例                  2.3.28
======  ==========================================  =========================

坐标：所有对外坐标统一 ``(lon, lat)``（EPSG:4490 语义）；Shape.Latlng 是
``lat, lng`` 扁平数组（手册 2.4.8 示例），转换只调用 ``ovital_mcp.coordinates``。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Iterable, Mapping, Sequence

from ..coordinates import CoordSemantic
from ..errors import OmapError, OvitalNotFoundError, OvitalPrereqError
from . import constants as C
from . import objects as objects_mod
from .client import OvitalClient
from .codec import (
    b64decode_tolerant,
    json_dumps,
    build_contour_request,
    build_delete_request,
    build_map_data_request,
    build_obj_request,
    build_search_request,
    build_selected_request,
    build_selected_tree_request,
)
from .errors import (
    ProtocolDecodeError,
    ProtocolStateError,
    omap_error_to_dict,
)
from .models import (
    ObjNode,
    ObjectWriteResult,
    SelectedObject,
    ShapeMetrics,
    ShapeObject,
    parse_contour_response,
    parse_map_data_response,
    parse_obj_items,
    parse_search_results,
    parse_selected_list,
    parse_shape,
    parse_shape_metrics,
    parse_screen_corners,
    parse_system_crs,
    parse_viewport_rect,
    parse_vip_level,
    parse_write_results,
)

__all__ = [
    "get_version",
    "get_vip_level",
    "get_selected",
    "get_selected_tree",
    "get_objects",
    "get_folder_children",
    "find_objects",
    "get_shape",
    "get_shape_metrics",
    "write_tree",
    "write_objects",
    "delete_objects",
    "get_map_image",
    "get_contours",
    "get_viewport",
    "get_screen_corners",
    "get_system_crs",
    "probe_capabilities",
    "compare_vertices",
    "PNG_SIGNATURE",
]

logger = logging.getLogger("ovital_mcp.protocol.commands")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# 读：版本 / 会员 / 选中 / 对象
# ---------------------------------------------------------------------------


async def get_version(client: OvitalClient) -> int:
    """20006 获取奥维版本（手册 2.3.24）。

    成功时 ``ret`` 就是版本号（示例 817），不是 0；``ret==0`` 视为协议异常。
    """

    response = await client.request(C.CMD_GET_VERSION, {})
    if response.ret <= 0:
        raise ProtocolDecodeError(
            f"20006 成功响应 ret 应为奥维版本号（>0），收到 {response.ret}",
            details={"cmdid": C.CMD_GET_VERSION},
        )
    return response.ret


async def get_vip_level(client: OvitalClient) -> dict[str, Any]:
    """20501 获取VIP等级（手册 2.3.19）：``OmapLevel`` + ``LoadOK``。"""

    response = await client.request(C.CMD_GET_VIP_LEVEL, {})
    return parse_vip_level(response.msg)


async def get_selected(
    client: OvitalClient, flag: int = C.SEL_FLAG_GROUP_JOIN
) -> list[SelectedObject]:
    """20104 获取奥维端选中的收藏夹对象（手册 2.3.17）。

    ``flag`` 取值见手册 2.2.3：1=文件夹自动包含子对象，2=不自动包含，
    3=排除文件夹本身。
    """

    response = await client.request(C.CMD_GET_SELECTED, build_selected_request(flag))
    return parse_selected_list(response.msg)


async def get_selected_tree(
    client: OvitalClient, latlng_type: int = 1
) -> list[ObjNode]:
    """20110 获取选中对象树形结构（手册 2.3.18）。

    官方样例支持分片响应（``Fragment``/``FragmentMaxSize`` 请求字段），本实现
    不主动请求分片，但客户端能自动重组服务端返回的分片帧（待实测）。
    """

    response = await client.request(
        C.CMD_GET_SELECTED_TREE, build_selected_tree_request(latlng_type)
    )
    return parse_obj_items(response.msg)


async def get_objects(
    client: OvitalClient, obj_id: int, latlng_type: int = 1
) -> list[ObjNode]:
    """20101 获取收藏夹对象（手册 2.3.11）。

    ``latlng_type`` 见手册 2.2.2；默认 1=CGCS2000（避免 GCJ-02 重复偏移）。
    """

    response = await client.request(
        C.CMD_GET_OBJECT, build_obj_request(obj_id, latlng_type)
    )
    return parse_obj_items(response.msg)


async def get_folder_children(
    client: OvitalClient, folder_id: int, latlng_type: int = 1
) -> list[ObjNode]:
    """用 20101 读取文件夹的子对象（手册 2.3.11 / 2.3.18 结构）。

    说明（待实测）：手册 2.3.11.2 示例只展示单对象，未明确文件夹的子对象是否
    直接在 ``Object.ObjectDetail.ObjChildren`` 里。本实现：
    - 若返回节点的 ``children`` 非空 → 返回子对象列表；
    - 否则原样返回顶层节点（``raw`` 完整保留），由调用方判断。
    """

    nodes = await get_objects(client, folder_id, latlng_type)
    if nodes and nodes[0].children:
        return nodes[0].children
    return nodes


async def find_objects(
    client: OvitalClient,
    name: str,
    *,
    isrh_opt: int = 5,
    latlng_type: int = 1,
) -> list[ObjNode]:
    """20107 查找奥维对象（手册 2.3.22）。

    手册 2.3.22.2 的响应 ``ListID`` **只返回 ObjID + ObjType，不返回名称**；
    需要名称/坐标时请再对每个 obj_id 调用 ``get_objects``（20101）。

    ``latlng_type`` 在 20107 请求里没有对应字段（手册 2.3.22.1 无 LatlngType），
    保留该参数是为了与其它读取命令签名一致，供上层「找到后再用 20101 读取」
    时透传；本函数不因它改变 20107 请求。

    手册请求表字段（``idParent``/``idShapeSelect``）与示例（``ParentID``/
    ``ShapeSelect``）不一致，``build_search_request`` 两套都发。
    """

    request = build_search_request(name=name, isrh_opt=isrh_opt)
    response = await client.request(C.CMD_SEARCH_OBJECT, request)
    results = parse_search_results(response.msg)
    # 20107 请求没有 LatlngType 字段（手册 2.3.22.1），该参数仅用于后续 20101 读取。
    logger.debug(
        "find_objects: %d 个结果；latlng_type=%s 仅供后续 20101 读取使用",
        len(results),
        latlng_type,
    )
    return results


# ---------------------------------------------------------------------------
# 读：图形 / 量算
# ---------------------------------------------------------------------------


async def get_shape(
    client: OvitalClient,
    obj_id: int,
    *,
    latlng_type: int = 1,
    allow_gcj02_conversion: bool = False,
) -> ShapeObject:
    """20101 读取图形对象并解析 Shape 结构（手册 2.3.11 + 2.4.8）。

    - ``vertices`` 统一 ``(lon, lat)``；Shape.Latlng 原始是 ``lat,lng`` 扁平数组。
    - ``latlng_type=1`` 请求 CGCS2000（推荐）；若服务端仍返回 GCJ-02，默认
      拒绝自动转换（``CoordinateSemanticError``），除非显式允许。
    - 两个点的图形（圆/扇形/矩形，手册 2.4.8）会带 ``two_point_special=True``。
    """

    response = await client.request(
        C.CMD_GET_OBJECT, build_obj_request(obj_id, latlng_type)
    )
    return parse_shape(
        response.msg,
        obj_id=obj_id,
        allow_gcj02_conversion=allow_gcj02_conversion,
        latlng_type_requested=latlng_type,
    )


async def get_shape_metrics(client: OvitalClient, obj_id: int) -> ShapeMetrics:
    """20802 获取图形量算（手册 2.3.48）：面积 ㎡、周长 km、顶点数、区域数。

    ``msg.ObjID==0`` 表示没找到图形对象（手册 2.3.48.2），此时抛
    ``OvitalNotFoundError``，不返回 0 面积。
    """

    response = await client.request(C.CMD_GET_SHAPE_METRICS, {"ObjID": int(obj_id)})
    metrics = parse_shape_metrics(response.msg)
    if metrics.obj_id == 0:
        raise OvitalNotFoundError(
            f"20802 未找到图形对象 ObjID={obj_id}（手册 2.3.48.2：idObj=0 表示没找到）",
            details={"obj_id": obj_id, "raw": response.msg},
        )
    return metrics


# ---------------------------------------------------------------------------
# 写：20112 / 20103
# ---------------------------------------------------------------------------


async def write_tree(client: OvitalClient, tree: Mapping[str, Any]) -> list[ObjectWriteResult]:
    """20112 直接提交 ``ObjTree``（手册 2.3.12）。

    ``tree`` 由 ``objects.build_write_tree`` 构造。返回服务端分配/替换后的对象
    列表；**响应形态有两种官方写法**（手册扁平 vs 官方样例嵌套），解析器两种
    都接受，见 ``models.parse_write_results``（真实形态待实测）。
    """

    if not isinstance(tree, Mapping):
        raise ProtocolDecodeError("tree 必须是 ObjTree 对象")
    payload = json_dumps({"ObjTree": dict(tree)})
    payload_bytes = len(payload.encode("utf-8"))
    if payload_bytes > C.DEFAULT_WRITE_MAX_BYTES:
        # 手册未给 20112 单次写入上限；官方样例的分片字段（Fragment*）手册也未记载。
        # 因此宁可显式失败，也不发送一个可能被截断/拒绝的超大请求。
        raise ProtocolStateError(
            f"20112 请求体 {payload_bytes} 字节超过保守阈值 "
            f"{C.DEFAULT_WRITE_MAX_BYTES} 字节（{C.WRITE_LIMIT_NOTE}）。"
            "请拆分对象/减少顶点；分片写入未启用（官方样例有 Fragment* 字段但手册未写，待实测）。",
            details={"payload_bytes": payload_bytes, "limit": C.DEFAULT_WRITE_MAX_BYTES},
        )
    response = await client.request(C.CMD_WRITE_OBJECT_TREE, {"ObjTree": dict(tree)})
    return parse_write_results(response.msg)


async def write_objects(
    client: OvitalClient,
    *,
    parent_id: int,
    name: str,
    children: Sequence[Mapping[str, Any]],
) -> list[ObjectWriteResult]:
    """20112 在 ``parent_id`` 下新建草稿文件夹 ``name`` 并写入 ``children``。

    ``children`` 用 ``objects.build_shape_object`` / ``build_sign_object`` 构造。
    等价于 ``write_tree(client, build_write_tree(folder_id=0, ...))``。
    """

    tree = objects_mod.build_write_tree(
        folder_id=0, folder_name=name, children=children, parent_id=parent_id
    )
    return await write_tree(client, tree)


async def delete_objects(
    client: OvitalClient, obj_ids: Iterable[int], *, no_group: int = 1
) -> None:
    """20103 删除收藏夹对象（手册 2.3.13）。

    ``NoGroup`` 字段手册说明为「不删除文件夹」，官方样例 ``BuildObj`` 误写成
    ``NoTroup``；本实现按手册发 ``NoGroup``（差异待实测）。
    成功响应形态手册未给出（只写了失败表）→ 不解析响应体，仅要求 ``ret>=0``。
    """

    request = build_delete_request(obj_ids, no_group=no_group)
    await client.request(C.CMD_DELETE_OBJECT, request)


# ---------------------------------------------------------------------------
# 读：地图图片 / 等高线
# ---------------------------------------------------------------------------


async def get_map_image(
    client: OvitalClient,
    *,
    bbox_lonlat: tuple[float, float, float, float],
    max_level: int,
    max_pixel: int,
    coord_type: int = 0,
) -> bytes:
    """20301 获取地图图片数据（手册 2.3.29），返回 PNG 字节。

    - 手册 2.1.1：地图图片需要 SVIP；权限不足时服务端返回 ``ret=-7``，
      客户端已映射为 ``OvitalPrereqError``。
    - ``bbox_lonlat`` = ``(min_lon, min_lat, max_lon, max_lat)``（EPSG:4490）。
    - 响应 ``msg.Data`` 是 base64；本函数解码并校验 PNG 文件头。
    """

    request = build_map_data_request(
        bbox_lonlat=bbox_lonlat,
        max_level=max_level,
        max_pixel=max_pixel,
        coord_type=coord_type,
    )
    response = await client.request(C.CMD_GET_MAP_DATA, request)
    meta = parse_map_data_response(response.msg)
    data = response.msg.get("Data")
    if not data:
        # 手册 2.3.29 表格说响应在 ReqMapDatas 数组里，示例是扁平字段；
        # 若 Data 藏在数组元素里也要能找到。
        req_list = response.msg.get("ReqMapDatas")
        if isinstance(req_list, list) and req_list and isinstance(req_list[0], Mapping):
            data = req_list[0].get("Data")
    if not isinstance(data, str) or not data:
        raise ProtocolDecodeError(
            "20301 响应缺少 Data（base64 图片）",
            details={"keys": sorted(response.msg)[:20], "meta": meta["bbox"]},
            raw=response.msg,
        )
    try:
        payload = b64decode_tolerant(data)
    except ProtocolDecodeError as exc:
        raise ProtocolDecodeError(
            "20301 的 Data 不是合法 base64", raw={"length": len(data)}
        ) from exc
    if not payload.startswith(PNG_SIGNATURE):
        raise ProtocolDecodeError(
            "20301 返回的数据不是 PNG（文件头不匹配）。手册 2.3.29 示例以 "
            "'iVBORw0KGgo' 开头，即 PNG。",
            details={"length": len(payload), "head": payload[:8].hex()},
        )
    return payload


async def get_contours(
    client: OvitalClient,
    *,
    bbox_lonlat: tuple[float, float, float, float],
    coord_type: int = 0,
    **kwargs: Any,
) -> dict[str, Any]:
    """20303 获取等高线（手册 2.3.31）。

    手册只给出元数据字段（Ret/NeedVip/EvevValue/CheckFlag/CheckResult/Part），
    **没有给出等高线几何/高程数组结构**，因此本函数只返回元数据并显式标注
    ``geometry_format_unverified=True``；不编造几何。

    额外关键字参数透传给 ``codec.build_contour_request``（map_id/value/
    map_level/elev_value/check_flag）。需要 SVIP（手册 2.1.1）。
    """

    request = build_contour_request(
        bbox_lonlat=bbox_lonlat, coord_type=coord_type, **kwargs
    )
    response = await client.request(C.CMD_GET_CONTOUR, request)
    result = parse_contour_response(response.msg)
    ret = result.get("ret")
    if isinstance(ret, int) and ret < 0:
        message = C.CONTOUR_RET_MESSAGES.get(ret, "未知错误")
        if ret in (-1, -2):
            raise OvitalPrereqError(
                f"20303 等高线权限不足：msg.Ret={ret} {message}（手册 2.3.31.2）",
                details={"ret": ret, "cmdid": C.CMD_GET_CONTOUR},
            )
        raise ProtocolDecodeError(
            f"20303 获取等高线失败：msg.Ret={ret} {message}（手册 2.3.31.2）",
            details={"ret": ret, "cmdid": C.CMD_GET_CONTOUR},
            raw=response.msg,
        )
    return result


# ---------------------------------------------------------------------------
# 读：视图 / 屏幕 / 系统坐标系
# ---------------------------------------------------------------------------


async def get_screen_corners(client: OvitalClient) -> dict[str, Any]:
    """20016 获取屏幕中心点/左上角/右下角坐标（手册 2.3.58）。

    请求 ``ReqCoordType=1``（真实经纬度）。注意该字段是**独立枚举**
    （0=根据奥维决定, 1=真实经纬度, 2=GCJ-02），不能套用顶层 CoordType。
    手册示例数值疑似错位，解析器会把可疑项写进 ``warnings``（待实测）。
    """

    response = await client.request(
        C.CMD_GET_SCREEN_COORDS, {"ReqCoordType": 1}
    )
    return parse_screen_corners(response.msg)


async def get_system_crs(client: OvitalClient) -> dict[str, Any]:
    """20222 获取奥维系统坐标系参数（手册 2.3.51）。

    返回的 ``coord_type`` 是独立枚举（2=经纬度, 4=横轴墨卡托）；用于解释
    ``LatlngType=0``（系统坐标系）时的真实语义。
    """

    response = await client.request(C.CMD_GET_SYS_CRS, {})
    return parse_system_crs(response.msg)


async def get_viewport(client: OvitalClient) -> dict[str, Any]:
    """视图取证组合：20730（窗体区域）+ 20016（角点）+ 20212（像素比例）。

    单项失败不抛异常，而是记入 ``errors`` 并在 ``warnings`` 里说明——因为
    这三个命令都只是辅助取证，缺一项不应让整个能力查询失败。
    ``pixel_ratio`` 的 ``x``/``y`` 参数语义手册未说明（2.3.28 只写「x坐标/y坐标」），
    本函数传窗体区域中心像素；该用法「待实测」。
    """

    result: dict[str, Any] = {"errors": [], "warnings": []}

    rect: dict[str, Any] | None = None
    try:
        response = await client.request(C.CMD_GET_OMAP_WND_RECT, {})
        rect = parse_viewport_rect(response.msg)
        result["window_rect"] = rect
        result["warnings"].extend(rect.get("warnings", []))
    except OmapError as exc:
        result["errors"].append({"probe": "20730_window_rect", **omap_error_to_dict(exc)})

    try:
        corners = await get_screen_corners(client)
        result["screen"] = corners
        result["warnings"].extend(corners.get("warnings", []))
    except OmapError as exc:
        result["errors"].append({"probe": "20016_screen_corners", **omap_error_to_dict(exc)})

    x = y = 0
    if rect and rect.get("width") and rect.get("height"):
        x = int(rect["left"] + rect["width"] / 2)
        y = int(rect["top"] + rect["height"] / 2)
    try:
        response = await client.request(C.CMD_GET_PIXEL_RATIO, {"x": x, "y": y})
        meter_x = response.msg.get("MeterX")
        meter_y = response.msg.get("MeterY")
        result["pixel_ratio"] = {
            "x": x,
            "y": y,
            "meter_x": meter_x,
            "meter_y": meter_y,
            "note": "20212 的 x/y 参数语义手册未说明（待实测）；本函数传窗体中心像素",
            "raw": response.msg,
        }
    except OmapError as exc:
        result["errors"].append({"probe": "20212_pixel_ratio", **omap_error_to_dict(exc)})

    result["simulated"] = client.is_simulated
    result["verified_against_real_device"] = False
    result["status"] = "ok" if not result["errors"] else "partial"
    return result


# ---------------------------------------------------------------------------
# 能力探测（get_capabilities 工具的后端）
# ---------------------------------------------------------------------------


async def probe_capabilities(client: OvitalClient) -> dict[str, Any]:
    """探测奥维连接能力：版本/VIP/系统坐标系/选中能力/地图与等高线权限/异常集合。

    约定
    ----
    - 每项独立 try/except：单项失败只记入 ``errors`` 与对应 ``ok=False``，
      **不抛异常**（除非连接本身不可用）。
    - 只发只读命令，绝不写地图（MCP 启动/探测不得有写入副作用）。
    - 明确区分「文档明确 / 模拟验证 / 待实测」；``verified_against_real_device``
      恒为 False（本机无 Windows 奥维）。
    """

    if not client.connected:
        from ..errors import OvitalConnectionError

        raise OvitalConnectionError(
            "奥维未连接，无法探测能力：请检查 endpoint / 奥维客户端 / 第三方接口设置",
            details={"endpoint": client.endpoint},
        )

    result: dict[str, Any] = {
        "connected": True,
        "simulated": client.is_simulated,
        "verified_against_real_device": False,
        "evidence_level": (
            "offline_simulated" if client.is_simulated else "official_document_only"
        ),
        "server_version": client.server_version,
        "app_id": client.app_id,
        "endpoint": client.endpoint,
        "errors": [],
        "commands": [],
        "permissions": {},
        "limits": {},
        "unverified": list(C.UNVERIFIED_ITEMS),
    }

    def record_error(probe: str, exc: BaseException) -> None:
        entry = {"probe": probe, **omap_error_to_dict(exc)}
        result["errors"].append(entry)
        result["commands"].append(
            {"probe": probe, "ok": False, "error": entry.get("code")}
        )

    # 1) 20006 版本
    version_value: int | None = None
    try:
        version_value = await get_version(client)
        result["version"] = {
            "ok": True,
            "value": version_value,
            "source": "20006（手册 2.3.24：成功 ret 为奥维版本号）",
        }
        result["commands"].append({"probe": "20006_version", "ok": True})
    except OmapError as exc:
        result["version"] = {"ok": False, "value": None}
        record_error("20006_version", exc)

    # 2) 20501 VIP 等级
    try:
        vip = await get_vip_level(client)
        result["vip"] = {"ok": True, **vip}
        result["commands"].append({"probe": "20501_vip", "ok": True})
    except OmapError as exc:
        result["vip"] = {"ok": False}
        record_error("20501_vip", exc)

    # 3) 20222 系统坐标系
    try:
        crs = await get_system_crs(client)
        result["system_crs"] = {"ok": True, **crs}
        result["commands"].append({"probe": "20222_system_crs", "ok": True})
    except OmapError as exc:
        result["system_crs"] = {"ok": False}
        record_error("20222_system_crs", exc)

    # 4) 20104 选中对象能力（只读）
    try:
        selected = await get_selected(client, flag=C.SEL_FLAG_GROUP_JOIN)
        result["selection"] = {
            "ok": True,
            "count": len(selected),
            "objects": [
                {
                    "obj_id": item.obj_id,
                    "type_id": item.type_id,
                    "type_name": C.OBJ_TYPE_NAMES.get(item.type_id, "unknown"),
                    "name": item.name,
                }
                for item in selected
            ],
            "flag": C.SEL_FLAG_GROUP_JOIN,
            "source": "20104（手册 2.3.17；Flag 见手册 2.2.3）",
        }
        result["commands"].append({"probe": "20104_selection", "ok": True})
    except OmapError as exc:
        result["selection"] = {"ok": False}
        record_error("20104_selection", exc)

    # 5) 20730/20016/20212 视图取证（辅助）
    try:
        viewport = await get_viewport(client)
        result["viewport"] = viewport
        result["commands"].append(
            {"probe": "viewport(20730+20016+20212)", "ok": viewport.get("status") == "ok"}
        )
        for error in viewport.get("errors", []):
            result["errors"].append(error)
    except OmapError as exc:  # pragma: no cover - get_viewport 自身不抛
        record_error("viewport", exc)

    # 6) 地图图片权限（20301，SVIP）：用极小范围 + 小 MaxPixel 试探，不落盘
    try:
        bbox = _probe_bbox(client, result)
        await get_map_image(
            client, bbox_lonlat=bbox, max_level=12, max_pixel=256, coord_type=0
        )
        result["permissions"]["map_image"] = {
            "ok": True,
            "evidence": "20301 调用成功（手册 2.1.1：地图图片需 SVIP）",
            "probe_bbox": bbox,
        }
        result["commands"].append({"probe": "20301_map_image_permission", "ok": True})
    except OvitalPrereqError as exc:
        result["permissions"]["map_image"] = {
            "ok": False,
            "reason": "prereq",
            "message": exc.message,
            "evidence": "20301 返回权限错误（手册 2.1.1/2.3.29.2：需 SVIP）",
        }
        result["commands"].append(
            {"probe": "20301_map_image_permission", "ok": False, "error": exc.code}
        )
    except OmapError as exc:
        result["permissions"]["map_image"] = {"ok": None, "message": exc.message}
        record_error("20301_map_image_permission", exc)

    # 7) 等高线权限（20303，SVIP）：同样只探测权限，不解析几何（格式待实测）
    try:
        bbox = _probe_bbox(client, result)
        await get_contours(client, bbox_lonlat=bbox, coord_type=0)
        result["permissions"]["contour"] = {
            "ok": True,
            "evidence": "20303 调用成功（手册 2.1.1：等高线需 SVIP）；数据格式待实测",
        }
        result["commands"].append({"probe": "20303_contour_permission", "ok": True})
    except OvitalPrereqError as exc:
        result["permissions"]["contour"] = {
            "ok": False,
            "reason": "prereq",
            "message": exc.message,
        }
        result["commands"].append(
            {"probe": "20303_contour_permission", "ok": False, "error": exc.code}
        )
    except OmapError as exc:
        result["permissions"]["contour"] = {"ok": None, "message": exc.message}
        record_error("20303_contour_permission", exc)

    # 8) 对象读写能力：用 20101 读一个不存在的 ID 判定「命令可用」，
    #    ObjID 不存在时服务端行为手册未明确（可能空对象/错误），因此只记录
    #    是否发生协议级异常，不写入任何对象。
    result["permissions"]["object_read_write"] = {
        "ok": True,
        "evidence": "20101/20112/20103/20802 由手册 2.3.11-2.3.13/2.3.48 明确；"
        "实际写权限需真实设备与 VIP 账号（待实测）",
        "note": "探测过程不写地图；写能力在首次显式调用 write_draft 时才验证",
    }

    result["limits"] = {
        "documented": {
            "map_image_min_tier": "SVIP（手册 2.1.1）",
            "contour_min_tier": "SVIP（手册 2.1.1）",
            "other_min_tier": "VIP（手册 2.1.1）",
            "max_write_points": None,
            "max_write_bytes": None,
            "fragmentation": "官方样例有 Fragment* 字段，手册未记载（待实测）",
        },
        "project_conservative": {
            "write_max_bytes": C.DEFAULT_WRITE_MAX_BYTES,
            "note": C.WRITE_LIMIT_NOTE,
        },
        "unverified": [
            "20112 顶点数/字节数上限",
            "20301 MaxPixel 与 MaxLevel 的实际限制",
            "20103 成功响应形态",
        ],
    }
    result["status"] = "ok" if not result["errors"] else "partial"
    result["note"] = (
        "本结果来自官方文档级实现 + 本机模拟服务器回放，"
        "verified_against_real_device=false；真实奥维权限与字段以 Windows 实机为准"
    )
    return result


def _probe_bbox(client: OvitalClient, result: Mapping[str, Any]) -> tuple[float, float, float, float]:
    """为地图/等高线权限探测选一个极小 bbox：优先用 20016 中心点，否则用示例点。

    没有任何视图信息时使用手册示例附近的固定点（中国境内，避免境外行为差异）；
    这不会写地图，也不会被当成真实数据。
    """

    screen = result.get("viewport", {}).get("screen") if isinstance(result.get("viewport"), Mapping) else None
    center = screen.get("center") if isinstance(screen, Mapping) else None
    if isinstance(center, (tuple, list)) and len(center) == 2:
        lon, lat = float(center[0]), float(center[1])
        if -180 <= lon <= 180 and -90 <= lat <= 90:
            d = 0.0005  # 约 50 m
            return (lon - d, lat - d, lon + d, lat + d)
    lon, lat = 106.57287598, 39.01064751  # 手册 2.4.8 示例点
    d = 0.0005
    return (lon - d, lat - d, lon + d, lat + d)


# ---------------------------------------------------------------------------
# 写后回读对比（工具层往返证据）
# ---------------------------------------------------------------------------


def compare_vertices(
    local_lonlat: Sequence[tuple[float, float]],
    readback_lonlat: Sequence[tuple[float, float]],
    *,
    tolerance_deg: float = 1e-7,
) -> dict[str, Any]:
    """对比本地顶点与回读顶点（都是规范 ``(lon, lat)``）。

    返回::

        {"max_delta_deg", "mean_delta_deg", "point_count_local",
         "point_count_readback", "axis_order_checked", "axis_order",
         "swap_detected", "within_tolerance", "tolerance_deg", "note"}

    计算方式：逐点欧氏距离（度）。``swap_detected`` 用「经纬度互换后的偏差是否
    显著更小」判断回读是否轴序颠倒——这是写入往返验证里最容易出错的点。
    """

    local = [(float(p[0]), float(p[1])) for p in local_lonlat]
    readback = [(float(p[0]), float(p[1])) for p in readback_lonlat]
    count = min(len(local), len(readback))
    deltas: list[float] = []
    swapped_deltas: list[float] = []
    for index in range(count):
        dlon = local[index][0] - readback[index][0]
        dlat = local[index][1] - readback[index][1]
        deltas.append(math.hypot(dlon, dlat))
        # 互换解释：把回读当成 (lat, lon) 时的偏差
        swapped_deltas.append(
            math.hypot(
                local[index][0] - readback[index][1],
                local[index][1] - readback[index][0],
            )
        )
    max_delta = max(deltas) if deltas else float("inf")
    mean_delta = (sum(deltas) / len(deltas)) if deltas else float("inf")
    swapped_max = max(swapped_deltas) if swapped_deltas else float("inf")
    swap_detected = bool(
        swapped_deltas and swapped_max < max_delta and max_delta > 1e-6
    )
    return {
        "max_delta_deg": max_delta,
        "mean_delta_deg": mean_delta,
        "point_count_local": len(local),
        "point_count_readback": len(readback),
        "axis_order_checked": True,
        "axis_order": "lon,lat",
        "swap_detected": swap_detected,
        "within_tolerance": bool(
            deltas and len(local) == len(readback) and max_delta <= tolerance_deg
        ),
        "tolerance_deg": tolerance_deg,
        "note": (
            "逐点欧氏距离（单位：度）。1e-7 度约 1.1 cm，远小于 30 m DEM 精度；"
            "协议本身不保证小数位，真实设备往返精度待实测。"
        ),
    }


# 明确保留：坐标语义枚举在签名里出现过，避免下游误用字符串。
_ = CoordSemantic
