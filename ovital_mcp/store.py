"""持久化存储：任务目录、provenance、输入摘要、草稿快照、变更记录、幂等键。

目录布局（``storage.data_root`` 之下）::

    tasks/<task_id>/task.json
    tasks/<task_id>/provenance.json
    tasks/<task_id>/events.jsonl
    tasks/<task_id>/inputs.json
    tasks/<task_id>/artifacts/            # GIS 成果（boundary.geojson、mask.tif、图）
    tasks/<task_id>/snapshots/<snapshot_id>.json
    jobs/<job_id>/job.json                # jobs.py 使用
    changes/<change_id>.json              # 每次奥维写入的前后证据
    versions/<obj_id>/<version>.json      # 对象版本历史
    exports/                              # 导出成果
    idempotency.json                      # 幂等键 -> 值
    logs/

所有 JSON 写入均为原子写（临时文件 + os.replace），跨进程用文件锁保护读改写。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import AppConfig, RuntimePaths
from .errors import StoreError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{secrets.token_hex(3)}"


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: ``os.replace`` 在 Windows 上会因为目标文件被短暂占用而失败：
#: - WinError 5（拒绝访问，PermissionError）
#: - WinError 32/33（共享冲突/锁冲突，OSError）
#: Python 的 ``open()`` 不带 ``FILE_SHARE_DELETE``，所以同一进程的心跳线程或
#: 另一个进程正在读目标文件时，替换就会失败。这类失败是瞬时的，做有界重试即可；
#: 超过重试次数仍然失败才向上抛，避免把真实错误吞掉。
_TRANSIENT_REPLACE_WINERR = frozenset({5, 32, 33})
_REPLACE_RETRY_ATTEMPTS = 40
_REPLACE_RETRY_DELAY_S = 0.025


def _is_transient_replace_error(exc: BaseException) -> bool:
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in _TRANSIENT_REPLACE_WINERR
    # 非 Windows 平台没有 winerror；按普通 OSError 处理，不重试。
    return False


def _replace_with_retry(tmp: Path, target: Path) -> None:
    for attempt in range(_REPLACE_RETRY_ATTEMPTS):
        try:
            os.replace(tmp, target)
            return
        except OSError as exc:
            if not _is_transient_replace_error(exc) or attempt == _REPLACE_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_RETRY_DELAY_S)


def atomic_write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        _replace_with_retry(tmp, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def atomic_write_json(path: str | Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def read_json(path: str | Path, *, default: Any = None) -> Any:
    target = Path(path)
    if not target.is_file():
        return default
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StoreError(f"读取 JSON 失败 {target}: {exc}") from exc


def jsonable_safe(value: Any) -> Any:
    """轻量 JSON 化：Path -> str，其余交给 json 序列化时再报错。"""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonable_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable_safe(v) for v in value]
    return value


class FileLock:
    """跨进程文件锁（POSIX/Windows 通用，O_EXCL 实现）。

    用于 idempotency.json 这类小文件的读改写。锁持有时间很短；超过
    ``stale_after_s`` 的锁视为崩溃残留并回收。
    """

    def __init__(self, path: str | Path, *, timeout_s: float = 10.0, stale_after_s: float = 60.0) -> None:
        self.path = Path(path)
        self.timeout_s = timeout_s
        self.stale_after_s = stale_after_s
        self._fd: int | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(self._fd, f"{os.getpid()} {time.time():.0f}\n".encode())
                return self
            except FileExistsError:
                self._maybe_break_stale()
                if time.monotonic() >= deadline:
                    raise StoreError(f"获取文件锁超时：{self.path}") from None
                time.sleep(0.02)
            except OSError as exc:  # pragma: no cover - 平台相关
                raise StoreError(f"无法创建文件锁 {self.path}: {exc}") from exc

    def _maybe_break_stale(self) -> None:
        with contextlib.suppress(OSError):
            age = time.time() - self.path.stat().st_mtime
            if age > self.stale_after_s:
                self.path.unlink(missing_ok=True)

    def __exit__(self, *exc_info: object) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None
        with contextlib.suppress(OSError):
            self.path.unlink()


@dataclass
class TaskRecord:
    task_id: str
    kind: str
    created_at: str
    params: dict[str, Any]
    task_dir: Path
    parent_task_id: str | None = None
    status: str = "running"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "created_at": self.created_at,
            "params": self.params,
            "parent_task_id": self.parent_task_id,
            "status": self.status,
            "extra": self.extra,
            "task_dir": str(self.task_dir),
        }


class TaskStore:
    """任务/成果/快照/变更/幂等的统一入口。"""

    def __init__(self, config: AppConfig, paths: RuntimePaths | None = None) -> None:
        self.config = config
        self.paths = paths or RuntimePaths.build(config)
        self._ensured = False

    # -- 基础 ---------------------------------------------------------------

    def ensure(self) -> "TaskStore":
        if not self._ensured:
            self.paths.ensure()
            (self.paths.data_root / "jobs").mkdir(parents=True, exist_ok=True)
            (self.paths.data_root / "changes").mkdir(parents=True, exist_ok=True)
            (self.paths.data_root / "versions").mkdir(parents=True, exist_ok=True)
            self._ensured = True
        return self

    @property
    def data_root(self) -> Path:
        return self.paths.data_root

    def _under_root(self, *parts: str) -> Path:
        root = self.paths.data_root.resolve()
        target = root.joinpath(*parts).resolve()
        if root != target and root not in target.parents:
            raise StoreError(f"路径越界：{target} 不在数据根目录 {root} 内")
        return target

    # -- 任务 ---------------------------------------------------------------

    def create_task(
        self,
        kind: str,
        params: dict[str, Any],
        *,
        parent_task_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> TaskRecord:
        self.ensure()
        task_id = _new_id("t")
        task_dir = self.paths.tasks / task_id
        (task_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        (task_dir / "snapshots").mkdir(parents=True, exist_ok=True)
        record = TaskRecord(
            task_id=task_id,
            kind=kind,
            created_at=utc_now(),
            params=params,
            task_dir=task_dir,
            parent_task_id=parent_task_id,
            extra=extra or {},
        )
        atomic_write_json(task_dir / "task.json", record.to_dict())
        atomic_write_json(task_dir / "provenance.json", {
            "task_id": task_id,
            "kind": kind,
            "created_at": record.created_at,
            "engine": None,
            "verified_against_real_device": False,
            "simulated": False,
            "inputs": [],
            "outputs": [],
            "events": [],
        })
        return record

    def load_task(self, task_id: str) -> TaskRecord:
        self.ensure()
        task_dir = self._under_root("tasks", task_id)
        data = read_json(task_dir / "task.json")
        if not data:
            raise StoreError(f"任务不存在：{task_id}")
        return TaskRecord(
            task_id=data["task_id"],
            kind=data["kind"],
            created_at=data["created_at"],
            params=data.get("params") or {},
            task_dir=task_dir,
            parent_task_id=data.get("parent_task_id"),
            status=data.get("status", "running"),
            extra=data.get("extra") or {},
        )

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any]:
        """更新 task.json（状态/结果摘要等小字段）。"""

        lock = FileLock(self._under_root("tasks", task_id, ".task.lock"))
        with lock:
            path = self._under_root("tasks", task_id, "task.json")
            data = read_json(path)
            if not data:
                raise StoreError(f"任务不存在：{task_id}")
            data.update(jsonable_safe(fields))
            atomic_write_json(path, data)
            return data

    def list_tasks(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self.ensure()
        items: list[dict[str, Any]] = []
        if not self.paths.tasks.is_dir():
            return items
        dirs = sorted(self.paths.tasks.iterdir(), key=lambda p: p.name, reverse=True)
        for task_dir in dirs[:limit]:
            if not task_dir.is_dir():
                continue
            data = read_json(task_dir / "task.json")
            if data:
                items.append(data)
        return items

    def task_artifact_dir(self, task_id: str) -> Path:
        path = self._under_root("tasks", task_id, "artifacts")
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- provenance / 事件 ---------------------------------------------------

    def _provenance_path(self, task_id: str) -> Path:
        return self._under_root("tasks", task_id, "provenance.json")

    def update_provenance(self, task_id: str, **fields: Any) -> dict[str, Any]:
        lock = FileLock(self._under_root("tasks", task_id, ".provenance.lock"))
        with lock:
            path = self._provenance_path(task_id)
            data = read_json(path, default={}) or {}
            data.update(fields)
            data.setdefault("task_id", task_id)
            atomic_write_json(path, data)
            return data

    def get_provenance(self, task_id: str) -> dict[str, Any]:
        return read_json(self._provenance_path(task_id), default={}) or {}

    def append_event(self, task_id: str, event: dict[str, Any]) -> None:
        path = self._under_root("tasks", task_id, "events.jsonl")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": utc_now(), **event}, ensure_ascii=False) + "\n")

    def record_input(
        self,
        task_id: str,
        path: str | Path,
        *,
        role: str = "input",
        note: str | None = None,
    ) -> dict[str, Any]:
        """登记输入文件摘要（不复制大文件，只记录绝对路径 + 大小 + SHA256）。"""

        source = Path(os.path.expandvars(os.path.expanduser(str(path))))
        if not source.is_file():
            raise StoreError(f"输入文件不存在：{source}")
        entry = {
            "role": role,
            "path": str(source.resolve()),
            "size_bytes": source.stat().st_size,
            "sha256": sha256_file(source),
            "recorded_at": utc_now(),
            "note": note,
        }
        lock = FileLock(self._under_root("tasks", task_id, ".inputs.lock"))
        with lock:
            inputs_path = self._under_root("tasks", task_id, "inputs.json")
            items = read_json(inputs_path, default=[]) or []
            items.append(entry)
            atomic_write_json(inputs_path, items)
        return entry

    def record_output(self, task_id: str, path: str | Path, *, role: str = "output") -> dict[str, Any]:
        target = Path(path)
        if not target.is_file():
            raise StoreError(f"输出文件不存在：{target}")
        entry = {
            "role": role,
            "path": str(target.resolve()),
            "size_bytes": target.stat().st_size,
            "sha256": sha256_file(target),
            "recorded_at": utc_now(),
        }
        prov = self.get_provenance(task_id)
        outputs = list(prov.get("outputs") or [])
        outputs.append(entry)
        self.update_provenance(task_id, outputs=outputs)
        return entry

    # -- 快照与变更（供回滚） -------------------------------------------------

    def save_snapshot(
        self,
        task_id: str,
        *,
        label: str,
        payload: dict[str, Any],
        kind: str = "ovital_objects",
    ) -> str:
        """保存奥维对象/写前状态快照，返回 snapshot_id。"""

        snapshot_id = _new_id("s")
        record = {
            "snapshot_id": snapshot_id,
            "task_id": task_id,
            "kind": kind,
            "label": label,
            "created_at": utc_now(),
            "payload": payload,
            "payload_sha256": sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        }
        atomic_write_json(self._under_root("tasks", task_id, "snapshots", f"{snapshot_id}.json"), record)
        return snapshot_id

    def load_snapshot(self, task_id: str, snapshot_id: str) -> dict[str, Any]:
        data = read_json(self._under_root("tasks", task_id, "snapshots", f"{snapshot_id}.json"))
        if not data:
            raise StoreError(f"快照不存在：{task_id}/{snapshot_id}")
        return data

    def create_change(
        self,
        *,
        task_id: str,
        action: str,
        snapshot_id: str | None,
        request: dict[str, Any],
        result: dict[str, Any],
        idempotency_key: str | None = None,
        verified_against_real_device: bool = False,
        simulated: bool = False,
    ) -> dict[str, Any]:
        change_id = _new_id("c")
        record = {
            "change_id": change_id,
            "task_id": task_id,
            "action": action,
            "snapshot_id": snapshot_id,
            "request": request,
            "result": result,
            "idempotency_key": idempotency_key,
            "created_at": utc_now(),
            "rolled_back": False,
            "rolled_back_at": None,
            "verified_against_real_device": verified_against_real_device,
            "simulated": simulated,
        }
        atomic_write_json(self._under_root("changes", f"{change_id}.json"), record)
        return record

    def load_change(self, change_id: str) -> dict[str, Any]:
        data = read_json(self._under_root("changes", f"{change_id}.json"))
        if not data:
            raise StoreError(f"变更记录不存在：{change_id}")
        normalized = dict(data)
        change_id_value = normalized.get("change_id", change_id)
        # 兼容以 task 目录传递的调用：把 task_id 一并返回给上层。
        normalized.setdefault("change_id", change_id_value)
        return normalized

    def update_change(self, change_id: str, **fields: Any) -> dict[str, Any]:
        """更新变更记录（写入结果、回读验证等）。"""

        path = self._under_root("changes", f"{change_id}.json")
        lock = FileLock(path.with_suffix(".lock"))
        with lock:
            data = read_json(path)
            if not data:
                raise StoreError(f"变更记录不存在：{change_id}")
            data.update(jsonable_safe(fields))
            atomic_write_json(path, data)
            return data

    def mark_change_rolled_back(self, change_id: str, *, report: dict[str, Any]) -> dict[str, Any]:
        path = self._under_root("changes", f"{change_id}.json")
        lock = FileLock(path.with_suffix(".lock"))
        with lock:
            data = read_json(path)
            if not data:
                raise StoreError(f"变更记录不存在：{change_id}")
            data["rolled_back"] = True
            data["rolled_back_at"] = utc_now()
            data["rollback_report"] = report
            atomic_write_json(path, data)
            return data

    def list_changes(self, *, task_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self.ensure()
        changes_dir = self.paths.data_root / "changes"
        items: list[dict[str, Any]] = []
        for path in sorted(changes_dir.glob("c-*.json"), reverse=True):
            data = read_json(path)
            if not data:
                continue
            if task_id and data.get("task_id") != task_id:
                continue
            items.append(data)
            if len(items) >= limit:
                break
        return items

    # -- 版本历史 ------------------------------------------------------------

    def record_version(self, obj_id: int, *, task_id: str, version: int, payload: dict[str, Any]) -> Path:
        path = self._under_root("versions", str(obj_id), f"v{version:04d}.json")
        atomic_write_json(path, {
            "obj_id": obj_id,
            "task_id": task_id,
            "version": version,
            "recorded_at": utc_now(),
            **payload,
        })
        return path

    def list_versions(self, obj_id: int) -> list[dict[str, Any]]:
        directory = self._under_root("versions", str(obj_id))
        if not directory.is_dir():
            return []
        items = []
        for path in sorted(directory.glob("v*.json")):
            data = read_json(path)
            if data:
                items.append(data)
        return items

    # -- 幂等键 --------------------------------------------------------------

    def _idempotency_path(self) -> Path:
        return self._under_root("idempotency.json")

    def idempotency_get(self, key: str) -> dict[str, Any] | None:
        self.ensure()
        path = self._idempotency_path()
        with FileLock(path.with_suffix(".lock")):
            data = read_json(path, default={}) or {}
            entry = data.get(key)
            return dict(entry) if isinstance(entry, dict) else None

    def idempotency_put(self, key: str, value: dict[str, Any]) -> None:
        self.ensure()
        path = self._idempotency_path()
        with FileLock(path.with_suffix(".lock")):
            data = read_json(path, default={}) or {}
            data[key] = {"value": value, "recorded_at": utc_now()}
            atomic_write_json(path, data)

    def idempotency_delete(self, key: str) -> bool:
        """删除幂等键（回滚后允许重新写入）。返回是否真的删除了条目。"""

        self.ensure()
        path = self._idempotency_path()
        with FileLock(path.with_suffix(".lock")):
            data = read_json(path, default={}) or {}
            existed = key in data
            data.pop(key, None)
            atomic_write_json(path, data)
            return existed

    # -- 导出 ---------------------------------------------------------------

    def export_path(self, filename: str) -> Path:
        self.ensure()
        safe_name = Path(filename).name
        target = self._under_root("exports", safe_name)
        counter = 1
        while target.exists():
            target = self._under_root("exports", f"{Path(safe_name).stem}-{counter}{Path(safe_name).suffix}")
            counter += 1
        return target

    # -- 事件流（迭代读取，用于工具层轮询进度） --------------------------------

    def iter_events(self, task_id: str) -> Iterator[dict[str, Any]]:
        path = self._under_root("tasks", task_id, "events.jsonl")
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    yield json.loads(line)
