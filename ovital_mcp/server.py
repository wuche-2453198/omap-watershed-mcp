"""MCP 服务定义（stdio）。

启动方式：
    python -m ovital_mcp
    ovital-mcp

约束：
- stdout 只输出 MCP 协议帧；日志全部走 stderr（及配置的日志文件）。
- 启动时不连接奥维、不写地图、不创建任务；只在工具被调用时懒初始化。
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from collections.abc import AsyncIterator
from mcp.server.mcpserver import MCPServer

from . import __version__
from .app import AppContext
from .config import configure_logging, load_config
from .tools import register_all

logger = logging.getLogger("ovital_mcp.server")

INSTRUCTIONS = """\
本服务用于在奥维互动地图（Windows 桌面端）中，根据用户点选的坝址/取水口生成上游集水区（汇水区）草稿。

硬性规则（必须遵守）：
1. 集水区是分水岭闭合的地表汇水范围，不是可见水面；不要用"面积接近"替代边界正确性。
2. 不得为了凑目标面积而修改边界；不得伪造坐标或声称数据不支持的精度。
3. 先调用 get_capabilities 了解连接/权限/已验证范围；凡是 simulated=true 或
   verified_against_real_device=false 的结果，都不能作为真实工程验收。
4. 计算流程：get_capabilities → (get_selection/read_object 确定出口) → inspect_dem →
   start_watershed → 轮询 get_job → review_segment/review_segment 图像逐段复核 →
   有疑点用 revise_candidate → write_draft → read_back → 汇报 → finalize_draft/rollback_change。
5. 长任务用 job_id 轮询，不要用一次长同步调用；取消用 cancel_job。
6. review_segment/get_job 返回的图片必须实际查看后再判断边界；只看坐标或面积不算复核。
7. 写奥维草稿前必须确认写入的是独立草稿文件夹；写后必须回读坐标与量算结果。
8. 发现岩溶、涵洞、人工引排水、平缓分水岭等特殊情形时，记录工程条件与不确定段，
   不当成自动可判定的情形。
9. 结果必须说明数据来源、坐标系统、面积口径、边界疑点与精度适用范围。
"""


def create_server(ctx: AppContext) -> MCPServer:
    """构造 MCPServer（不启动传输）。测试可直接调用。"""

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await ctx.aclose()

    mcp = MCPServer(
        name="ovital-watershed",
        title="Ovital Watershed MCP",
        description=(
            "奥维集水区绘图：本地 DEM 确定性集水区计算 + 复核图 + 奥维草稿落图/回读/回滚。"
        ),
        instructions=INSTRUCTIONS,
        version=__version__,
        log_level="WARNING",
        lifespan=lifespan,
    )
    names = register_all(mcp, ctx)
    logger.info("registered tools: %s", ", ".join(names))
    return mcp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ovital-mcp",
        description="Ovital watershed MCP server (stdio). 日志走 stderr。",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（默认读取 OVITAL_MCP_CONFIG 或 config/ovital.yaml）",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--dump-tools",
        action="store_true",
        help="仅打印已注册工具名（JSON）后退出，用于诊断；不会连接奥维。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001 - 启动期错误只写 stderr，不污染 stdout
        print(f"[ovital-mcp] 配置加载失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    logger_ = configure_logging(
        level=config.logging.level,
        log_file=config.logging.file,
        secrets=[config.ovital.psk or ""],
    )
    try:
        ctx = AppContext(config=config, logger=logger_)
    except Exception as exc:  # noqa: BLE001 - 启动失败必须写 stderr 而非 stdout
        print(f"[ovital-mcp] 初始化失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.dump_tools:
        import json

        mcp = create_server(ctx)
        names = sorted(tool.name for tool in mcp._tool_manager.list_tools())  # noqa: SLF001
        print(json.dumps({"tools": names, "server": ctx.server_info()}, ensure_ascii=False))
        return 0
    logger_.info(
        "starting ovital-mcp %s (python %s); config=%s; data_root=%s",
        __version__,
        sys.version.split()[0],
        config.config_path or "<default>",
        ctx.paths.data_root,
    )
    mcp = create_server(ctx)
    try:
        mcp.run("stdio")
    finally:
        logger_.info("ovital-mcp stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
