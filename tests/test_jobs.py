"""持久任务管理测试（不依赖 GIS worker 的路径）。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ovital_mcp.config import AppConfig
from ovital_mcp.errors import JobNotFoundError, StoreError
from ovital_mcp.jobs import JobManager, _pid_alive, scrubbed_env
from ovital_mcp.store import atomic_write_json, read_json


def _manager(tmp_path: Path) -> JobManager:
    config = AppConfig.model_validate({"storage": {"data_root": str(tmp_path / "data")}, "gis": {"engine": "native"}})
    return JobManager(config)


def test_scrubbed_env_removes_credentials() -> None:
    env = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "DEEPSEEK_API_KEY": "sk-secret",
        "OVITAL_MCP_PSK": "psk-secret",
        "SOME_SECRET_TOKEN": "tok",
        "DSH_HOME": "/tmp/dsh",
        "LANG": "C.UTF-8",
    }
    cleaned = scrubbed_env(env)
    assert cleaned["PATH"] == "/usr/bin"
    assert cleaned["HOME"] == "/home/x"
    assert cleaned["LANG"] == "C.UTF-8"
    assert "DEEPSEEK_API_KEY" not in cleaned
    assert "OVITAL_MCP_PSK" not in cleaned
    assert "SOME_SECRET_TOKEN" not in cleaned
    assert "DSH_HOME" not in cleaned


def test_pid_alive_detects_current_and_missing() -> None:
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(999_999_999) is False
    assert _pid_alive(None) is False


def test_job_file_update_and_read(tmp_path: Path) -> None:
    job_dir = tmp_path / "j-1"
    job_dir.mkdir()
    atomic_write_json(job_dir / "job.json", {"job_id": "j-1", "kind": "watershed", "status": "queued"})
    JobManager.update_job_file(
        job_dir, status="running", progress={"stage": "computing", "message": "go"}, extra={"pid": 123}
    )
    data = JobManager.read_job_file(job_dir)
    assert data["status"] == "running"
    assert data["progress"]["stage"] == "computing"
    assert data["pid"] == 123
    assert data["heartbeat_at"]


def test_load_rejects_illegal_job_id(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(JobNotFoundError):
        manager.load("../../etc")


def test_reconcile_dead_pid_marks_failed(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    job_dir = manager.jobs_root / "j-dead"
    job_dir.mkdir(parents=True)
    atomic_write_json(
        job_dir / "job.json",
        {
            "job_id": "j-dead",
            "kind": "watershed",
            "status": "running",
            "created_at": "2026-01-01T00:00:00+00:00",
            "pid": 999_999_999,
            "params": {},
        },
    )
    record = manager.load("j-dead")
    assert record.status == "failed"
    assert record.error and record.error["code"] == "WORKER_EXITED"
    data = read_json(job_dir / "job.json")
    assert data["finished_at"]


def test_cancel_running_process_is_idempotent(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    job_dir = manager.jobs_root / "j-live"
    job_dir.mkdir(parents=True)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        atomic_write_json(
            job_dir / "job.json",
            {
                "job_id": "j-live",
                "kind": "watershed",
                "status": "running",
                "created_at": "2026-01-01T00:00:00+00:00",
                "pid": child.pid,
                "params": {},
            },
        )
        record = manager.cancel("j-live")
        assert record.status == "cancelled"
        assert record.cancel_requested is True
        # 子进程应被终止
        for _ in range(50):
            if child.poll() is not None:
                break
            time.sleep(0.1)
        assert child.poll() is not None
        # 幂等
        again = manager.cancel("j-live")
        assert again.status == "cancelled"
    finally:
        if child.poll() is None:  # pragma: no cover
            child.kill()


def test_list_jobs_summary(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    for index in range(2):
        job_dir = manager.jobs_root / f"j-{index}"
        job_dir.mkdir(parents=True)
        atomic_write_json(
            job_dir / "job.json",
            {
                "job_id": f"j-{index}",
                "kind": "watershed",
                "status": "succeeded",
                "created_at": "2026-01-01T00:00:00+00:00",
                "progress": {"stage": "done"},
            },
        )
    items = manager.list_jobs()
    assert len(items) == 2
    assert items[0]["status"] == "succeeded"
