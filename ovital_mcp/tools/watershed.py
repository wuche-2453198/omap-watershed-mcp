"""集水区任务工具：提交、查询、取消、修订。

设计约束（任务书 C 节）：
- GIS 长任务必须用持久 job_id + 状态查询 + 取消接口，不能依赖一次很长的同步 MCP 调用；
- 计算参数、输入摘要、引擎版本与失败原因全部落盘；
- 失败显式失败，不伪装为空结果。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from ..app import AppContext
from ..coordinates import verify_canonical_lonlat
from ..errors import DataValidationError, JobNotFoundError, OmapError
from ..store import sha256_file
from .common import (
    boundary_from_job,
    compute_provenance,
    effective_real_device_verified,
    guarded,
    image_object,
    jsonable,
    ok,
)

logger = logging.getLogger("ovital_mcp.tools.watershed")

DEFAULT_POLL_AFTER_S = 2.0


def _watershed_params(
    *,
    dem_path: str,
    outlet_lonlat: tuple[float, float],
    engine: str,
    snap_radius_m: float,
    metric_crs: str | None,
    target_res_m: float | None,
    min_accum_cells: int,
    simplify_tolerance_m: float,
    review_max_pixels: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "dem_path": dem_path,
        "outlet_lonlat": [outlet_lonlat[0], outlet_lonlat[1]],
        "engine": engine,
        "snap_radius_m": snap_radius_m,
        "metric_crs": metric_crs,
        "target_res_m": target_res_m,
        "min_accum_cells": min_accum_cells,
        "simplify_tolerance_m": simplify_tolerance_m,
        "review_max_pixels": review_max_pixels,
    }
    if extra:
        params.update(extra)
    return params


async def _resolve_outlet(
    ctx: AppContext,
    *,
    outlet_lonlat: list[float] | tuple[float, float] | None,
    outlet_obj_id: int | None,
    allow_gcj02_conversion: bool,
) -> tuple[tuple[float, float], dict[str, Any]]:
    """确定出口坐标：直接经纬度，或从奥维选中对象坐标读取。"""

    if (outlet_lonlat is None) == (outlet_obj_id is None):
        raise DataValidationError("必须且只能提供 outlet_lonlat 或 outlet_obj_id 之一")
    if outlet_obj_id is not None:
        from ..protocol import commands, objects as ovital_objects

        client = await ctx.ovital()
        nodes = await commands.get_objects(client, int(outlet_obj_id), ctx.config.ovital.latlng_type)
        if not nodes:
            raise OmapError(f"未找到出口对象 {outlet_obj_id}", details={"obj_id": outlet_obj_id})
        raw_node = getattr(nodes[0], "raw", nodes[0]) or {}
        try:
            lon, lat = ovital_objects.object_point_lonlat(
                raw_node, allow_gcj02_conversion=allow_gcj02_conversion
            )
        except Exception as exc:  # noqa: BLE001 - 坐标语义不明确必须显式失败
            raise DataValidationError(
                f"无法从对象 {outlet_obj_id} 解析出明确语义的坐标：{type(exc).__name__}: {exc}",
                details={"obj_id": outlet_obj_id},
            ) from exc
        verify_canonical_lonlat(lon, lat)
        simulated = ctx.is_simulated_connection()
        return (lon, lat), {
            "source": "ovital_object",
            "obj_id": int(outlet_obj_id),
            "object_type": raw_node.get("Type"),
            "simulated": bool(simulated),
            "verified_against_real_device": effective_real_device_verified(ctx, simulated=simulated),
        }
    assert outlet_lonlat is not None
    lon, lat = verify_canonical_lonlat(float(outlet_lonlat[0]), float(outlet_lonlat[1]))
    return (lon, lat), {"source": "explicit_lonlat", "simulated": False}


def _dem_sha(path: str) -> str:
    return sha256_file(path)


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(
        name="start_watershed",
        description=(
            "提交集水区计算任务（异步，立即返回 job_id）。输入为本地 DEM GeoTIFF + 出口坐标（或奥维对象 ID）。"
            "计算在独立 worker 进程运行；用 get_job 查询进度/结果，用 cancel_job 取消。"
            "不会写入奥维；落图需另行调用 write_draft。"
        ),
    )
    @guarded("start_watershed", ctx)
    async def start_watershed(
        dem_path: str,
        outlet_lonlat: list[float] | None = None,
        outlet_obj_id: int | None = None,
        obj_id: int | None = None,
        engine: str | None = None,
        snap_radius_m: float | None = None,
        metric_crs: str | None = None,
        target_res_m: float | None = None,
        min_accum_cells: int | None = None,
        simplify_tolerance_m: float | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        from .dem import require_dem_info

        if obj_id is not None:
            if outlet_obj_id is not None and int(outlet_obj_id) != int(obj_id):
                raise DataValidationError("outlet_obj_id 与兼容别名 obj_id 同时给出且不一致")
            outlet_obj_id = int(obj_id)
        dem = Path(dem_path).expanduser()
        if not dem.is_file():
            raise DataValidationError(f"DEM 文件不存在：{dem}")
        dem_info = await asyncio.to_thread(require_dem_info, str(dem))
        outlet, outlet_meta = await _resolve_outlet(
            ctx,
            outlet_lonlat=outlet_lonlat,
            outlet_obj_id=outlet_obj_id,
            allow_gcj02_conversion=ctx.config.ovital.allow_gcj02_conversion,
        )
        extent = getattr(dem_info, "extent_lonlat", None)
        if extent is not None:
            minx, miny, maxx, maxy = extent
            if not (minx <= outlet[0] <= maxx and miny <= outlet[1] <= maxy):
                raise DataValidationError(
                    "出口坐标不在 DEM 覆盖范围内，无法计算集水区",
                    details={
                        "outlet_lonlat": list(outlet),
                        "dem_extent_lonlat": list(extent),
                        "note": "请换用覆盖上游的 DEM，或修正出口坐标。",
                    },
                )

        resolved_engine = engine or ctx.config.gis.engine
        resolved_snap = float(snap_radius_m if snap_radius_m is not None else ctx.config.gis.snap_radius_m)
        resolved_min_accum = int(
            min_accum_cells if min_accum_cells is not None else ctx.config.gis.min_accum_cells
        )
        resolved_simplify = float(
            simplify_tolerance_m
            if simplify_tolerance_m is not None
            else ctx.config.gis.simplify_tolerance_m
        )
        resolved_metric_crs = metric_crs or ctx.config.gis.metric_crs
        if resolved_engine not in {"auto", "native", "grass"}:
            raise DataValidationError(f"engine 只能是 auto/native/grass，收到 {resolved_engine!r}")
        if not (0 <= resolved_snap <= 5000):
            raise DataValidationError("snap_radius_m 必须在 [0, 5000] 米内（0 表示不吸附，仅用于精确控制点/解析验证）")
        if resolved_simplify < 0:
            raise DataValidationError("simplify_tolerance_m 不能为负（禁止为凑面积而简化）")

        dem_hash = await asyncio.to_thread(_dem_sha, str(dem))
        auto_key = "watershed|{hash}|{lon:.7f},{lat:.7f}|{engine}|{snap}|{minacc}|{simp}".format(
            hash=dem_hash,
            lon=outlet[0],
            lat=outlet[1],
            engine=resolved_engine,
            snap=resolved_snap,
            minacc=resolved_min_accum,
            simp=resolved_simplify,
        )
        key = idempotency_key or hashlib.sha256(auto_key.encode()).hexdigest()[:32]
        existing = ctx.store.idempotency_get(f"job:{key}")  # type: ignore[union-attr]
        if existing:
            job_id = (existing.get("value") or {}).get("job_id")
            try:
                if job_id:
                    record = ctx.jobs.load(job_id)  # type: ignore[union-attr]
                    return ok(
                        "start_watershed",
                        {
                            "job_id": job_id,
                            "task_id": record.task_id,
                            "reused": True,
                            "idempotency_key": key,
                            "poll_after_s": DEFAULT_POLL_AFTER_S,
                            "note": "相同输入与参数的任务已存在，直接返回原任务；如需重算请更换 idempotency_key。",
                        },
                        warnings=["命中幂等键，未重复提交计算任务。"],
                        prov=compute_provenance({"idempotency_key": key}),
                    )
            except JobNotFoundError:
                pass  # 幂等键陈旧，继续新建

        params = _watershed_params(
            dem_path=str(dem.resolve()),
            outlet_lonlat=outlet,
            engine=resolved_engine,
            snap_radius_m=resolved_snap,
            metric_crs=resolved_metric_crs,
            target_res_m=(float(target_res_m) if target_res_m is not None else ctx.config.gis.target_res_m),
            min_accum_cells=resolved_min_accum,
            simplify_tolerance_m=resolved_simplify,
            review_max_pixels=ctx.config.mcp.max_image_pixels,
            extra={
                "grass_cmd": ctx.config.gis.grass_cmd,
                "max_cells": ctx.config.gis.max_cells,
                "data_root": str(ctx.paths.data_root),  # type: ignore[union-attr]
                "latlng_type": ctx.config.ovital.latlng_type,
            },
        )
        simulated = bool(outlet_meta.get("simulated"))
        task = ctx.store.create_task(  # type: ignore[union-attr]
            "watershed",
            params,
            extra={"outlet": outlet_meta, "dem_info": jsonable(dem_info)},
        )
        ctx.store.record_input(task.task_id, str(dem.resolve()), role="dem")  # type: ignore[union-attr]
        ctx.store.update_provenance(  # type: ignore[union-attr]
            task.task_id,
            inputs={"dem": jsonable(dem_info)},
            outlet=outlet_meta,
            params=params,
            simulated=simulated,
            verified_against_real_device=False,
            evidence_level="offline_computed",
        )
        params["task_id"] = task.task_id
        job = ctx.jobs.submit(  # type: ignore[union-attr]
            "watershed",
            params,
            task_id=task.task_id,
            simulated=simulated,
            verified_against_real_device=False,
        )
        ctx.store.update_task(  # type: ignore[union-attr]
            task.task_id,
            status="running",
            job_id=job.job_id,
            updated_at=__import__("datetime").datetime.utcnow().isoformat(timespec="seconds"),
        )
        ctx.store.idempotency_put(f"job:{key}", {"job_id": job.job_id, "task_id": task.task_id})  # type: ignore[union-attr]
        warnings = list(getattr(dem_info, "warnings", []) or [])
        warnings.append(
            "边界正确性取决于 DEM 质量与出口位置；本机无用户审核边界，真实工程验收未完成。"
        )
        if outlet_meta.get("source") == "ovital_object":
            warnings.append(
                "出口来自奥维对象坐标；其坐标语义/偏移必须与 DEM 的 CRS 基线一致（见 read_object 返回）。"
            )
        return ok(
            "start_watershed",
            {
                "job_id": job.job_id,
                "task_id": task.task_id,
                "idempotency_key": key,
                "poll_after_s": DEFAULT_POLL_AFTER_S,
                "params": params,
                "outlet": {"lon": outlet[0], "lat": outlet[1], **outlet_meta},
                "dem": jsonable(dem_info),
            },
            warnings=warnings,
            prov=compute_provenance(
                {
                    "dem_sha256": dem_hash,
                    "engine": resolved_engine,
                    "outlet": list(outlet),
                    "task_id": task.task_id,
                    "job_id": job.job_id,
                }
            ),
        )

    @mcp.tool(
        name="get_job",
        description=(
            "查询持久任务状态/阶段/错误/成果摘要；include_images=true 时返回复核图（MCP 图片内容）。"
            "若任务是修订任务，会附与父任务的面积/边界差异比较。"
        ),
    )
    @guarded("get_job", ctx)
    async def get_job(job_id: str, include_images: bool = False):
        record = ctx.jobs.load(job_id)  # type: ignore[union-attr]
        result = ctx.jobs.result(job_id) or {}  # type: ignore[union-attr]
        inner = result.get("result") if isinstance(result.get("result"), dict) else None
        payload: dict[str, Any] = {
            "job": record.summary(),
            "result_summary": {
                "ok": result.get("ok"),
                "boundary_geojson": (inner or {}).get("boundary_geojson"),
                "mask_geotiff": (inner or {}).get("mask_geotiff"),
                "area_m2": (inner or {}).get("area_m2"),
                "perimeter_m": (inner or {}).get("perimeter_m"),
                "metric_crs": (inner or {}).get("metric_crs"),
                "engine_used": (inner or {}).get("engine_used"),
                "engine_version": (inner or {}).get("engine_version"),
                "outlet_used": (inner or {}).get("outlet_used"),
                "snap_distance_m": (inner or {}).get("snap_distance_m"),
                "flow_accum_cells": (inner or {}).get("flow_accum_cells"),
                "warnings": (inner or {}).get("warnings"),
                "dem_info": (inner or {}).get("dem_info"),
                "review": result.get("review"),
            },
            "poll_after_s": DEFAULT_POLL_AFTER_S,
        }
        warnings: list[str] = []
        if record.status in {"queued", "running"}:
            warnings.append("任务尚未完成，请稍后再次调用 get_job；可用 cancel_job 取消。")
        if record.status == "failed":
            payload["log_tail"] = ctx.jobs.log_tail(job_id)  # type: ignore[union-attr]
        if record.parent_job_id and record.status == "succeeded":
            comparison = await asyncio.to_thread(_compare_jobs, ctx, record.parent_job_id, job_id)
            payload["comparison"] = comparison

        images = []
        if include_images and record.status == "succeeded":
            figure_paths = _review_figure_paths(ctx, job_id)
            for figure in figure_paths[:2]:
                try:
                    images.append(image_object(figure["path"], max_bytes=ctx.config.mcp.image_max_bytes))
                except OmapError as exc:
                    warnings.append(f"复核图读取失败：{exc.message}")
        payload["review_figure_count"] = len(_review_figure_paths(ctx, job_id))
        prov = compute_provenance(
            {
                "job_id": job_id,
                "task_id": record.task_id,
                "simulated": record.simulated,
                "verified_against_real_device": record.verified_against_real_device,
            }
        )
        if images:
            return [ok("get_job", payload, warnings=warnings, prov=prov), *images]
        return ok("get_job", payload, warnings=warnings, prov=prov)

    @mcp.tool(
        name="cancel_job",
        description="取消正在运行的持久任务（终止 worker 进程，幂等）。已完成的任务保持不变。",
    )
    @guarded("cancel_job", ctx)
    async def cancel_job(job_id: str) -> dict[str, Any]:
        record = await asyncio.to_thread(ctx.jobs.cancel, job_id)  # type: ignore[union-attr]
        return ok(
            "cancel_job",
            {"job": record.summary()},
            warnings=[] if record.status == "cancelled" else [f"任务当前状态：{record.status}"],
            prov=compute_provenance({"job_id": job_id}),
        )

    @mcp.tool(
        name="list_jobs",
        description="列出最近的计算任务（job_id/状态/进度），用于恢复上下文。",
    )
    @guarded("list_jobs", ctx)
    async def list_jobs(limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        items = ctx.jobs.list_jobs(limit=limit)  # type: ignore[union-attr]
        return ok(
            "list_jobs",
            {"count": len(items), "jobs": items},
            prov=compute_provenance(),
        )

    @mcp.tool(
        name="revise_candidate",
        description=(
            "基于已有任务发起修订任务（新 job）：可改出口、吸附半径、汇流阈值、简化容差、引擎或指定分析 CRS。"
            "可附带 anchors_lonlat 记录模型疑点锚点（v0.1 仅记录与对比，不会强行把边界拉过锚点）。"
            "修订是重新计算，保留父任务成果以供对比。"
        ),
    )
    @guarded("revise_candidate", ctx)
    async def revise_candidate(
        job_id: str,
        outlet_lonlat: list[float] | None = None,
        snap_radius_m: float | None = None,
        min_accum_cells: int | None = None,
        simplify_tolerance_m: float | None = None,
        engine: str | None = None,
        metric_crs: str | None = None,
        anchors_lonlat: list[list[float]] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        parent = ctx.jobs.load(job_id)  # type: ignore[union-attr]
        if parent.status != "succeeded":
            raise OmapError(f"父任务 {job_id} 尚未成功完成，无法修订（status={parent.status}）")
        parent_params = dict(parent.params)
        parent_inner = (ctx.jobs.result(job_id) or {}).get("result") or {}  # type: ignore[union-attr]
        warnings: list[str] = []
        new_outlet = parent_inner.get("outlet_used") or parent_params.get("outlet_lonlat")
        if outlet_lonlat is not None:
            lon, lat = verify_canonical_lonlat(float(outlet_lonlat[0]), float(outlet_lonlat[1]))
            new_outlet = [lon, lat]
        if snap_radius_m is not None:
            parent_params["snap_radius_m"] = float(snap_radius_m)
        if min_accum_cells is not None:
            parent_params["min_accum_cells"] = int(min_accum_cells)
        if simplify_tolerance_m is not None:
            parent_params["simplify_tolerance_m"] = float(simplify_tolerance_m)
        if engine is not None:
            if engine not in {"auto", "native", "grass"}:
                raise DataValidationError("engine 只能是 auto/native/grass")
            parent_params["engine"] = engine
        if metric_crs is not None:
            parent_params["metric_crs"] = metric_crs
        anchors: list[dict[str, Any]] = []
        if anchors_lonlat:
            for index, point in enumerate(anchors_lonlat, start=1):
                lon, lat = verify_canonical_lonlat(float(point[0]), float(point[1]))
                anchors.append({"anchor_id": f"R-{index:02d}", "lon": lon, "lat": lat})
            warnings.append(
                "anchors_lonlat 仅作为复核锚点记录在任务参数与报告中；v0.1 不会把边界强行拉过锚点"
                "（避免篡改水文拓扑）。锚点对应的局部修订功能列为待实现/待实测。"
            )
        if reason:
            parent_params["revision_reason"] = reason
        parent_params["revision_of"] = job_id
        parent_params["review_anchors"] = anchors
        parent_params["outlet_lonlat"] = new_outlet
        dem_path = parent_params.get("dem_path")
        if not dem_path:
            raise OmapError("父任务缺少 dem_path，无法修订")
        task = ctx.store.create_task(  # type: ignore[union-attr]
            "watershed_revision",
            parent_params,
            parent_task_id=parent.task_id,
            extra={"revision_of": job_id, "anchors": anchors, "reason": reason},
        )
        ctx.store.record_input(task.task_id, dem_path, role="dem")  # type: ignore[union-attr]
        ctx.store.update_provenance(  # type: ignore[union-attr]
            task.task_id,
            revision_of=job_id,
            anchors=anchors,
            params=parent_params,
            evidence_level="offline_computed",
            verified_against_real_device=False,
        )
        parent_params["task_id"] = task.task_id
        job = ctx.jobs.submit(  # type: ignore[union-attr]
            "watershed",
            parent_params,
            task_id=task.task_id,
            parent_job_id=job_id,
            simulated=parent.simulated,
            verified_against_real_device=False,
        )
        return ok(
            "revise_candidate",
            {
                "job_id": job.job_id,
                "task_id": task.task_id,
                "parent_job_id": job_id,
                "params": parent_params,
                "anchors": anchors,
                "poll_after_s": DEFAULT_POLL_AFTER_S,
                "comparison": "完成后再调用 get_job（会自动附 comparison：面积差与边界差异）",
            },
            warnings=warnings,
            prov=compute_provenance({"parent_job_id": job_id, "job_id": job.job_id}),
        )


def _review_figure_paths(ctx: AppContext, job_id: str) -> list[dict[str, Any]]:
    """从 job 结果/artifacts 读取复核图清单（不重复生成）。"""

    result = ctx.jobs.result(job_id) or {}  # type: ignore[union-attr]
    review = result.get("review") or {}
    figures = review.get("figures") or []
    if figures:
        return [f for f in figures if f.get("path")]
    job_dir = ctx.jobs.job_dir(job_id)  # type: ignore[union-attr]
    manifest_path = job_dir / "artifacts" / "review" / "manifest.json"
    if manifest_path.is_file():
        try:
            import json

            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            return [f for f in (data.get("figures") or []) if f.get("path")]
        except Exception:  # noqa: BLE001
            return []
    return []


def _compare_jobs(ctx: AppContext, parent_job_id: str, child_job_id: str) -> dict[str, Any]:
    """比较父/子任务边界：面积差与几何差异（统一投影到子任务 metric_crs）。"""

    try:
        parent_geom, parent_result, _ = boundary_from_job(ctx, parent_job_id)
        child_geom, child_result, _ = boundary_from_job(ctx, child_job_id)
    except OmapError as exc:
        return {"ok": False, "error": exc.to_dict()}
    from ..coordinates import transform_geometry

    metric_crs = child_result.get("metric_crs")
    if not metric_crs:
        return {"ok": False, "error": {"code": "MISSING_CRS", "message": "子任务缺少 metric_crs"}}
    try:
        parent_m = transform_geometry(parent_geom, "EPSG:4490", metric_crs)
        child_m = transform_geometry(child_geom, "EPSG:4490", metric_crs)
        parent_area = float(parent_m.area)
        child_area = float(child_m.area)
        symmetric = float(parent_m.symmetric_difference(child_m).area)
        hausdorff = float(parent_m.hausdorff_distance(child_m))
        return {
            "ok": True,
            "metric_crs": metric_crs,
            "parent_area_m2": parent_area,
            "child_area_m2": child_area,
            "area_delta_m2": child_area - parent_area,
            "area_delta_pct": ((child_area - parent_area) / parent_area * 100.0) if parent_area else None,
            "symmetric_difference_area_m2": symmetric,
            "symmetric_difference_pct_of_parent": (symmetric / parent_area * 100.0) if parent_area else None,
            "hausdorff_distance_m": hausdorff,
            "note": "面积相近不代表边界正确；请结合复核图与分段证据判断。",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": {"code": "COMPARISON_FAILED", "message": str(exc)}}
