#!/usr/bin/env python3
"""模拟奥维服务器进程管理（供验证脚本复用；非业务代码）。

以 ``scripts/run_mock_ovital.py`` 启动一个**独立进程**的 WebSocket 模拟服务器
（不是进程内 mock），等待 ``--ready-file`` 出现后返回真实监听 URL；退出时优雅关闭。

约定（与 scripts/run_mock_ovital.py 对齐）：
- 端口 0 = 系统分配，避免端口冲突；
- PSK 使用随机测试值，不用真实凭据；PSK 不出现在证据/日志中；
- 就绪 JSON：``{"url": "ws://127.0.0.1:<port>", "simulated": true, "pid": ...}``。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / "scripts" / "run_mock_ovital.py"


class MockOvitalProcess:
    """一次模拟奥维服务器进程（可用 ``url`` 连接）。"""

    def __init__(self, *, run_dir: Path, psk: str | None = None, faults: list[str] | None = None,
                 write_style: str = "manual_flat", print_requests: bool = False) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.psk = psk or f"mock-verify-psk-{uuid.uuid4().hex[:12]}"
        self.faults = faults or []
        self.write_style = write_style
        self.print_requests = print_requests
        self.proc: subprocess.Popen[str] | None = None
        self.ready: dict[str, Any] = {}
        self.stdout_path = run_dir / "mock-stdout.log"
        self.stderr_path = run_dir / "mock-stderr.log"

    @property
    def url(self) -> str:
        return str(self.ready["url"])

    def start(self, timeout_s: float = 30.0) -> "MockOvitalProcess":
        ready_file = self.run_dir / "mock-ready.json"
        ready_file.unlink(missing_ok=True)
        cmd = [
            sys.executable,
            str(RUNNER),
            "--psk", self.psk,
            "--port", "0",
            "--ready-file", str(ready_file),
            "--write-style", self.write_style,
        ]
        for fault in self.faults:
            cmd += ["--fault", fault]
        if self.print_requests:
            cmd.append("--print-requests")
        self._stdout = self.stdout_path.open("w", encoding="utf-8")
        self._stderr = self.stderr_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=self._stdout,
            stderr=self._stderr,
            text=True,
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if ready_file.is_file():
                try:
                    self.ready = json.loads(ready_file.read_text(encoding="utf-8"))
                    break
                except json.JSONDecodeError:
                    pass
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"模拟奥维服务器提前退出（exit={self.proc.returncode}）：\n"
                    f"{self.stderr_path.read_text(encoding='utf-8')[-2000:]}"
                )
            time.sleep(0.05)
        if not self.ready:
            raise TimeoutError(f"模拟奥维服务器 {timeout_s}s 内未就绪：{self.stderr_path}")
        if not self.ready.get("simulated"):
            raise RuntimeError(f"模拟服务器就绪 JSON 未声明 simulated=true：{self.ready}")
        return self

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        finally:
            for handle in (getattr(self, "_stdout", None), getattr(self, "_stderr", None)):
                if handle is not None:
                    handle.close()
            self.proc = None


@contextmanager
def running_mock_ovital(
    run_dir: Path,
    *,
    psk: str | None = None,
    faults: list[str] | None = None,
    write_style: str = "manual_flat",
    print_requests: bool = False,
    timeout_s: float = 30.0,
) -> Iterator[MockOvitalProcess]:
    """with 语句管理模拟服务器生命周期；异常时也会尝试关闭。"""

    server = MockOvitalProcess(
        run_dir=run_dir, psk=psk, faults=faults, write_style=write_style, print_requests=print_requests
    )
    server.start(timeout_s=timeout_s)
    try:
        yield server
    finally:
        server.stop()
