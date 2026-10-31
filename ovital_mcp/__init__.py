"""OMAP Watershed MCP.

本地 MCP 服务：把奥维互动地图（Windows 桌面端 WebSocket 第三方接口）与一套
确定性的 GIS 集水区计算链连接起来，供 DeepSeek Harness 中的模型调用。

设计文档见仓库根目录：
- ``OMAP_MCP_可行性与实施方案.md``
- ``DSH_实施任务书.md``
- ``docs/architecture.md``（实施接口契约）
"""

from __future__ import annotations

__version__ = "0.1.0"

#: DSH 中注册后的工具名前缀（serverName 为 ``ovital``）。
TOOL_NAME_PREFIX = "mcp__ovital__"

#: 内部规范坐标（CGCS2000 地理坐标，经度在前）。见 docs/architecture.md §3。
CANONICAL_CRS = "EPSG:4490"
