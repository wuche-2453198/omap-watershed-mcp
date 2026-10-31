"""OvitalClient 测试：登录、ret 语义、同 cmdid 串行、事件队列、断线/超时、PSK 不泄露。

全部基于本机模拟服务器（``ovital_mcp.mock``），**不是真实联机**；
``verified_against_real_device=False``。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import pytest

from ovital_mcp.errors import AuthError, OvitalConnectionError
from ovital_mcp.mock import Faults, MockOvitalServer
from ovital_mcp.protocol import OvitalClient, constants as C
from ovital_mcp.protocol.errors import (
    OvitalRetError,
    OvitalTimeoutError,
    ProtocolStateError,
)

PSK = "test-psk-not-secret"


def run(coro):
    return asyncio.run(coro)


@contextlib.asynccontextmanager
async def mock_server(**kwargs):
    server = MockOvitalServer(PSK, port=0, **kwargs)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def connected_client(server: MockOvitalServer, **kwargs) -> OvitalClient:
    client = OvitalClient(server.url, PSK, request_timeout_s=2.0, **kwargs)
    await client.connect()
    return client


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------


def test_login_success_returns_version_and_simulated_flag():
    async def scenario():
        async with mock_server() as server:
            client = OvitalClient(server.url, PSK)
            version = await client.connect()
            try:
                # 手册 2.3.1.2.1：成功 ret=版本号（817），不是 0
                assert version.ret == 817
                assert version.app_id is not None
                assert client.connected is True
                assert client.server_version == 817
                assert client.is_simulated is True
                assert client.version is version
            finally:
                await client.close()

    run(scenario())


def test_login_failure_raises_auth_error_without_leaking_secret():
    async def scenario():
        async with mock_server() as server:
            client = OvitalClient(server.url, "wrong-psk")
            with pytest.raises(AuthError) as info:
                await client.connect()
            message = str(info.value)
            assert "Pre-shared key error" in message
            assert "wrong-psk" not in message
            from ovital_mcp.protocol.codec import md5psk

            assert md5psk("wrong-psk") not in message
            assert client.connected is False
            assert client.pending_cmdids == []

    run(scenario())


def test_login_timeout_maps_to_timeout_error():
    async def scenario():
        faults = Faults()
        faults.timeout_on.add(C.CMD_LOGIN)
        async with mock_server(faults=faults) as server:
            client = OvitalClient(server.url, PSK, connect_timeout_s=0.4)
            with pytest.raises(OvitalTimeoutError):
                await client.connect()
            assert client.connected is False

    run(scenario())


def test_client_requires_ws_endpoint_and_psk():
    with pytest.raises(ProtocolStateError):
        OvitalClient("http://127.0.0.1:7699", PSK)
    with pytest.raises(ProtocolStateError):
        OvitalClient("ws://127.0.0.1:7699", "")
    client = OvitalClient("ws://127.0.0.1:7699", PSK)
    assert PSK not in repr(client)


def test_connect_failure_to_closed_port_raises_connection_error():
    async def scenario():
        client = OvitalClient("ws://127.0.0.1:9", PSK, connect_timeout_s=0.5)
        with pytest.raises(OvitalConnectionError):
            await client.connect()
        assert client.connected is False

    run(scenario())


# ---------------------------------------------------------------------------
# 请求/响应语义
# ---------------------------------------------------------------------------


def test_ret_version_number_is_success_not_error():
    async def scenario():
        async with mock_server() as server:
            client = await connected_client(server)
            try:
                response = await client.request(C.CMD_GET_VERSION, {})
                assert response.ret == 817
                # 20104 手册 2.3.17.2 也说成功 ret=版本号
                response = await client.request(
                    C.CMD_GET_SELECTED, {"Flag": C.SEL_FLAG_GROUP_JOIN}
                )
                assert response.ret == 817
            finally:
                await client.close()

    run(scenario())


def test_unknown_cmdid_maps_to_protocol_error_with_ret():
    async def scenario():
        async with mock_server() as server:
            client = await connected_client(server)
            try:
                with pytest.raises(OvitalRetError) as info:
                    await client.request(99999, {})
                assert info.value.ret == -4
                assert info.value.cmdid == 99999
                assert "99999" in str(info.value)
            finally:
                await client.close()

    run(scenario())


def test_request_before_connect_raises_connection_error():
    async def scenario():
        client = OvitalClient("ws://127.0.0.1:9", PSK)
        with pytest.raises(OvitalConnectionError):
            await client.request(C.CMD_GET_VERSION, {})

    run(scenario())


def test_request_timeout_does_not_retry_and_releases_lock():
    async def scenario():
        faults = Faults()
        faults.timeout_on.add(C.CMD_GET_VERSION)
        async with mock_server(faults=faults) as server:
            client = await connected_client(server)
            try:
                with pytest.raises(OvitalTimeoutError):
                    await client.request(C.CMD_GET_VERSION, {}, timeout=0.3)
                assert client.pending_cmdids == []
                faults.timeout_on.clear()
                # 超时后连接仍可用；但绝不自动重试（server 只收到 1 次）
                version = await client.request(C.CMD_GET_VERSION, {})
                assert version.ret == 817
                received = [r for r in server.requests if r["cmdid"] == C.CMD_GET_VERSION]
                assert len(received) == 2  # 1 次超时请求 + 1 次显式请求
            finally:
                await client.close()

    run(scenario())


# ---------------------------------------------------------------------------
# 同 cmdid 串行 / 不同 cmdid 并发
# ---------------------------------------------------------------------------


def test_same_cmdid_requests_are_serialized():
    async def scenario():
        faults = Faults()
        faults.delay_on[C.CMD_GET_VERSION] = 0.15
        async with mock_server(faults=faults) as server:
            client = await connected_client(server)
            try:
                started = time.monotonic()
                first, second = await asyncio.gather(
                    client.request(C.CMD_GET_VERSION, {}),
                    client.request(C.CMD_GET_VERSION, {}),
                )
                elapsed = time.monotonic() - started
                assert first.cmdid == second.cmdid == C.CMD_GET_VERSION
                assert elapsed >= 0.28  # 两次串行，各 0.15s
                received = [r["t"] for r in server.requests if r["cmdid"] == C.CMD_GET_VERSION]
                sent = [r["t"] for r in server.responses if r["cmdid"] == C.CMD_GET_VERSION]
                assert len(received) == 2 and len(sent) == 2
                # 第二个请求必须在第一个响应之后才发出（协议无请求 ID，不能串包）
                assert received[1] >= sent[0]
            finally:
                await client.close()

    run(scenario())


def test_different_cmdids_run_concurrently():
    async def scenario():
        faults = Faults()
        faults.delay_on[C.CMD_GET_VERSION] = 0.15
        faults.delay_on[C.CMD_GET_VIP_LEVEL] = 0.15
        async with mock_server(faults=faults) as server:
            client = await connected_client(server)
            try:
                started = time.monotonic()
                version, vip = await asyncio.gather(
                    client.request(C.CMD_GET_VERSION, {}),
                    client.request(C.CMD_GET_VIP_LEVEL, {}),
                )
                elapsed = time.monotonic() - started
                assert version.cmdid == C.CMD_GET_VERSION
                assert vip.cmdid == C.CMD_GET_VIP_LEVEL
                assert vip.msg["OmapLevel"] == 9
                assert elapsed < 0.28  # 两个不同 cmdid 并行
                received = [
                    r for r in server.requests if r["cmdid"] in (C.CMD_GET_VERSION, C.CMD_GET_VIP_LEVEL)
                ]
                sent = [r["t"] for r in server.responses if r["cmdid"] in (C.CMD_GET_VERSION, C.CMD_GET_VIP_LEVEL)]
                assert received[0]["t"] < sent[0] and received[1]["t"] < sent[0]
            finally:
                await client.close()

    run(scenario())


# ---------------------------------------------------------------------------
# 事件队列
# ---------------------------------------------------------------------------


def test_unsolicited_event_goes_to_queue():
    async def scenario():
        async with mock_server() as server:
            client = await connected_client(server)
            try:
                # 手册 2.3.2：20008 奥维被关闭事件
                await server.broadcast_event(
                    C.CMD_OMAP_CLOSED, {"msg": "Omap is Closed! "}
                )
                event = await client.next_event(C.CMD_OMAP_CLOSED, timeout=1.0)
                assert event.cmdid == C.CMD_OMAP_CLOSED
                assert event.msg["msg"] == "Omap is Closed! "
                assert client.queued_event_count == 0
            finally:
                await client.close()

    run(scenario())


def test_next_event_filters_by_cmdid_and_times_out():
    async def scenario():
        async with mock_server() as server:
            client = await connected_client(server)
            try:
                await server.broadcast_event(C.CMD_SAVE_CLICKED, {"msg": "The save button was pressed! "})
                await server.broadcast_event(C.CMD_OMAP_CLOSED, {"msg": "Omap is Closed! "})
                closed = await client.next_event(C.CMD_OMAP_CLOSED, timeout=1.0)
                assert closed.cmdid == C.CMD_OMAP_CLOSED
                # 20009 仍在队列里
                save = await client.next_event(C.CMD_SAVE_CLICKED, timeout=1.0)
                assert save.cmdid == C.CMD_SAVE_CLICKED
                with pytest.raises(OvitalTimeoutError):
                    await client.next_event(C.CMD_OBJ_CHANGED, timeout=0.2)
            finally:
                await client.close()

    run(scenario())


def test_write_emits_20015_change_event():
    async def scenario():
        from ovital_mcp.protocol import objects

        async with mock_server() as server:
            client = await connected_client(server)
            try:
                tree = objects.build_write_tree(
                    folder_id=0,
                    folder_name="事件测试",
                    children=[
                        objects.build_sign_object(name="出口", comment="", lon=106.57, lat=39.01)
                    ],
                )
                await client.request(C.CMD_WRITE_OBJECT_TREE, {"ObjTree": tree})
                event = await client.next_event(C.CMD_OBJ_CHANGED, timeout=1.0)
                assert event.msg["Flag"] == 0  # 手册 2.3.49.2：0=添加或修改
                assert len(event.msg["ListID"]) == 2
            finally:
                await client.close()

    run(scenario())


# ---------------------------------------------------------------------------
# 断线 / 重复响应 / 分片
# ---------------------------------------------------------------------------


def test_disconnect_fails_pending_and_never_auto_retries_write():
    async def scenario():
        from ovital_mcp.protocol import objects

        faults = Faults()
        faults.drop_on_write = True
        async with mock_server(faults=faults) as server:
            client = await connected_client(server)
            tree = objects.build_write_tree(folder_id=0, folder_name="断线测试", children=[])
            with pytest.raises(OvitalConnectionError):
                await client.request(C.CMD_WRITE_OBJECT_TREE, {"ObjTree": tree})
            assert client.connected is False
            writes = [r for r in server.requests if r["cmdid"] == C.CMD_WRITE_OBJECT_TREE]
            assert len(writes) == 1  # 绝不自动重发写请求
            # 断线后请求显式失败，不静默重连
            with pytest.raises(OvitalConnectionError):
                await client.request(C.CMD_GET_VERSION, {})
            await client.close()

    run(scenario())


def test_duplicate_20112_response_does_not_duplicate_write_result():
    async def scenario():
        from ovital_mcp.protocol import objects

        faults = Faults()
        faults.duplicate_20112 = True
        async with mock_server(faults=faults) as server:
            client = await connected_client(server)
            try:
                tree = objects.build_write_tree(
                    folder_id=0,
                    folder_name="重复响应",
                    children=[
                        objects.build_shape_object(
                            name="图形",
                            comment="",
                            vertices_lonlat=[
                                (106.57, 39.01),
                                (106.58, 39.01),
                                (106.58, 39.02),
                            ],
                        )
                    ],
                )
                response = await client.request(C.CMD_WRITE_OBJECT_TREE, {"ObjTree": tree})
                assert response.cmdid == C.CMD_WRITE_OBJECT_TREE
                # 重复响应不能被当成第二次写入结果：它落在事件队列，可观测但未被消费为响应
                duplicate = await client.next_event(C.CMD_WRITE_OBJECT_TREE, timeout=1.0)
                assert duplicate.cmdid == C.CMD_WRITE_OBJECT_TREE
                assert len(server.world.object_ids()) == 2  # 文件夹 + 图形，只写一次
            finally:
                await client.close()

    run(scenario())


def test_fragmented_20110_response_is_reassembled():
    async def scenario():
        from ovital_mcp.protocol import objects

        faults = Faults()
        faults.fragment_on.add(C.CMD_GET_SELECTED_TREE)
        async with mock_server(faults=faults) as server:
            client = await connected_client(server)
            try:
                tree = objects.build_write_tree(
                    folder_id=0,
                    folder_name="分片树",
                    children=[
                        objects.build_sign_object(name="出口", comment="", lon=106.57, lat=39.01)
                    ],
                )
                results = await client.request(C.CMD_WRITE_OBJECT_TREE, {"ObjTree": tree})
                assert results.cmdid == C.CMD_WRITE_OBJECT_TREE
                folder_id = int(results.msg["ObjItems"][0]["ObjID"])
                server.world.selected_ids = [folder_id]
                response = await client.request(
                    C.CMD_GET_SELECTED_TREE, {"LatlngType": 1}
                )
                assert response.fragments > 1
                nodes = response.msg["ObjItems"]
                assert nodes and nodes[0]["Object"]["ObjectDetail"]["ObjChildren"]
            finally:
                await client.close()

    run(scenario())


# ---------------------------------------------------------------------------
# 凭据不泄露
# ---------------------------------------------------------------------------


def test_psk_never_appears_in_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="ovital_mcp")

    async def scenario():
        async with mock_server() as server:
            client = await connected_client(server)
            try:
                await client.request(C.CMD_GET_VERSION, {})
                await client.request(
                    C.CMD_GET_SELECTED, {"Flag": C.SEL_FLAG_GROUP_JOIN}
                )
            finally:
                await client.close()
            # 失败登录也不能泄露
            bad = OvitalClient(server.url, "another-secret-psk")
            with pytest.raises(AuthError):
                await bad.connect()

    run(scenario())
    from ovital_mcp.protocol.codec import md5psk

    assert PSK not in caplog.text
    assert md5psk(PSK) not in caplog.text
    assert "another-secret-psk" not in caplog.text
    assert md5psk("another-secret-psk") not in caplog.text


def test_client_stats_and_repr_have_no_secret():
    async def scenario():
        async with mock_server() as server:
            client = await connected_client(server)
            try:
                stats = client.stats()
                assert stats["simulated"] is True
                assert stats["server_version"] == 817
                assert PSK not in str(stats)
                assert PSK not in repr(client)
            finally:
                await client.close()

    run(scenario())


# ---------------------------------------------------------------------------
# 上下文管理器 / 事件订阅
# ---------------------------------------------------------------------------


def test_async_context_manager_and_monitor():
    async def scenario():
        async with mock_server() as server:
            async with OvitalClient(server.url, PSK) as client:
                assert client.connected is True
                response = await client.monitor_object_changes(0)
                assert response.cmdid == C.CMD_MONITOR_OBJ_CHANGE
            assert client.connected is False

    run(scenario())
