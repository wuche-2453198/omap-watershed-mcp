#!/usr/bin/env python
"""独立启动模拟奥维 WebSocket 服务器（仅供离线测试，非真实奥维）。

用法::

    .venv/bin/python scripts/run_mock_ovital.py --port 0 --psk-file /tmp/mock-psk.txt
    .venv/bin/python scripts/run_mock_ovital.py --fault drop-on-write --fault duplicate-20112

约定
----
- 默认端口 0（系统随机分配），便于并行测试；
- PSK 来源优先级：``--psk-file`` > ``--psk`` > 环境变量 ``OVITAL_MCP_MOCK_PSK`` >
  内置测试值 ``test-psk-not-secret``。**绝不读取真实奥维凭据**；
- stdout 只输出一行可解析 JSON：``{"url": ..., "simulated": true, ...}``；
  其余日志（含实际端口与凭据位置）写 stderr；
- ``--ready-file`` 在服务器就绪后写入同样内容的 JSON，供集成测试等待；
- 收到 SIGINT/SIGTERM 时优雅关闭并关闭所有连接。

故障注入（对应 ``ovital_mcp/mock/faults.py``）
----------------------------------------------
- ``drop-on-write``：收到 20112 写入立即断开（验证「断线不盲目重发创建」）；
- ``timeout-on:<cmdid>``：指定命令不返回响应（验证超时处理）；
- ``duplicate-20112``：20112 响应发两次（验证重复响应处理）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

# 允许直接从仓库根目录运行（不依赖 pip install -e）。
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ovital_mcp.mock import Faults, MockOvitalServer  # noqa: E402

DEFAULT_TEST_PSK = "test-psk-not-secret"
DEFAULT_FAULT_PSK_ENV = "OVITAL_MCP_MOCK_PSK"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="启动模拟奥维 WebSocket 服务器（离线测试用，非真实奥维）"
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=0, help="监听端口；0=随机（默认）")
    parser.add_argument("--psk", default=None, help="模拟预共享密钥（测试值）")
    parser.add_argument("--psk-file", default=None, help="从文件首行读取模拟 PSK")
    parser.add_argument(
        "--fault",
        action="append",
        default=[],
        metavar="FAULT",
        help="故障注入：drop-on-write | timeout-on:<cmdid> | duplicate-20112（可重复）",
    )
    parser.add_argument(
        "--write-style",
        choices=("manual_flat", "sdk_nested"),
        default="manual_flat",
        help="20112 响应形态（手册扁平 / 官方样例嵌套；真实设备待实测）",
    )
    parser.add_argument(
        "--ready-file", default=None, help="就绪后把 JSON（url/simulated/pid）写入该文件"
    )
    parser.add_argument(
        "--stop-file",
        default=None,
        help=(
            "出现该文件时优雅停止（跨平台测试入口；Windows 上 Popen 无法投递 SIGTERM/SIGBREAK，"
            "用文件触发可复现）"
        ),
    )
    parser.add_argument(
        "--print-requests",
        action="store_true",
        help="把收到的请求以 JSON 行写 stderr（调试用）",
    )
    parser.add_argument("--log-level", default="WARNING")
    return parser


def _resolve_psk(args: argparse.Namespace) -> tuple[str, str]:
    """返回 (psk, 来源说明)。来源说明会写 stderr，但不会打印密钥本身。"""

    if args.psk_file:
        path = Path(args.psk_file).expanduser()
        text = path.read_text(encoding="utf-8")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            raise SystemExit(f"psk_file 为空：{path}")
        return lines[0], f"psk_file={path}"
    if args.psk:
        return args.psk, "--psk（命令行，仅限测试）"
    env_value = os.environ.get(DEFAULT_FAULT_PSK_ENV)
    if env_value:
        return env_value, f"环境变量 {DEFAULT_FAULT_PSK_ENV}"
    return DEFAULT_TEST_PSK, "内置测试值（未提供 --psk/--psk-file/环境变量）"


def _parse_faults(raw: list[str]) -> tuple[Faults, list[str]]:
    faults = Faults()
    applied: list[str] = []
    for item in raw:
        value = item.strip()
        if value == "drop-on-write":
            faults.drop_on_write = True
            applied.append("drop_on_write")
        elif value == "duplicate-20112":
            faults.duplicate_20112 = True
            applied.append("duplicate_20112")
        elif value.startswith("timeout-on:"):
            _, _, cmdid_text = value.partition(":")
            try:
                cmdid = int(cmdid_text)
            except ValueError as exc:
                raise SystemExit(f"timeout-on 需要整数 cmdid：{value!r}") from exc
            faults.timeout_on.add(cmdid)
            applied.append(f"timeout_on:{cmdid}")
        else:
            raise SystemExit(
                f"未知故障注入 {value!r}；支持 drop-on-write | timeout-on:<cmdid> | duplicate-20112"
            )
    return faults, applied


async def _run(args: argparse.Namespace) -> int:
    psk, psk_source = _resolve_psk(args)
    faults, applied_faults = _parse_faults(args.fault)

    server = MockOvitalServer(
        psk,
        args.host,
        args.port,
        faults=faults,
        write_response_style=args.write_style,
    )
    port = await server.start()
    ready = {
        "url": server.url,
        "host": args.host,
        "port": port,
        "simulated": True,
        "verified_against_real_device": False,
        "psk_source": psk_source,
        "faults": applied_faults,
        "write_style": args.write_style,
        "pid": os.getpid(),
    }
    # stdout：一行可解析 JSON（集成测试按行读取）。
    print(json.dumps(ready, ensure_ascii=False), flush=True)
    print(
        f"模拟奥维服务器已启动 {server.url}（simulated=true，"
        f"PSK 来源：{psk_source}，故障：{applied_faults or '无'}）",
        file=sys.stderr,
        flush=True,
    )
    if args.ready_file:
        ready_path = Path(args.ready_file).expanduser()
        ready_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = ready_path.with_suffix(ready_path.suffix + ".tmp")
        tmp.write_text(json.dumps(ready, ensure_ascii=False), encoding="utf-8")
        tmp.replace(ready_path)
        print(f"就绪信息已写入 {ready_path}", file=sys.stderr, flush=True)

    stop_event = asyncio.Event()

    def _request_stop() -> None:
        if not stop_event.is_set():
            print("收到停止信号，正在优雅关闭模拟服务器…", file=sys.stderr, flush=True)
            stop_event.set()

    stop_file = Path(args.stop_file).expanduser() if args.stop_file else None
    stop_watcher: asyncio.Task[None] | None = None
    if stop_file is not None:

        async def _watch_stop_file() -> None:
            while not stop_event.is_set():
                if stop_file.exists():
                    _request_stop()
                    return
                await asyncio.sleep(0.05)

        stop_watcher = asyncio.create_task(_watch_stop_file())

    loop = asyncio.get_running_loop()
    handled = (signal.SIGINT, signal.SIGTERM)
    if hasattr(signal, "SIGBREAK"):  # Windows：附带的进程组收 Ctrl+Break 时可优雅停止
        handled += (signal.SIGBREAK,)
    for sig in handled:
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # pragma: no cover - Windows 事件循环
            signal.signal(sig, lambda *_: _request_stop())

    try:
        await stop_event.wait()
    finally:
        if stop_watcher is not None:
            stop_watcher.cancel()
        await server.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    import logging

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.print_requests:
        logging.getLogger("ovital_mcp.mock.server").setLevel(logging.DEBUG)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
