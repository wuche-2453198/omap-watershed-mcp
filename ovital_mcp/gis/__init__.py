"""确定性 GIS 集水区计算包。

对 tools 层的公开接口（契约 ``docs/architecture.md`` §5）：

- :func:`ovital_mcp.gis.dem.inspect_dem` — DEM 检查（CRS/NoData/量程/基准/分辨率）；
- :func:`ovital_mcp.gis.engine.run_watershed` — 唯一计算入口
  （``auto`` 先 GRASS，再 native）；
- :func:`ovital_mcp.gis.geometry.validate_geometry` / ``measure`` /
  ``simplify_preserving`` / ``revise_with_anchors`` — 几何校验、量算与受控修订；
- :func:`ovital_mcp.gis.segment.segment_boundary` / ``list_segments`` — 边界分段；
- ``review.render_overview`` / ``render_review_pack`` / ``render_segment`` —
  复核图（matplotlib 非交互后端）。

``review`` 依赖 matplotlib，按需惰性导入（MCP 服务启动时不加载）。
"""

from __future__ import annotations

from .dem import DemInfo, inspect_dem, sample_dem_lonlat, sha256_file
from .engine import (
    BOUNDARY_GEOJSON,
    FAILURE_JSON,
    MASK_GEOTIFF,
    PROVENANCE_JSON,
    RESULT_JSON,
    WatershedEngine,
    WatershedParams,
    WatershedResult,
    get_engine,
    load_result,
    run_watershed,
)
from .geometry import (
    GeometryCheck,
    GeometryMeasure,
    measure,
    revise_with_anchors,
    simplify_preserving,
    validate_geometry,
)
from .segment import (
    Anchor,
    BoundarySegment,
    choose_segment_count,
    list_segments,
    segment_boundary,
    segments_to_geojson,
)

#: 复核图相关名字按需惰性导入（避免启动时加载 matplotlib）。
_LAZY_REVIEW_NAMES = {
    "ReviewFigure",
    "render_overview",
    "render_review_pack",
    "render_segment",
    "render_hillshade_contours",
}

__all__ = [
    "Anchor",
    "BOUNDARY_GEOJSON",
    "BoundarySegment",
    "DemInfo",
    "FAILURE_JSON",
    "GeometryCheck",
    "GeometryMeasure",
    "MASK_GEOTIFF",
    "PROVENANCE_JSON",
    "RESULT_JSON",
    "WatershedEngine",
    "WatershedParams",
    "WatershedResult",
    "choose_segment_count",
    "get_engine",
    "inspect_dem",
    "list_segments",
    "load_result",
    "measure",
    "revise_with_anchors",
    "run_watershed",
    "sample_dem_lonlat",
    "segment_boundary",
    "segments_to_geojson",
    "sha256_file",
    "simplify_preserving",
    "validate_geometry",
]


def __getattr__(name: str):  # pragma: no cover - 惰性导入的薄包装
    if name in _LAZY_REVIEW_NAMES:
        from . import review

        return getattr(review, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
