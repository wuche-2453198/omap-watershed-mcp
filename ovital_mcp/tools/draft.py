"""草稿落图与回读/定稿/回滚工具。

这是唯一会**写入奥维地图**的工具组。安全规则（任务书 B/E 节）：
- 写入独立草稿文件夹，不覆盖用户原成果；
- 写前保存快照，写后必须回读坐标与量算结果；
- 幂等键防重复创建；断线/超时不得盲目重发"创建"；
- 未在真实 Windows 奥维上验证的语义（ObjID/替换/轴序/面积口径）必须如实标注。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import MCPServer

from ..app import AppContext
from ..coordinates import verify_canonical_lonlat
from ..errors import DataValidationError, GeometryError, OmapError, WriteVerificationError
from ..store import FileLock, atomic_write_json, read_json
from .common import (
    boundary_from_job,
    geometry_from_geojson,
    guarded,
    jsonable,
    load_job_result,
    ok,
    ovital_provenance,
)

logger = logging.getLogger("ovital_mcp.tools.draft")

VERTEX_TOLERANCE_DEG = 1e-6  # ≈0.11 m，仅为协议精度检查，不代表工程精度


# ---------------------------------------------------------------------------
# 几何与顶点
# ---------------------------------------------------------------------------


def _polygon_from_input(job_id: str | None, geojson: Any, ctx: AppContext):
    if job_id and geojson is not None:
        raise DataValidationError("job_id 与 geojson 只能提供一个")
    if job_id:
        geom, _result, _job = boundary_from_job(ctx, job_id)
    elif geojson is not None:
        geom = geometry_from_geojson(geojson)
    else:
        raise DataValidationError("必须提供 job_id 或 geojson 之一")
    if geom.geom_type == "MultiPolygon":
        raise DataValidationError(
            "v0.1 只支持单一连通多边形；多部件边界的协议表示（OuterRgnIdx/多区域）未验证，"
            "请先人工确认后再扩展。",
            details={"geom_type": geom.geom_type, "components": len(geom.geoms)},
        )
    if geom.geom_type != "Polygon":
        raise DataValidationError(f"需要 Polygon，收到 {geom.geom_type}")
    if geom.is_empty:
        raise GeometryError("边界为空，无法写入")
    if not geom.is_valid:
        from shapely.validation import explain_validity

        raise GeometryError(
            "边界几何无效（自相交等），拒绝写入；先修复或重新计算",
            details={"reason": str(explain_validity(geom))},
        )
    if geom.interiors:
        raise DataValidationError(
            "v0.1 不写入带洞多边形：奥维孔洞表示（OuterRgnIdx/区域数）未实测，"
            "请先按无洞边界处理或人工确认。",
            details={"holes": len(geom.interiors)},
        )
    return geom


def _exterior_vertices_lonlat(geom) -> list[tuple[float, float]]:
    coords = list(geom.exterior.coords)
    if len(coords) >= 2 and coords[0] == coords[-1]:
        coords = coords[:-1]
    vertices = [(float(lon), float(lat)) for lon, lat in coords]
    if len(vertices) < 3:
        raise GeometryError("多边形外环顶点少于 3 个")
    for lon, lat in vertices:
        verify_canonical_lonlat(lon, lat)
    return vertices


def _compare_vertices(
    local: list[tuple[float, float]], readback: list[tuple[float, float]]
) -> dict[str, Any]:
    """比较写前/回读顶点（相同顺序假设；数量不一致时按最近点距离报告）。"""

    import math

    if len(local) != len(readback):
        # 数量不同：用最近距离粗略衡量（并明确标记顺序不可比）
        max_d = 0.0
        total = 0.0
        for lon, lat in local:
            best = min(
                math.hypot((lon - b[0]) * 111320 * math.cos(math.radians(lat)), (lat - b[1]) * 110540)
                for b in readback
            ) if readback else float("inf")
            max_d = max(max_d, best)
            total += best
        return {
            "comparable_order": False,
            "point_count_local": len(local),
            "point_count_readback": len(readback),
            "max_nearest_distance_m": max_d,
            "mean_nearest_distance_m": total / len(local) if local else None,
            "ok": False,
            "note": "顶点数量不一致，无法按顺序逐点比较；请人工核查。",
        }
    deltas = []
    for (lon1, lat1), (lon2, lat2) in zip(local, readback):
        deltas.append(math.hypot(lon1 - lon2, lat1 - lat2))
    max_delta = max(deltas) if deltas else 0.0
    import statistics

    return {
        "comparable_order": True,
        "point_count_local": len(local),
        "point_count_readback": len(readback),
        "max_delta_deg": max_delta,
        "mean_delta_deg": statistics.fmean(deltas) if deltas else 0.0,
        "ok": max_delta <= VERTEX_TOLERANCE_DEG,
        "tolerance_deg": VERTEX_TOLERANCE_DEG,
        "note": "坐标小数位/写入成功不能单独证明工程精度；这只是协议往返检查。",
    }


# ---------------------------------------------------------------------------
# 草稿文件夹
# ---------------------------------------------------------------------------


class DraftFolderRegistry:
    """记录"草稿文件夹名 → ObjID"的映射（避免重复创建文件夹）。"""

    def __init__(self, ctx: AppContext) -> None:
        self.path = ctx.paths.data_root / "draft_folders.json"  # type: ignore[union-attr]

    def get(self, key: str) -> int | None:
        data = read_json(self.path, default={}) or {}
        value = data.get(key)
        return int(value) if value else None

    def put(self, key: str, folder_id: int) -> None:
        with FileLock(self.path.with_suffix(".lock")):
            data = read_json(self.path, default={}) or {}
            data[key] = int(folder_id)
            atomic_write_json(self.path, data)


async def _read_node(client: Any, obj_id: int, ctx: AppContext):
    """读取单个对象节点；不存在返回 None。"""

    from ..protocol import commands

    try:
        nodes = await commands.get_objects(client, int(obj_id), ctx.config.ovital.latlng_type)
    except OmapError:
        return None
    return nodes[0] if nodes else None


async def _match_folder_by_name(client: Any, name: str, ctx: AppContext) -> int | None:
    """按名称查找类型为 30 的文件夹。

    手册 2.3.22 的 20107 响应只给 ObjID/ObjType，不给名称（本机模拟服务器也按此实现），
    因此必须对候选再调用 20101 读回名称与类型后判断。
    """

    from ..protocol import commands

    try:
        candidates = await commands.find_objects(
            client, name, isrh_opt=5, latlng_type=ctx.config.ovital.latlng_type
        )
    except OmapError:
        return None
    for node in candidates or []:
        type_id = int(getattr(node, "type_id", 0) or 0)
        if type_id not in (0, 30):
            continue
        detail = await _read_node(client, int(getattr(node, "obj_id")), ctx)
        if detail is None:
            continue
        if int(getattr(detail, "type_id", 0)) == 30 and str(getattr(detail, "name", "")) == name:
            return int(getattr(detail, "obj_id"))
    return None


async def _resolve_folder(
    ctx: AppContext,
    client: Any,
    *,
    folder_name: str,
    parent_folder_id: int | None,
) -> tuple[int, bool, list[str]]:
    """返回 (folder_id, created, warnings)。"""

    from ..protocol import commands, objects as ovital_objects

    warnings: list[str] = []
    if parent_folder_id:
        return int(parent_folder_id), False, ["使用调用方显式提供的 parent_folder_id；请自行确认该文件夹归属。"]

    registry = DraftFolderRegistry(ctx)
    key = f"{ctx.config.ovital.endpoint}|{folder_name}"
    cached = registry.get(key)
    if cached:
        node = await _read_node(client, cached, ctx)
        if node is not None and int(getattr(node, "type_id", 0)) == 30:
            return cached, False, warnings
        warnings.append(f"记录的草稿文件夹 {cached} 读取失败或类型不符，将重新查找/创建。")

    existing = await _match_folder_by_name(client, folder_name, ctx)
    if existing is not None:
        registry.put(key, existing)
        return existing, False, warnings

    # 新建文件夹（写前没有被替换对象，因此无快照风险）。
    tree = ovital_objects.build_write_tree(folder_id=0, folder_name=folder_name, children=[])
    results = await commands.write_tree(client, tree)
    folder_id = None
    for item in results or []:
        if int(getattr(item, "type_id", 0)) == 30:
            folder_id = int(getattr(item, "obj_id"))
            break
    if folder_id is None:
        # 手册示例未包含 Type=30 文件夹，20112 响应形态待实测：用 20107+20101 兜底。
        warnings.append(
            "20112 响应未包含文件夹 ObjID（该响应形态待实测）；已按名称回查兜底，禁止直接重试创建。"
        )
        folder_id = await _match_folder_by_name(client, folder_name, ctx)
    if folder_id is None:
        raise WriteVerificationError(
            "创建草稿文件夹后无法确认其 ObjID（20112 响应与 20107 回查均未确证）；"
            "为避免在错误位置写入或重复创建，已中止。请检查奥维或稍后用 find 重新尝试。",
            details={"response": jsonable(results), "folder_name": folder_name},
        )
    registry.put(key, folder_id)
    return folder_id, True, warnings


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(
        name="write_draft",
        description=(
            "把候选集水区边界写入奥维独立草稿文件夹（图形 + 出口标签），并立即回读坐标/量算结果。"
            "必须提供 idempotency_key（或由 job_id 自动派生）；重复调用返回同一对象，不会重复创建。"
            "写前保存快照，可用 rollback_change 回滚。"
        ),
    )
    @guarded("write_draft", ctx)
    async def write_draft(
        job_id: str | None = None,
        geojson: Any = None,
        draft_folder: str | None = None,
        name: str | None = None,
        comment: str = "",
        parent_folder_id: int | None = None,
        idempotency_key: str | None = None,
        expected_area_m2: float | None = None,
    ) -> dict[str, Any]:
        from ..protocol import commands, objects as ovital_objects

        geom = _polygon_from_input(job_id, geojson, ctx)
        vertices = _exterior_vertices_lonlat(geom)
        folder_name = draft_folder or ctx.config.ovital.draft_root_folder
        shape_name = name or f"集水区草稿-{(job_id or datetime.now().strftime('%Y%m%d-%H%M%S'))}"
        comment_text = comment or (
            f"OMAP-MCP 草稿；job_id={job_id or 'n/a'}；顶点数={len(vertices)}；"
            "边界为 DEM 计算候选，需人工复核后定稿。"
        )
        auto_key = hashlib.sha256(
            "|".join(
                [
                    "draft",
                    job_id or json.dumps(jsonable(geom.__geo_interface__), sort_keys=True),
                    folder_name,
                    shape_name,
                ]
            ).encode("utf-8")
        ).hexdigest()[:40]
        key = idempotency_key or f"auto-{auto_key}"
        client = await ctx.ovital()
        stale_warnings: list[str] = []
        existing = ctx.store.idempotency_get(f"draft:{key}")  # type: ignore[union-attr]
        if existing:
            value = existing.get("value") or {}
            obj_id = value.get("obj_id")
            if obj_id:
                from ..errors import OvitalNotFoundError as _NotFound

                try:
                    shape = await commands.get_shape(client, int(obj_id))
                except _NotFound:
                    # 陈旧幂等键（对象已被删除，例如上一次已回滚）：清除后继续新建，
                    # 而不是让用户困惑地被挡住。
                    ctx.store.idempotency_delete(f"draft:{key}")  # type: ignore[union-attr]
                    stale_warnings.append(
                        f"幂等键指向的对象 {obj_id} 已不存在（可能已被回滚/删除），已清除陈旧键并继续创建。"
                    )
                    existing = None
                    shape = None
                except OmapError as exc:
                    raise OmapError(
                        f"幂等键已存在但对象 {obj_id} 读取失败：请更换 idempotency_key 或先回滚",
                        details={"idempotency_key": key, "obj_id": obj_id, "cause": exc.message},
                    ) from exc
                if existing is not None and shape is not None:
                    metrics = await commands.get_shape_metrics(client, int(obj_id))
                    return ok(
                        "write_draft",
                        {
                            "reused": True,
                            "obj_id": int(obj_id),
                            "folder_id": value.get("folder_id"),
                            "change_id": value.get("change_id"),
                            "readback": {
                                "vertex_count": len(shape.vertices),
                                "metrics": jsonable(metrics),
                            },
                            "idempotency_key": key,
                        },
                        warnings=["命中幂等键，未重复创建对象。"],
                        prov=ovital_provenance(ctx, extra={"reused": True}),
                    )

        folder_id, folder_created, folder_warnings = await _resolve_folder(
            ctx, client, folder_name=folder_name, parent_folder_id=parent_folder_id
        )

        # 写前快照：记录文件夹当前状态（若已存在）。
        snapshot_payload: dict[str, Any] = {
            "folder": {"folder_id": folder_id, "folder_name": folder_name, "created_in_this_change": folder_created},
            "replaced_objects": [],
        }
        try:
            nodes = await commands.get_objects(client, folder_id, ctx.config.ovital.latlng_type)
            snapshot_payload["folder"]["raw"] = jsonable([getattr(n, "raw", n) for n in nodes])
        except OmapError:
            snapshot_payload["folder"]["raw"] = None
        draft_task = ctx.store.create_task("draft_write", {"job_id": job_id, "folder_id": folder_id})  # type: ignore[union-attr]
        snapshot_id = ctx.store.save_snapshot(  # type: ignore[union-attr]
            draft_task.task_id,
            label=f"write_draft 前快照 folder={folder_id}",
            payload=snapshot_payload,
        )

        from ..coordinates import CoordSemantic, semantic_for_latlng_type

        shape_semantic = semantic_for_latlng_type(ctx.config.ovital.latlng_type)
        if shape_semantic not in (CoordSemantic.CGCS2000, CoordSemantic.GCJ02):
            raise DataValidationError(
                "写入 Shape 需要明确的经纬度语义；请把 ovital.latlng_type 设为 1(CGCS2000) 或 2(GCJ02)"
            )
        if expected_area_m2 is None and job_id:
            try:
                _job_dict, _result = load_job_result(ctx, job_id)
                expected_area_m2 = _result.get("area_m2")
            except OmapError:
                expected_area_m2 = None
        shape_node = ovital_objects.build_shape_object(
            name=shape_name,
            comment=comment_text,
            vertices_lonlat=vertices,
            semantic=shape_semantic,
            line_color=ctx.config.ovital.draft_line_color,
            line_width=ctx.config.ovital.draft_line_width,
            area_color=ctx.config.ovital.draft_area_color,
            area_alpha=ctx.config.ovital.draft_area_alpha,
            show_flag=2,
        )
        outlet_lon, outlet_lat = _first_vertex_or_centroid(geom, vertices)
        sign_node = ovital_objects.build_sign_object(
            name=f"{shape_name}-出口",
            comment="集水区出口（由 OMAP-MCP 标注）",
            lon=outlet_lon,
            lat=outlet_lat,
        )
        tree = ovital_objects.build_write_tree(
            folder_id=folder_id, folder_name=folder_name, children=[shape_node, sign_node]
        )
        request_summary = {
            "folder_id": folder_id,
            "folder_name": folder_name,
            "shape_name": shape_name,
            "vertex_count": len(vertices),
            "idempotency_key": key,
            "outlet_lonlat": [outlet_lon, outlet_lat],
        }
        change = ctx.store.create_change(  # type: ignore[union-attr]
            task_id=draft_task.task_id,
            action="write_draft",
            snapshot_id=snapshot_id,
            request=request_summary,
            result={"status": "pending"},
            idempotency_key=key,
            verified_against_real_device=False,
            simulated=bool(ctx.is_simulated_connection()),
        )
        try:
            write_results = await commands.write_tree(client, tree)
        except Exception as exc:
            ctx.store.update_change(change["change_id"], result={"status": "failed", "error": str(exc)})  # type: ignore[union-attr]
            raise OmapError(
                f"20112 写入失败：{type(exc).__name__}: {exc}；请先 read_back/检查奥维，禁止直接重试创建以防重复对象",
                details={"change_id": change["change_id"], "snapshot_id": snapshot_id},
            ) from exc
        # 关键：20112 响应可能回显父文件夹（既有对象），不能把它当作本次创建对象，
        # 否则 rollback 会误删用户已有文件夹。
        created_ids: list[int] = []
        for item in write_results or []:
            oid = getattr(item, "obj_id", None)
            if oid is None:
                continue
            oid = int(oid)
            if oid == int(folder_id) and not folder_created:
                continue
            created_ids.append(oid)
        shape_id = next(
            (int(getattr(item, "obj_id")) for item in write_results or [] if int(getattr(item, "type_id", 0)) == 13),
            None,
        )
        sign_id = next(
            (int(getattr(item, "obj_id")) for item in write_results or [] if int(getattr(item, "type_id", 0)) == 7),
            None,
        )
        if shape_id is None:
            ctx.store.update_change(  # type: ignore[union-attr]
                change["change_id"],
                result={"status": "failed", "error": "20112 响应未包含图形 ObjID", "raw_items": jsonable(write_results)},
            )
            raise WriteVerificationError(
                "20112 写入后未解析到图形 ObjID；可能已部分创建，请先用 read_back/奥维界面核对，禁止直接重试。",
                details={"change_id": change["change_id"], "raw_items": jsonable(write_results)},
            )

        # 写后回读：坐标 + 20802 量算。
        warnings = list(folder_warnings) + stale_warnings
        readback: dict[str, Any] = {"obj_id": shape_id}
        try:
            shape = await commands.get_shape(client, shape_id)
            readback_shape = {"vertex_count": len(shape.vertices), "vertices": [[lon, lat] for lon, lat in shape.vertices]}
            comparison = _compare_vertices(vertices, list(shape.vertices))
            readback.update({"shape": readback_shape, "vertex_comparison": comparison})
            if not comparison.get("ok"):
                warnings.append(
                    "写后回读顶点与写入值不一致（超过协议往返容差）：请人工核对轴序/坐标语义。"
                )
        except OmapError as exc:
            warnings.append(f"写后回读图形失败：{exc.message}")
            readback["shape_error"] = exc.to_dict()
        try:
            metrics = await commands.get_shape_metrics(client, shape_id)
            readback["metrics"] = jsonable(metrics)
            local_area = float(geom.area) if expected_area_m2 is None else None
            if expected_area_m2 is not None and metrics.area_m2:
                delta = float(metrics.area_m2) - float(expected_area_m2)
                readback["area_comparison"] = {
                    "local_area_m2": float(expected_area_m2),
                    "ovital_area_m2": float(metrics.area_m2),
                    "delta_m2": delta,
                    "note": "奥维 20802 的面积口径（投影/椭球/孔洞）未实测；差异必须先统一口径再判断。",
                }
            elif local_area is not None:
                readback["local_planar_area_deg2"] = local_area
                warnings.append("未提供 expected_area_m2：20802 面积与本地米制面积不可直接比较。")
        except OmapError as exc:
            warnings.append(f"20802 量算失败：{exc.message}")
            readback["metrics_error"] = exc.to_dict()

        ctx.store.update_change(  # type: ignore[union-attr]
            change["change_id"],
            result={
                "status": "written",
                "created_obj_ids": created_ids,
                "shape_obj_id": shape_id,
                "sign_obj_id": sign_id,
                "folder_id": folder_id,
                "folder_created": folder_created,
                "readback": readback,
            },
        )
        ctx.store.idempotency_put(  # type: ignore[union-attr]
            f"draft:{key}",
            {"obj_id": shape_id, "folder_id": folder_id, "change_id": change["change_id"], "sign_obj_id": sign_id},
        )
        if job_id:
            try:
                task_id = ctx.jobs.load(job_id).task_id  # type: ignore[union-attr]
                ctx.store.record_version(  # type: ignore[union-attr]
                    shape_id,
                    task_id=task_id,
                    version=1,
                    payload={"action": "write_draft", "change_id": change["change_id"], "area_m2": expected_area_m2},
                )
            except Exception:  # noqa: BLE001 - 版本记录失败不应掩盖写入结果
                logger.debug("版本记录失败", exc_info=True)
        if folder_created:
            warnings.append(f"已新建草稿文件夹（ObjID={folder_id}，名称={folder_name!r}）。")
        prov = ovital_provenance(
            ctx,
            extra={"change_id": change["change_id"], "obj_id": shape_id, "snapshot_id": snapshot_id},
        )
        return ok(
            "write_draft",
            {
                "reused": False,
                "obj_id": shape_id,
                "sign_obj_id": sign_id,
                "folder_id": folder_id,
                "folder_created": folder_created,
                "change_id": change["change_id"],
                "snapshot_id": snapshot_id,
                "idempotency_key": key,
                "readback": readback,
                "vertex_count": len(vertices),
            },
            warnings=warnings,
            prov=prov,
        )

    @mcp.tool(
        name="read_back",
        description=(
            "回读奥维对象（图形顶点 + 20802 量算），可选与某个任务/本地边界对比，"
            "报告坐标差与面积差口径警告。"
        ),
    )
    @guarded("read_back", ctx)
    async def read_back(obj_id: int, job_id: str | None = None, expected_area_m2: float | None = None) -> dict[str, Any]:
        from ..protocol import commands

        client = await ctx.ovital()
        shape = await commands.get_shape(client, int(obj_id))
        metrics = await commands.get_shape_metrics(client, int(obj_id))
        payload: dict[str, Any] = {
            "obj_id": int(obj_id),
            "vertex_count": len(shape.vertices),
            "vertices": [[lon, lat] for lon, lat in shape.vertices],
            "metrics": jsonable(metrics),
            "source_semantic": getattr(shape.source_semantic, "value", str(shape.source_semantic)),
        }
        warnings: list[str] = [
            "20802 面积单位按手册为平方米、周长千米；字段名在手册表格与示例之间存在差异，已做兼容。",
            "奥维面积口径（投影/椭球/孔洞）未实测，与本地米制面积比较前必须统一。",
        ]
        if job_id:
            geom, result, _job = boundary_from_job(ctx, job_id)
            local_vertices = _exterior_vertices_lonlat(geom)
            payload["comparison"] = _compare_vertices(local_vertices, list(shape.vertices))
            if expected_area_m2 is None:
                expected_area_m2 = result.get("area_m2")
            if expected_area_m2 is not None:
                delta = float(metrics.area_m2) - float(expected_area_m2)
                payload["area_comparison"] = {
                    "local_area_m2": float(expected_area_m2),
                    "ovital_area_m2": float(metrics.area_m2),
                    "delta_m2": delta,
                    "relative": (delta / float(expected_area_m2)) if expected_area_m2 else None,
                }
        return ok("read_back", payload, warnings=warnings, prov=ovital_provenance(ctx))

    @mcp.tool(
        name="finalize_draft",
        description=(
            "把草稿标记为正式成果（需要 confirm=true）：保存版本记录，可选改名/备注。"
            "该操作通过 20112 替换对象实现，替换语义未在真实设备验证，写前自动快照。"
        ),
    )
    @guarded("finalize_draft", ctx)
    async def finalize_draft(
        obj_id: int,
        confirm: bool = False,
        new_name: str | None = None,
        comment: str | None = None,
        version_note: str = "",
    ) -> dict[str, Any]:
        if not confirm:
            raise DataValidationError(
                "finalize_draft 会修改奥维对象（替换语义），必须显式传 confirm=true；"
                "如只想保存本地版本记录，请用 export_result。"
            )
        from ..protocol import commands, objects as ovital_objects

        client = await ctx.ovital()
        nodes = await commands.get_objects(client, int(obj_id), ctx.config.ovital.latlng_type)
        if not nodes:
            raise OmapError(f"对象不存在：{obj_id}")
        node = nodes[0]
        raw = getattr(node, "raw", None) or {}
        parent_id = int(getattr(node, "parent_id", None) or raw.get("ParentID") or 0)
        if not parent_id:
            raise DataValidationError("只有位于收藏夹文件夹中的对象才能定稿（ParentID=0 的根对象不支持）。")
        folder_nodes = await commands.get_objects(client, parent_id, ctx.config.ovital.latlng_type)
        folder_name = ""
        if folder_nodes:
            folder_name = str(getattr(folder_nodes[0], "name", "") or "")
        payload = json.loads(json.dumps(jsonable(raw)))
        payload.setdefault("Object", {})
        if new_name:
            payload["Object"]["Name"] = new_name
        if comment is not None:
            payload["Object"]["Comment"] = comment
        snapshot_task = ctx.store.create_task("finalize", {"obj_id": int(obj_id)})  # type: ignore[union-attr]
        snapshot_id = ctx.store.save_snapshot(  # type: ignore[union-attr]
            snapshot_task.task_id,
            label=f"finalize 前对象快照 obj={obj_id}",
            payload={"objects": [{"obj_id": int(obj_id), "parent_id": parent_id, "raw": jsonable(raw)}],
                     "folder_name": folder_name},
        )
        change = ctx.store.create_change(  # type: ignore[union-attr]
            task_id=snapshot_task.task_id,
            action="finalize_draft",
            snapshot_id=snapshot_id,
            request={"obj_id": int(obj_id), "new_name": new_name, "version_note": version_note},
            result={"status": "pending"},
            verified_against_real_device=False,
            simulated=bool(ctx.is_simulated_connection()),
        )
        tree = ovital_objects.build_write_tree(folder_id=parent_id, folder_name=folder_name, children=[payload])
        results = await commands.write_tree(client, tree)
        readback = await commands.get_shape(client, int(obj_id))
        ctx.store.update_change(  # type: ignore[union-attr]
            change["change_id"],
            result={
                "status": "written",
                "created_obj_ids": [],
                "shape_obj_id": int(obj_id),
                "replaced": True,
                "readback_vertex_count": len(readback.vertices),
                "write_response": jsonable(results),
            },
        )
        versions = ctx.store.list_versions(int(obj_id))  # type: ignore[union-attr]
        next_version = len(versions) + 1
        ctx.store.record_version(  # type: ignore[union-attr]
            int(obj_id),
            task_id=snapshot_task.task_id,
            version=next_version,
            payload={"action": "finalize_draft", "change_id": change["change_id"], "note": version_note},
        )
        return ok(
            "finalize_draft",
            {
                "obj_id": int(obj_id),
                "version": next_version,
                "change_id": change["change_id"],
                "snapshot_id": snapshot_id,
                "readback_vertex_count": len(readback.vertices),
            },
            warnings=[
                "20112 替换语义与 ParentID 挂接尚未在真实奥维验证；请人工确认对象未被复制或移动到意外位置。",
                "如需回退，用 rollback_change(change_id)。",
            ],
            prov=ovital_provenance(ctx),
        )

    @mcp.tool(
        name="rollback_change",
        description=(
            "回滚一次 write_draft/finalize_draft 变更：删除本次创建的对象，并尝试用写前快照恢复被替换对象。"
            "幂等；返回逐项结果与仍需人工核查的事项。"
        ),
    )
    @guarded("rollback_change", ctx)
    async def rollback_change(change_id: str) -> dict[str, Any]:
        from ..protocol import commands

        change = ctx.store.load_change(change_id)  # type: ignore[union-attr]
        if change.get("rolled_back"):
            return ok(
                "rollback_change",
                {"change_id": change_id, "already_rolled_back": True, "report": change.get("rollback_report")},
                warnings=["该变更已回滚过，未重复执行。"],
                prov=ovital_provenance(ctx),
            )
        result = change.get("result") or {}
        created_ids = [int(v) for v in (result.get("created_obj_ids") or [])]
        client = await ctx.ovital()
        report: dict[str, Any] = {"deleted_obj_ids": [], "restore_attempts": [], "warnings": []}
        if created_ids:
            await commands.delete_objects(client, created_ids, no_group=1)
            report["deleted_obj_ids"] = created_ids
        snapshot = None
        if change.get("snapshot_id"):
            try:
                task_id = change.get("task_id")
                if task_id:
                    snapshot = ctx.store.load_snapshot(task_id, change["snapshot_id"])  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                report["warnings"].append(f"读取写前快照失败，无法恢复被替换对象：{exc}")
        if snapshot:
            payload = snapshot.get("payload") or {}
            objects_to_restore = payload.get("objects") or []
            if objects_to_restore:
                from ..protocol import objects as ovital_objects

                for item in objects_to_restore:
                    try:
                        raw = item.get("raw") or {}
                        parent_id = int(item.get("parent_id") or raw.get("ParentID") or 0)
                        folder_nodes = (
                            await commands.get_objects(client, parent_id, ctx.config.ovital.latlng_type)
                            if parent_id
                            else []
                        )
                        folder_name = str(getattr(folder_nodes[0], "name", "") if folder_nodes else "")
                        tree = ovital_objects.build_write_tree(
                            folder_id=parent_id, folder_name=folder_name, children=[raw]
                        )
                        await commands.write_tree(client, tree)
                        report["restore_attempts"].append({"obj_id": item.get("obj_id"), "ok": True})
                    except Exception as exc:  # noqa: BLE001
                        report["restore_attempts"].append(
                            {"obj_id": item.get("obj_id"), "ok": False, "error": str(exc)}
                        )
        ctx.store.mark_change_rolled_back(change_id, report=report)  # type: ignore[union-attr]
        # 回滚后清除该变更的幂等键，允许用户重新写入（否则会被陈旧键挡住）。
        if change.get("idempotency_key"):
            with contextlib.suppress(Exception):
                ctx.store.idempotency_delete(f"draft:{change['idempotency_key']}")  # type: ignore[union-attr]
        if report["warnings"]:
            report["warnings"].append("回滚不是无损操作；请人工核对奥维中对象状态。")
        return ok(
            "rollback_change",
            {"change_id": change_id, "report": report},
            warnings=["真实奥维上的删除/恢复行为未验证；回滚后必须人工核查。"],
            prov=ovital_provenance(ctx),
        )


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _first_vertex_or_centroid(geom, vertices: list[tuple[float, float]]) -> tuple[float, float]:
    centroid = geom.centroid
    lon, lat = float(centroid.x), float(centroid.y)
    verify_canonical_lonlat(lon, lat)
    return lon, lat
