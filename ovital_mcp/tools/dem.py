"""DEM 检查工具。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.mcpserver import MCPServer

from ..app import AppContext
from ..errors import DataValidationError
from .common import compute_provenance, guarded, jsonable, ok

logger = logging.getLogger("ovital_mcp.tools.dem")


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(
        name="inspect_dem",
        description=(
            "检查本地 DEM（GeoTIFF）：CRS/轴序、分辨率、NoData、高程范围、覆盖范围、高程基准未知项等。"
            "不修改文件。返回 warnings 中的问题必须在后续集水区计算前处理或记录。"
        ),
    )
    @guarded("inspect_dem", ctx)
    async def inspect_dem(dem_path: str) -> dict[str, Any]:
        from ..gis.dem import inspect_dem as _inspect

        info = await asyncio.to_thread(_inspect, dem_path)
        payload = {"dem": jsonable(info)}
        warnings: list[str] = list(getattr(info, "warnings", []) or [])
        if getattr(info, "provisional", False):
            warnings.append("DEM 检查结果被标记为 provisional：存在未解决的数据质量疑点。")
        prov = compute_provenance(
            {
                "dem_path": str(getattr(info, "path", dem_path)),
                "dem_sha256": getattr(info, "sha256", None),
                "note": "DEM 由用户提供；本工具不下载、不修改 DEM。",
            }
        )
        return ok("inspect_dem", payload, warnings=warnings, prov=prov)


def require_dem_info(dem_path: str):
    """供其他工具复用的预检；无 CRS / 文件不可读时抛 DataValidationError。"""

    from ..gis.dem import inspect_dem as _inspect

    info = _inspect(dem_path)
    if getattr(info, "crs_wkt", None) is None and getattr(info, "crs_epsg", None) is None:
        raise DataValidationError(
            f"DEM 缺少 CRS 定义，无法安全计算集水区：{getattr(info, 'path', dem_path)}",
            details={"path": str(getattr(info, "path", dem_path))},
        )
    return info
