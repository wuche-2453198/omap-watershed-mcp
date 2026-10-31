"""协议层错误类型。

本模块只**新增**协议专用错误，不修改公共 ``ovital_mcp/errors.py``（Lead 所有）。
所有新增错误都继承公共基类 ``OmapError``，因此工具层统一 ``except OmapError``
依然有效。

关键约定
========
- 异常消息与 ``details`` 里**不得**出现 PSK / md5psk；本模块所有构造点都遵守。
- ``raise_for_ret()`` 是 ret<0 的唯一映射入口，映射依据是 WebSocket V2.7 手册
  2.2.7「奥维错误ID」以及各命令自己的失败表（见 ``constants.RET_ERRORS``）。
- 本机没有 Windows 奥维客户端，协议实现为「官方文档级 + 模拟服务器回放」，
  任何错误类型都不代表已在真实设备上验证过。
"""

from __future__ import annotations

from typing import Any

from ..errors import (
    OmapError,
    OvitalConnectionError,
    OvitalPrereqError,
    OvitalProtocolError,
)

# 从公共模块再导出，方便 `from ovital_mcp.protocol.errors import AuthError`。
from ..errors import AuthError  # noqa: F401  (re-export)

__all__ = [
    "AuthError",
    "OvitalConnectionError",
    "OvitalPrereqError",
    "OvitalProtocolError",
    "ProtocolDecodeError",
    "ProtocolStateError",
    "OvitalRetError",
    "OvitalTimeoutError",
    "UnsupportedCommandError",
    "raise_for_ret",
    "RET_ERROR_MESSAGES",
]


class ProtocolDecodeError(OvitalProtocolError):
    """帧/字段不符合官方协议（无法解析 JSON、缺少必需字段、类型不对）。

    ``raw`` 保留原始消息，便于日志与回放定位；``raw`` 中不会包含凭据，因为
    登录请求的 msg 只在客户端内部构造、从不进入异常。
    """

    code = "OVITAL_PROTOCOL_DECODE_ERROR"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        raw: Any = None,
    ) -> None:
        merged = dict(details or {})
        if raw is not None:
            merged.setdefault("raw", raw)
        super().__init__(message, details=merged)
        self.raw = raw


class ProtocolStateError(OvitalProtocolError):
    """调用顺序/连接状态不允许该操作（例如未登录就发请求）。"""

    code = "OVITAL_PROTOCOL_STATE_ERROR"


class OvitalRetError(OvitalProtocolError):
    """服务端返回 ``ret<0`` 且没有更具体分类时的错误。

    携带 ``cmdid`` / ``ret``，错误消息含命令号与手册错误含义，但绝不含凭据。
    """

    code = "OVITAL_RET_ERROR"

    def __init__(
        self,
        message: str,
        *,
        cmdid: int,
        ret: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = {"cmdid": cmdid, "ret": ret, **(details or {})}
        super().__init__(message, details=merged)
        self.cmdid = cmdid
        self.ret = ret


class OvitalTimeoutError(OvitalConnectionError):
    """等待响应/事件超时。

    继承 ``OvitalConnectionError``（retryable=True），因为超时通常意味着链路
    异常；但**上层不得据此自动重发写请求**（见 docs/architecture.md §4.2）。
    """

    code = "OVITAL_TIMEOUT"


class UnsupportedCommandError(OvitalProtocolError):
    """手册/官方样例不足以可靠实现的命令。

    这是显式失败：宁可报「不支持/待实测」，也不返回猜测字段。
    """

    code = "OVITAL_UNSUPPORTED_COMMAND"


#: 手册 2.2.7「奥维错误ID」。用于错误消息，不作为枚举校验。
RET_ERROR_MESSAGES: dict[int, str] = {
    -1: "没有找到奥维地图程序窗口",
    -2: "向奥维地图程序注册失败",
    -3: "奥维地图程序版本太低",
    -4: "参数错误",
    -5: "当前奥维地图绑定用户不是VIP用户，不支持此API接口",
    -6: "消息处理失败",
    -7: "当前奥维地图绑定用户不是SVIP，不支持此API接口",
}

#: 个别命令的失败表把 -7 写成「不是VIP5或VIP5以上」/「不是SVIP」（手册 2.3.29、
#: 2.3.31、2.3.30 与 2.2.7 用词不一致）。统一提示两个称呼，避免误导。
RET_HINTS: dict[int, str] = {
    -5: "需要 VIP 及以上会员（手册 2.1.1：其他功能至少需要 VIP）",
    -7: "需要 SVIP / VIP5 及以上会员（手册 2.1.1：地图图片、等高线需 SVIP）",
}


def _message_for(ret: int, cmdid: int) -> str:
    base = RET_ERROR_MESSAGES.get(ret, "未知错误")
    hint = RET_HINTS.get(ret)
    text = f"奥维命令 {cmdid} 返回 ret={ret}：{base}（手册 2.2.7）"
    if hint:
        text += f"；{hint}"
    return text


def raise_for_ret(cmdid: int, ret: int, msg: Any = None) -> None:
    """把 ``ret<0`` 映射为具体异常；``ret>=0`` 直接返回（不抛）。

    依据
    ----
    - 手册 2.2.7 通用错误 ID（-1..-7）。
    - 手册 2.3.1.2.2：登录失败 ``ret=0`` 且带 ``msg.errmsg``，由
      ``client.connect()`` 单独处理，**不走本函数**。
    - 手册 2.3.1.2.1 / 2.3.17.2 / 2.3.18.2 / 2.3.24.2：成功时 ``ret`` 是
      奥维版本号（正整数），因此 ``ret>0`` 是成功，绝不能当失败。

    映射规则
    --------
    - ``-1/-2/-3`` → ``OvitalConnectionError``（奥维没运行/注册失败/版本太低）。
    - ``-5`` / ``-7`` → ``OvitalPrereqError``（会员权限不足）。
    - ``-4`` / ``-6`` / 其他负数 → ``OvitalRetError``（协议级失败，保留 ret）。

    错误 ``details`` 只含 cmdid/ret/msg 摘要；调用方传入的 ``msg`` 只保留
    白名单字段（errmsg/msg），避免把请求内容（可能含 md5psk）带进异常。
    """

    if not isinstance(ret, int):
        raise ProtocolDecodeError(f"响应 ret 必须是整数，收到 {type(ret).__name__}")
    if ret >= 0:
        return

    details: dict[str, Any] = {"ret": ret}
    if isinstance(msg, dict):
        for key in ("errmsg", "msg"):
            value = msg.get(key)
            if isinstance(value, str) and value:
                details[key] = value[:200]

    text = _message_for(ret, cmdid)
    if ret in (-1, -2, -3):
        raise OvitalConnectionError(text, details=details)
    if ret in (-5, -7):
        raise OvitalPrereqError(text, details=details)
    raise OvitalRetError(text, cmdid=cmdid, ret=ret, details=details)


def is_known_ret(ret: int) -> bool:
    """``ret`` 是否在手册 2.2.7 列出的错误 ID 中（供测试/能力探测使用）。"""

    return ret in RET_ERROR_MESSAGES


def omap_error_to_dict(exc: BaseException) -> dict[str, Any]:
    """把领域错误转成可安全对外展示的 dict（不含凭据）。"""

    if isinstance(exc, OmapError):
        return exc.to_dict()
    return {"code": "UNEXPECTED_ERROR", "message": f"{type(exc).__name__}: {exc}"}
