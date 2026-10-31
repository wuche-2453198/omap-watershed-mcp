"""成果导出：GeoJSON / KML / 投影坐标 JSON / 报告（GeoPackage 可选）。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from ..app import AppContext
from ..coordinates import transform_geometry
from ..errors import DataValidationError, ExportError
from ..store import sha256_file
from .common import (
    boundary_from_job,
    compute_provenance,
    guarded,
    jsonable,
    ok,
)

logger = logging.getLogger("ovital_mcp.tools.export")

EXPORT_FORMATS = {"geojson", "kml", "report", "projected_json", "gpkg"}


def _feature_collection(geom, *, properties: dict[str, Any]) -> dict[str, Any]:
    from shapely.geometry import mapping

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": jsonable(mapping(geom)),
                "properties": jsonable(properties),
            }
        ],
    }


def _kml(geom, *, name: str, description: str) -> str:
    if geom.geom_type != "Polygon":
        raise ExportError(f"KML 导出 v0.1 只支持单个 Polygon，收到 {geom.geom_type}")
    coords = " ".join(f"{lon},{lat},0" for lon, lat in geom.exterior.coords)
    from xml.sax.saxutils import escape

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{escape(name)}</name>
    <description>{escape(description)}</description>
    <Placemark>
      <name>{escape(name)}</name>
      <description>{escape(description)}</description>
      <Style>
        <LineStyle><color>ff0080ff</color><width>3</width></LineStyle>
        <PolyStyle><color>400080ff</color></PolyStyle>
      </Style>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing><coordinates>{coords}</coordinates></LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
"""


def _report_markdown(
    ctx: AppContext,
    job_id: str,
    job: dict[str, Any],
    result: dict[str, Any],
    review: dict[str, Any] | None = None,
) -> str:
    review = review or {}
    figures = review.get("figures") or []
    warnings = result.get("warnings") or []
    lines = [
        f"# 集水区成果报告 {job_id}",
        "",
        f"- 生成时间（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 任务 ID：`{job_id}`；任务类型：{job.get('kind')}",
        "- 证据级别：offline_computed（本机计算，未经真实 Windows 奥维联机与人工审核边界验收）",
        f"- 模拟连接：{bool(job.get('simulated'))}；real_device_verified：false",
        "",
        "## 1. 输入与参数",
        "",
        f"- DEM：`{result.get('dem_info', {}).get('path') if isinstance(result.get('dem_info'), dict) else 'n/a'}`",
        f"- DEM SHA256：`{(result.get('dem_info') or {}).get('sha256') if isinstance(result.get('dem_info'), dict) else 'n/a'}`",
        f"- 计算引擎：{result.get('engine_used')}（{result.get('engine_version')}）",
        f"- 分析投影 CRS：{result.get('metric_crs')}",
        f"- 出口输入：{result.get('outlet_input')}；出口采用：{result.get('outlet_used')}；吸附距离：{result.get('snap_distance_m')} m",
        f"- 分析范围（EPSG:4490 bbox）：{result.get('analysis_extent_lonlat')}",
        "",
        "## 2. 成果量算",
        "",
        "| 项目 | 数值 | 口径 |",
        "|---|---|---|",
        f"| 水平面积 | {result.get('area_m2')} m² | shapely 在 {result.get('metric_crs')} 的平面面积 |",
        f"| 周长 | {result.get('perimeter_m')} m | 同上 |",
        f"| 几何有效 | {result.get('geometry_valid')} | 闭合/自交检查 |",
        f"| 部件/洞 | multipart={result.get('multipart')} / holes={result.get('hole_count')} | |",
        "",
        "## 3. 复核图",
        "",
    ]
    if figures:
        for fig in figures:
            lines.append(f"- `{fig.get('figure_id')}`：{fig.get('path')}（segment={fig.get('segment_id')}）")
    else:
        lines.append("- 无（复核图生成失败或未启用；不得声称已完成视觉复核）")
    lines += ["", "## 4. 警告与限制", ""]
    if warnings:
        lines += [f"- {item}" for item in warnings]
    else:
        lines.append("- 无引擎警告")
    lines += [
        "- 面积相近不代表边界正确；本边界未与独立人工/测绘参考比较。",
        "- 奥维 20802 的面积口径未实测，落图回读面积不可直接与本地米制面积等同。",
        "- 高程基准/DSM-DTM 情况见 DEM 检查警告；真实工程精度需更高精度数据与人工复核。",
        "",
        "## 5. 未完成的验收项",
        "",
        "- 真实 Windows 奥维联机读写与回读",
        "- 用户账号 VIP/SVIP 权限与地图图片/等高线接口",
        "- 用户提供的 DEM、坐标系统与经审核参照边界的精度验收",
        "- DSH 视觉模型在真实会话中接收复核图并完成判读（由独立验证步骤完成）",
        "",
        "## 6. 复现",
        "",
        f"- 任务参数：`{json.dumps(jsonable(job.get('params')), ensure_ascii=False)}`",
        "",
    ]
    return "\n".join(lines)


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(
        name="export_result",
        description=(
            "导出任务成果：geojson（EPSG:4490）、kml、report（Markdown）、projected_json（米制投影，非标准 GeoJSON）、"
            "gpkg（需可选 fiona，不可用会显式报错）。返回文件路径、大小与 SHA256。"
        ),
    )
    @guarded("export_result", ctx)
    async def export_result(
        job_id: str,
        format: str = "geojson",
        include_report: bool = True,
    ) -> dict[str, Any]:
        fmt = (format or "geojson").lower()
        if fmt not in EXPORT_FORMATS:
            raise DataValidationError(f"不支持的导出格式 {format!r}；可选 {sorted(EXPORT_FORMATS)}")
        geom, result, job = boundary_from_job(ctx, job_id)
        job_record = ctx.jobs.load(job_id)  # type: ignore[union-attr]
        # review 图清单在 worker 写出的外层 result 包装里（boundary_from_job 返回内层引擎成果）。
        review_block = (ctx.jobs.result(job_id) or {}).get("review") or {}
        properties = {
            "job_id": job_id,
            "task_id": job_record.task_id,
            "area_m2": result.get("area_m2"),
            "metric_crs": result.get("metric_crs"),
            "engine_used": result.get("engine_used"),
            "engine_version": result.get("engine_version"),
            "outlet_used": result.get("outlet_used"),
            "snap_distance_m": result.get("snap_distance_m"),
            "warnings": result.get("warnings"),
            "evidence_level": "offline_computed",
            "verified_against_real_device": False,
            "simulated": bool(job_record.simulated),
            "crs": "EPSG:4490",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "limitations": [
                "本成果为本机确定性计算，未经真实 Windows 奥维联机与人工审核边界验收",
                "水平投影面积，不含坡面面积；不承诺工程精度",
            ],
        }
        exported: list[dict[str, Any]] = []
        warnings: list[str] = []
        stem = f"{job_id}-watershed"

        def _export_path(name: str) -> Path:
            return ctx.store.export_path(name)  # type: ignore[union-attr]

        if fmt == "geojson":
            payload = _feature_collection(geom, properties=properties)
            path = _export_path(f"{stem}.geojson")
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            exported.append(_file_entry(path, "geojson"))
            warnings.append("GeoJSON 坐标按 RFC 7946 为 EPSG:4490 经纬度（CGCS2000 与 WGS84 厘米级差异）。")
        elif fmt == "kml":
            path = _export_path(f"{stem}.kml")
            path.write_text(
                _kml(geom, name=f"集水区 {job_id}", description="OMAP-MCP 草稿成果，需人工复核"),
                encoding="utf-8",
            )
            exported.append(_file_entry(path, "kml"))
        elif fmt == "projected_json":
            metric_crs = result.get("metric_crs")
            if not metric_crs:
                raise ExportError("任务缺少 metric_crs，无法导出投影成果")
            projected = transform_geometry(geom, "EPSG:4490", metric_crs)
            payload = _feature_collection(projected, properties={**properties, "metric_crs": metric_crs})
            payload["note"] = (
                "非标准 GeoJSON：几何坐标为米制投影坐标（" + metric_crs + "），仅供工程计算/交换，"
                "不是 RFC 7946 WGS84 GeoJSON。"
            )
            path = _export_path(f"{stem}-{metric_crs.replace(':', '')}.json")
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            exported.append(_file_entry(path, "projected_json"))
        elif fmt == "gpkg":
            path = await asyncio.to_thread(_write_gpkg, geom, properties, _export_path(f"{stem}.gpkg"))
            exported.append(_file_entry(path, "gpkg"))
            warnings.append("GeoPackage 由可选依赖 fiona 写出；本机是否可用见成功/失败结果。")
        else:  # report
            pass

        if include_report or fmt == "report":
            report_path = _export_path(f"{stem}-report.md")
            report_path.write_text(
                _report_markdown(ctx, job_id, job, result, review_block), encoding="utf-8"
            )
            exported.append(_file_entry(report_path, "report"))

        warnings.append(
            "导出文件不改变奥维草稿对象状态；定稿/回滚请用 finalize_draft / rollback_change。"
        )
        return ok(
            "export_result",
            {
                "job_id": job_id,
                "format": fmt,
                "files": exported,
                "area_m2": result.get("area_m2"),
                "metric_crs": result.get("metric_crs"),
                "geometry_crs": "EPSG:4490",
                "review_figures": [
                    {"figure_id": f.get("figure_id"), "path": f.get("path")}
                    for f in (review_block.get("figures") or [])
                ],
            },
            warnings=warnings,
            prov=compute_provenance({"job_id": job_id, "exports": [f["path"] for f in exported]}),
        )


def _file_entry(path: Path, role: str) -> dict[str, Any]:
    return {
        "role": role,
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _write_gpkg(geom, properties: dict[str, Any], path: Path) -> Path:
    try:
        import fiona  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ExportError(
            "GeoPackage 导出需要可选依赖 fiona（含 GDAL 向量驱动），当前环境不可用；"
            "请改用 geojson/kml/report，或在 GIS 环境安装 fiona 后重试。",
            details={"install_hint": "conda install -c conda-forge fiona（与 rasterio 同一环境）"},
        ) from exc
    from shapely.geometry import mapping

    schema = {"geometry": "Polygon", "properties": {"area_m2": "float", "job_id": "str", "metric_crs": "str"}}
    with fiona.open(
        path,
        "w",
        driver="GPKG",
        crs="EPSG:4490",
        schema=schema,
        layer="watershed",
    ) as dst:
        dst.write(
            {
                "geometry": mapping(geom),
                "properties": {
                    "area_m2": properties.get("area_m2"),
                    "job_id": properties.get("job_id"),
                    "metric_crs": properties.get("metric_crs"),
                },
            }
        )
    return path
