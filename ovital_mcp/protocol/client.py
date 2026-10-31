"""奥维 WebSocket V2.7 客户端（连接、登录、按 cmdid 关联请求/响应、事件队列）。

协议事实（全部有官方出处）
==========================
- 连接：``new WebSocket("ws://host:port", "ws")``（官方样例 omapinterface.js），
  端口由用户在奥维里自定义（手册 2.1.2.1，7699 只是示例）。
- 登录：手册 2.3.1.1 发送 ``{"cmdid":20000,"ret":0,"msg":{"md5psk":"MD5大写前16位"}}``；
  成功时 ``ret`` 是奥维版本号（手册 2.3.1.2.1），失败时 ``ret=0`` 且
  ``msg.errmsg`` 给原因（手册 2.3.1.2.2）。
- **协议没有请求 ID**：响应只能按 ``cmdid`` 关联。因此：
  * 同一 cmdid 的请求必须串行（每 cmdid 一把 ``asyncio.Lock``）；
  * 不同 cmdid 可以并发；
  * 没有被等待的响应（事件 20008/20009/20015 等）进事件队列。
- 断线：正在等待的请求以 ``OvitalConnectionError`` 失败，**不自动重试**；
  ``request()`` 不自动重连；重连后也不得自动重发写请求（由上层用回读确认）。

安全
====
- PSK 与 md5psk 只存在于实例私有字段/登录帧内，**永不**写日志、异常、repr。
- 日志只记 cmdid、耗时、ret 与脱敏后的 msg 摘要。

证据分级：本机无 Windows 奥维客户端，客户端行为属「官方文档级实现 + 模拟
服务器回放验证」，``verified_against_real_device=False``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Mapping

from ..errors import AuthError, OmapError, OvitalConnectionError
from . import constants as C
from .codec import (
    FragmentAccumulator,
    decode_message,
    encode_request,
    md5psk,
    redact_msg,
    redact_text,
    safe_msg_summary,
)
from .errors import (
    ProtocolDecodeError,
    ProtocolStateError,
    OvitalTimeoutError,
    raise_for_ret,
)
from .models import OvitalResponse, OvitalVersion, parse_version

__all__ = ["OvitalClient", "DEFAULT_ENDPOINT_SCHEMES"]

DEFAULT_ENDPOINT_SCHEMES = ("ws://", "wss://")


class OvitalClient:
    """奥维桌面端 WebSocket 客户端。

    参数
    ----
    endpoint
        ``ws://host:port`` 或 ``wss://host:port``。端口必须显式配置——手册 2.1.2.1
        明确端口由用户自定义，7699 只是官方示例，本类不自造默认值。
    psk
        奥维「第三方接口」里设置的预共享密钥。对象只在内存中保存，不落盘。
    connect_timeout_s
        建立连接 + 登录的总超时。
    request_timeout_s
        单次请求默认超时；超时抛 ``OvitalTimeoutError``，且**不自动重试**。
    logger
        传入 ``logging.Logger`` 时用于记录协议日志（默认 ``ovital_mcp.protocol``）。
    max_message_bytes
        单帧上限。20301 返回 base64 图片可能超过 websockets 默认 1 MiB。
    """

    def __init__(
        self,
        endpoint: str,
        psk: str,
        *,
        connect_timeout_s: float = 5.0,
        request_timeout_s: float = 10.0,
        logger: logging.Logger | None = None,
        max_message_bytes: int = C.DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint:
            raise ProtocolStateError("endpoint 不能为空")
        if not endpoint.startswith(DEFAULT_ENDPOINT_SCHEMES):
            raise ProtocolStateError(
                f"endpoint 必须以 ws:// 或 wss:// 开头（收到 {endpoint!r}）"
            )
        if not isinstance(psk, str) or not psk:
            raise ProtocolStateError("psk 不能为空")
        self._endpoint = endpoint
        #: 私有字段：绝不进 repr/日志。
        self._psk = psk
        self._md5psk = md5psk(psk)
        self._connect_timeout_s = float(connect_timeout_s)
        self._request_timeout_s = float(request_timeout_s)
        self._logger = logger or logging.getLogger("ovital_mcp.protocol")
        self._max_message_bytes = int(max_message_bytes)

        self._ws: Any = None
        self._recv_task: asyncio.Task[None] | None = None
        self._connected = False
        self._closing = False
        self._version: OvitalVersion | None = None

        #: 每 cmdid 一把锁：协议无请求 ID，同 cmdid 必须串行。
        self._locks: dict[int, asyncio.Lock] = {}
        #: cmdid -> 正在等待的 future（每个 cmdid 最多一个）。
        self._waiters: dict[int, asyncio.Future[OvitalResponse]] = {}
        #: 分片累加器（仅官方样例里出现的 Fragment* 字段，手册未写，待实测）。
        self._fragment_buffers: dict[int, FragmentAccumulator] = {}
        #: 未被等待的响应/事件队列。
        self._events: list[OvitalResponse] = []
        self._event_condition = asyncio.Condition()
        self._last_error: BaseException | None = None
        self._request_count = 0
        self._event_count = 0

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def version(self) -> OvitalVersion | None:
        return self._version

    @property
    def server_version(self) -> int:
        """握手 ``ret``（奥维版本号）；未连接时为 0。"""

        return self._version.ret if self._version else 0

    @property
    def app_id(self) -> str | None:
        return self._version.app_id if self._version else None

    @property
    def is_simulated(self) -> bool:
        """握手 ``msg.Simulated`` 为真时 True（本项目模拟协议扩展）。

        真实奥维不会返回该字段 → 恒为 False。工具层据此在 provenance 里标
        ``simulated``，**不得**把模拟结果当成真实联机结果。
        """

        return bool(self._version.simulated) if self._version else False

    @property
    def last_error(self) -> BaseException | None:
        return self._last_error

    @property
    def pending_cmdids(self) -> list[int]:
        return sorted(self._waiters)

    @property
    def queued_event_count(self) -> int:
        return len(self._events)

    def __repr__(self) -> str:
        # 注意：不包含 psk / md5psk。
        return (
            f"OvitalClient(endpoint={self._endpoint!r}, connected={self._connected}, "
            f"simulated={self.is_simulated}, version={self.server_version})"
        )

    # ------------------------------------------------------------------
    # 连接 / 关闭
    # ------------------------------------------------------------------

    async def connect(self) -> OvitalVersion:
        """建立 WebSocket 并发送 20000 登录。

        返回 ``OvitalVersion``（``ret`` = 奥维版本号）。失败抛：
        - ``AuthError``：``ret==0`` 且 ``msg.errmsg``（手册 2.3.1.2.2，密钥错误）；
        - ``OvitalConnectionError``：连不上/握手失败/被关闭；
        - ``OvitalRetError``：``ret<0``（手册 2.2.7）。
        """

        if self._connected and self._version is not None:
            return self._version
        if self._ws is not None:
            # 上次连接残留（未正常 close）：先清理，避免两个 recv 循环。
            await self._teardown("重连前清理旧连接")

        from websockets.asyncio.client import connect as ws_connect

        try:
            self._ws = await asyncio.wait_for(
                ws_connect(
                    self._endpoint,
                    subprotocols=[C.SUBPROTOCOL],
                    open_timeout=self._connect_timeout_s,
                    close_timeout=2.0,
                    max_size=self._max_message_bytes,
                    # 显式关闭代理自动探测：本机 127.0.0.1 不应走 HTTP 代理。
                    proxy=None,
                ),
                timeout=self._connect_timeout_s + 1.0,
            )
        except Exception as exc:  # noqa: BLE001 - 连接失败统一映射
            self._ws = None
            self._connected = False
            raise OvitalConnectionError(
                f"无法连接奥维 WebSocket（{self._endpoint}）：{type(exc).__name__}: {exc}。"
                "请确认奥维客户端已运行、第三方接口已启用且端口/预共享密钥配置正确。",
                details={"endpoint": self._endpoint},
            ) from exc

        self._closing = False
        self._recv_task = asyncio.create_task(
            self._recv_loop(), name="ovital-recv"
        )

        try:
            response = await self._send_request(
                C.CMD_LOGIN,
                {C.MD5PSK_FIELD: self._md5psk},
                timeout=self._connect_timeout_s,
                log_payload=False,
            )
        except BaseException:
            await self._teardown("登录失败")
            raise

        if response.ret == 0:
            await self._teardown("登录失败（ret=0）")
            errmsg = response.msg.get(C.LOGIN_ERRMSG_FIELD)
            message = errmsg if isinstance(errmsg, str) and errmsg else "未提供错误信息"
            raise AuthError(
                "奥维登录失败（手册 2.3.1.2.2：ret=0 且 msg.errmsg 表示失败）："
                + redact_text(message, [self._psk, self._md5psk]),
                details={"cmdid": C.CMD_LOGIN, "endpoint": self._endpoint},
            )
        if response.ret < 0:
            await self._teardown("登录返回 ret<0")
            raise_for_ret(C.CMD_LOGIN, response.ret, response.msg)

        version = parse_version(response.msg, response.ret, raw=response.raw)
        self._version = version
        self._connected = True
        self._logger.info(
            "奥维连接成功 endpoint=%s version=%s app_id=%s simulated=%s",
            self._endpoint,
            version.ret,
            version.app_id,
            version.simulated,
        )
        return version

    async def close(self) -> None:
        """关闭连接（幂等）；正在等待的请求以 ``OvitalConnectionError`` 失败。"""

        await self._teardown("客户端主动关闭")

    async def __aenter__(self) -> "OvitalClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _teardown(self, reason: str) -> None:
        self._closing = True
        self._connected = False
        ws, self._ws = self._ws, None
        task, self._recv_task = self._recv_task, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响状态清理
                pass
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._fail_pending(reason)
        self._fragment_buffers.clear()

    # ------------------------------------------------------------------
    # 请求 / 响应
    # ------------------------------------------------------------------

    async def request(
        self,
        cmdid: int,
        msg: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> OvitalResponse:
        """发送请求并等待同 cmdid 的响应。

        - 同一 cmdid 串行、不同 cmdid 并发（协议无请求 ID）。
        - ``ret<0`` 抛 ``raise_for_ret`` 映射后的异常（-5/-7 → 权限不足）。
        - 超时抛 ``OvitalTimeoutError``；**不自动重试、不自动重连**。
        """

        if not self._connected or self._ws is None:
            raise OvitalConnectionError(
                "奥维未连接：请先调用 connect() 完成登录",
                details={"endpoint": self._endpoint},
            )
        return await self._send_request(cmdid, msg, timeout=timeout)

    async def _send_request(
        self,
        cmdid: int,
        msg: Mapping[str, Any] | None,
        *,
        timeout: float | None = None,
        log_payload: bool = True,
    ) -> OvitalResponse:
        """内部发送：不检查 connected（登录时也要用）。"""

        if not isinstance(cmdid, int) or isinstance(cmdid, bool):
            raise ProtocolDecodeError(f"cmdid 必须是整数：{cmdid!r}")
        timeout_value = self._request_timeout_s if timeout is None else float(timeout)
        lock = self._locks.setdefault(cmdid, asyncio.Lock())
        frame = encode_request(cmdid, msg)

        async with lock:
            ws = self._ws
            if ws is None:
                raise OvitalConnectionError("奥维连接已断开", details={"cmdid": cmdid})
            loop = asyncio.get_running_loop()
            future: asyncio.Future[OvitalResponse] = loop.create_future()
            self._waiters[cmdid] = future
            started = time.monotonic()
            self._request_count += 1
            if log_payload:
                self._logger.debug(
                    "send cmdid=%s(%s) msg=%s",
                    cmdid,
                    C.CMD_NAMES.get(cmdid, "?"),
                    safe_msg_summary(msg or {}, secrets=[self._psk, self._md5psk]),
                )
            else:
                self._logger.debug("send cmdid=%s(%s) msg=<redacted>", cmdid, C.CMD_NAMES.get(cmdid, "?"))
            try:
                await ws.send(frame)
                response = await asyncio.wait_for(future, timeout=timeout_value)
            except OvitalConnectionError:
                raise
            except asyncio.TimeoutError as exc:
                elapsed = time.monotonic() - started
                self._logger.warning(
                    "timeout cmdid=%s after %.2fs（不自动重试）", cmdid, elapsed
                )
                raise OvitalTimeoutError(
                    f"等待奥维响应超时（cmdid={cmdid}，{timeout_value:.1f}s）。"
                    "已按协议约定不自动重试：写请求请先回读确认再决定。",
                    details={"cmdid": cmdid, "timeout_s": timeout_value},
                ) from exc
            except Exception as exc:  # noqa: BLE001 - 连接层异常统一为领域错误
                self._connected = False
                raise OvitalConnectionError(
                    f"发送/等待奥维响应时连接异常（cmdid={cmdid}）："
                    f"{type(exc).__name__}: {exc}。不自动重试。",
                    details={"cmdid": cmdid},
                ) from exc
            finally:
                if self._waiters.get(cmdid) is future:
                    self._waiters.pop(cmdid, None)
            elapsed = time.monotonic() - started
            self._logger.debug(
                "recv cmdid=%s ret=%s elapsed=%.3fs fragments=%s",
                cmdid,
                response.ret,
                elapsed,
                response.fragments,
            )
            if response.ret < 0:
                raise_for_ret(cmdid, response.ret, response.msg)
            return response

    async def next_event(
        self,
        cmdid: int | None = None,
        timeout: float | None = None,
    ) -> OvitalResponse:
        """取下一条事件（20008 奥维被关闭 / 20009 保存 / 20015 对象变动 等）。

        事件来自「没有被任何 pending 请求等待的响应」。``cmdid`` 为 None 时返回
        任意事件；否则只返回该 cmdid 的事件（其它事件留在队列里）。
        """

        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            async with self._event_condition:
                for index, event in enumerate(self._events):
                    if cmdid is None or event.cmdid == cmdid:
                        del self._events[index]
                        return event
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                if remaining == 0.0:
                    raise OvitalTimeoutError(
                        f"等待奥维事件超时（cmdid={cmdid}）",
                        details={"cmdid": cmdid},
                    )
                try:
                    await asyncio.wait_for(self._event_condition.wait(), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise OvitalTimeoutError(
                        f"等待奥维事件超时（cmdid={cmdid}）",
                        details={"cmdid": cmdid},
                    ) from exc

    # ------------------------------------------------------------------
    # 接收循环
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        ws = self._ws
        if ws is None:  # pragma: no cover - 防御
            return
        try:
            async for raw in ws:
                try:
                    response = decode_message(raw)
                except ProtocolDecodeError as exc:
                    # 单帧解析失败不应杀死整个连接；记录并继续。
                    self._last_error = exc
                    self._logger.warning("收到无法解析的帧：%s", exc.message)
                    continue
                self._dispatch(response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 连接层异常统一按断线处理
            if not self._closing:
                self._last_error = exc
                self._logger.warning(
                    "奥维连接异常断开：%s: %s", type(exc).__name__, exc
                )
        finally:
            if not self._closing:
                self._connected = False
                self._fail_pending("奥维连接已断开")

    def _dispatch(self, response: OvitalResponse) -> None:
        cmdid = response.cmdid
        future = self._waiters.get(cmdid)
        if future is not None and not future.done():
            if C.FRAGMENT_NUM_FIELD in response.msg:
                assembled = self._accumulate_fragment(cmdid, response)
                if assembled is None:
                    return
                response = assembled
            future.set_result(response)
            return

        # 没有等待者 → 事件/未被消费的响应，进事件队列。
        self._event_count += 1
        if cmdid not in C.EVENT_CMDS:
            self._logger.debug(
                "未等待的响应进入事件队列 cmdid=%s ret=%s", cmdid, response.ret
            )
        self._events.append(response)
        self._notify_event_waiters()

    def _accumulate_fragment(self, cmdid: int, response: OvitalResponse) -> OvitalResponse | None:
        """重组分片响应（官方样例有、手册未写 → 待实测）。"""

        accumulator = self._fragment_buffers.setdefault(cmdid, FragmentAccumulator())
        try:
            text = accumulator.add(response.msg)
        except ProtocolDecodeError as exc:
            self._fragment_buffers.pop(cmdid, None)
            self._last_error = exc
            self._logger.warning("分片响应解析失败：%s", exc.message)
            return response
        if text is None:
            return None
        self._fragment_buffers.pop(cmdid, None)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProtocolDecodeError(
                f"分片重组后的 JSON 非法：{exc}", details={"cmdid": cmdid}
            ) from exc
        if not isinstance(data, dict):
            raise ProtocolDecodeError("分片重组后的顶层不是 JSON 对象")
        msg = data.get("msg", {})
        if not isinstance(msg, dict):
            msg = {"_raw": msg}
        ret_raw = data.get("ret", response.ret)
        try:
            ret = int(ret_raw)
        except (TypeError, ValueError):
            ret = response.ret
        return OvitalResponse(
            cmdid=cmdid,
            ret=ret,
            msg=msg,
            raw=data,
            fragments=response.fragments or 0,
        )

    def _notify_event_waiters(self) -> None:
        async def notify() -> None:
            async with self._event_condition:
                self._event_condition.notify_all()

        task = asyncio.create_task(notify(), name="ovital-event-notify")
        # 任务只做一次 notify，不持有引用；异常交给事件循环默认处理。
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    def _fail_pending(self, reason: str) -> None:
        if not self._waiters:
            return
        error = OvitalConnectionError(
            f"{reason}；正在等待的奥维请求已失败（协议无请求 ID，不能自动重试）",
            details={"pending_cmdids": sorted(self._waiters)},
        )
        for cmdid, future in list(self._waiters.items()):
            if not future.done():
                future.set_exception(error)
        self._waiters.clear()

    # ------------------------------------------------------------------
    # 事件订阅（手册 2.3.49，20014）
    # ------------------------------------------------------------------

    async def monitor_object_changes(self, app_id: int = 0) -> OvitalResponse:
        """发送 20014 注册对象变动监听（0=停止, -1=监听所有, >0=指定 AppID）。

        手册 2.3.49.1。之后对象变动会以 20015 事件推送。
        """

        return await self.request(C.CMD_MONITOR_OBJ_CHANGE, {"AppID": int(app_id)})

    # ------------------------------------------------------------------
    # 调试/证据辅助
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """脱敏统计（写进回放证据，不含 PSK）。"""

        return {
            "endpoint": self._endpoint,
            "connected": self._connected,
            "simulated": self.is_simulated,
            "server_version": self.server_version,
            "app_id": self.app_id,
            "requests": self._request_count,
            "events": self._event_count,
            "queued_events": len(self._events),
            "last_error": (
                f"{type(self._last_error).__name__}: {self._last_error}"
                if self._last_error
                else None
            ),
        }


def redact_login_frame(frame: str, secrets: list[str]) -> str:
    """把登录帧里的 md5psk 替换掉（供日志/证据使用）。"""

    try:
        data = json.loads(frame)
    except json.JSONDecodeError:  # pragma: no cover - 防御
        return redact_text(frame, secrets)
    return json.dumps(redact_msg(data, secrets), ensure_ascii=False)


# 保留 OmapError 导入以便调用方 `from .client import OmapError` 不成立时排查。
_ = OmapError
