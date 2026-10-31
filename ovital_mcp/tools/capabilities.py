"""只读类工具：能力查询、选择/对象读取、地图场景。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import MCPServer

from ..app import AppContext
from ..coordinates import (
    CoordSemantic,
    semantic_for_latlng_type,
    verify_canonical_lonlat,
)
from ..errors import OmapError, OvitalPrereqError
from .common import (
    bbox_list,
    effective_real_device_verified,
    error,
    guarded,
    image_object,
    jsonable,
    ok,
    ovital_provenance,
    provenance,
    type_name,
)

logger = logging.getLogger("ovital_mcp.tools.capabilities")


# 官方依据：WebSocket V2.7 手册 https://www.gpsov.com/appdoc/index.php?id=32
# 会员要求见手册 2.1.1：获取地图图片数据、获取等高线数据需 SVIP；其他功能至少 VIP。
CAPABILITY_MATRIX: list[dict[str, Any]] = [
    {
        "id": "ovital_object_read",
        "name": "读取收藏夹对象 / 选中对象",
        "commands": ["20101", "20104", "20110"],
        "status": "official_document",
        "real_device": "pending",
        "source": "手册 2.3.11 / 2.3.17 / 2.3.18",
        "notes": "选中对象只返回基本信息，详细坐标需再按 ObjID 读取。",
    },
    {
        "id": "ovital_object_write",
        "name": "添加/替换/删除收藏夹对象",
        "commands": ["20112", "20103"],
        "status": "official_document",
        "real_device": "pending",
        "source": "手册 2.3.12 / 2.3.13",
        "notes": "20112 在 ObjID 已存在时是替换语义，写前必须快照；写后必须回读。",
    },
    {
        "id": "ovital_shape_metrics",
        "name": "图形面积/周长/顶点数/区域数",
        "commands": ["20802"],
        "status": "official_document",
        "real_device": "pending",
        "source": "手册 2.3.48",
        "notes": "手册标注面积为平方米、周长为千米；表格字段名与示例不一致，适配器需兼容两者。",
    },
    {
        "id": "ovital_map_image",
        "name": "获取地图图片数据/文件",
        "commands": ["20301", "20302"],
        "status": "official_document",
        "real_device": "pending",
        "prereq": "SVIP 及以上（手册 2.1.1）",
        "source": "手册 2.3.29 / 2.3.30",
        "notes": "返回的是地图图片，不是原始 DEM；本机无 SVIP 账号，未验证。",
    },
    {
        "id": "ovital_contour",
        "name": "获取地图等高线",
        "commands": ["20303"],
        "status": "official_document",
        "real_device": "pending",
        "prereq": "SVIP 及以上（手册 2.1.1）",
        "source": "手册 2.3.31",
        "notes": "数据格式与批次未实测；不能假定等同于 GeoTIFF DEM。",
    },
    {
        "id": "ovital_viewport",
        "name": "屏幕中心/角点/可显示区域/像素比例",
        "commands": ["20016", "20212", "20730"],
        "status": "official_document",
        "real_device": "pending",
        "source": "手册 2.3.52 / 2.3.58 / 2.3.28",
        "notes": "仅作视图取证与配准辅助，不能单靠角点保证任意三维视图的像素映射。",
    },
    {
        "id": "local_dem_watershed",
        "name": "本地 DEM 确定性集水区计算",
        "commands": [],
        "status": "offline_computed",
        "real_device": "not_applicable",
        "source": "GRASS r.watershed/r.water.outlet 官方文档；本机合成 DEM 解析真值测试",
        "notes": "DEM 由项目提供 GeoTIFF；不能假定可从奥维导出原始 DEM。",
    },
    {
        "id": "review_images",
        "name": "概览/分段复核图生成",
        "commands": [],
        "status": "offline_computed",
        "real_device": "not_applicable",
        "source": "本项目实现（matplotlib 本地渲染，带坐标配准信息）",
        "notes": "模型是否真正收到图片需要 DSH 视觉链路验证，见 docs/offline-acceptance.md。",
    },
    {
        "id": "mock_protocol_loop",
        "name": "模拟奥维服务器协议闭环",
        "commands": [
            "20000",
            "20006",
            "20101",
            "20103",
            "20104",
            "20110",
            "20112",
            "20802",
            "20301",
            "20303",
            "20501",
            "20222",
        ],
        "status": "offline_simulated",
        "real_device": "not_applicable",
        "source": "项目 mock 服务器 + 官方 V2.7 手册/样例",
        "notes": "模拟结果不能替代真实联机验收；capabilities 会标记 simulated=true。",
    },
    {
        "id": "draft_roundtrip",
        "name": "草稿写入/回读/回滚",
        "commands": ["20112", "20101", "20802", "20103"],
        "status": "offline_simulated",
        "real_device": "pending",
        "source": "本项目实现 + 模拟服务器",
        "notes": "真实奥维上尚未验证；ObjID 替换语义、坐标轴序与面积口径必须实机复核。",
    },
]

UNVERIFIED_ITEMS = [
    "真实 Windows 奥维客户端的接口启用、端口、预共享密钥派生与鉴权（本机未联机）",
    "目标账号的 VIP/SVIP 等级、地图图片与等高线接口的实际可用性",
    "20112 写入图形后 ObjID 的生成/替换语义、ParentID 挂接规则与数量限制",
    "奥维图形 Latlng 扁平数组轴序（文档示例为 lat,lng；需实机回读确认）",
    "奥维 20802 面积/周长的投影口径与孔洞处理规则",
    "多区域/孔洞/超长顶点对象在 20112/20802 上的表示与限制",
    "断线重连后 20112 重复写入行为与幂等性（本机仅模拟注入）",
    "地图图片/等高线接口返回数据的格式与是否满足复核需要",
    "DSH 视觉模型在真实会话中收到 MCP 图片并完成判读（本机未配置 API Key，见验收报告）",
    "用户真实 DEM、坐标系统与经审核参照边界的精度验收（缺少数据与人工参考）",
]


async def _connection_state(ctx: AppContext) -> dict[str, Any]:
    """懒连接探测；失败也返回结构化状态，不抛异常（get_capabilities 本身不能失败）。"""

    try:
        client = await ctx.ovital()
    except OmapError as exc:
        return {"available": False, "error": exc.to_dict()}
    except Exception as exc:  # noqa: BLE001 - 连接库异常类型不稳定
        return {
            "available": False,
            "error": {"code": "OVITAL_CONNECTION_ERROR", "message": f"{type(exc).__name__}: {exc}"},
        }
    from ..protocol import commands

    probe: dict[str, Any] = {
        "available": True,
        "version": getattr(client, "server_version", None),
        "simulated": bool(getattr(client, "is_simulated", False)),
        "endpoint": ctx.config.ovital.endpoint,
    }
    try:
        probe.update(jsonable(await commands.probe_capabilities(client)))
    except Exception as exc:  # noqa: BLE001 - 探测失败要如实记录
        probe["probe_error"] = {"code": getattr(exc, "code", "PROBE_ERROR"), "message": str(exc)}
    return probe


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(
        name="get_capabilities",
        description=(
            "查询当前 MCP/奥维连接能力、权限、限制与已验证范围。不会写入地图。"
            "返回中 simulated=true 表示连接的是本地模拟服务器；"
            "verified_against_real_device=false 表示尚未在真实 Windows 奥维上验证。"
        ),
    )
    @guarded("get_capabilities", ctx)
    async def get_capabilities() -> dict[str, Any]:
        connection = await _connection_state(ctx)
        simulated = connection.get("simulated")
        if simulated is None:
            simulated = bool(ctx.config.mock.enabled)
        prov = provenance(
            "offline_simulated" if simulated else "official_document",
            simulated=bool(simulated),
            verified_against_real_device=(
                bool(connection.get("available"))
                and effective_real_device_verified(ctx, simulated=simulated)
            ),
            extra={"connection": {k: v for k, v in connection.items() if k != "probe_error"}},
        )
        warnings: list[str] = []
        if not connection.get("available"):
            warnings.append(
                "奥维未连接（本机通常没有 Windows 奥维客户端）；以下能力均为文档级/模拟验证，"
                "真实联机验收未完成。"
            )
        if simulated:
            warnings.append("当前连接是本地模拟服务器（Simulated=true），不能作为真实联机证据。")
        payload = {
            "server": ctx.server_info(),
            "connection": connection,
            "permissions": {
                "map_image_requires": "SVIP 及以上（手册 2.1.1）",
                "contour_requires": "SVIP 及以上（手册 2.1.1）",
                "other_commands_require": "至少 VIP（手册 2.1.1）",
                "note": "以上为官方文档声明；目标账号实际等级与接口开放情况未实测。",
            },
            "capabilities": CAPABILITY_MATRIX,
            "limits": {
                "mcp_image_max_bytes": ctx.config.mcp.image_max_bytes,
                "max_image_pixels": ctx.config.mcp.max_image_pixels,
                "snap_radius_m_default": ctx.config.gis.snap_radius_m,
                "simplify_tolerance_m_default": ctx.config.gis.simplify_tolerance_m,
                "job_timeout_s": ctx.config.gis.job_timeout_s,
                "tool_call_timeout_ms_hint": 60000,
                "long_tasks": "start_watershed 返回 job_id，用 get_job 轮询；勿用单次长调用",
            },
            "data_sources": {
                "ovital_websocket_manual": "https://www.gpsov.com/appdoc/index.php?id=32",
                "ovital_third_party": "https://www.ovital.com/openapi/",
                "ovital_sample_zip": "https://download.ovital.com/pub/api/omapInterfaceWeb_latest.zip",
                "grass_watershed": "https://grass.osgeo.org/grass-stable/manuals/r.watershed.html",
                "grass_water_outlet": "https://grass.osgeo.org/grass-stable/manuals/r.water.outlet.html",
            },
            "unverified": UNVERIFIED_ITEMS,
            "evidence_levels": {
                "official_document": "官方文档/官方示例明确写出",
                "offline_simulated": "本机对模拟服务器/样例回放验证",
                "offline_computed": "本机对合成/公开 DEM 的确定性计算",
                "real_device_pending": "必须在 Windows 奥维实机验证，当前未完成",
            },
        }
        return ok("get_capabilities", payload, warnings=warnings, prov=prov)

    @mcp.tool(
        name="get_selection",
        description=(
            "读取用户在奥维中当前选中的收藏夹对象基本信息（命令 20104）。"
            "只返回 ID/类型/名称；需要坐标时再调用 read_object。"
        ),
    )
    @guarded("get_selection", ctx)
    async def get_selection(flag: int = 1, latlng_type: int = 1) -> dict[str, Any]:
        from ..protocol import commands

        if flag not in (1, 2, 3):
            return error("get_selection", OmapError("flag 只能是 1/2/3（手册 2.2.3）"),
                         prov=provenance("official_document"))
        semantic = semantic_for_latlng_type(latlng_type)
        client = await ctx.ovital()
        selected = await commands.get_selected(client, flag)
        items = [
            {
                "obj_id": int(getattr(item, "obj_id", 0)),
                "type_id": getattr(item, "type_id", None),
                "type": type_name(getattr(item, "type_id", None)),
                "name": getattr(item, "name", None),
                "raw": jsonable(getattr(item, "raw", None)),
            }
            for item in selected
        ]
        warnings = []
        if not items:
            warnings.append("当前没有选中对象；请确认在奥维中已选中坝址/取水口标签或参照图形。")
        if semantic == CoordSemantic.SYSTEM:
            warnings.append("请求的 LatlngType=0（系统坐标系），后续坐标解释必须显式查询系统坐标系参数。")
        return ok(
            "get_selection",
            {
                "count": len(items),
                "objects": items,
                "latlng_type": latlng_type,
                "requested_semantic": semantic.value,
            },
            warnings=warnings,
            prov=ovital_provenance(ctx, extra={"latlng_type": latlng_type}),
        )

    @mcp.tool(
        name="read_object",
        description=(
            "按对象 ID 读取奥维收藏夹对象（命令 20101），图形会返回 EPSG:4490 的 (lon,lat) 顶点、"
            "坐标语义说明与 20802 量算结果。不修改任何对象。"
        ),
    )
    @guarded("read_object", ctx)
    async def read_object(obj_id: int, latlng_type: int = 1, include_metrics: bool = True) -> dict[str, Any]:
        from ..protocol import commands

        semantic = semantic_for_latlng_type(latlng_type)
        client = await ctx.ovital()
        nodes = await commands.get_objects(client, int(obj_id), latlng_type)
        if not nodes:
            raise OmapError(f"未找到对象 {obj_id}", details={"obj_id": obj_id})
        node = nodes[0]
        raw_node = getattr(node, "raw", node) or {}
        type_id = int(getattr(node, "type_id", raw_node.get("Type") or 0))
        payload: dict[str, Any] = {
            "obj_id": int(obj_id),
            "type_id": type_id,
            "type": type_name(type_id),
            "name": getattr(node, "name", None) or (raw_node.get("Object") or {}).get("Name"),
            "requested_semantic": semantic.value,
            "axis_order": "内部统一 (lon, lat)；奥维 Shape.Latlng 为 lat,lng 扁平数组",
        }
        warnings: list[str] = []
        if type_id == 13:
            shape = await commands.get_shape(
                client,
                int(obj_id),
                latlng_type=latlng_type,
                allow_gcj02_conversion=ctx.config.ovital.allow_gcj02_conversion,
            )
            if getattr(shape, "two_point_special", False):
                warnings.append(
                    "该图形只有 2 个点：手册 2.4.8 说明两点图形用起止角度表示圆/扇形/矩形，"
                    "不是多边形边界，禁止当作集水区/面状对象使用。"
                )
            payload["geometry"] = {
                "type": "LineString/Polygon(顶点序列)",
                "two_point_special": bool(getattr(shape, "two_point_special", False)),
                "crs": "EPSG:4490",
                "vertices": [[lon, lat] for lon, lat in shape.vertices],
                "vertex_count": len(shape.vertices),
                "source_semantic": getattr(shape.source_semantic, "value", str(shape.source_semantic)),
                "axis_order_used": getattr(shape, "axis_order_used", None),
            }
            if include_metrics:
                try:
                    metrics = await commands.get_shape_metrics(client, int(obj_id))
                    payload["metrics"] = jsonable(metrics)
                    warnings.append(
                        "20802 的面积/周长口径（投影、孔洞处理）未在真实设备验证；"
                        "与本地米制计算比较前必须统一 CRS/椭球/单位。"
                    )
                except OmapError as exc:
                    warnings.append(f"20802 量算失败：{exc.message}")
            if raw_node:
                payload["raw_summary"] = _summarize_raw(raw_node)
        elif type_id == 7:
            from ..protocol import objects as ovital_objects

            try:
                lon, lat = ovital_objects.object_point_lonlat(
                    raw_node,
                    allow_gcj02_conversion=ctx.config.ovital.allow_gcj02_conversion,
                )
                verify_canonical_lonlat(lon, lat)
                payload["point"] = {"lon": lon, "lat": lat, "crs": "EPSG:4490"}
            except Exception as exc:  # noqa: BLE001 - 坐标语义不明确必须如实报告
                warnings.append(f"标签坐标解析未完成：{type(exc).__name__}: {exc}")
                payload["raw_summary"] = _summarize_raw(raw_node)
        else:
            payload["raw_summary"] = _summarize_raw(raw_node)
        return ok("read_object", payload, warnings=warnings, prov=ovital_provenance(ctx))

    @mcp.tool(
        name="get_scene",
        description=(
            "从奥维获取指定范围的地图图片数据（命令 20301，需 SVIP），返回 MCP 图片 + 范围/像素/CRS 元数据。"
            "这是地图图片，不是原始 DEM；附带数据来源与去偏移警告。"
        ),
    )
    @guarded("get_scene", ctx)
    async def get_scene(
        bbox_lonlat: list[float],
        max_level: int = 15,
        max_pixel: int = 1000000,
        coord_type: int = 0,
    ):
        from ..protocol import commands

        bbox = bbox_list(bbox_lonlat)
        if not (1 <= int(max_level) <= 21):
            raise OmapError("max_level 必须在 1..21 之间")
        client = await ctx.ovital()
        try:
            png = await commands.get_map_image(
                client,
                bbox_lonlat=(bbox[0], bbox[1], bbox[2], bbox[3]),
                max_level=int(max_level),
                max_pixel=int(max_pixel),
                coord_type=int(coord_type),
            )
        except OmapError as exc:
            if "SVIP" in exc.message or "svip" in exc.message.lower():
                raise OvitalPrereqError(
                    f"获取地图图片需要 SVIP 及以上会员权限（手册 2.1.1）：{exc.message}",
                    details={"command": "20301"},
                ) from exc
            raise
        scenes_dir = ctx.paths.data_root / "scenes"  # type: ignore[union-attr]
        scenes_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = scenes_dir / f"scene-{stamp}.png"
        path.write_bytes(png)
        width = height = None
        try:
            from PIL import Image as PILImage

            with PILImage.open(path) as im:
                width, height = im.size
        except Exception:  # noqa: BLE001 - 尺寸探测失败不影响图片返回
            pass
        warnings = [
            "地图图片仅用于复核/取证；像素坐标不等于经纬度，配准需另行验证（手册 2.3.58 只能辅助）。",
            "地图图片接口需 SVIP；本结果未在真实账号验证。",
            "若底图/显示坐标系含 GCJ-02 偏移，必须按协议语义显式处理，禁止重复偏移。",
        ]
        payload = {
            "bbox_lonlat": bbox,
            "max_level": int(max_level),
            "max_pixel": int(max_pixel),
            "coord_type": int(coord_type),
            "image_path": str(path),
            "image_bytes": len(png),
            "pixel_width": width,
            "pixel_height": height,
            "approx_deg_per_pixel": (
                [(bbox[2] - bbox[0]) / width, (bbox[3] - bbox[1]) / height] if width and height else None
            ),
            "registration_status": "待实测（需用已知控制点核对图片像素与经纬度映射）",
        }
        return [
            ok("get_scene", payload, warnings=warnings, prov=ovital_provenance(ctx)),
            image_object(path, max_bytes=ctx.config.mcp.image_max_bytes),
        ]


def _summarize_raw(raw: dict[str, Any], *, max_text: int = 300) -> dict[str, Any]:
    """给模型看的原始字段摘要（避免超长输出）。"""

    detail = ((raw.get("Object") or {}).get("ObjectDetail")) or {}
    summary: dict[str, Any] = {"Type": raw.get("Type"), "ObjID": raw.get("ObjID"), "ParentID": raw.get("ParentID")}
    for key in ("Mtp", "Latlng", "Gcj02", "Lat", "Lng", "AreaSize", "Perimeter", "PointCnt", "AreaCnt"):
        if key in detail:
            summary[key] = detail[key]
    text = jsonable(raw)
    if isinstance(text, dict):
        import json as _json

        rendered = _json.dumps(text, ensure_ascii=False)
        if len(rendered) > max_text * 4:
            summary["raw_truncated"] = True
    return summary
