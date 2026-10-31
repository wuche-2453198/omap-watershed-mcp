"""MCP 应用上下文：配置、存储、任务、奥维连接（懒建立）。

启动约束（任务书 B 节）：
- MCP 进程启动时**不得**自动改地图，也不要求奥维一定在线；
- 奥维连接只在真正需要它的工具被调用时建立；
- 工具失败必须显式返回 ``status="error"``，不能伪成功。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .config import AppConfig, RuntimePaths, load_config
from .jobs import JobManager
from .store import TaskStore


@dataclass
class AppContext:
    config: AppConfig
    repo_root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("ovital_mcp"))

    paths: RuntimePaths | None = None
    store: TaskStore | None = None
    jobs: JobManager | None = None
    _ovital: Any = None
    _ovital_lock: asyncio.Lock | None = None

    def __post_init__(self) -> None:
        self.paths = RuntimePaths.build(self.config, base_dir=self.repo_root)
        self.store = TaskStore(self.config, self.paths)
        self.jobs = JobManager(self.config, self.paths, repo_root=self.repo_root)

    # -- 奥维连接 -----------------------------------------------------------

    def _lock(self) -> asyncio.Lock:
        if self._ovital_lock is None:
            self._ovital_lock = asyncio.Lock()
        return self._ovital_lock

    async def ovital(self):
        """返回已连接（懒初始化）的 ``OvitalClient``；凭据缺失/连接失败显式报错。"""

        from .protocol.client import OvitalClient

        async with self._lock():
            if self._ovital is not None and getattr(self._ovital, "connected", False):
                return self._ovital
            if self._ovital is not None:
                await self.close_ovital()
            psk = self.config.ovital.resolve_psk()
            client = OvitalClient(
                self.config.ovital.endpoint,
                psk,
                connect_timeout_s=self.config.ovital.connect_timeout_s,
                request_timeout_s=self.config.ovital.request_timeout_s,
                logger=self.logger,
            )
            try:
                await client.connect()
            except Exception:
                await self.close_ovital()
                raise
            self._ovital = client
            self.logger.info(
                "ovital connected: version=%s simulated=%s",
                getattr(client, "server_version", None),
                getattr(client, "is_simulated", None),
            )
            return client

    async def close_ovital(self) -> None:
        client, self._ovital = self._ovital, None
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不掩盖主错误
                self.logger.debug("关闭奥维连接失败", exc_info=True)

    def is_simulated_connection(self) -> bool | None:
        """当前连接是否模拟服务器；未连接返回 None（不猜测）。"""

        client = self._ovital
        if client is None:
            return None
        return bool(getattr(client, "is_simulated", False))

    # -- 生命周期 -----------------------------------------------------------

    async def aclose(self) -> None:
        await self.close_ovital()

    def server_info(self) -> dict[str, Any]:
        return {
            "name": "ovital-watershed-mcp",
            "version": __version__,
            "python": f"{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}",
            "config_path": self.config.config_path,
            "data_root": str(self.paths.data_root) if self.paths else None,
        }


def build_context(config_path: str | Path | None = None, *, logger: logging.Logger | None = None) -> AppContext:
    config = load_config(config_path)
    ctx = AppContext(config=config, logger=logger or logging.getLogger("ovital_mcp"))
    return ctx
