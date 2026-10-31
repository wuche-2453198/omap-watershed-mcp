"""奥维 WebSocket V2.7 协议适配器（Teammate A）。

对外只暴露稳定入口：``OvitalClient``、``commands``、模型、构造器与错误类型。
内部实现依据：``.research/ovital-scratch/manual-32.md``（官方 V2.7 手册）与官方
样例 SDK ``omapInterfaceWeb_latest/js/{omapinterface,omapobj}.js``；逐条出处见
``docs/protocol-notes.md``。

证据声明
========
本机是 macOS、没有 Windows 奥维客户端，因此本包是「官方文档级实现 + 官方样例/
夹具回放 + 模拟服务器闭环」，**不是**真实联机验证：
``verified_against_real_device = False``。真实设备待核验项见
``constants.UNVERIFIED_ITEMS`` 与 ``docs/protocol-notes.md``。
"""

from __future__ import annotations

from .client import OvitalClient
from .errors import (
    AuthError,
    OvitalConnectionError,
    OvitalPrereqError,
    OvitalProtocolError,
    OvitalRetError,
    OvitalTimeoutError,
    ProtocolDecodeError,
    ProtocolStateError,
    UnsupportedCommandError,
    raise_for_ret,
)
from .models import (
    ObjNode,
    ObjectWriteResult,
    OvitalResponse,
    OvitalVersion,
    SelectedObject,
    ShapeMetrics,
    ShapeObject,
)
from . import commands, constants, codec, models, objects

__all__ = [
    "AuthError",
    "ObjNode",
    "ObjectWriteResult",
    "OvitalClient",
    "OvitalConnectionError",
    "OvitalPrereqError",
    "OvitalProtocolError",
    "OvitalResponse",
    "OvitalRetError",
    "OvitalTimeoutError",
    "OvitalVersion",
    "ProtocolDecodeError",
    "ProtocolStateError",
    "SelectedObject",
    "ShapeMetrics",
    "ShapeObject",
    "UnsupportedCommandError",
    "codec",
    "commands",
    "constants",
    "models",
    "objects",
    "raise_for_ret",
]
