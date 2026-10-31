"""模拟奥维服务器（离线闭环验证）。

用法::

    import asyncio
    from ovital_mcp.mock import MockOvitalServer, MockWorld, Faults

    async def main():
        server = MockOvitalServer("test-psk", port=0)
        port = await server.start()
        ...  # 用 ovital_mcp.protocol.OvitalClient 连接 server.url
        await server.stop()

证据声明：模拟通过 ≠ 真实联机通过；``verified_against_real_device`` 恒为 False。
"""

from __future__ import annotations

from .faults import Faults
from .server import MockOvitalServer, main
from .world import MockWorld, MockWorldError, render_simulated_map_png

__all__ = [
    "Faults",
    "MockOvitalServer",
    "MockWorld",
    "MockWorldError",
    "main",
    "render_simulated_map_png",
]
