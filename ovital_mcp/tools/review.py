"""复核与几何工具：分段复核图、几何校验、面积/周长量算。"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

from mcp.server.mcpserver import MCPServer

from ..app import AppContext
from ..coordinates import metric_crs_for
from ..errors import DataValidationError, GeometryError, OmapError
from .common import (
    boundary_from_job,
    compute_provenance,
    geometry_from_geojson,
    guarded,
    image_object,
    jsonable,
    load_job_result,
    ok,
)

logger = logging.getLogger("ovital_mcp.tools.review")


def _result_object(ctx: AppContext, job_id: str):
    """把 JSON 化的结果恢复成 GIS 结果对象（供复核渲染调用）。"""

    from ..gis.engine import WatershedResult

    _job, result = load_job_result(ctx, job_id)
    field_names = {f.name for f in dataclasses.fields(WatershedResult)}
    kwargs = {key: value for key, value in result.items() if key in field_names}
    try:
        return WatershedResult(**kwargs)
    except TypeError as exc:
        raise OmapError(
            f"任务结果无法恢复为 WatershedResult：{exc}",
            details={"job_id": job_id, "available_fields": sorted(kwargs)},
        ) from exc


def _fallback_boundary_geometry(value: Any):
    geom = geometry_from_geojson(value)
    if geom.geom_type not in {"Polygon", "MultiPolygon"}:
        raise DataValidationError(f"需要面状几何，收到 {geom.geom_type}")
    return geom


def _metric_crs_for_geom(geom: Any, requested: str | None) -> str:
    if requested:
        return requested
    centroid = geom.centroid
    return metric_crs_for(float(centroid.x), float(centroid.y))


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(
        name="review_segment",
        description=(
            "返回指定边界分段的局部复核图（MCP 图片）+ 该段统计（长度/平均高程/锚点 ID/范围）。"
            "调用后必须实际查看图片再判断边界；坐标/面积不能替代地形判读。"
        ),
    )
    @guarded("review_segment", ctx)
    async def review_segment(job_id: str, segment_id: int | None = None, max_pixels: int = 1400):
        from ..gis.review import render_segment
        from ..gis.segment import list_segments

        _job, _result = load_job_result(ctx, job_id)
        result_obj = await asyncio.to_thread(_result_object, ctx, job_id)
        boundary_path = getattr(result_obj, "boundary_geojson", None)
        if not boundary_path:
            raise GeometryError(f"任务 {job_id} 缺少边界成果")
        segments = await asyncio.to_thread(
            list_segments,
            boundary_path,
            n_segments=8,
            metric_crs=getattr(result_obj, "metric_crs", None),
        )
        if not segments:
            raise GeometryError(f"任务 {job_id} 的边界无法分段")
        target = segment_id if segment_id is not None else segments[0]["segment_id"]
        selected = next((s for s in segments if int(s["segment_id"]) == int(target)), None)
        if selected is None:
            raise DataValidationError(
                f"segment_id={target} 不存在；可选范围 {[s['segment_id'] for s in segments]}"
            )
        out_dir = ctx.jobs.job_dir(job_id) / "artifacts" / "review_segments"  # type: ignore[union-attr]
        out_dir.mkdir(parents=True, exist_ok=True)
        rendered = await asyncio.to_thread(
            render_segment,
            result_obj,
            int(target),
            out_dir,
            max_pixels=max(400, min(int(max_pixels), ctx.config.mcp.max_image_pixels)),
        )
        payload = {
            "job_id": job_id,
            "segment_id": int(target),
            "segment": jsonable(selected),
            "all_segments": [
                {
                    "segment_id": s["segment_id"],
                    "length_m": s.get("length_m"),
                    "mean_elev_m": s.get("mean_elev_m"),
                    "bbox_lonlat": s.get("bbox_lonlat"),
                    "start_anchor_id": s.get("start_anchor_id"),
                    "end_anchor_id": s.get("end_anchor_id"),
                }
                for s in segments
            ],
            "figure": jsonable(rendered),
            "review_instructions": [
                "检查该段是否沿山脊/分水岭而非河谷。",
                "检查是否跨越鞍部纳入邻近流域，尤其是低洼平缓处。",
                "检查出口段是否包含全部关键支流。",
                "若发现疑点，记录段号、锚点 ID、依据与所需数据，再调用 revise_candidate。",
            ],
        }
        warnings = list(rendered.get("warnings") or [])
        images = []
        for figure in rendered.get("figures") or []:
            if figure.get("path"):
                images.append(image_object(figure["path"], max_bytes=ctx.config.mcp.image_max_bytes))
        if not images:
            warnings.append("复核图渲染未产出图片；不得在没有视觉复核的情况下声称边界已复核。")
        prov = compute_provenance(
            {
                "job_id": job_id,
                "segment_id": int(target),
                "evidence_level": "offline_computed",
                "note": "复核图由本地 DEM 渲染；不是奥维底图，也不构成独立验证。",
            }
        )
        if images:
            return [ok("review_segment", payload, warnings=warnings, prov=prov), *images]
        return ok("review_segment", payload, warnings=warnings, prov=prov)

    @mcp.tool(
        name="validate_geometry",
        description=(
            "检查边界几何：闭合/环有效性、自相交、重复点、异常细长片、多部件与洞。"
            "输入可以是任务 ID，或 GeoJSON 文本/文件（假定 EPSG:4490）。"
        ),
    )
    @guarded("validate_geometry", ctx)
    async def validate_geometry(
        job_id: str | None = None,
        geojson: Any = None,
        simplify_tolerance_m: float | None = None,
    ) -> dict[str, Any]:
        from ..gis.geometry import simplify_preserving, validate_geometry as _validate

        geom = await _geometry_for(ctx, job_id=job_id, geojson=geojson)
        check = await asyncio.to_thread(_validate, geom)
        payload: dict[str, Any] = {"geometry": jsonable(check)}
        warnings: list[str] = list(getattr(check, "issues", []) or [])
        if simplify_tolerance_m is not None:
            if simplify_tolerance_m < 0:
                raise DataValidationError("simplify_tolerance_m 不能为负")
            if simplify_tolerance_m > 0:
                from ..coordinates import metric_crs_for as _mcrs

                metric = _mcrs(float(geom.centroid.x), float(geom.centroid.y))
                simplified = await asyncio.to_thread(
                    simplify_preserving,
                    geom,
                    float(simplify_tolerance_m),
                    metric_crs=metric,
                )
                simplified_geom, simplify_report = simplified
                payload["simplification"] = {
                    "geometry_vertex_count": len(simplified_geom.exterior.coords)
                    if getattr(simplified_geom, "exterior", None) is not None
                    else None,
                    "report": jsonable(simplify_report),
                }
                warnings.append("简化只允许在明确容差内进行；任何拓扑/关键转折变化必须在报告中说明。")
        return ok(
            "validate_geometry",
            payload,
            warnings=warnings,
            prov=compute_provenance({"job_id": job_id, "assumed_crs": "EPSG:4490"}),
        )

    @mcp.tool(
        name="measure_geometry",
        description=(
            "计算水平面积/周长/顶点数/部件与洞数量。输入任务 ID 或 GeoJSON（假定 EPSG:4490），"
            "内部投影到米制 CRS 计算；返回明确的方法、CRS 与单位。"
        ),
    )
    @guarded("measure_geometry", ctx)
    async def measure_geometry(
        job_id: str | None = None,
        geojson: Any = None,
        metric_crs: str | None = None,
    ) -> dict[str, Any]:
        from ..gis.geometry import measure as _measure

        geom = await _geometry_for(ctx, job_id=job_id, geojson=geojson)
        crs = _metric_crs_for_geom(geom, metric_crs)
        measured = await asyncio.to_thread(_measure, geom, crs)
        warnings = [
            "水平投影面积，不含坡面面积；面积口径（投影/椭球/孔洞处理）见字段说明。",
        ]
        if job_id:
            _job, result = load_job_result(ctx, job_id)
            local_area = result.get("area_m2")
            if local_area is not None:
                try:
                    delta = float(measured.area_m2) - float(local_area)
                except (TypeError, ValueError):
                    delta = None
                if delta is not None:
                    warnings.append(
                        f"与任务记录面积差 {delta:.3f} m²（任务值来自引擎，"
                        f"metric_crs={result.get('metric_crs')}）。面积相近不代表边界正确。"
                    )
        return ok(
            "measure_geometry",
            {
                "measurement": jsonable(measured),
                "metric_crs": crs,
                "method": "shapely 在米制投影下的 planar area/length；EPSG:4490 → 米制 CRS（pyproj always_xy）",
                "units": {"area": "m²", "length": "m"},
            },
            warnings=warnings,
            prov=compute_provenance({"job_id": job_id, "metric_crs": crs}),
        )


async def _geometry_for(ctx: AppContext, *, job_id: str | None, geojson: Any):
    if job_id and geojson is not None:
        raise DataValidationError("job_id 与 geojson 只能提供一个")
    if job_id:
        geom, _result, _job = boundary_from_job(ctx, job_id)
        return geom
    if geojson is None:
        raise DataValidationError("必须提供 job_id 或 geojson 之一")
    return await asyncio.to_thread(_fallback_boundary_geometry, geojson)
