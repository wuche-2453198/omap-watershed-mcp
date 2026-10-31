"""模拟奥维服务器测试（线上帧级行为、故障注入、自标识、独立进程启动）。

模拟服务器只是**离线夹具**：模拟通过 ≠ 真实联机通过。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest

from ovital_mcp.mock import Faults, MockOvitalServer, MockWorld
from ovital_mcp.protocol import OvitalClient, commands, constants as C, objects
from ovital_mcp.protocol.codec import encode_request, md5psk

PSK = "test-psk-not-secret"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


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


@contextlib.asynccontextmanager
async def raw_connection(server: MockOvitalServer):
    from websockets.asyncio.client import connect

    async with connect(server.url, subprotocols=[C.SUBPROTOCOL], proxy=None) as ws:
        yield ws


async def raw_request(ws, cmdid: int, msg: dict | None = None) -> dict:
    await ws.send(encode_request(cmdid, msg or {}))
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))


async def raw_login(ws, psk: str = PSK) -> dict:
    return await raw_request(ws, C.CMD_LOGIN, {"md5psk": md5psk(psk)})


# ---------------------------------------------------------------------------
# 生命周期 / 自标识
# ---------------------------------------------------------------------------


def test_url_requires_start():
    server = MockOvitalServer(PSK)
    with pytest.raises(RuntimeError):
        _ = server.url


def test_start_returns_actual_port_and_stop_closes():
    async def scenario():
        server = MockOvitalServer(PSK, port=0)
        port = await server.start()
        try:
            assert port > 0
            assert server.url == f"ws://127.0.0.1:{port}"
            assert server.started is True
        finally:
            await server.stop()
        assert server.started is False

    run(scenario())


def test_handshake_self_identifies_as_simulated():
    async def scenario():
        async with mock_server() as server, raw_connection(server) as ws:
            response = await raw_login(ws)
            # 本项目模拟协议扩展：真实奥维不会返回 Simulated 字段
            assert response["ret"] == 817
            assert response["msg"]["Simulated"] is True
            assert response["msg"]["AppId"] == 42
            # 官方样例要求的子协议
            assert ws.subprotocol == C.SUBPROTOCOL

    run(scenario())


def test_login_failure_ret_zero_with_errmsg():
    async def scenario():
        async with mock_server() as server, raw_connection(server) as ws:
            response = await raw_login(ws, "wrong-psk")
            assert response["ret"] == 0
            assert response["msg"]["errmsg"]
            assert md5psk("wrong-psk") not in json.dumps(response)

    run(scenario())


def test_unknown_cmdid_returns_minus_4():
    async def scenario():
        async with mock_server() as server, raw_connection(server) as ws:
            await raw_login(ws)
            response = await raw_request(ws, 99999)
            assert response["ret"] == -4  # 手册 2.2.7：参数错误
            assert response["msg"].get("Simulated") is True

    run(scenario())


def test_all_responses_carry_simulated_flag():
    async def scenario():
        async with mock_server() as server, raw_connection(server) as ws:
            await raw_login(ws)
            for cmdid, msg in (
                (C.CMD_GET_VERSION, {}),
                (C.CMD_GET_VIP_LEVEL, {}),
                (C.CMD_GET_SYS_CRS, {}),
                (C.CMD_GET_SCREEN_COORDS, {"ReqCoordType": 1}),
                (C.CMD_GET_OMAP_WND_RECT, {}),
                (C.CMD_GET_PIXEL_RATIO, {"x": 1, "y": 1}),
            ):
                response = await raw_request(ws, cmdid, msg)
                assert response["msg"]["Simulated"] is True, cmdid

    run(scenario())


# ---------------------------------------------------------------------------
# 对象读写 / 量算（帧级）
# ---------------------------------------------------------------------------


def test_write_then_read_object_wire_level():
    async def scenario():
        faults = Faults(emit_change_events=False)
        async with mock_server(faults=faults) as server, raw_connection(server) as ws:
            await raw_login(ws)
            shape = objects.build_shape_object(
                name="图形",
                comment="备注",
                vertices_lonlat=[(106.57, 39.01), (106.58, 39.01), (106.58, 39.02)],
            )
            sign = objects.build_sign_object(name="出口", comment="", lon=106.57, lat=39.01)
            tree = objects.build_write_tree(folder_id=0, folder_name="草稿", children=[shape, sign])
            response = await raw_request(ws, C.CMD_WRITE_OBJECT_TREE, {"ObjTree": tree})
            items = response["msg"]["ObjItems"]
            assert [item["Type"] for item in items] == [30, 13, 7]
            folder_id = int(items[0]["ObjID"])
            shape_id = int(items[1]["ObjID"])
            assert int(items[1]["ParentID"]) == folder_id

            response = await raw_request(
                ws, C.CMD_GET_OBJECT, {"ObjID": shape_id, "LatlngType": 1}
            )
            node = response["msg"]["ObjItems"][0]
            assert node["Type"] == 13
            assert node["Object"]["Name"] == "图形"
            detail = node["Object"]["ObjectDetail"]
            # Shape.Latlng 是扁平 lat,lng（手册 2.4.8 示例）
            assert detail["Latlng"][0] == pytest.approx(39.01)
            assert detail["Latlng"][1] == pytest.approx(106.57)
            assert detail["Mtp"] == 3
            assert detail["Gcj02"] == 0

    run(scenario())


def test_shape_metrics_wire_level_matches_independent_computation():
    async def scenario():
        from pyproj import Transformer
        from shapely.geometry import Polygon

        from ovital_mcp.coordinates import metric_crs_for

        faults = Faults(emit_change_events=False)
        async with mock_server(faults=faults) as server, raw_connection(server) as ws:
            await raw_login(ws)
            vertices = [(106.57, 39.01), (106.58, 39.01), (106.58, 39.02), (106.57, 39.02)]
            shape = objects.build_shape_object(name="图形", comment="", vertices_lonlat=vertices)
            tree = objects.build_write_tree(folder_id=0, folder_name="量算", children=[shape])
            created = await raw_request(ws, C.CMD_WRITE_OBJECT_TREE, {"ObjTree": tree})
            shape_id = int(created["msg"]["ObjItems"][1]["ObjID"])

            response = await raw_request(ws, C.CMD_GET_SHAPE_METRICS, {"ObjID": shape_id})
            metrics = response["msg"]
            # 手册 2.3.48 示例字段名（官方样例也用这套无前缀名）
            assert set(metrics) >= {"ObjID", "PointCnt", "AreaCnt", "Perimeter", "AreaSize"}

            transformer = Transformer.from_crs(
                "EPSG:4490", metric_crs_for(106.575, 39.015), always_xy=True
            )
            polygon = Polygon([transformer.transform(lon, lat) for lon, lat in vertices])
            assert metrics["PointCnt"] == 4
            assert metrics["AreaCnt"] == 1
            assert metrics["AreaSize"] == pytest.approx(polygon.area, abs=1e-6)
            assert metrics["Perimeter"] == pytest.approx(polygon.length / 1000.0, abs=1e-6)

    run(scenario())


def test_shape_metrics_not_found_returns_zero_id():
    async def scenario():
        async with mock_server() as server, raw_connection(server) as ws:
            await raw_login(ws)
            response = await raw_request(ws, C.CMD_GET_SHAPE_METRICS, {"ObjID": 123})
            assert response["msg"]["ObjID"] == 0

    run(scenario())


def test_write_response_style_variants():
    async def scenario():
        for style in ("manual_flat", "sdk_nested"):
            async with mock_server(
                write_response_style=style, faults=Faults(emit_change_events=False)
            ) as server:
                async with raw_connection(server) as ws:
                    await raw_login(ws)
                    shape = objects.build_shape_object(
                        name="图形", comment="", vertices_lonlat=[(106.57, 39.01), (106.58, 39.02)]
                    )
                    tree = objects.build_write_tree(folder_id=0, folder_name="样式", children=[shape])
                    response = await raw_request(ws, C.CMD_WRITE_OBJECT_TREE, {"ObjTree": tree})
                    items = response["msg"]["ObjItems"]
                    if style == "manual_flat":
                        assert [item["Type"] for item in items] == [30, 13]
                    else:
                        assert len(items) == 1 and items[0]["Type"] == 30
                        assert items[0]["Object"]["ObjectDetail"]["ObjChildren"]

    run(scenario())


def test_deterministic_object_ids():
    async def scenario():
        async with mock_server() as server1:
            async with mock_server() as server2:
                async def create(server):
                    async with raw_connection(server) as ws:
                        await raw_login(ws)
                        tree = objects.build_write_tree(
                            folder_id=0, folder_name="确定性", children=[]
                        )
                        response = await raw_request(
                            ws, C.CMD_WRITE_OBJECT_TREE, {"ObjTree": tree}
                        )
                        return int(response["msg"]["ObjItems"][0]["ObjID"])

                assert await create(server1) == await create(server2)

    run(scenario())


# ---------------------------------------------------------------------------
# 20301 / 20303
# ---------------------------------------------------------------------------


def test_map_image_wire_level_has_watermark_and_metadata():
    async def scenario():
        from PIL import Image

        async with mock_server() as server, raw_connection(server) as ws:
            await raw_login(ws)
            response = await raw_request(
                ws,
                C.CMD_GET_MAP_DATA,
                {
                    "ReqMapDatas": [
                        {
                            "x_lng1": 106.55,
                            "y_lat1": 38.99,
                            "x_lng2": 106.60,
                            "y_lat2": 39.03,
                            "CoordType": 0,
                            "MaxLevel": 12,
                            "MaxPixel": 250000,
                        }
                    ]
                },
            )
            msg = response["msg"]
            assert msg["CoordType"] == 0
            assert msg["MaxLevel"] == 12
            image = Image.open(io.BytesIO(base64.b64decode(msg["Data"])))
            assert "SIMULATED MAP" in image.info.get("Description", "")
            assert image.info.get("Simulated") == "True"
            assert image.size[0] * image.size[1] <= 250_000
            assert server.world.last_map_request["bbox"] == (106.55, 38.99, 106.60, 39.03)

    run(scenario())


def test_map_image_rejects_unsupported_coord_type():
    async def scenario():
        async with mock_server() as server, raw_connection(server) as ws:
            await raw_login(ws)
            response = await raw_request(
                ws,
                C.CMD_GET_MAP_DATA,
                {
                    "ReqMapDatas": [
                        {
                            "x_lng1": 697827.09,
                            "y_lat1": 2873083.29,
                            "x_lng2": 712790.02,
                            "y_lat2": 2884313.99,
                            "CoordType": 2,
                            "MaxLevel": 12,
                            "MaxPixel": 1000,
                        }
                    ]
                },
            )
            assert response["ret"] == -4  # 模拟器不支持平面坐标（手册 2.3.29 允许但模拟器未实现）

    run(scenario())


def test_contour_returns_metadata_only():
    async def scenario():
        async with mock_server() as server, raw_connection(server) as ws:
            await raw_login(ws)
            response = await raw_request(
                ws,
                C.CMD_GET_CONTOUR,
                {
                    "ReqContours": [
                        {
                            "MapID": 1,
                            "Value": 1,
                            "x_lng1": 106.55,
                            "y_lat1": 38.99,
                            "x_lng2": 106.60,
                            "y_lat2": 39.03,
                            "CoordType": 0,
                            "MapLevel": 12,
                            "ElevValue": 40,
                            "CheckFlag": 0,
                        }
                    ]
                },
            )
            msg = response["msg"]
            assert msg["Ret"] == 0
            assert msg["NeedVip"] == 9
            assert msg["EvevValue"] == 40
            assert "MockNote" in msg  # 明确标注数据格式未定义

    run(scenario())


# ---------------------------------------------------------------------------
# 故障注入
# ---------------------------------------------------------------------------


def test_fault_drop_on_write_closes_connection():
    async def scenario():
        faults = Faults()
        faults.drop_on_write = True
        async with mock_server(faults=faults) as server, raw_connection(server) as ws:
            await raw_login(ws)
            tree = objects.build_write_tree(folder_id=0, folder_name="断线", children=[])
            await ws.send(encode_request(C.CMD_WRITE_OBJECT_TREE, {"ObjTree": tree}))
            with pytest.raises(Exception):
                await asyncio.wait_for(ws.recv(), timeout=2.0)
            assert server.world.object_count() == 0

    run(scenario())


def test_fault_timeout_on_returns_nothing():
    async def scenario():
        faults = Faults()
        faults.timeout_on.add(C.CMD_GET_VERSION)
        async with mock_server(faults=faults) as server, raw_connection(server) as ws:
            await raw_login(ws)
            await ws.send(encode_request(C.CMD_GET_VERSION, {}))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.3)

    run(scenario())


def test_fault_duplicate_20112_sends_two_frames():
    async def scenario():
        faults = Faults()
        faults.duplicate_20112 = True
        async with mock_server(faults=faults) as server, raw_connection(server) as ws:
            await raw_login(ws)
            tree = objects.build_write_tree(folder_id=0, folder_name="重复", children=[])
            await ws.send(encode_request(C.CMD_WRITE_OBJECT_TREE, {"ObjTree": tree}))
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            second = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert first["cmdid"] == second["cmdid"] == C.CMD_WRITE_OBJECT_TREE
            assert first["msg"]["ObjItems"][0]["ObjID"] == second["msg"]["ObjItems"][0]["ObjID"]

    run(scenario())


def test_fault_forced_ret_maps_permission_error():
    async def scenario():
        faults = Faults()
        faults.fail_on[C.CMD_GET_VIP_LEVEL] = -5
        async with mock_server(faults=faults) as server, raw_connection(server) as ws:
            await raw_login(ws)
            response = await raw_request(ws, C.CMD_GET_VIP_LEVEL, {})
            assert response["ret"] == -5

    run(scenario())


def test_fault_delay_on_supports_concurrency():
    async def scenario():
        faults = Faults()
        faults.delay_on[C.CMD_GET_VERSION] = 0.1
        faults.delay_on[C.CMD_GET_VIP_LEVEL] = 0.1
        async with mock_server(faults=faults) as server, raw_connection(server) as ws:
            await raw_login(ws)
            started = time.monotonic()
            await asyncio.gather(
                ws.send(encode_request(C.CMD_GET_VERSION, {})),
                ws.send(encode_request(C.CMD_GET_VIP_LEVEL, {})),
            )
            await asyncio.wait_for(ws.recv(), timeout=2.0)
            await asyncio.wait_for(ws.recv(), timeout=2.0)
            elapsed = time.monotonic() - started
            assert elapsed < 0.19  # 两个延迟并行

    run(scenario())


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------


def test_events_after_write_and_delete():
    async def scenario():
        async with mock_server() as server, raw_connection(server) as ws:
            await raw_login(ws)
            tree = objects.build_write_tree(folder_id=0, folder_name="事件", children=[])
            await ws.send(encode_request(C.CMD_WRITE_OBJECT_TREE, {"ObjTree": tree}))
            write_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert event["cmdid"] == C.CMD_OBJ_CHANGED
            assert event["msg"]["Flag"] == 0
            folder_id = int(write_response["msg"]["ObjItems"][0]["ObjID"])
            await ws.send(
                encode_request(
                    C.CMD_DELETE_OBJECT, {"NoGroup": 0, "ListID": [folder_id]}
                )
            )
            delete_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert delete_response["cmdid"] == C.CMD_DELETE_OBJECT
            delete_event = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert delete_event["msg"]["Flag"] == 1
            assert delete_event["msg"]["ListID"] == [folder_id]

    run(scenario())


def test_broadcast_event_returns_connection_count():
    async def scenario():
        async with mock_server() as server, raw_connection(server) as ws:
            await raw_login(ws)
            sent = await server.broadcast_event(C.CMD_OMAP_CLOSED, {"msg": "Omap is Closed! "})
            assert sent == 1
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert event["cmdid"] == C.CMD_OMAP_CLOSED

    run(scenario())


# ---------------------------------------------------------------------------
# 世界模型：搜索 / 选中 / 删除语义
# ---------------------------------------------------------------------------


def test_world_search_and_selection():
    world = MockWorld()
    folder = {
        "Type": 30,
        "ObjID": 0,
        "ParentID": 0,
        "Object": {"Name": "草稿文件夹", "Type": 30, "ObjectDetail": {}},
    }
    sign = objects.build_sign_object(name="出口点", comment="坝址", lon=106.57, lat=39.01)
    folder["Object"]["ObjectDetail"]["ObjChildren"] = [sign]
    created = world.create_tree(folder)
    folder_id = int(created[0]["ObjID"])
    assert [item["ObjID"] for item in world.search(name="草稿文件夹", srh_opt=5)] == [folder_id]
    assert world.search(name="不存在", srh_opt=5) == []
    world.selected_ids = [folder_id]
    assert world.selected_objects()[0]["Name"] == "草稿文件夹"
    tree = world.selected_tree()
    assert tree["ObjItems"][0]["Object"]["ObjectDetail"]["ObjChildren"]
    # NoGroup=1 不删除文件夹
    result = world.delete([folder_id], no_group=1)
    assert result["skipped"] == [folder_id]
    result = world.delete([folder_id], no_group=0)
    assert result["deleted"] == [folder_id]
    assert world.object_count() == 0


def test_mock_fixtures_match_mock_field_names():
    """模拟服务器返回的字段名必须与官方夹具（手册/官方样例）一致。"""

    async def scenario():
        fixtures = REPO_ROOT / "ovital_mcp" / "mock" / "fixtures"

        async with mock_server() as server:
            async with OvitalClient(server.url, PSK) as client:
                shape = objects.build_shape_object(
                    name="图形",
                    comment="",
                    vertices_lonlat=[(106.57, 39.01), (106.58, 39.01), (106.58, 39.02)],
                )
                tree = objects.build_write_tree(folder_id=0, folder_name="字段", children=[shape])
                results = await commands.write_tree(client, tree)
                shape_id = next(item.obj_id for item in results if item.type_id == 13)
                metrics = await commands.get_shape_metrics(client, shape_id)
                fixture = json.loads(
                    (fixtures / "20802-shape-metrics-response.json").read_text(encoding="utf-8")
                )
                assert set(fixture["msg"]) <= set(metrics.raw) | {"Simulated"}

                version_fixture = json.loads(
                    (fixtures / "20006-version-response.json").read_text(encoding="utf-8")
                )
                assert version_fixture["ret"] == 817 == client.server_version

    run(scenario())


# ---------------------------------------------------------------------------
# 独立进程启动（scripts/run_mock_ovital.py）
# ---------------------------------------------------------------------------


def test_standalone_script_starts_and_stops(tmp_path):
    ready_file = tmp_path / "ready.json"
    stop_file = tmp_path / "stop.flag"
    env = dict(os.environ)
    env.pop("OVITAL_MCP_MOCK_PSK", None)
    process = subprocess.Popen(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "run_mock_ovital.py"),
            "--port",
            "0",
            "--psk",
            PSK,
            "--fault",
            "duplicate-20112",
            "--ready-file",
            str(ready_file),
            "--stop-file",
            str(stop_file),
        ],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + 15
        ready: dict | None = None
        while time.monotonic() < deadline:
            if ready_file.exists():
                ready = json.loads(ready_file.read_text(encoding="utf-8"))
                break
            if process.poll() is not None:
                break
            time.sleep(0.1)
        assert ready is not None, f"模拟服务器未就绪：stderr={process.stderr.read() if process.poll() is not None else ''}"
        assert ready["simulated"] is True
        assert ready["verified_against_real_device"] is False
        assert ready["url"].startswith("ws://127.0.0.1:")
        assert ready["faults"] == ["duplicate_20112"]
        assert ready["psk_source"].startswith("--psk")

        async def connect_once():
            async with OvitalClient(ready["url"], PSK) as client:
                assert client.is_simulated is True
                assert await commands.get_version(client) == 817

        run(connect_once())
    finally:
        if os.name == "nt":
            # Windows 上 Popen.send_signal(SIGTERM) 会走 TerminateProcess（强杀、无优雅退出），
            # 因此用 --stop-file 触发与信号相同的 _request_stop 路径。
            stop_file.write_text("stop", encoding="utf-8")
        else:
            process.send_signal(signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
            stdout, stderr = process.communicate()
        assert process.returncode == 0, f"stderr={stderr}"
        # stdout 第一行必须是可解析 JSON（便于集成测试读取）
        first_line = stdout.strip().splitlines()[0]
        assert json.loads(first_line)["simulated"] is True
        assert "收到停止信号" in stderr or "已停止" in stderr


# ---------------------------------------------------------------------------
# 20112 替换语义（finalize_draft 依赖：ObjID 非 0 且已存在 = 替换，不是新增）
# ---------------------------------------------------------------------------


def test_20112_replace_semantics_for_existing_obj_id():
    """同一 mock 世界内两次写请求：第二次带已有 ObjID → 替换，不新增对象。"""

    async def scenario():
        async with mock_server() as server:
            async with OvitalClient(server.url, PSK) as client:
                # 第一次写：新建文件夹 + 图形（ObjID 全为 0）
                shape = objects.build_shape_object(
                    name="集水区草稿",
                    comment="第一版",
                    vertices_lonlat=[(106.57, 39.01), (106.58, 39.01), (106.58, 39.02)],
                )
                tree = objects.build_write_tree(folder_id=0, folder_name="草稿文件夹", children=[shape])
                first = await commands.write_tree(client, tree)
                folder_id = next(item.obj_id for item in first if item.type_id == 30)
                shape_id = next(item.obj_id for item in first if item.type_id == 13)
                assert server.world.object_count() == 2

                # 读取 raw（模拟 finalize_draft 的写前快照路径）
                nodes = await commands.get_objects(client, shape_id)
                raw = nodes[0].raw
                assert raw["Object"]["Name"] == "集水区草稿"

                # 第二次写：根节点 ObjID=folder_id、子节点 ObjID=shape_id → 替换
                modified = json.loads(json.dumps(raw, ensure_ascii=False))
                modified["Object"]["Name"] = "集水区草稿-定稿"
                modified["Object"]["Comment"] = "第二版（替换）"
                modified["Object"]["ObjectDetail"]["Latlng"] = [
                    39.0100, 106.5700,
                    39.0100, 106.5900,
                    39.0300, 106.5900,
                    39.0300, 106.5700,
                ]
                modified["Object"]["ObjectDetail"]["Mtp"] = 4
                replace_tree = objects.build_write_tree(
                    folder_id=folder_id, folder_name="草稿文件夹-定稿", children=[modified]
                )
                second = await commands.write_tree(client, replace_tree)
                assert {item.obj_id for item in second} == {folder_id, shape_id}
                # 没有新增对象：替换而不是重复创建
                assert server.world.object_count() == 2
                assert server.world.object_ids() == [folder_id, shape_id]

                # 回读：名称/备注/顶点都被替换
                shape_back = await commands.get_shape(client, shape_id)
                assert shape_back.name == "集水区草稿-定稿"
                assert shape_back.comment == "第二版（替换）"
                assert len(shape_back.vertices) == 4
                assert shape_back.vertices[1][0] == pytest.approx(106.59)
                folder_back = await commands.get_objects(client, folder_id)
                assert folder_back[0].name == "草稿文件夹-定稿"
                # 量算反映新几何
                metrics = await commands.get_shape_metrics(client, shape_id)
                assert metrics.point_count == 4

    run(scenario())


def test_20112_repeated_identical_write_is_idempotent_on_existing_ids():
    """两次完全相同、且 ObjID 已存在的写请求：结果仍是替换，不产生重复对象。"""

    async def scenario():
        async with mock_server() as server:
            async with OvitalClient(server.url, PSK) as client:
                shape = objects.build_shape_object(
                    name="图形", comment="", vertices_lonlat=[(106.57, 39.01), (106.58, 39.02)]
                )
                tree = objects.build_write_tree(folder_id=0, folder_name="幂等", children=[shape])
                first = await commands.write_tree(client, tree)
                folder_id = next(item.obj_id for item in first if item.type_id == 30)
                shape_id = next(item.obj_id for item in first if item.type_id == 13)
                nodes = await commands.get_objects(client, shape_id)
                raw = json.loads(json.dumps(nodes[0].raw, ensure_ascii=False))
                repeat_tree = objects.build_write_tree(
                    folder_id=folder_id, folder_name="幂等", children=[raw]
                )
                await commands.write_tree(client, repeat_tree)
                await commands.write_tree(client, repeat_tree)
                assert server.world.object_count() == 2
                assert server.world.object_ids() == [folder_id, shape_id]

    run(scenario())
