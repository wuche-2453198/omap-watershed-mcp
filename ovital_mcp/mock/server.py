"""模拟奥维 WebSocket 服务器（离线闭环验证用）。

定位
====
本机（macOS）没有 Windows 奥维客户端，也没有用户奥维账号，真实联机不可能。
本模块提供一个**自标识的模拟服务器**，让协议适配器可以在「登录 → 读取选中 →
写草稿 → 回读 → 量算 → 地图图片 → 删除」全链路上跑通，并把每一步的请求/响应
留作证据。

严格边界
========
- 所有成功响应都带 ``msg.Simulated: true``（本项目模拟协议扩展；真实奥维不会
  返回该字段），客户端据此在 provenance 标 ``simulated=true``；
- 20301 返回的 PNG 带 ``SIMULATED MAP`` 水印与 PNG 元数据，不冒充奥维地图；
- 行为依据手册章节（见各 handler docstring），但**模拟器不是协议权威**：
  模拟通过 ≠ 真实联机通过，``verified_against_real_device`` 恒为 False；
- 手册没写的行为（例如 20101 找不到对象时的响应）在模拟器里显式标注为
  「模拟器选择」，并列入 ``docs/protocol-notes.md`` 待实测。

支持的命令（架构契约 §4.4 要求 + 20107/20014 辅助）：
20000、20006、20100、20101、20103、20104、20107、20110、20112、20802、
20301、20303、20016、20730、20212、20222、20501、20014。
未实现命令返回 ``ret:-4``（手册 2.2.7 参数错误）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from ..protocol import constants as C
from ..protocol.codec import b64encode, md5psk, redact_msg
from .faults import Faults
from .world import MockWorld, MockWorldError

__all__ = ["MockOvitalServer", "main"]

logger = logging.getLogger("ovital_mcp.mock.server")


@dataclass
class _Connection:
    ws: Any
    conn_id: int
    app_id: int
    logged_in: bool = False
    messages: int = 0


class MockOvitalServer:
    """模拟奥维 WebSocket 服务器。

    参数
    ----
    psk
        模拟的预共享密钥（测试值，禁止真实凭据）。
    host / port
        监听地址；``port=0`` 由系统分配，``start()`` 返回实际端口。
    world / faults
        内存对象世界与故障注入配置，测试可直接读写。
    write_response_style
        20112 响应形态：
        - ``"manual_flat"``：手册 2.3.12.2 示例的扁平 ``msg.ObjItems[]``（默认）；
        - ``"sdk_nested"``：官方样例 omapinterface.js 读取的
          ``msg.ObjItems[0].Object.ObjectDetail.ObjChildren`` 嵌套形态。
        两种形态客户端都必须能解析（真实设备是哪种「待实测」）。
    """

    def __init__(
        self,
        psk: str,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        world: MockWorld | None = None,
        faults: Faults | None = None,
        write_response_style: str = "manual_flat",
        logger_: logging.Logger | None = None,
    ) -> None:
        if not isinstance(psk, str) or not psk:
            raise ValueError("模拟服务器 psk 不能为空")
        if write_response_style not in ("manual_flat", "sdk_nested"):
            raise ValueError("write_response_style 只能是 manual_flat / sdk_nested")
        self.psk = psk
        self._md5psk = md5psk(psk)
        self.host = host
        self.port = port
        self.world = world or MockWorld()
        self.faults = faults or Faults()
        self.write_response_style = write_response_style
        self.log = logger_ or logger

        self._server: Any = None
        self._connections: dict[int, _Connection] = {}
        self._next_conn_id = 1
        self._next_app_id = 41  # 手册 2.3.1.2.1 示例 AppId=41
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.connection_count = 0
        self.started = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        if not self.started:
            raise RuntimeError("模拟服务器尚未启动：请先 await start()")
        return f"ws://{self.host}:{self.port}"

    async def start(self) -> int:
        """启动服务并返回实际端口。"""

        from websockets.asyncio.server import serve

        self._server = await serve(
            self._handler,
            self.host,
            self.port,
            subprotocols=[C.SUBPROTOCOL],
            max_size=None,
            ping_interval=None,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        self.started = True
        self.log.info("模拟奥维服务器已启动 %s（simulated=true）", self.url)
        return self.port

    async def stop(self) -> None:
        """停止服务并关闭所有连接。"""

        server, self._server = self._server, None
        self.started = False
        for conn in list(self._connections.values()):
            try:
                await conn.ws.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响停止
                pass
        self._connections.clear()
        if server is not None:
            server.close()
            await server.wait_closed()
        self.log.info("模拟奥维服务器已停止")

    async def __aenter__(self) -> "MockOvitalServer":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # 连接处理
    # ------------------------------------------------------------------

    async def _handler(self, ws: Any) -> None:
        conn_id = self._next_conn_id
        self._next_conn_id += 1
        self._next_app_id += 1
        conn = _Connection(ws=ws, conn_id=conn_id, app_id=self._next_app_id)
        self._connections[conn_id] = conn
        self.connection_count += 1
        self.log.debug("模拟服务器：新连接 #%s subprotocol=%s", conn_id, ws.subprotocol)
        tasks: set[asyncio.Task[None]] = set()
        try:
            async for raw in ws:
                # 每条消息独立 task：模拟真实服务端「不同命令可并发处理」，
                # 这样 faults.delay_on 才能验证客户端的不同 cmdid 并发能力。
                # 同一 cmdid 的串行由客户端保证（协议无请求 ID）。
                task = asyncio.create_task(self._handle_raw(conn, raw))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                conn.messages += 1
                if self.faults.should_close_after(conn.messages):
                    self.log.debug("故障注入：收到 %s 条消息后断开", conn.messages)
                    await ws.close()
                    break
        except Exception as exc:  # noqa: BLE001 - 连接级异常不应终止服务器
            if type(exc).__name__ not in ("ConnectionClosed", "ConnectionClosedOK", "ConnectionClosedError"):
                self.log.warning("模拟服务器连接异常：%s: %s", type(exc).__name__, exc)
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._connections.pop(conn_id, None)
            self.log.debug("模拟服务器：连接 #%s 关闭", conn_id)

    async def _handle_raw(self, conn: _Connection, raw: str | bytes) -> None:
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            await self._send_error(conn.ws, 0, -4)
            return
        if not isinstance(data, dict) or "cmdid" not in data:
            await self._send_error(conn.ws, 0, -4)
            return
        try:
            cmdid = int(data["cmdid"])
        except (TypeError, ValueError):
            await self._send_error(conn.ws, 0, -4)
            return
        msg = data.get("msg") or {}
        if not isinstance(msg, dict):
            msg = {}
        self.requests.append(
            {
                "t": time.monotonic(),
                "conn_id": conn.conn_id,
                "cmdid": cmdid,
                "ret": data.get("ret"),
                "msg": msg,
                "raw": text,
            }
        )
        self.log.debug(
            "模拟服务器 <- cmdid=%s msg=%s",
            cmdid,
            _short(redact_msg(msg, [self.psk, self._md5psk])),
        )

        if self.faults.should_timeout(cmdid):
            self.log.debug("故障注入：cmdid=%s 不返回响应", cmdid)
            return
        if self.faults.should_drop_on_write(cmdid):
            self.log.debug("故障注入：收到写入 cmdid=%s 直接断开", cmdid)
            await conn.ws.close()
            return
        delay = self.faults.delay_for(cmdid)
        if delay > 0:
            await asyncio.sleep(delay)
        forced = self.faults.forced_ret(cmdid)
        if forced is not None:
            await self._send(conn.ws, cmdid, {}, ret=forced)
            return

        handler = self._handlers().get(cmdid)
        if handler is None:
            await self._send_error(conn.ws, cmdid, -4)
            return
        try:
            await handler(conn, msg)
        except MockWorldError as exc:
            await self._send_error(conn.ws, cmdid, exc.ret)
        except Exception as exc:  # noqa: BLE001 - 模拟器内部错误映射为 -6
            self.log.exception("模拟服务器处理 cmdid=%s 失败", cmdid)
            await self._send_error(conn.ws, cmdid, -6, note=f"{type(exc).__name__}: {exc}")

    def _handlers(self) -> dict[int, Callable[[_Connection, Mapping[str, Any]], Awaitable[None]]]:
        return {
            C.CMD_LOGIN: self._cmd_login,
            C.CMD_GET_VERSION: self._cmd_version,
            C.CMD_GET_FAV_LIST: self._cmd_fav_list,
            C.CMD_GET_OBJECT: self._cmd_get_object,
            C.CMD_DELETE_OBJECT: self._cmd_delete,
            C.CMD_GET_SELECTED: self._cmd_selected,
            C.CMD_SEARCH_OBJECT: self._cmd_search,
            C.CMD_GET_SELECTED_TREE: self._cmd_selected_tree,
            C.CMD_WRITE_OBJECT_TREE: self._cmd_write_tree,
            C.CMD_GET_SHAPE_METRICS: self._cmd_shape_metrics,
            C.CMD_GET_MAP_DATA: self._cmd_map_data,
            C.CMD_GET_CONTOUR: self._cmd_contour,
            C.CMD_GET_SCREEN_COORDS: self._cmd_screen_corners,
            C.CMD_GET_OMAP_WND_RECT: self._cmd_window_rect,
            C.CMD_GET_PIXEL_RATIO: self._cmd_pixel_ratio,
            C.CMD_GET_SYS_CRS: self._cmd_system_crs,
            C.CMD_GET_VIP_LEVEL: self._cmd_vip,
            C.CMD_MONITOR_OBJ_CHANGE: self._cmd_monitor,
        }

    # ------------------------------------------------------------------
    # 发送辅助
    # ------------------------------------------------------------------

    async def _send(
        self,
        ws: Any,
        cmdid: int,
        msg: Mapping[str, Any],
        *,
        ret: int | None = None,
        simulated: bool = True,
    ) -> None:
        payload: dict[str, Any] = dict(msg)
        if simulated:
            payload.setdefault(C.SIMULATED_FIELD, True)
        envelope: dict[str, Any] = {"cmdid": cmdid, "msg": payload}
        if ret is not None:
            envelope["ret"] = ret
        text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        await ws.send(text)
        self.responses.append({"t": time.monotonic(), "cmdid": cmdid, "ret": ret, "msg": payload})

    async def _send_error(self, ws: Any, cmdid: int, ret: int, *, note: str | None = None) -> None:
        """错误响应：手册 2.2.7 的 ret<0，msg 为空对象（模拟器不编造错误字段）。"""

        msg: dict[str, Any] = {}
        if note:
            msg["MockNote"] = note
        await self._send(ws, cmdid, msg, ret=ret)

    async def _send_fragmented(
        self, ws: Any, cmdid: int, msg: Mapping[str, Any], *, chunk_chars: int = 120
    ) -> None:
        """把一条响应拆成分片帧发送（官方样例 omapinterface.js 的 Fragment* 字段）。

        手册 2.3.12/2.3.18 未记载该机制 → 「official_sample + 待实测」；这里只用于
        验证客户端重组能力。``FragmentData`` 是 base64 编码的 JSON 文本片段。
        """

        payload = dict(msg)
        payload.setdefault(C.SIMULATED_FIELD, True)
        text = json.dumps(
            {"cmdid": cmdid, "msg": payload}, ensure_ascii=False, separators=(",", ":")
        )
        parts = [text[i : i + chunk_chars] for i in range(0, len(text), chunk_chars)] or [""]
        for index, part in enumerate(parts):
            await self._send(
                ws,
                cmdid,
                {
                    C.FRAGMENT_NUM_FIELD: len(parts),
                    C.FRAGMENT_INDEX_FIELD: index,
                    C.FRAGMENT_DATA_FIELD: b64encode(part.encode("utf-8")),
                },
                simulated=False,
            )

    async def broadcast_event(self, cmdid: int, msg: Mapping[str, Any]) -> int:
        """向所有连接推送事件（例如 20008/20009/20015），返回发送数量。"""

        payload = dict(msg)
        payload.setdefault(C.SIMULATED_FIELD, True)
        text = json.dumps(
            {"cmdid": cmdid, "msg": payload}, ensure_ascii=False, separators=(",", ":")
        )
        sent = 0
        for conn in list(self._connections.values()):
            try:
                await conn.ws.send(text)
                sent += 1
                self.responses.append({"t": time.monotonic(), "cmdid": cmdid, "ret": None, "msg": payload})
            except Exception:  # noqa: BLE001 - 单个连接失败不影响其它
                continue
        return sent

    async def _emit_change_event(self, ids: list[int], *, flag: int) -> None:
        if not self.faults.emit_change_events or not ids:
            return
        # 手册 2.3.49.2：20015 msg = {Version, Flag(0 添加/修改,1 删除), ListID}
        await self.broadcast_event(
            C.CMD_OBJ_CHANGED, {"Version": 0, "Flag": int(flag), "ListID": ids}
        )

    # ------------------------------------------------------------------
    # 各命令实现（docstring 标注手册章节）
    # ------------------------------------------------------------------

    async def _cmd_login(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20000 登录（手册 2.3.1）。成功 ret=版本号；失败 ret=0 + msg.errmsg。"""

        provided = msg.get(C.MD5PSK_FIELD)
        if not isinstance(provided, str) or provided.upper() != self._md5psk:
            # 手册 2.3.1.2.2：ret=0 且 msg.errmsg；错误信息不泄露密钥。
            await self._send(
                conn.ws,
                C.CMD_LOGIN,
                {"errmsg": "Pre-shared key error"},
                ret=0,
            )
            return
        conn.logged_in = True
        await self._send(
            conn.ws,
            C.CMD_LOGIN,
            {"AppId": conn.app_id},
            ret=self.world.version,
        )

    async def _cmd_version(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20006 获取版本（手册 2.3.24）：成功 ret=版本号。"""

        await self._send(conn.ws, C.CMD_GET_VERSION, {}, ret=self.world.version)

    async def _cmd_fav_list(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20100 收藏夹列表（手册 2.3.10）。"""

        items = self.world.list_root()
        obj_list = [
            {"ObjID": int(item["ObjID"]), "Type": item["Type"], "Name": item["Object"]["Name"]}
            for item in items
        ]
        await self._send(conn.ws, C.CMD_GET_FAV_LIST, {"Count": len(obj_list), "ObjList": obj_list})

    async def _cmd_get_object(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20101 获取收藏夹对象（手册 2.3.11）。

        找不到对象时模拟器返回空 ``ObjItems``（手册未规定，标「模拟器选择」）。
        """

        obj_id = _require_int(msg, "ObjID")
        latlng_type = int(msg.get("LatlngType", 1))
        if latlng_type not in (0, 1, 2):
            raise MockWorldError(
                f"LatlngType={latlng_type} 模拟器未实现（手册 2.2.2 允许 0-4，"
                "但模拟器只支持 0/1/2）",
                ret=-4,
            )
        node = self.world.read_obj(obj_id, include_children=True, latlng_type=latlng_type)
        if node is None:
            await self._send(conn.ws, C.CMD_GET_OBJECT, {"ObjItems": []})
            return
        await self._send(conn.ws, C.CMD_GET_OBJECT, {"ObjItems": [node]})

    async def _cmd_selected(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20104 获取选中对象（手册 2.3.17）。成功 ret=版本号（手册 2.3.17.2）。"""

        flag = int(msg.get("Flag", C.SEL_FLAG_GROUP_JOIN))
        if flag not in C.SEL_FLAG_NAMES:
            raise MockWorldError(f"Flag={flag} 不在手册 2.2.3 的 1/2/3 内", ret=-4)
        obj_list = self.world.selected_objects()
        await self._send(
            conn.ws,
            C.CMD_GET_SELECTED,
            {"Count": len(obj_list), "ObjList": obj_list},
            ret=self.world.version,
        )

    async def _cmd_selected_tree(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20110 获取选中对象树（手册 2.3.18）。"""

        latlng_type = int(msg.get("LatlngType", 1))
        if latlng_type not in (0, 1, 2):
            raise MockWorldError(f"LatlngType={latlng_type} 模拟器未实现", ret=-4)
        payload = self.world.selected_tree(latlng_type=latlng_type)
        if self.faults.should_fragment(C.CMD_GET_SELECTED_TREE):
            await self._send_fragmented(conn.ws, C.CMD_GET_SELECTED_TREE, payload)
            return
        await self._send(conn.ws, C.CMD_GET_SELECTED_TREE, payload)

    async def _cmd_search(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20107 查找对象（手册 2.3.22）：响应只含 ObjID/ObjType。"""

        name = msg.get("SrhTxt")
        if not isinstance(name, str):
            raise MockWorldError("SrhTxt 必须是字符串", ret=-4)
        srh_opt = int(msg.get("SrhOpt", 0))
        obj_type = int(msg.get("ObjType", 0))
        parent_id = int(msg.get("ParentID", msg.get("idParent", 0)) or 0)
        no_child = int(msg.get("NoChild", 0))
        results = self.world.search(
            name=name,
            srh_opt=srh_opt,
            obj_type=obj_type,
            parent_id=parent_id,
            no_child=no_child,
        )
        await self._send(
            conn.ws,
            C.CMD_SEARCH_OBJECT,
            {
                "ReqVer": 0,
                "SrhOpt": srh_opt,
                "ObjType": obj_type,
                "ParentID": parent_id,
                "ShapeSelect": int(msg.get("ShapeSelect", msg.get("idShapeSelect", 0)) or 0),
                "NoChild": no_child,
                "MaxCnt": int(msg.get("MaxCnt", 0)),
                "SrhTxt": name,
                "SrhValue": int(msg.get("SrhValue", 0)),
                "NoCase": int(msg.get("NoCase", 0)),
                "ListID": results,
            },
        )

    async def _cmd_write_tree(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20112 写入对象树（手册 2.3.12）。

        成功响应有两种官方写法，由 ``write_response_style`` 控制（真实设备待实测）。
        写入后按手册 2.3.49 推送 20015 对象变动事件。
        """

        tree = msg.get("ObjTree")
        if not isinstance(tree, dict):
            raise MockWorldError("缺少 ObjTree 对象（手册 2.3.12.1）", ret=-4)
        created = self.world.create_tree(tree)
        if self.write_response_style == "manual_flat":
            await self._send(conn.ws, C.CMD_WRITE_OBJECT_TREE, {"ObjItems": created})
        else:
            folder = created[0]
            folder_detail = folder["Object"]["ObjectDetail"]
            folder_detail["ObjChildren"] = created[1:]
            await self._send(conn.ws, C.CMD_WRITE_OBJECT_TREE, {"ObjItems": [folder]})
        if self.faults.should_duplicate(C.CMD_WRITE_OBJECT_TREE):
            self.log.debug("故障注入：20112 响应重复发送一次")
            if self.write_response_style == "manual_flat":
                await self._send(conn.ws, C.CMD_WRITE_OBJECT_TREE, {"ObjItems": created})
            else:
                await self._send(conn.ws, C.CMD_WRITE_OBJECT_TREE, {"ObjItems": [folder]})
        ids = [int(item["ObjID"]) for item in created]
        await self._emit_change_event(ids, flag=0)

    async def _cmd_delete(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20103 删除对象（手册 2.3.13）。成功响应形态手册未给 → 模拟器返回空 msg。"""

        list_id = msg.get("ListID")
        if not isinstance(list_id, list) or not list_id:
            raise MockWorldError("ListID 必须是非空数组（手册 2.3.13.1）", ret=-4)
        obj_ids = [_as_int(item, "ListID[]") for item in list_id]
        no_group = int(msg.get("NoGroup", msg.get("NoTroup", 1)) or 0)
        result = self.world.delete(obj_ids, no_group=no_group)
        await self._send(conn.ws, C.CMD_DELETE_OBJECT, {})
        await self._emit_change_event(result["deleted"], flag=1)

    async def _cmd_shape_metrics(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20802 图形量算（手册 2.3.48）。"""

        obj_id = _require_int(msg, "ObjID")
        metrics = self.world.shape_metrics(obj_id)
        await self._send(conn.ws, C.CMD_GET_SHAPE_METRICS, metrics)

    async def _cmd_map_data(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20301 地图图片数据（手册 2.3.29）。

        请求字段：手册表写 ``MaxLevel``，官方样例发 ``MapLevel`` → 两者都接受。
        模拟器只支持 CoordType 0/1（经纬度）；平面坐标需要奥维系统坐标系参数，
        模拟器不实现（返回 -4，手册 2.2.7 参数错误）。
        """

        req = _first_request_item(msg, "ReqMapDatas")
        bbox = _bbox_from_item(req)
        coord_type = int(req.get("CoordType", 0))
        if coord_type not in (0, 1):
            raise MockWorldError(
                f"模拟器只支持 CoordType=0/1（经纬度）；收到 {coord_type}。"
                "平面/系统坐标系需要奥维 20222 参数，模拟器未实现（待实测）",
                ret=-4,
            )
        max_level = int(req.get("MaxLevel", req.get("MapLevel", 12)) or 12)
        max_pixel = int(req.get("MaxPixel", 1_000_000) or 1_000_000)
        png = self.world.render_map_png(
            bbox=bbox, coord_type=coord_type, max_level=max_level, max_pixel=max_pixel
        )
        await self._send(
            conn.ws,
            C.CMD_GET_MAP_DATA,
            {
                "x_lng1": bbox[0],
                "y_lat1": bbox[1],
                "x_lng2": bbox[2],
                "y_lat2": bbox[3],
                "CoordType": coord_type,
                "MaxLevel": max_level,
                "MapLevel": max_level,
                "MaxPixel": max_pixel,
                "Data": b64encode(png),
                "DataFormat": "PNG",
                "MockNote": "SIMULATED MAP：非奥维真实影像，见 PNG 水印与元数据",
            },
        )

    async def _cmd_contour(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20303 等高线（手册 2.3.31）。

        手册只给出元数据字段，没有给出等高线数据格式，因此模拟器只回元数据
        （``Ret=0``、``EvevValue``、``NeedVip`` 等），**不编造几何**。
        """

        req = _first_request_item(msg, "ReqContours", fallback="ReqMapDatas")
        bbox = _bbox_from_item(req)
        self.world.last_contour_request = {"bbox": bbox, "raw": dict(req)}
        await self._send(
            conn.ws,
            C.CMD_GET_CONTOUR,
            {
                "Ret": 0,
                "MapID": int(req.get("MapID", 1)),
                "Value": int(req.get("Value", 1)),
                "x_lng1": bbox[0],
                "y_lat1": bbox[1],
                "x_lng2": bbox[2],
                "y_lat2": bbox[3],
                "CoordType": int(req.get("CoordType", 0)),
                "MapLevel": int(req.get("MapLevel", 12)),
                "EvevValue": int(req.get("ElevValue", 30)),
                "CheckFlag": int(req.get("CheckFlag", 0)),
                "CheckResult": 3,
                "NeedVip": 9,
                "Part": 0,
                "MockNote": "手册 2.3.31 未定义等高线数据数组格式；模拟器只回元数据（待实测）",
            },
        )

    async def _cmd_screen_corners(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20016 屏幕角点（手册 2.3.58）。"""

        req_coord_type = int(msg.get("ReqCoordType", 1))
        if req_coord_type not in C.SCREEN_COORD_REQ_TYPE:
            raise MockWorldError(
                f"ReqCoordType={req_coord_type} 不在手册 2.3.58 的 0/1/2 内", ret=-4
            )
        await self._send(
            conn.ws, C.CMD_GET_SCREEN_COORDS, self.world.screen_corners(req_coord_type)
        )

    async def _cmd_window_rect(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20730 窗体可显示区域（手册 2.3.52）。"""

        await self._send(conn.ws, C.CMD_GET_OMAP_WND_RECT, self.world.window_rect)

    async def _cmd_pixel_ratio(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20212 像素比例（手册 2.3.28）。"""

        x = int(msg.get("x", 0) or 0)
        y = int(msg.get("y", 0) or 0)
        await self._send(conn.ws, C.CMD_GET_PIXEL_RATIO, self.world.pixel_ratio(x, y))

    async def _cmd_system_crs(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20222 系统坐标系（手册 2.3.51）。"""

        await self._send(conn.ws, C.CMD_GET_SYS_CRS, self.world.system_crs)

    async def _cmd_vip(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20501 VIP 等级（手册 2.3.19）。"""

        await self._send(
            conn.ws,
            C.CMD_GET_VIP_LEVEL,
            {"OmapLevel": self.world.vip_level, "LoadOK": self.world.load_ok},
        )

    async def _cmd_monitor(self, conn: _Connection, msg: Mapping[str, Any]) -> None:
        """20014 注册对象变动监听（手册 2.3.49）。模拟器回空对象。"""

        await self._send(conn.ws, C.CMD_MONITOR_OBJ_CHANGE, {})


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _require_int(msg: Mapping[str, Any], field_name: str) -> int:
    if field_name not in msg:
        raise MockWorldError(f"缺少必需字段 {field_name}", ret=-4)
    return _as_int(msg[field_name], field_name)


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise MockWorldError(f"{field_name} 不能是 bool", ret=-4)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise MockWorldError(f"{field_name} 需要整数，收到 {value!r}", ret=-4)


def _first_request_item(
    msg: Mapping[str, Any], *keys: str, fallback: str | None = None
) -> Mapping[str, Any]:
    candidates = list(keys) + ([fallback] if fallback else [])
    for key in candidates:
        value = msg.get(key)
        if isinstance(value, list) and value and isinstance(value[0], Mapping):
            return value[0]
        if isinstance(value, Mapping):
            return value
    raise MockWorldError(
        f"请求缺少数组字段 {'/'.join(candidates)}（手册 2.3.29/2.3.31）", ret=-4
    )


def _bbox_from_item(item: Mapping[str, Any]) -> tuple[float, float, float, float]:
    names = ("x_lng1", "y_lat1", "x_lng2", "y_lat2")
    try:
        values = [float(item[name]) for name in names]
    except KeyError as exc:
        raise MockWorldError(f"请求缺少字段 {exc.args[0]}", ret=-4) from exc
    except (TypeError, ValueError) as exc:
        raise MockWorldError(f"bbox 字段不是数字：{exc}", ret=-4) from exc
    min_lon, min_lat = min(values[0], values[2]), min(values[1], values[3])
    max_lon, max_lat = max(values[0], values[2]), max(values[1], values[3])
    if not (min_lon < max_lon and min_lat < max_lat):
        raise MockWorldError("bbox 退化（宽或高为 0）", ret=-4)
    return (min_lon, min_lat, max_lon, max_lat)


def _short(value: Mapping[str, Any], limit: int = 160) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# 独立运行入口：python -m ovital_mcp.mock.server --port 7699 --psk test-psk
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="启动模拟奥维 WebSocket 服务器（仅用于离线测试，非真实奥维）"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port", type=int, default=0, help="监听端口；0=自动分配（默认）"
    )
    parser.add_argument("--psk", default="test-psk-not-secret", help="模拟预共享密钥")
    parser.add_argument(
        "--psk-file", default=None, help="从文件读取模拟 PSK（首行）"
    )
    parser.add_argument(
        "--write-style",
        choices=("manual_flat", "sdk_nested"),
        default="manual_flat",
        help="20112 响应形态（真实设备待实测）",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


async def _serve_forever(args: argparse.Namespace) -> None:
    psk = args.psk
    if args.psk_file:
        with open(args.psk_file, encoding="utf-8") as handle:
            psk = handle.read().strip().splitlines()[0].strip()
    server = MockOvitalServer(
        psk, args.host, args.port, write_response_style=args.write_style
    )
    port = await server.start()
    print(
        f"模拟奥维服务器已启动 ws://{args.host}:{port} "
        f"(simulated=true, write_style={args.write_style})",
        file=sys.stderr,
    )
    try:
        await asyncio.Future()  # 永远等待，直到 Ctrl+C
    finally:
        await server.stop()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_serve_forever(args))
    except KeyboardInterrupt:  # pragma: no cover - 交互式退出
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
