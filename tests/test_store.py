"""存储层测试：任务、快照、变更、幂等、原子写、路径越界。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ovital_mcp.errors import StoreError
from ovital_mcp.store import FileLock, TaskStore, atomic_write_json, read_json, sha256_file


def test_create_and_load_task(store: TaskStore) -> None:
    task = store.create_task("watershed", {"dem": "x.tif"}, extra={"note": "t"})
    assert task.task_id.startswith("t-")
    assert (task.task_dir / "task.json").is_file()
    loaded = store.load_task(task.task_id)
    assert loaded.kind == "watershed"
    assert loaded.params == {"dem": "x.tif"}
    assert store.list_tasks()[0]["task_id"] == task.task_id


def test_update_task(store: TaskStore) -> None:
    task = store.create_task("watershed", {})
    updated = store.update_task(task.task_id, status="done", job_id="j-1")
    assert updated["status"] == "done"
    assert updated["job_id"] == "j-1"


def test_provenance_and_events(store: TaskStore) -> None:
    task = store.create_task("watershed", {})
    store.update_provenance(task.task_id, engine="native", verified_against_real_device=False)
    prov = store.get_provenance(task.task_id)
    assert prov["engine"] == "native"
    assert prov["verified_against_real_device"] is False
    store.append_event(task.task_id, {"stage": "dem_inspect"})
    events = list(store.iter_events(task.task_id))
    assert events[0]["stage"] == "dem_inspect"
    assert "at" in events[0]


def test_record_input_and_output(store: TaskStore, tmp_path: Path) -> None:
    task = store.create_task("watershed", {})
    dem = tmp_path / "dem.tif"
    dem.write_bytes(b"fake-dem-bytes")
    entry = store.record_input(task.task_id, dem, role="dem")
    assert entry["sha256"] == sha256_file(dem)
    assert entry["size_bytes"] == len(b"fake-dem-bytes")
    inputs = read_json(task.task_dir / "inputs.json")
    assert inputs[0]["role"] == "dem"

    out = tmp_path / "boundary.geojson"
    out.write_text("{}", encoding="utf-8")
    store.record_output(task.task_id, out, role="boundary")
    outputs = store.get_provenance(task.task_id)["outputs"]
    assert outputs[0]["role"] == "boundary"
    assert outputs[0]["sha256"] == sha256_file(out)


def test_snapshot_save_and_load(store: TaskStore) -> None:
    task = store.create_task("draft", {})
    payload = {"folder": {"folder_id": 1, "raw": [{"Type": 30}]}}
    snapshot_id = store.save_snapshot(task.task_id, label="before", payload=payload)
    loaded = store.load_snapshot(task.task_id, snapshot_id)
    assert loaded["payload"] == payload
    assert loaded["payload_sha256"]


def test_change_lifecycle(store: TaskStore) -> None:
    task = store.create_task("draft", {})
    change = store.create_change(
        task_id=task.task_id,
        action="write_draft",
        snapshot_id="s-1",
        request={"name": "x"},
        result={"status": "pending"},
        idempotency_key="k1",
    )
    assert change["rolled_back"] is False
    updated = store.update_change(change["change_id"], result={"status": "written", "created_obj_ids": [5]})
    assert updated["result"]["created_obj_ids"] == [5]
    rolled = store.mark_change_rolled_back(change["change_id"], report={"deleted_obj_ids": [5]})
    assert rolled["rolled_back"] is True
    assert store.load_change(change["change_id"])["rollback_report"]["deleted_obj_ids"] == [5]
    assert store.list_changes(task_id=task.task_id)[0]["change_id"] == change["change_id"]


def test_idempotency_roundtrip(store: TaskStore) -> None:
    store.idempotency_put("draft:abc", {"obj_id": 42})
    entry = store.idempotency_get("draft:abc")
    assert entry is not None
    assert entry["value"]["obj_id"] == 42
    assert store.idempotency_get("missing") is None


def test_export_path_is_unique(store: TaskStore) -> None:
    first = store.export_path("a.geojson")
    first.write_text("x", encoding="utf-8")
    second = store.export_path("a.geojson")
    assert first != second
    assert second.name == "a-1.geojson"


def test_path_traversal_rejected(store: TaskStore) -> None:
    with pytest.raises(StoreError):
        store.load_task("../../etc/passwd")


def test_atomic_json_write_and_read(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "a.json"
    atomic_write_json(path, {"b": 1, "中文": "值"})
    assert read_json(path) == {"b": 1, "中文": "值"}
    assert read_json(tmp_path / "missing.json", default={"x": 1}) == {"x": 1}


def test_file_lock_blocks_second_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "x.lock"
    with FileLock(lock_path, timeout_s=0.2):
        with pytest.raises(StoreError):
            with FileLock(lock_path, timeout_s=0.1):
                pass
    # 释放后可再次获取
    with FileLock(lock_path, timeout_s=0.5):
        pass


def test_corrupt_json_raises_store_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError):
        read_json(path)


def test_atomic_write_retries_transient_windows_replace_error(tmp_path: Path, monkeypatch) -> None:
    """Windows 上目标文件被短暂占用时 os.replace 会抛 WinError 5/32，必须重试而不是失败。"""

    import ovital_mcp.store as store_mod

    calls: list[int] = []
    real_replace = store_mod.os.replace

    def flaky_replace(src, dst):  # noqa: ANN001
        calls.append(1)
        if len(calls) < 3:
            exc = PermissionError(13, "拒绝访问")
            exc.winerror = 5  # type: ignore[attr-defined]
            raise exc
        return real_replace(src, dst)

    monkeypatch.setattr(store_mod.os, "replace", flaky_replace)
    path = tmp_path / "job.json"
    store_mod.atomic_write_json(path, {"status": "succeeded"})
    assert read_json(path) == {"status": "succeeded"}
    assert len(calls) == 3


def test_atomic_write_does_not_retry_real_permission_error(tmp_path: Path, monkeypatch) -> None:
    import ovital_mcp.store as store_mod

    calls: list[int] = []

    def bad_replace(src, dst):  # noqa: ANN001
        calls.append(1)
        raise OSError(13, "权限错误（非 Windows 瞬时错误）")

    monkeypatch.setattr(store_mod.os, "replace", bad_replace)
    with pytest.raises(OSError):
        store_mod.atomic_write_json(tmp_path / "a.json", {"a": 1})
    assert len(calls) == 1
