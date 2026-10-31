"""工具注册入口。

顺序无关；每个模块负责自己的工具名与描述（与 docs/architecture.md §6 对齐）。
总共 18 个工具（§6 的 17 个 + list_jobs 辅助工具）。
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..app import AppContext
from . import capabilities, dem, draft, export, review, watershed

MODULES = (capabilities, dem, watershed, review, draft, export)


def register_all(mcp: MCPServer, ctx: AppContext) -> list[str]:
    for module in MODULES:
        module.register(mcp, ctx)
    return [tool.name for tool in mcp._tool_manager.list_tools()]  # noqa: SLF001 - SDK 无公开列举 API
