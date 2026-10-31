"""模拟服务器的故障注入（用于测试幂等/断线/超时防护）。

任务书要求至少支持：
- ``drop_on_write``：收到 20112 写入时直接断开连接（模拟断线，验证「不盲目重发创建」）；
- ``timeout_on(cmdid)``：收到指定命令不回应（验证客户端超时与「超时后先回读」）；
- ``duplicate_20112``：20112 响应发两次（验证客户端不会把重复响应当第二次写入）。

额外提供：
- ``delay_on``：给指定 cmdid 的响应加延迟（验证同 cmdid 串行、不同 cmdid 并发）；
- ``fail_on``：直接返回指定 ret（验证 -5/-7 权限错误映射）。

这些是**测试夹具行为**，不是官方协议行为；真实奥维的行为见
``docs/protocol-notes.md`` 的「待实测」。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..protocol import constants as C

__all__ = ["Faults"]


@dataclass
class Faults:
    """可变的故障注入配置（测试中可随时修改并 ``reset()``）。"""

    #: 收到 20112 时立刻断开连接（不返回任何响应）。
    drop_on_write: bool = False
    #: 这些 cmdid 收到后不返回响应（客户端会超时）。
    timeout_on: set[int] = field(default_factory=set)
    #: 20112 成功响应发送两次（模拟服务端重复推送）。
    duplicate_20112: bool = False
    #: cmdid -> 响应延迟秒数（用于并发/串行测试）。
    delay_on: dict[int, float] = field(default_factory=dict)
    #: cmdid -> 强制返回的 ret（例如 -5 需VIP、-7 需SVIP、-4 参数错误）。
    fail_on: dict[int, int] = field(default_factory=dict)
    #: 这些 cmdid 的响应改为分片发送（验证客户端 Fragment* 重组；官方样例有、手册未写）。
    fragment_on: set[int] = field(default_factory=set)
    #: 收到第 N 条消息后断开连接（None 表示不断开）。
    close_after_messages: int | None = None
    #: 是否在写入/删除成功后推送 20015 对象变动事件（默认 True，符合手册 2.3.49）。
    emit_change_events: bool = True

    def reset(self) -> None:
        """清空所有注入（保留默认值）。"""

        self.drop_on_write = False
        self.timeout_on.clear()
        self.duplicate_20112 = False
        self.delay_on.clear()
        self.fail_on.clear()
        self.fragment_on.clear()
        self.close_after_messages = None
        self.emit_change_events = True

    # ------------------------------------------------------------------
    # 查询辅助（server.py 使用）
    # ------------------------------------------------------------------

    def should_drop_on_write(self, cmdid: int) -> bool:
        return self.drop_on_write and cmdid == C.CMD_WRITE_OBJECT_TREE

    def should_timeout(self, cmdid: int) -> bool:
        return cmdid in self.timeout_on

    def delay_for(self, cmdid: int) -> float:
        return float(self.delay_on.get(cmdid, 0.0))

    def forced_ret(self, cmdid: int) -> int | None:
        return self.fail_on.get(cmdid)

    def should_fragment(self, cmdid: int) -> bool:
        return cmdid in self.fragment_on

    def should_duplicate(self, cmdid: int) -> bool:
        return self.duplicate_20112 and cmdid == C.CMD_WRITE_OBJECT_TREE

    def should_close_after(self, message_count: int) -> bool:
        return (
            self.close_after_messages is not None
            and message_count >= self.close_after_messages
        )
