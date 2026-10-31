"""持久后台任务（GIS 长计算）。

设计要点（对应任务书 C 节「GIS 长任务用持久任务 ID、状态查询和取消接口」）：

- MCP 工具调用只负责提交任务并立即返回 ``job_id``；真正的计算在独立子进程中跑，
  避免超过宿主的工具调用超时，也使任务在 MCP 进程重启后仍可查询。
- 任务状态落在 ``<data_root>/jobs/<job_id>/job.json``，worker 定期心跳；
  查询时做进程存活核对，进程消失但没写终态则显式标记 ``failed``（不伪装成功）。
- 取消：杀手进程组 + 写 ``cancel_requested`` 标记；重复取消幂等。
- worker 子进程**不接触奥维凭据**（只做 GIS 计算），启动环境会清理敏感变量。
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import AppConfig, RuntimePaths
from .errors import JobNotFoundError, JobStateError
from .store import FileLock, atomic_write_json, read_json, utc_now

REPO_ROOT = Path(__file__).resolve().parent.parent

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
VALID_STATES = frozenset({"queued", "running"}) | TERMINAL_STATES

_SENSITIVE_ENV_MARKERS = ("PSK", "KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY")


def _new_job_id() -> str:
    from datetime import datetime, timezone
    import secrets

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"j-{stamp}-{secrets.token_hex(3)}"


def scrubbed_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """返回清理过的子进程环境：去掉凭据类变量，保留运行必需项。"""

    source = dict(os.environ if env is None else env)
    cleaned: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if upper.startswith("DSH_"):
            continue
        if any(marker in upper for marker in _SENSITIVE_ENV_MARKERS):
            continue
        cleaned[key] = value
    return cleaned


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows 路径
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and exit_code.value == 259  # STILL_ACTIVE
        except Exception:  # noqa: BLE001 - 探测失败按已退出处理
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_pid(pid: int | None, *, grace_s: float = 3.0) -> None:
    if not pid or pid <= 0:
        return
    if os.name == "nt":  # pragma: no cover - Windows 路径
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


@dataclass
class JobRecord:
    job_id: str
    kind: str
    status: str
    created_at: str
    job_dir: Path
    params: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    parent_job_id: str | None = None
    pid: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    heartbeat_at: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    cancel_requested: bool = False
    simulated: bool = False
    verified_against_real_device: bool = False

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "params": self.params,
            "task_id": self.task_id,
            "parent_job_id": self.parent_job_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "heartbeat_at": self.heartbeat_at,
            "progress": self.progress,
            "error": self.error,
            "result": self.result,
            "cancel_requested": self.cancel_requested,
            "simulated": self.simulated,
            "verified_against_real_device": self.verified_against_real_device,
            "job_dir": str(self.job_dir),
        }

    def summary(self) -> dict[str, Any]:
        data = self.to_dict()
        data.pop("params", None)
        data.pop("job_dir", None)
        return data


class JobManager:
    """创建/查询/取消持久 GIS 任务。"""

    def __init__(
        self,
        config: AppConfig,
        paths: RuntimePaths | None = None,
        *,
        python_executable: str | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.config = config
        self.paths = paths or RuntimePaths.build(config)
        self.paths.ensure()
        self.jobs_root = self.paths.data_root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.python_executable = python_executable or sys.executable
        self.repo_root = repo_root or REPO_ROOT

    # -- 路径 ---------------------------------------------------------------

    def job_dir(self, job_id: str) -> Path:
        root = self.jobs_root.resolve()
        target = (root / job_id).resolve()
        if root not in target.parents:
            raise JobNotFoundError(f"非法 job_id：{job_id}")
        return target

    def _job_file(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    # -- 创建 ---------------------------------------------------------------

    def submit(
        self,
        kind: str,
        params: dict[str, Any],
        *,
        task_id: str | None = None,
        parent_job_id: str | None = None,
        simulated: bool = False,
        verified_against_real_device: bool = False,
    ) -> JobRecord:
        job_id = _new_job_id()
        job_dir = self.jobs_root / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        (job_dir / "artifacts").mkdir(exist_ok=True)
        record = JobRecord(
            job_id=job_id,
            kind=kind,
            status="queued",
            created_at=utc_now(),
            job_dir=job_dir,
            params=params,
            task_id=task_id,
            parent_job_id=parent_job_id,
            simulated=simulated,
            verified_against_real_device=verified_against_real_device,
            progress={"stage": "queued", "message": "已提交，等待 worker 启动"},
        )
        atomic_write_json(self._job_file(job_id), record.to_dict())
        log_path = job_dir / "worker.log"
        env = scrubbed_env()
        env["PYTHONUNBUFFERED"] = "1"
        env["OVITAL_MCP_JOB_DIR"] = str(job_dir)
        cmd = [self.python_executable, "-m", "ovital_mcp.worker", str(job_dir)]
        with log_path.open("ab") as log_handle:
            process = subprocess.Popen(  # noqa: S603 - 受控本地子进程
                cmd,
                cwd=str(self.repo_root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
        record.pid = process.pid
        record.started_at = utc_now()
        record.status = "running"
        record.progress = {"stage": "starting", "message": "worker 进程已启动"}
        atomic_write_json(self._job_file(job_id), record.to_dict())
        return record

    # -- 查询 ---------------------------------------------------------------

    def load(self, job_id: str) -> JobRecord:
        data = read_json(self._job_file(job_id))
        if not data:
            raise JobNotFoundError(f"任务不存在：{job_id}")
        record = JobRecord(
            job_id=data["job_id"],
            kind=data["kind"],
            status=data.get("status", "running"),
            created_at=data.get("created_at", ""),
            job_dir=self.job_dir(job_id),
            params=data.get("params") or {},
            task_id=data.get("task_id"),
            parent_job_id=data.get("parent_job_id"),
            pid=data.get("pid"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            heartbeat_at=data.get("heartbeat_at"),
            progress=data.get("progress") or {},
            error=data.get("error"),
            result=data.get("result"),
            cancel_requested=bool(data.get("cancel_requested")),
            simulated=bool(data.get("simulated")),
            verified_against_real_device=bool(data.get("verified_against_real_device")),
        )
        return self._reconcile(record)

    def _reconcile(self, record: JobRecord) -> JobRecord:
        """进程已消失但状态未落终态 → 显式失败（避免伪运行）。"""

        if record.terminal:
            return record
        if record.pid and not _pid_alive(record.pid):
            # 给 worker 一点时间写完终态（进程退出与落盘之间有竞争）。
            time.sleep(0.05)
            data = read_json(self._job_file(record.job_id)) or {}
            if data.get("status") in TERMINAL_STATES:
                return self.load(record.job_id)
            result_path = record.job_dir / "result.json"
            result = read_json(result_path)
            if result and result.get("ok") is not None:
                return self._finalize_failure(
                    record,
                    code="WORKER_EXITED",
                    message="worker 进程已退出但未写任务终态；已从 result.json 恢复结果",
                    result=result,
                )
            return self._finalize_failure(
                record,
                code="WORKER_EXITED",
                message="worker 进程已退出且未写任务终态（可能被系统杀死或崩溃，详见 worker.log）",
            )
        return record

    def _finalize_failure(
        self,
        record: JobRecord,
        *,
        code: str,
        message: str,
        result: dict[str, Any] | None = None,
    ) -> JobRecord:
        lock = FileLock(record.job_dir / ".job.lock")
        with lock:
            data = read_json(self._job_file(record.job_id)) or {}
            if data.get("status") in TERMINAL_STATES:
                return self.load(record.job_id)
            data["status"] = "failed"
            data["finished_at"] = utc_now()
            data["error"] = {"code": code, "message": message}
            if result is not None:
                data["result"] = result
            atomic_write_json(self._job_file(record.job_id), data)
        return self.load(record.job_id)

    def wait(self, job_id: str, *, timeout_s: float = 60.0, poll_s: float = 0.2) -> JobRecord:
        deadline = time.monotonic() + timeout_s
        while True:
            record = self.load(job_id)
            if record.terminal or time.monotonic() >= deadline:
                return record
            time.sleep(poll_s)

    def list_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not self.jobs_root.is_dir():
            return items
        for path in sorted(self.jobs_root.iterdir(), key=lambda p: p.name, reverse=True):
            if not path.is_dir():
                continue
            data = read_json(path / "job.json")
            if not data:
                continue
            items.append(
                {
                    "job_id": data.get("job_id"),
                    "kind": data.get("kind"),
                    "status": data.get("status"),
                    "created_at": data.get("created_at"),
                    "task_id": data.get("task_id"),
                    "progress": data.get("progress"),
                }
            )
            if len(items) >= limit:
                break
        return items

    def result(self, job_id: str) -> dict[str, Any] | None:
        """返回完整 worker 结果（优先 result.json），而不是 job.json 里的摘要。"""

        record = self.load(job_id)
        full = read_json(record.job_dir / "result.json")
        if isinstance(full, dict):
            return full
        return record.result

    def artifacts_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "artifacts"

    def log_tail(self, job_id: str, *, max_chars: int = 4000) -> str:
        path = self.job_dir(job_id) / "worker.log"
        if not path.is_file():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-max_chars:]

    # -- 取消 ---------------------------------------------------------------

    def cancel(self, job_id: str) -> JobRecord:
        record = self.load(job_id)
        if record.terminal:
            return record
        lock = FileLock(record.job_dir / ".job.lock")
        with lock:
            data = read_json(self._job_file(job_id)) or {}
            if data.get("status") in TERMINAL_STATES:
                return self.load(job_id)
            data["cancel_requested"] = True
            data["progress"] = {"stage": "cancelling", "message": "已请求取消"}
            atomic_write_json(self._job_file(job_id), data)
        _terminate_pid(record.pid)
        with lock:
            data = read_json(self._job_file(job_id)) or {}
            if data.get("status") in TERMINAL_STATES:
                return self.load(job_id)
            data["status"] = "cancelled"
            data["finished_at"] = utc_now()
            data["error"] = {"code": "JOB_CANCELLED", "message": "任务被取消"}
            atomic_write_json(self._job_file(job_id), data)
        return self.load(job_id)

    # -- worker 侧状态更新（worker.py 调用） -----------------------------------

    @staticmethod
    def update_job_file(
        job_dir: Path,
        *,
        status: str | None = None,
        progress: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """worker 更新 job.json（带文件锁与心跳）。"""

        directory = Path(job_dir)
        job_file = directory / "job.json"
        lock = FileLock(directory / ".job.lock")
        with lock:
            data = read_json(job_file) or {}
            data["heartbeat_at"] = utc_now()
            if status:
                if status not in VALID_STATES:
                    raise JobStateError(f"非法任务状态：{status}")
                data["status"] = status
                if status in TERMINAL_STATES:
                    data["finished_at"] = utc_now()
            if progress is not None:
                data["progress"] = progress
            if error is not None:
                data["error"] = error
            if result is not None:
                data["result"] = result
            if extra:
                data.update(extra)
            atomic_write_json(job_file, data)
            return data

    @staticmethod
    def read_job_file(job_dir: Path) -> dict[str, Any]:
        return read_json(Path(job_dir) / "job.json") or {}
