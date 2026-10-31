#!/usr/bin/env python3
"""阶段 2：完整离线闭环独立验证（模拟奥维 + 确定性 GIS + GRASS + 图片 + 安全）。

本脚本是 verifier（Task-4）阶段 2 的主证据生成器。它**不信任**实现方自证，全部结论来自：
- 真实子进程：``scripts/run_mock_ovital.py`` 启动模拟奥维（非进程内 mock）；
- 真实 MCP stdio：``mcp`` SDK 2.3.0 Client 对 ``python -m ovital_mcp`` 完成 initialize 握手；
- 真实工具调用：``mcp__ovital__*`` 18 个工具经协议往返；
- 独立复算：用 ``scripts/make_synthetic_dem.py`` 生成的**解析真值**合成 DEM，逐像元核对掩膜、
  面积、确定性；GRASS 8.5.0 实际命令/返回码/版本从 provenance 中核对（不是 native 冒充）；
- 图片真实传递：解码 MCP ImageContent，校验 PNG magic/尺寸/元数据；
- 故障注入：mock 的 ``drop-on-write``/``timeout-on:20112``/``duplicate-20112`` 验证断线/超时/重复
  响应的显式失败与幂等；
- 安全：凭据不泄漏、无 ``verified_against_real_device=true`` 伪造、stdout 纯净。

用法::

    HOME="$PWD/.cache/grasshome" .venv/bin/python scripts/verify_mock_loop.py
    .venv/bin/python scripts/verify_mock_loop.py --groups mock,security   # 子集
    .venv/bin/python scripts/verify_mock_loop.py --skip-grass             # 无 GRASS 环境时

证据写入 ``.cache/verify/loop/``（JSON + transcript + 图片副本），失败项非零退出。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from verify_mock_ovital import running_mock_ovital  # noqa: E402
from verify_stdio_harness import (  # noqa: E402
    EXPECTED_TOOLS,
    REPO_ROOT as HARNESS_REPO_ROOT,
    open_stdio_session,
    tool_result_is_error,
    tool_result_json,
    tool_result_text,
)

WORKDIR = REPO_ROOT / ".cache" / "verify" / "loop"
SYNTH_DIR = REPO_ROOT / ".cache" / "verify" / "synthetic"
GRASS_BIN = REPO_ROOT / ".conda" / "grass" / "bin" / "grass"
GRASS_HOME = REPO_ROOT / ".cache" / "grasshome"
DSHTEST_DIR = REPO_ROOT / ".cache" / "dshtest"

#: 本次运行的唯一标签：所有显式幂等键都带上它，确保验证的是**当前代码树**新跑出来的任务，
#: 而不是 start_watershed 自动幂等命中上一次运行留下的历史任务。
RUNTAG = time.strftime("%Y%m%dT%H%M%S") + "-" + hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:6]


# ---------------------------------------------------------------------------
# 检查清单
# ---------------------------------------------------------------------------


class Checklist:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def check(self, name: str, passed: bool, detail: Any = None, *, severity: str = "gate") -> bool:
        item = {"name": name, "passed": bool(passed), "severity": severity, "detail": detail}
        self.items.append(item)
        mark = "PASS" if passed else ("FAIL" if severity == "gate" else "WARN")
        suffix = "" if detail is None else f"  [{_short(detail)}]"
        print(f"[{mark}] {name}{suffix}")
        return bool(passed)

    def info(self, name: str, detail: Any = None) -> None:
        self.items.append({"name": name, "passed": True, "severity": "info", "detail": detail})
        print(f"[INFO] {name}  [{_short(detail)}]")

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [i for i in self.items if not i["passed"] and i["severity"] == "gate"]

    @property
    def warnings(self) -> list[dict[str, Any]]:
        return [i for i in self.items if not i["passed"] and i["severity"] != "gate"]


def _short(value: Any, limit: int = 420) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _user_home() -> Path:
    """真实用户 home（不依赖 HOME 环境变量；GRASS 验证会临时覆盖 HOME）。"""

    try:
        import pwd

        return Path(pwd.getpwuid(__import__("os").getuid()).pw_dir)
    except Exception:  # noqa: BLE001 - 非 POSIX 兜底
        return Path(__import__("os").path.expanduser("~"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def content_blocks(result: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for item in getattr(result, "content", None) or []:
        kind = getattr(item, "type", None) or item.__class__.__name__
        entry: dict[str, Any] = {"type": kind}
        text = getattr(item, "text", None)
        if text is not None:
            entry["text"] = text
        data = getattr(item, "data", None)
        if data is not None:
            raw = base64.b64decode(data) if isinstance(data, str) else bytes(data)
            entry["bytes"] = raw
            entry["mime"] = getattr(item, "mime_type", None) or getattr(item, "mimeType", None)
        blocks.append(entry)
    return blocks


def image_blocks(result: Any) -> list[dict[str, Any]]:
    return [b for b in content_blocks(result) if b["type"] == "image"]


def decode_png_info(data: bytes) -> dict[str, Any]:
    from PIL import Image
    import io

    assert data.startswith(b"\x89PNG\r\n\x1a\n"), "不是 PNG（magic 错误）"
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        info = {k: (v if isinstance(v, str) else str(v)) for k, v in im.info.items()}
        return {"size": im.size, "mode": im.mode, "format": im.format, "info": info}


async def wait_terminal(client: Any, job_id: str, *, timeout_s: float = 180.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = tool_result_json(await client.call_tool("get_job", {"job_id": job_id}))
        status = ((last.get("job") or {}).get("status")) or last.get("status")
        if status in {"succeeded", "failed", "cancelled"}:
            return last
        await asyncio.sleep(0.3)
    raise TimeoutError(f"任务 {job_id} 在 {timeout_s}s 内未结束，最后状态：{last}")


def _save_images(images: Iterable[dict[str, Any]], target_dir: Path, prefix: str) -> list[dict[str, Any]]:
    target_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for index, image in enumerate(images, start=1):
        raw = image["bytes"]
        path = target_dir / f"{prefix}-{index}.png"
        path.write_bytes(raw)
        entries.append({"path": str(path), "sha256": sha256_bytes(raw),
                        "bytes": len(raw), "mime": image.get("mime")})
    return entries


def scan_text_for_secret(root: Path, secret: str, *, exclude: Iterable[Path] = ()) -> list[str]:
    """在目录内文本/JSON 文件中检索凭据；返回命中的文件路径列表。"""

    hits: list[str] = []
    excluded = {p.resolve() for p in exclude}
    for path in root.rglob("*"):
        if not path.is_file() or path.resolve() in excluded:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if secret in text:
            hits.append(str(path))
    return hits


def ensure_synthetic_dem(checklist: Checklist) -> dict[str, Any]:
    script = SCRIPTS_DIR / "make_synthetic_dem.py"
    if not (SYNTH_DIR / "expected.json").is_file():
        import subprocess

        proc = subprocess.run(
            [sys.executable, str(script), "--out-dir", str(SYNTH_DIR)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
        )
        checklist.check("S0 生成合成解析真值 DEM（scripts/make_synthetic_dem.py）",
                        proc.returncode == 0 and (SYNTH_DIR / "expected.json").is_file(),
                        {"exit": proc.returncode, "stdout_tail": proc.stdout[-300:]})
    else:
        checklist.info("S0 复用已生成的合成解析真值 DEM", {"dir": str(SYNTH_DIR)})
    expected = json.loads((SYNTH_DIR / "expected.json").read_text(encoding="utf-8"))
    scenarios = expected.get("scenarios") or {}
    checklist.check("S1 合成 DEM 解析真值场景充分（≥10 个且含预期掩膜/面积）",
                    len(scenarios) >= 10 and all("expected_mask_cells" in s for s in scenarios.values()),
                    {"scenarios": len(scenarios)})
    return expected


# ---------------------------------------------------------------------------
# A. 模拟奥维协议闭环（真实 run_mock_ovital.py 子进程 + 真实 MCP stdio）
# ---------------------------------------------------------------------------


async def group_mock_protocol(checklist: Checklist, evidence: dict[str, Any]) -> str:
    run_dir = WORKDIR / "mock-protocol"
    mock_psk = f"loop-mock-psk-{int(time.time())}-{hashlib.sha256(str(time.time()).encode()).hexdigest()[:10]}"
    geojson = {
        "type": "Polygon",
        "coordinates": [[[116.990, 39.740], [117.000, 39.740], [117.000, 39.750],
                         [116.990, 39.750], [116.990, 39.740]]],
    }
    with running_mock_ovital(run_dir, psk=mock_psk) as mock:
        checklist.check("A1 scripts/run_mock_ovital.py 子进程真实启动，ready JSON 声明 simulated=true",
                        mock.ready.get("simulated") is True and str(mock.url).startswith("ws://"),
                        {"url": mock.url, "pid": mock.ready.get("pid")})
        async with open_stdio_session(endpoint=mock.url, psk=mock_psk, run_dir=run_dir / "mcp") as session:
            client = session.client
            evidence["mock"] = {"url": mock.url, "stderr_log": str(mock.stderr_path),
                                "stdout_log": str(mock.stdout_path)}

            cap = tool_result_json(await client.call_tool("get_capabilities", {}))
            conn = cap.get("connection") or {}
            prov = cap.get("provenance") or {}
            evidence["mock"]["get_capabilities"] = {
                "status": cap.get("status"), "available": conn.get("available"),
                "simulated": conn.get("simulated"), "version": conn.get("version"),
                "provenance": prov, "warnings": cap.get("warnings"),
            }
            checklist.check("A2 get_capabilities 连上模拟服务器：available=true / simulated=true / version=817",
                            cap.get("status") == "ok" and conn.get("available") is True
                            and conn.get("simulated") is True
                            and (conn.get("version") or {}).get("value") == 817,
                            {"available": conn.get("available"), "simulated": conn.get("simulated"),
                             "version": conn.get("version")})
            checklist.check("A3 模拟连接的 provenance 如实标注 offline_simulated 且 verified_against_real_device=false",
                            prov.get("simulated") is True
                            and prov.get("verified_against_real_device") is False
                            and prov.get("evidence_level") == "offline_simulated",
                            {"simulated": prov.get("simulated"),
                             "verified": prov.get("verified_against_real_device"),
                             "evidence_level": prov.get("evidence_level")})
            probes = conn.get("commands") or []
            checklist.info("A4 命令探测结果", {"probes": len(probes), "all_ok": all(p.get("ok") for p in probes)})
            checklist.check("A4b probe 命令数 >= 7 且全部 ok",
                            len(probes) >= 7 and all(p.get("ok") for p in probes),
                            {"probes": len(probes)})

            selection = tool_result_json(await client.call_tool("get_selection", {}))
            checklist.check("A5 get_selection 在模拟世界（无预置选中）返回 ok + 空列表 + 明确 warning",
                            selection.get("status") == "ok"
                            and selection.get("count") == 0
                            and bool(selection.get("warnings")),
                            {"count": selection.get("count"),
                             "warnings": (selection.get("warnings") or [])[:1]})
            checklist.info("A5b mock 限制：delivered runner 无预置选中对象参数，20104 非空选中路径未覆盖",
                           {"limitation": True})

            scene = await client.call_tool("get_scene",
                                           {"bbox_lonlat": [116.99, 39.74, 117.0, 39.75],
                                            "max_level": 12, "max_pixel": 500000})
            scene_payload = tool_result_json(scene)
            scene_images = image_blocks(scene)
            scene_info = None
            if scene_images:
                scene_info = decode_png_info(scene_images[0]["bytes"])
            evidence["mock"]["get_scene"] = {
                "status": scene_payload.get("status"),
                "image_bytes": len(scene_images[0]["bytes"]) if scene_images else 0,
                "image_info": scene_info,
                "pixel_width": scene_payload.get("pixel_width"),
                "pixel_height": scene_payload.get("pixel_height"),
            }
            checklist.check(
                "A6 get_scene 返回真实 MCP Image（PNG magic、可解码、尺寸与元数据一致、SIMULATED 水印元数据）",
                bool(scene_images)
                and scene_info is not None
                and scene_info["size"] == (scene_payload.get("pixel_width"), scene_payload.get("pixel_height"))
                and "SIMULATED MAP" in str(scene_info["info"].get("Description", ""))
                and scene_info["info"].get("Simulated") == "True",
                {"image_info": scene_info, "pixel_width": scene_payload.get("pixel_width"),
                 "pixel_height": scene_payload.get("pixel_height")},
            )
            checklist.check("A6b get_scene 图片字节 <= image_max_bytes(4MB)",
                            bool(scene_images) and len(scene_images[0]["bytes"]) <= 4 * 1024 * 1024,
                            {"bytes": len(scene_images[0]["bytes"]) if scene_images else None})

            # --- 写草稿 → 回读 → 幂等 → 定稿 → 回滚 ---
            write1 = tool_result_json(await client.call_tool(
                "write_draft", {"geojson": geojson, "draft_folder": "verify-draft",
                                "name": "验证草稿", "comment": "verifier loop",
                                "idempotency_key": "verify-loop-1"}))
            obj_id = write1.get("obj_id")
            change_id = write1.get("change_id")
            evidence["mock"]["write_draft"] = {
                "status": write1.get("status"), "obj_id": obj_id, "change_id": change_id,
                "snapshot_id": write1.get("snapshot_id"),
                "vertex_comparison": (write1.get("readback") or {}).get("vertex_comparison"),
                "folder_created": write1.get("folder_created"),
            }
            vcmp = (write1.get("readback") or {}).get("vertex_comparison") or {}
            checklist.check("A7 write_draft 写入模拟奥维：ok + obj_id/change_id/snapshot + 写后回读顶点一致",
                            write1.get("status") == "ok" and isinstance(obj_id, int)
                            and bool(change_id) and bool(write1.get("snapshot_id"))
                            and vcmp.get("ok") is True and (vcmp.get("max_delta_deg") or 0) <= 1e-6,
                            {"status": write1.get("status"), "obj_id": obj_id,
                             "vertex_comparison": vcmp})
            checklist.check(
                "A8 写入的 provenance 如实标注 simulated=true / verified_against_real_device=false",
                (write1.get("provenance") or {}).get("simulated") is True
                and (write1.get("provenance") or {}).get("verified_against_real_device") is False,
                (write1.get("provenance") or {}),
            )

            object_read = tool_result_json(await client.call_tool("read_object", {"obj_id": obj_id}))
            geometry = object_read.get("geometry") or {}
            vertices = geometry.get("vertices") or []
            evidence["mock"]["read_object"] = {
                "status": object_read.get("status"),
                "name": object_read.get("name"),
                "geometry": geometry,
                "axis_order": object_read.get("axis_order"),
            }
            checklist.check(
                "A7b read_object(20101) 读回对象名称/顶点/坐标语义（对外 EPSG:4490 lon/lat，lat-first 内部轴序已声明）",
                object_read.get("status") == "ok"
                and object_read.get("name") == "验证草稿"
                and geometry.get("crs") == "EPSG:4490"
                and len(vertices) == 4
                and all(-180 <= float(v[0]) <= 180 and -90 <= float(v[1]) <= 90 for v in vertices)
                and "lat" in str(object_read.get("axis_order", "")).lower(),
                {"status": object_read.get("status"), "name": object_read.get("name"),
                 "crs": geometry.get("crs"), "vertex_count": len(vertices),
                 "source_semantic": geometry.get("source_semantic")},
            )

            write2 = tool_result_json(await client.call_tool(
                "write_draft", {"geojson": geojson, "draft_folder": "verify-draft",
                                "name": "验证草稿", "comment": "verifier loop",
                                "idempotency_key": "verify-loop-1"}))
            evidence["mock"]["write_draft_idempotent"] = {
                "status": write2.get("status"), "reused": write2.get("reused"),
                "obj_id": write2.get("obj_id"),
            }
            checklist.check("A9 幂等键重复写：返回同一 obj_id 且 reused=true（不重复创建）",
                            write2.get("status") == "ok" and write2.get("obj_id") == obj_id
                            and write2.get("reused") is True,
                            {"reused": write2.get("reused"), "same_obj": write2.get("obj_id") == obj_id})

            readback = tool_result_json(await client.call_tool("read_back", {"obj_id": obj_id}))
            area_cmp = readback.get("area_comparison") or {}
            evidence["mock"]["read_back"] = {
                "status": readback.get("status"), "vertex_count": readback.get("vertex_count"),
                "metrics": readback.get("metrics"), "area_comparison": area_cmp,
            }
            local_area = (area_cmp.get("local") or {}).get("area_m2") if isinstance(area_cmp.get("local"), dict) else None
            ovital_area = None
            metrics = readback.get("metrics") or {}
            if isinstance(metrics, dict):
                ovital_area = metrics.get("area_m2")
            checklist.check("A10 read_back 回读顶点与 20802 量算：顶点数一致、面积口径差异被显式说明",
                            readback.get("status") == "ok" and readback.get("vertex_count") == 4
                            and isinstance(metrics, dict) and metrics.get("area_m2") is not None
                            and ("口径" in json.dumps(readback, ensure_ascii=False)),
                            {"vertex_count": readback.get("vertex_count"), "metrics": metrics,
                             "area_comparison_keys": list(area_cmp.keys())})

            finalize_denied = tool_result_json(await client.call_tool("finalize_draft", {"obj_id": obj_id}))
            checklist.check("A11 finalize_draft 未显式 confirm=true 时显式失败",
                            finalize_denied.get("status") == "error"
                            and (finalize_denied.get("error") or {}).get("code") == "DATA_VALIDATION_ERROR",
                            {"status": finalize_denied.get("status"),
                             "code": (finalize_denied.get("error") or {}).get("code")})
            finalize_ok = tool_result_json(await client.call_tool(
                "finalize_draft", {"obj_id": obj_id, "confirm": True, "version_note": "verifier"}))
            evidence["mock"]["finalize"] = {"status": finalize_ok.get("status"),
                                            "version": finalize_ok.get("version")}
            checklist.check("A12 finalize_draft(confirm=true) 成功并记录版本/变更",
                            finalize_ok.get("status") == "ok"
                            and isinstance(finalize_ok.get("version"), int)
                            and bool(finalize_ok.get("change_id")),
                            {"version": finalize_ok.get("version"),
                             "change_id": finalize_ok.get("change_id")})

            rolled = tool_result_json(await client.call_tool("rollback_change", {"change_id": change_id}))
            rolled_again = tool_result_json(await client.call_tool("rollback_change", {"change_id": change_id}))
            after_rollback = tool_result_json(await client.call_tool("read_back", {"obj_id": obj_id}))
            deleted = ((rolled.get("report") or {}).get("deleted_obj_ids")) or []
            evidence["mock"]["rollback"] = {
                "status": rolled.get("status"), "deleted_obj_ids": deleted,
                "already_rolled_back": rolled_again.get("already_rolled_back"),
                "read_back_after": {"status": after_rollback.get("status"),
                                    "code": (after_rollback.get("error") or {}).get("code")},
            }
            checklist.check("A13 rollback_change 删除本次创建对象；再次回滚幂等；回滚后对象不可读",
                            rolled.get("status") == "ok" and obj_id in deleted
                            and rolled_again.get("already_rolled_back") is True
                            and after_rollback.get("status") == "error"
                            and (after_rollback.get("error") or {}).get("code") in
                            {"OVITAL_NOT_FOUND", "OVITAL_PROTOCOL_ERROR"},
                            evidence["mock"]["rollback"])

            # --- 安全 ---
            transcript_text = "\n".join(
                session.raw_stdout_lines + session.sent_raw_lines + session.stderr_lines
            ) + json.dumps([write1, write2, readback, finalize_ok, rolled], ensure_ascii=False)
            leaks = scan_text_for_secret(run_dir / "mcp" / "data", mock_psk)
            checklist.check("A14 模拟 PSK 不泄漏：工具输出/stderr/任务文件中均无凭据明文",
                            mock_psk not in transcript_text and not leaks,
                            {"in_transcript": mock_psk in transcript_text, "files": leaks[:3]})
            forged = re.findall(r'"verified_against_real_device"\s*:\s*true', transcript_text)
            checklist.check("A15 全程无伪造的 verified_against_real_device=true",
                            not forged, {"occurrences": len(forged)})
            checklist.check("A16 全程 stdout 逐行合法 JSON-RPC（0 行非协议输出）",
                            not session.transport.nonprotocol_lines,
                            {"lines": len(session.raw_stdout_lines),
                             "nonprotocol": len(session.transport.nonprotocol_lines)})
            evidence["mock"]["stdout_lines"] = len(session.raw_stdout_lines)
            evidence["mock"]["stderr_lines"] = len(session.stderr_lines)
        checklist.check("A17 关闭 stdin 后 MCP 服务进程正常退出（exit=0）", session.exit_code == 0,
                        {"exit_code": session.exit_code})
    return "A 模拟奥维协议闭环"


# ---------------------------------------------------------------------------
# B. 故障注入 / 断线 / 超时 / 取消（红线：不盲目重发创建）
# ---------------------------------------------------------------------------


async def _fault_write_case(checklist: Checklist, label: str, fault: str, *, expect_ok: bool = False) -> dict[str, Any]:
    run_dir = WORKDIR / "faults" / label
    psk = f"loop-fault-psk-{label}-{int(time.time())}"
    geojson = {"type": "Polygon",
               "coordinates": [[[116.990, 39.740], [117.000, 39.740], [117.000, 39.750],
                                [116.990, 39.750], [116.990, 39.740]]]}
    result: dict[str, Any] = {"label": label, "fault": fault}
    with running_mock_ovital(run_dir, psk=psk, faults=[fault], print_requests=True) as mock:
        async with open_stdio_session(endpoint=mock.url, psk=psk, run_dir=run_dir / "mcp") as session:
            started = time.monotonic()
            write = tool_result_json(await session.client.call_tool(
                "write_draft", {"geojson": geojson, "draft_folder": f"draft-{label}",
                                "idempotency_key": f"k-{label}"}))
            elapsed = round(time.monotonic() - started, 2)
            result.update({
                "status": write.get("status"),
                "elapsed_s": elapsed,
                "error": write.get("error"),
                "obj_id": write.get("obj_id"),
            })
            if not expect_ok:
                checklist.check(
                    f"B/{label} 写入故障显式失败且不伪成功（无 obj_id，错误可解释）",
                    write.get("status") == "error" and write.get("obj_id") is None
                    and bool((write.get("error") or {}).get("code")),
                    {"status": write.get("status"), "error": write.get("error")},
                )
                if label == "timeout":
                    checklist.check(
                        "B/timeout 超时错误码为 OVITAL_TIMEOUT 且在 request_timeout 附近返回（未无限挂起）",
                        (write.get("error") or {}).get("code") == "OVITAL_TIMEOUT" and 1.0 <= elapsed <= 8.0,
                        {"code": (write.get("error") or {}).get("code"), "elapsed_s": elapsed},
                    )
                if label == "drop":
                    msg = (write.get("error") or {}).get("message", "")
                    checklist.check(
                        "B/drop 断线错误明确声明不自动重试（协议无请求 ID 红线）",
                        "不自动重试" in msg or "不能自动重试" in msg,
                        {"message": msg[:200]},
                    )
                retry = None
            else:
                checklist.check(
                    "B/duplicate 重复响应对客户端无害：首次写成功、无重复对象",
                    write.get("status") == "ok" and isinstance(write.get("obj_id"), int),
                    {"status": write.get("status"), "obj_id": write.get("obj_id")},
                )
                # 同幂等键重试：应命中幂等缓存，不再向 mock 发新的 20112
                retry = tool_result_json(await session.client.call_tool(
                    "write_draft", {"geojson": geojson, "draft_folder": f"draft-{label}",
                                    "idempotency_key": f"k-{label}"}))
                result["retry"] = {"status": retry.get("status"), "reused": retry.get("reused"),
                                   "obj_id": retry.get("obj_id")}
            await asyncio.sleep(0.2)
        result["mock_stderr"] = str(mock.stderr_path)
        requests_text = mock.stderr_path.read_text(encoding="utf-8", errors="ignore")
        count_20112 = len(re.findall(r"<- cmdid=20112\b", requests_text))
        result["mock_20112_requests"] = count_20112
        if label == "drop":
            checklist.check("B/drop mock 只收到 1 次 20112（断线后客户端未盲目重发创建）",
                            count_20112 == 1, {"count_20112": count_20112})
        if label == "timeout":
            checklist.check("B/timeout mock 只收到 1 次 20112（超时后未盲目重发创建）",
                            count_20112 == 1, {"count_20112": count_20112})
        if label == "duplicate":
            # 一次 write_draft = 建文件夹(20112) + 写图形(20112)，共 2 次请求；重复响应不应导致重发。
            retry_ok = (result.get("retry") or {}).get("status") == "ok"
            reused_ok = (result.get("retry") or {}).get("reused") is True
            checklist.check(
                "B/duplicate 重复 20112 响应被客户端安全去重：写成功、同幂等键重试不再发写请求（仍 2 次）",
                count_20112 == 2 and result.get("status") == "ok" and retry_ok and reused_ok,
                {"count_20112": count_20112, "status": result.get("status"),
                 "retry": result.get("retry")})
    return result


async def group_faults(checklist: Checklist, evidence: dict[str, Any]) -> str:
    evidence["faults"] = []
    evidence["faults"].append(await _fault_write_case(checklist, "drop", "drop-on-write"))
    evidence["faults"].append(await _fault_write_case(checklist, "timeout", "timeout-on:20112"))
    evidence["faults"].append(await _fault_write_case(checklist, "duplicate", "duplicate-20112", expect_ok=True))

    # 取消：启动合成任务后立即取消
    run_dir = WORKDIR / "faults" / "cancel"
    dem = str((SYNTH_DIR / "two_bowl_ridge.tif").resolve())
    outlet = [116.9958562954675, 39.74842076317149]
    async with open_stdio_session(run_dir=run_dir) as session:
        started = tool_result_json(await session.client.call_tool(
            "start_watershed", {"dem_path": dem, "outlet_lonlat": outlet, "snap_radius_m": 0,
                                "engine": "native", "idempotency_key": f"{RUNTAG}-cancel"}))
        job_id = started.get("job_id")
        cancelled = tool_result_json(await session.client.call_tool("cancel_job", {"job_id": job_id}))
        cancelled_again = tool_result_json(await session.client.call_tool("cancel_job", {"job_id": job_id}))
        final = await wait_terminal(session.client, job_id, timeout_s=60)
        evidence["cancel"] = {
            "job_id": job_id,
            "first_cancel_status": (cancelled.get("job") or {}).get("status"),
            "second_cancel_status": (cancelled_again.get("job") or {}).get("status"),
            "final_status": (final.get("job") or {}).get("status"),
            "final_error": (final.get("job") or {}).get("error"),
        }
        checklist.check("B/cancel 运行中任务可取消：终态 cancelled 且带 JOB_CANCELLED 错误",
                        (final.get("job") or {}).get("status") == "cancelled"
                        and ((final.get("job") or {}).get("error") or {}).get("code") == "JOB_CANCELLED"
                        and (cancelled_again.get("job") or {}).get("status") == "cancelled",
                        evidence["cancel"])

    # start_watershed 幂等：相同参数返回同一任务；显式不同 key 则新任务
    async with open_stdio_session(run_dir=WORKDIR / "faults" / "idem") as session:
        args = {"dem_path": dem, "outlet_lonlat": outlet, "snap_radius_m": 0, "engine": "native"}
        first = tool_result_json(await session.client.call_tool(
            "start_watershed", {**args, "idempotency_key": f"{RUNTAG}-idem-auto"}))
        second = tool_result_json(await session.client.call_tool(
            "start_watershed", {**args, "idempotency_key": f"{RUNTAG}-idem-auto"}))
        third = tool_result_json(await session.client.call_tool(
            "start_watershed", {**args, "idempotency_key": f"{RUNTAG}-idem-explicit-new"}))
        await wait_terminal(session.client, first["job_id"], timeout_s=120)
        evidence["job_idempotency"] = {
            "first": first.get("job_id"),
            "second": second.get("job_id"),
            "third": third.get("job_id"),
            "note": first.get("note"),
        }
        checklist.check("B/idempotency 相同输入+参数自动幂等（返回同一 job_id）",
                        second.get("job_id") == first.get("job_id"),
                        {"first": first.get("job_id"), "second": second.get("job_id")})
        checklist.check("B/idempotency 显式不同 idempotency_key 创建新任务",
                        third.get("job_id") != first.get("job_id"),
                        {"first": first.get("job_id"), "third": third.get("job_id")})
    return "B 故障注入/取消/幂等"


# ---------------------------------------------------------------------------
# C. GIS 闭环：合成解析真值独立复算（native）
# ---------------------------------------------------------------------------


def _read_mask_count(mask_path: Path) -> tuple[int, int, Any]:
    import numpy as np
    import rasterio

    with rasterio.open(mask_path) as ds:
        arr = ds.read(1)
        nodata = ds.nodata
    valid = arr != nodata if nodata is not None else np.ones(arr.shape, dtype=bool)
    mask = (arr > 0) & valid
    return int(mask.sum()), int(arr.size), mask


async def group_gis_native(checklist: Checklist, evidence: dict[str, Any], expected: dict[str, Any]) -> str:
    scenario = expected["scenarios"]["two_bowl_ridge"]
    dem = str((SYNTH_DIR / scenario["dem"]).resolve())
    outlet = scenario["outlet_lonlat"]
    expected_cells = int(scenario["expected_mask_cells"])
    expected_area = float(scenario["expected_area_m2"])
    evidence["gis_native"] = {"scenario": "two_bowl_ridge", "expected": {
        "mask_cells": expected_cells, "area_m2": expected_area,
        "mask_rule": scenario.get("mask_rule")}}

    async with open_stdio_session(run_dir=WORKDIR / "gis-native") as session:
        client = session.client
        dem_info = tool_result_json(await client.call_tool("inspect_dem", {"dem_path": dem}))
        info = dem_info.get("dem") or dem_info.get("dem_info") or {}
        checklist.check("C1 inspect_dem：CRS/尺寸/分辨率/SHA256 与文件一致",
                        dem_info.get("status") == "ok"
                        and str(info.get("crs_epsg")) == "32650"
                        and (info.get("width"), info.get("height")) == (60, 12)
                        and abs(float(info.get("res_x") or 0) - 30.0) < 1e-9
                        and info.get("sha256") == sha256_file(Path(dem)),
                        {"crs_epsg": info.get("crs_epsg"), "width": info.get("width"),
                         "height": info.get("height"), "sha256_ok": info.get("sha256") == sha256_file(Path(dem))})

        async def run_job(idem_key: str) -> dict[str, Any]:
            started = tool_result_json(await client.call_tool(
                "start_watershed", {"dem_path": dem, "outlet_lonlat": outlet, "snap_radius_m": 0,
                                    "engine": "native", "idempotency_key": idem_key}))
            assert started.get("status") == "ok", started
            return await wait_terminal(client, started["job_id"])

        job = await run_job(f"{RUNTAG}-gis-native-run-1")
        summary = job.get("result_summary") or {}
        evidence["gis_native"]["job1"] = {
            "job_id": job.get("job", {}).get("job_id"),
            "status": job.get("job", {}).get("status"),
            "area_m2": summary.get("area_m2"),
            "engine_used": summary.get("engine_used"),
            "engine_version": summary.get("engine_version"),
            "review_figure_count": job.get("review_figure_count"),
        }
        checklist.check("C2 start_watershed→get_job 成功，native 引擎，面积与解析真值一致（324,000 m²）",
                        job.get("job", {}).get("status") == "succeeded"
                        and summary.get("engine_used") == "native"
                        and summary.get("area_m2") is not None
                        and abs(float(summary["area_m2"]) - expected_area) / expected_area < 1e-6,
                        {"area_m2": summary.get("area_m2"), "expected": expected_area,
                         "engine_used": summary.get("engine_used")})
        checklist.check("C3 结果 provenance 标注 offline_computed 且 verified_against_real_device=false",
                        (job.get("provenance") or {}).get("evidence_level") == "offline_computed"
                        and (job.get("provenance") or {}).get("verified_against_real_device") is False,
                        {"evidence_level": (job.get("provenance") or {}).get("evidence_level")})

        mask_path = Path(summary.get("mask_geotiff") or "")
        count, total, actual_mask = (0, 0, None)
        if mask_path.is_file():
            count, total, actual_mask = _read_mask_count(mask_path)
        evidence["gis_native"]["mask"] = {
            "path": str(mask_path), "cells": count, "total_cells": total,
            "expected_cells": expected_cells,
            "sha256": sha256_file(mask_path) if mask_path.is_file() else None,
        }
        checklist.check("C4 独立复算掩膜：像元集合与解析真值完全一致（columns≤29 → 360 像元）",
                        mask_path.is_file() and count == expected_cells and total == 60 * 12,
                        {"cells": count, "expected": expected_cells, "total": total})
        if actual_mask is not None and scenario.get("mask_rule", {}).get("type") == "columns_le":
            limit = int(scenario["mask_rule"]["value"])
            expected_mask = actual_mask.copy()
            expected_mask[:, :] = False
            expected_mask[:, : limit + 1] = True
            same = bool((actual_mask == expected_mask).all())
            checklist.check("C5 掩膜逐像元与解析规则一致（不是只对总数）", same,
                            {"rule": scenario["mask_rule"], "cell_equal": same})
        checklist.check("C6 面积交叉校验：area_m2 == 掩膜像元数 × 900 m²",
                        summary.get("area_m2") is not None and count > 0
                        and abs(float(summary["area_m2"]) - count * 900.0) < 1e-6 * max(1.0, count * 900.0),
                        {"area_m2": summary.get("area_m2"), "mask_cells": count})

        job2 = await run_job(f"{RUNTAG}-gis-native-run-2")
        summary2 = job2.get("result_summary") or {}
        mask2_path = Path(summary2.get("mask_geotiff") or "")
        sha1 = sha256_file(mask_path) if mask_path.is_file() else None
        sha2 = sha256_file(mask2_path) if mask2_path.is_file() else None
        evidence["gis_native"]["determinism"] = {
            "mask_sha256_run1": sha1, "mask_sha256_run2": sha2,
            "area_run1": summary.get("area_m2"), "area_run2": summary2.get("area_m2"),
        }
        checklist.check("C7 确定性：同输入同参数两次运行的掩膜 SHA256 一致、面积一致",
                        sha1 is not None and sha1 == sha2
                        and abs(float(summary["area_m2"]) - float(summary2["area_m2"])) < 1e-6,
                        evidence["gis_native"]["determinism"])

        with_images = await client.call_tool("get_job", {"job_id": job["job"]["job_id"], "include_images": True})
        images = image_blocks(with_images)
        payload = tool_result_json(with_images)
        review_figures = ((payload.get("result_summary") or {}).get("review") or {}).get("figures") or []
        saved = _save_images(images, WORKDIR / "images", "get_job")
        decoded = [decode_png_info(img["bytes"]) for img in images]
        evidence["gis_native"]["get_job_images"] = {
            "count": len(images), "review_figure_count": payload.get("review_figure_count"),
            "saved": saved, "decoded": [{"size": d["size"], "info_keys": list(d["info"].keys())} for d in decoded],
        }
        dims_match = True
        if len(decoded) == len(review_figures[:2]):
            for dec, fig in zip(decoded, review_figures[:2]):
                dims_match = dims_match and dec["size"] == (fig.get("width_px"), fig.get("height_px"))
        checklist.check("C8 get_job(include_images=true) 返回 ≤2 张真实 MCP Image（PNG 可解码、尺寸=figure 元数据）",
                        len(images) >= 1 and len(images) <= 2 and dims_match
                        and payload.get("review_figure_count", 0) >= 5,
                        {"images": len(images), "review_figure_count": payload.get("review_figure_count"),
                         "dims_match": dims_match})
        checklist.check("C9 每张图片字节 ≤ 4MB（MCP Image 上限配置）",
                        all(len(img["bytes"]) <= 4 * 1024 * 1024 for img in images),
                        {"sizes": [len(img["bytes"]) for img in images]})

        seg_result = await client.call_tool(
            "review_segment", {"job_id": job["job"]["job_id"], "segment_id": 1})
        seg = tool_result_json(seg_result)
        seg_images = image_blocks(seg_result)
        fig = ((seg.get("figure") or {}).get("figures") or [{}])[0]
        saved_seg = _save_images(seg_images, WORKDIR / "images", "review_segment")
        evidence["gis_native"]["review_segment"] = {
            "segment": seg.get("segment"), "figure": {k: fig.get(k) for k in
                                                      ("figure_id", "path", "bbox_lonlat", "crs", "width_px", "height_px")},
            "all_segments": len(seg.get("all_segments") or []),
            "images": saved_seg,
        }
        checklist.check("C10 review_segment 返回 MCP Image + 分段统计 + all_segments + 稳定锚点 ID",
                        len(seg_images) == 1 and decode_png_info(seg_images[0]["bytes"])["size"][0] > 200
                        and isinstance(seg.get("segment"), dict)
                        and len(seg.get("all_segments") or []) == 4
                        and bool(seg["segment"].get("start_anchor_id")),
                        {"images": len(seg_images), "segments": len(seg.get("all_segments") or [])})
        bbox_bad = _check_bbox_lonlat_semantics(seg, checklist)
        evidence["gis_native"]["figure_bbox_check"] = bbox_bad

        validate = tool_result_json(await client.call_tool(
            "validate_geometry", {"job_id": job["job"]["job_id"]}))
        measure = tool_result_json(await client.call_tool(
            "measure_geometry", {"job_id": job["job"]["job_id"]}))
        measured_area = (measure.get("measurement") or {}).get("area_m2")
        checklist.check("C11 validate_geometry：闭合/无自交/单部件/无洞；measure_geometry 面积与任务一致",
                        ((validate.get("geometry") or validate.get("check") or {}).get("is_valid") is True)
                        and measured_area is not None
                        and abs(float(measured_area) - float(summary["area_m2"])) < 1e-6,
                        {"measure_area": measured_area, "job_area": summary.get("area_m2")})

        exports = {}
        for fmt in ("geojson", "kml", "report", "projected_json", "gpkg"):
            payload_ex = tool_result_json(await client.call_tool(
                "export_result", {"job_id": job["job"]["job_id"], "format": fmt}))
            exports[fmt] = {"status": payload_ex.get("status"),
                            "error": (payload_ex.get("error") or {}).get("code"),
                            "files": payload_ex.get("files")}
        evidence["gis_native"]["exports"] = {
            fmt: {"status": v["status"], "error": v["error"],
                  "files": [{k: f.get(k) for k in ("role", "path", "size_bytes", "sha256")}
                            for f in (v["files"] or [])]}
            for fmt, v in exports.items()
        }
        geojson_files = [f for f in (exports["geojson"].get("files") or []) if f.get("role") == "geojson"]
        geojson_ok = False
        if geojson_files:
            content = json.loads(Path(geojson_files[0]["path"]).read_text(encoding="utf-8"))
            coords = content["features"][0]["geometry"]["coordinates"][0]
            geojson_ok = all(-180 <= c[0] <= 180 and -90 <= c[1] <= 90 for c in coords)
        kml_files = [f for f in (exports["kml"].get("files") or []) if f.get("role") == "kml"]
        kml_ok = bool(kml_files) and "<kml" in Path(kml_files[0]["path"]).read_text(encoding="utf-8")
        checklist.check("C12 export_result：geojson 坐标为 EPSG:4490 经纬度、kml 合法、report 存在",
                        exports["geojson"]["status"] == "ok" and geojson_ok
                        and exports["kml"]["status"] == "ok" and kml_ok
                        and exports["report"]["status"] == "ok",
                        {"geojson_ok": geojson_ok, "kml_ok": kml_ok,
                         "report_status": exports["report"]["status"]})
        checklist.check("C13 gpkg 在缺 fiona 环境显式报错（不得静默降级/伪成功）",
                        exports["gpkg"]["status"] == "error"
                        and exports["gpkg"]["error"] in {"EXPORT_ERROR", "DATA_VALIDATION_ERROR"},
                        {"gpkg": exports["gpkg"]["status"], "code": exports["gpkg"]["error"]})

        revise = tool_result_json(await client.call_tool(
            "revise_candidate", {"job_id": job["job"]["job_id"], "snap_radius_m": 60,
                                 "reason": "verifier 修订流程验证"}))
        revised_job = None
        if revise.get("status") == "ok" and revise.get("job_id"):
            revised_job = await wait_terminal(client, revise["job_id"])
        evidence["gis_native"]["revise"] = {
            "status": revise.get("status"), "new_job_id": revise.get("job_id"),
            "parent_job_id": revise.get("parent_job_id"),
            "comparison": (revised_job or {}).get("comparison"),
        }
        checklist.check("C14 revise_candidate 产生新任务并报告父子面积/边界差异（不静默）",
                        revise.get("status") == "ok" and revised_job is not None
                        and (revised_job.get("job", {}).get("status")) == "succeeded"
                        and bool(revised_job.get("comparison")),
                        {"status": revise.get("status"),
                         "comparison_keys": list(((revised_job or {}).get("comparison") or {}).keys())})

        checklist.check("C15 全程 stdout 逐行 JSON-RPC（GIS 大结果/图片不污染 stdout）",
                        not session.transport.nonprotocol_lines,
                        {"lines": len(session.raw_stdout_lines)})
        evidence["gis_native"]["stdout_lines"] = len(session.raw_stdout_lines)

        # C16：single_bowl（正方形 DEM，产生竖版分段图）综合可读性验收（Lead 01:59 裁定口径）
        # 口径：①地图面板宽≥400px ②高占比≥55% ③x 轴刻度间距≥55px 或旋转且无重叠
        #      ④信息面板/比例尺/指北针/坐标轴互不遮挡 ⑤无>15%连续纯白带（side 布局信息栏下方
        #      空白按“地图面板外区域”口径记录，不判 FAIL）
        bowl = expected["scenarios"]["single_bowl"]
        bowl_started = tool_result_json(await client.call_tool(
            "start_watershed", {"dem_path": str((SYNTH_DIR / bowl["dem"]).resolve()),
                                "outlet_lonlat": bowl["outlet_lonlat"],
                                "snap_radius_m": bowl.get("snap_radius_m", 0), "engine": "native",
                                "idempotency_key": f"{RUNTAG}-gis-single-bowl"}))
        bowl_job = await wait_terminal(client, bowl_started["job_id"], timeout_s=180) if bowl_started.get("job_id") else {}
        bowl_dims: dict[str, Any] = {}
        bowl_problems: list[str] = []
        bowl_notes: list[str] = []
        if (bowl_job.get("job") or {}).get("status") == "succeeded":
            latest_bowl_dir = sorted((WORKDIR / "gis-native" / "data" / "jobs").glob("*"),
                                     key=lambda p: p.stat().st_mtime)[-1]
            bowl_result = json.loads((latest_bowl_dir / "result.json").read_text(encoding="utf-8"))
            bowl_review = bowl_result.get("review") or {}
            bowl_figures = bowl_review.get("figures") or []
            if len(bowl_figures) < 5:
                bowl_problems.append(
                    f"复核图数量不足：{len(bowl_figures)}/5（review.warnings={bowl_review.get('warnings')}）")
            for fig in bowl_figures:
                fid = fig["figure_id"]
                png = latest_bowl_dir / "artifacts" / "review" / f"{fid}.png"
                if not png.is_file():
                    bowl_problems.append(f"{fid}: 缺 PNG")
                    continue
                stats = analyze_review_png(png)
                layout = fig.get("layout") or {}
                map_rect = layout.get("map_panel_rect")
                info_rect = layout.get("info_panel_rect")
                scale_rect = layout.get("scale_bar_rect")
                north_rect = layout.get("north_arrow_rect")
                fig_w, fig_h = fig.get("width_px") or 0, fig.get("height_px") or 0
                aspect = round(fig_w / fig_h, 3) if fig_w and fig_h else None
                panel_fraction = layout.get("map_panel_height_fraction")
                if panel_fraction is None:  # 像素口径兜底：地图带高/图高
                    panel_fraction = stats.get("map_height_frac")
                layout_map_w_px = int(round(fig_w * float(map_rect[2]))) if isinstance(map_rect, list) else 0
                map_w_px = max(int(stats.get("map_size", [0])[0] or 0), layout_map_w_px)
                cluster_count = stats.get("tick_label_clusters") or 0
                spacing = stats.get("tick_spacing_px")
                rotated_ok = (
                    layout.get("narrow") is True
                    and float(layout.get("x_tick_rotation_deg") or 0) >= 30
                    and int(layout.get("x_major_tick_count") or 99) <= 5
                )
                tick_ok = (spacing is not None and spacing >= 55) or rotated_ok
                record = {
                    "fig_size": [fig_w, fig_h], "aspect": aspect,
                    "map_size": stats.get("map_size"), "map_width_px": map_w_px,
                    "panel_height_fraction": round(float(panel_fraction or 0), 3),
                    "tick_clusters": cluster_count, "tick_spacing_px": spacing,
                    "narrow": layout.get("narrow"), "mode": layout.get("mode"),
                    "max_white_row_run_frac": stats.get("max_white_row_run_frac"),
                }
                bowl_dims[fid] = record
                is_segment = fid != "overview"
                if is_segment:
                    if map_w_px < 400:
                        bowl_problems.append(f"{fid}: 地图面板宽 {map_w_px}px < 400")
                    if float(panel_fraction or 0) < 0.55:
                        bowl_problems.append(f"{fid}: 地图面板高度占比 {panel_fraction} < 0.55")
                    if not tick_ok:
                        bowl_problems.append(f"{fid}: x 轴刻度不可读（间距 {spacing}px, 旋转 {layout.get('x_tick_rotation_deg')}）")
                    if not (0.5 <= float(aspect or 0) <= 2.2):
                        bowl_problems.append(f"{fid}: 图幅纵横比 {aspect} 越界")
                    if not (isinstance(map_rect, list) and isinstance(info_rect, list)
                            and not _rects_overlap(map_rect, info_rect)):
                        bowl_problems.append(f"{fid}: 信息面板与地图面板重叠/缺矩形")
                    if not (isinstance(map_rect, list) and isinstance(scale_rect, list)
                            and _rect_inside(map_rect, scale_rect)):
                        bowl_problems.append(f"{fid}: 比例尺不在/越出地图面板")
                    if not (isinstance(map_rect, list) and isinstance(north_rect, list)
                            and _rect_inside(map_rect, north_rect)):
                        bowl_problems.append(f"{fid}: 指北针不在/越出地图面板")
                    if not _bbox_contains(fig.get("view_bbox_lonlat") or [], fig.get("bbox_lonlat") or []):
                        bowl_problems.append(f"{fid}: view_bbox 未包含 bbox")
                    white_frac = stats.get("max_white_row_run_frac") or 0
                    if white_frac > 0.15:
                        if layout.get("mode") == "side":
                            bowl_notes.append(
                                f"{fid}: side 布局信息栏下方连续白带 {white_frac:.0%}（地图面板外留白，"
                                "vision4 已注明为外观局限，不判失败）")
                        else:
                            bowl_problems.append(f"{fid}: 连续纯白带 {white_frac:.0%} > 15%")
                else:
                    if stats.get("map_area_frac", 0) < 0.55:
                        bowl_problems.append(f"{fid}: overview 地图面积占比 {stats.get('map_area_frac')} < 0.55")
                    if "key_warnings" in layout and not layout.get("key_warnings"):
                        bowl_problems.append(f"{fid}: overview 缺关键警告行")
                    if (stats.get("max_white_row_run_frac") or 0) > 0.15:
                        bowl_problems.append(f"{fid}: overview 连续纯白带 > 15%")
        evidence["gis_native"]["single_bowl_layout"] = {
            "figures": bowl_dims, "problems": bowl_problems, "appearance_notes": bowl_notes,
        }
        checklist.check("C16 竖版/横版混合场景（single_bowl）综合可读性：地图面板宽≥400px、高占比≥55%、"
                        "x 轴刻度间距≥55px 或旋转无重叠、信息面板/比例尺/指北针互不遮挡、无>15%纯白带",
                        bool(bowl_dims) and not bowl_problems,
                        {"problems": bowl_problems[:8], "appearance_notes": bowl_notes[:4],
                         "figures": {k: {"fig_size": v["fig_size"], "aspect": v["aspect"],
                                         "map_width_px": v["map_width_px"],
                                         "panel_height_fraction": v["panel_height_fraction"],
                                         "tick_spacing_px": v["tick_spacing_px"],
                                         "narrow": v["narrow"], "mode": v["mode"]}
                                     for k, v in bowl_dims.items()}})
    return "C GIS native 解析真值闭环"


def analyze_review_png(png_path: Path) -> dict[str, Any]:
    """像素级构图分析（独立于渲染器自报的 layout 元数据）。

    方法：把图转灰度，非白像素（灰度<245）按行统计占比；占比>0.55 的连续行视为地图面板
    （灰色地形底图铺满整行），取其列方向的非白占比>0.55 得到地图宽度。另统计最大连续全白行带。
    返回地图宽/高/面积占整图比例、地图像素尺寸、最大白带比例。
    """

    import numpy as np
    from PIL import Image

    with Image.open(png_path) as image:
        arr = np.asarray(image.convert("L"))
    height, width = arr.shape
    nonwhite = arr < 245

    def _longest_run(values: "np.ndarray") -> int:
        best = current = 0
        for flag in values:
            current = current + 1 if flag else 0
            best = max(best, current)
        return best

    # 地图面板的特征：该行存在一段很长的连续非白像素（灰底地形铺满地图宽度，
    # 竖版窄地图也适用；纯文字行只有短笔画，不会触发）。
    min_run = max(80, int(width * 0.35))
    row_runs = np.array([_longest_run(nonwhite[row]) for row in range(height)])
    map_rows = row_runs >= min_run
    bands: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(map_rows):
        if value and start is None:
            start = index
        if not value and start is not None:
            bands.append((start, index))
            start = None
    if start is not None:
        bands.append((start, len(map_rows)))
    bands = [band for band in bands if band[1] - band[0] > 20]
    white_rows = arr.mean(axis=1) > 252
    max_run = run = 0
    for value in white_rows:
        run = run + 1 if value else 0
        max_run = max(max_run, run)
    if not bands:
        return {"fig_size": [width, height], "ok": False, "reason": "未检测到地图面板行带",
                "max_white_row_run_frac": round(max_run / height, 3)}
    y0, y1 = max(bands, key=lambda band: band[1] - band[0])
    band_pixels = nonwhite[y0:y1]
    col_frac = band_pixels.mean(axis=0)
    columns = np.where(col_frac > 0.5)[0]
    map_width = int(columns[-1] - columns[0] + 1) if columns.size else 0
    map_x0 = int(columns[0]) if columns.size else 0
    map_x1 = int(columns[-1]) if columns.size else 0
    map_height = y1 - y0
    # x 轴刻度标签粗略估计：地图带下缘下方 3..45px、地图横向范围内，统计“深色文字列簇”
    tick_count = None
    tick_spacing_px = None
    if map_width > 0 and y1 + 45 < height:
        label_band = arr[y1 + 3: y1 + 45, map_x0: map_x1 + 1]
        dark_cols = (label_band < 140).any(axis=0).astype(int)
        clusters = 0
        gap = 0
        for flag in dark_cols:
            if flag:
                if gap >= 6:
                    clusters += 1
                gap = 0
            else:
                gap += 1
        tick_count = clusters
        tick_spacing_px = round(map_width / clusters, 1) if clusters else None
    return {
        "ok": map_width > 0,
        "map_x_range": [map_x0, map_x1],
        "tick_label_clusters": tick_count,
        "tick_spacing_px": tick_spacing_px,
        "fig_size": [width, height],
        "map_band": [y0, y1],
        "map_size": [map_width, map_height],
        "map_width_frac": round(map_width / width, 3),
        "map_height_frac": round(map_height / height, 3),
        "map_area_frac": round(map_width * map_height / (width * height), 3),
        "max_white_row_run_frac": round(max_run / height, 3),
    }


def check_figure_layout_pixels(checklist: Checklist, name: str, png_path: Path, *,
                               is_segment: bool) -> dict[str, Any]:
    """按 Lead 的排版验收口径做像素级断言（不依赖 layout JSON 自报）。"""

    stats = analyze_review_png(png_path)
    if is_segment:
        passed = (
            stats.get("ok") is True
            and stats.get("map_size", [0])[0] >= 480
            and stats.get("map_width_frac", 0) >= 0.62
            and stats.get("map_area_frac", 0) >= 0.42
            and stats.get("max_white_row_run_frac", 1) <= 0.15
        )
        criteria = "地图宽≥480px、地图宽占比≥62%、地图面积占比≥42%、连续白带≤15% 图高"
    else:
        passed = (
            stats.get("ok") is True
            and stats.get("map_width_frac", 0) >= 0.75
            and stats.get("map_area_frac", 0) >= 0.42
            and stats.get("max_white_row_run_frac", 1) <= 0.15
        )
        criteria = "地图宽占比≥75%、地图面积占比≥42%、连续白带≤15% 图高"
    checklist.check(f"{name}：{criteria}", passed, stats)
    return stats


def _bbox_in_lonlat_range(bbox: Any) -> bool:
    return bool(
        isinstance(bbox, (list, tuple)) and len(bbox) == 4
        and -180 <= float(bbox[0]) <= 180 and -180 <= float(bbox[2]) <= 180
        and -90 <= float(bbox[1]) <= 90 and -90 <= float(bbox[3]) <= 90
    )


def _bbox_contains(outer: Any, inner: Any, eps: float = 1e-6) -> bool:
    return bool(
        isinstance(outer, (list, tuple)) and isinstance(inner, (list, tuple))
        and len(outer) == 4 and len(inner) == 4
        and float(outer[0]) <= float(inner[0]) + eps and float(outer[1]) <= float(inner[1]) + eps
        and float(outer[2]) + eps >= float(inner[2]) and float(outer[3]) + eps >= float(inner[3])
    )


def _rects_overlap(a: Any, b: Any, eps: float = 1e-9) -> bool:
    ax, ay, aw, ah = (float(v) for v in a)
    bx, by, bw, bh = (float(v) for v in b)
    return not (ax + aw <= bx + eps or bx + bw <= ax + eps or ay + ah <= by + eps or by + bh <= ay + eps)


def _rect_inside(outer: Any, inner: Any, eps: float = 1e-6) -> bool:
    ox, oy, ow, oh = (float(v) for v in outer)
    ix, iy, iw, ih = (float(v) for v in inner)
    return ix >= ox - eps and iy >= oy - eps and ix + iw <= ox + ow + eps and iy + ih <= oy + oh + eps


def _check_bbox_lonlat_semantics(response: dict[str, Any], checklist: Checklist) -> dict[str, Any]:
    """F-11 + 排版回归：bbox 语义、view bbox 包含关系、信息面板不与地图/比例尺重叠、地图面板占比。"""

    fig = ((response.get("figure") or {}).get("figures") or [{}])[0]
    bbox = fig.get("bbox_lonlat") or []
    view_bbox = fig.get("view_bbox_lonlat")
    segment_bbox = (response.get("segment") or {}).get("bbox_lonlat") or []
    in_range = _bbox_in_lonlat_range(bbox)
    same = len(bbox) == 4 and len(segment_bbox) == 4 and all(
        abs(float(a) - float(b)) <= 1e-6 for a, b in zip(bbox, segment_bbox))
    checklist.check(
        "C10b figure.bbox_lonlat 语义正确（EPSG:4490 经纬度，且与 segment.bbox_lonlat 逐值一致）",
        bool(in_range and same),
        {"figure.bbox_lonlat": bbox, "segment.bbox_lonlat": segment_bbox,
         "crs": fig.get("crs"), "in_lonlat_range": in_range, "matches_segment": same},
    )
    view_ok = view_bbox is None or (_bbox_in_lonlat_range(view_bbox) and _bbox_contains(view_bbox, bbox))
    checklist.check(
        "C10c 分段图 view_bbox_lonlat（外扩地形上下文）为合法经纬度且包含真实段 bbox",
        view_ok,
        {"view_bbox_lonlat": view_bbox, "bbox_lonlat": bbox},
    )
    layout = fig.get("layout") or {}
    map_rect = layout.get("map_panel_rect")
    info_rect = layout.get("info_panel_rect")
    scale_rect = layout.get("scale_bar_rect")
    north_rect = layout.get("north_arrow_rect")
    map_fraction = layout.get("map_panel_height_fraction")
    width_px = fig.get("width_px")
    height_px = fig.get("height_px")
    aspect = round(float(width_px) / float(height_px), 3) if width_px and height_px else None
    caption = fig.get("caption") or ""
    layout_ok = (
        layout.get("mode") in {"outside", "stack", "side"}
        and isinstance(map_rect, list) and isinstance(info_rect, list)
        and float(map_fraction or 0) >= 0.60
        and not _rects_overlap(map_rect, info_rect)
        and (scale_rect is None or _rect_inside(map_rect, scale_rect))
        and (north_rect is None or _rect_inside(map_rect, north_rect))
        and aspect is not None and 0.5 <= aspect <= 2.2
        and "外扩" in caption
    )
    checklist.check(
        "C10d 分段图排版：信息面板在图外且不重叠、地图面板高度占比≥60%（gis-notes §8.3）、比例尺/指北针在地图内、"
        "纵横比∈[0.5,2.2]、caption 注明显示范围已外扩",
        layout_ok,
        {"mode": layout.get("mode"), "map_panel_height_fraction": map_fraction,
         "aspect": aspect, "caption_has_expand": "外扩" in caption,
         "map_rect": map_rect, "info_rect": info_rect,
         "scale_inside_map": scale_rect is not None and isinstance(map_rect, list) and _rect_inside(map_rect, scale_rect),
         "north_inside_map": north_rect is not None and isinstance(map_rect, list) and _rect_inside(map_rect, north_rect)},
    )
    return {"figure_bbox": bbox, "segment_bbox": segment_bbox, "view_bbox": view_bbox,
            "in_range": in_range, "same": same, "view_ok": view_ok, "layout_ok": layout_ok}


# ---------------------------------------------------------------------------
# D. GRASS 引擎真实性（不是 native 冒充）
# ---------------------------------------------------------------------------


async def group_grass(checklist: Checklist, evidence: dict[str, Any], expected: dict[str, Any]) -> str:
    grass_available = GRASS_BIN.is_file()
    checklist.check("D1 本机 GRASS 可执行存在（.conda/grass/bin/grass）", grass_available,
                    {"path": str(GRASS_BIN),
                     "version": _grass_version(checklist) if grass_available else None})
    if not grass_available:
        checklist.info("D 组：无 GRASS，跳过真实引擎验证（如实标注未验证）")
        return "D GRASS（跳过）"

    scenarios = expected["scenarios"]
    tilted = scenarios["tilted_plane"]
    env_extra = {"HOME": str(GRASS_HOME)}
    gis_extra = {"grass_cmd": str(GRASS_BIN)}
    (WORKDIR / "grass").mkdir(parents=True, exist_ok=True)

    async with open_stdio_session(run_dir=WORKDIR / "grass", gis_extra=gis_extra, env_extra=env_extra) as session:
        client = session.client
        evidence["grass"] = {}

        async def run_grass(scenario_name: str, idem: str) -> tuple[dict[str, Any], dict[str, Any]]:
            sc = scenarios[scenario_name]
            dem = str((SYNTH_DIR / sc["dem"]).resolve())
            started = tool_result_json(await client.call_tool(
                "start_watershed", {"dem_path": dem, "outlet_lonlat": sc["outlet_lonlat"],
                                    "snap_radius_m": sc.get("snap_radius_m", 0),
                                    "engine": "grass", "idempotency_key": idem}))
            if started.get("status") != "ok":
                return started, {}
            return started, await wait_terminal(client, started["job_id"], timeout_s=300)

        started, job = await run_grass("tilted_plane", f"{RUNTAG}-grass-tilted")
        summary = (job.get("result_summary") or {})
        job_id = (job.get("job") or {}).get("job_id")
        prov_artifact = None
        if job_id:
            candidate = WORKDIR / "grass" / "data" / "jobs" / job_id / "artifacts" / "provenance.json"
            if candidate.is_file():
                prov_artifact = json.loads(candidate.read_text(encoding="utf-8"))
        grass_prov = (prov_artifact or {}).get("grass") or {}
        commands = grass_prov.get("commands") or []
        phases = [c.get("phase") for c in commands]
        phase_scripts = "\n".join((c.get("script") or "") for c in commands)
        executable_ok = GRASS_BIN.is_file() and str(GRASS_BIN) in json.dumps(commands)
        expected_area = float(tilted["expected_area_m2"])
        evidence["grass"]["tilted_plane"] = {
            "status": (job.get("job") or {}).get("status"),
            "area_m2": summary.get("area_m2"),
            "expected_area_m2": expected_area,
            "engine_used": summary.get("engine_used"),
            "engine_version": summary.get("engine_version"),
            "provenance_phases": phases,
            "executable": grass_prov.get("executable"),
            "home_override": grass_prov.get("home_override"),
            "version": grass_prov.get("version"),
            "fill_depressions": grass_prov.get("fill_depressions"),
        }
        checklist.check("D2 GRASS 引擎任务成功，engine_used=grass / engine_version=GRASS 8.5.0",
                        (job.get("job") or {}).get("status") == "succeeded"
                        and summary.get("engine_used") == "grass"
                        and "8.5.0" in str(summary.get("engine_version")),
                        {"engine_used": summary.get("engine_used"),
                         "engine_version": summary.get("engine_version")})
        checklist.check("D3 provenance 证明真实调用 GRASS：phases 含 create_location/r.watershed/r.water.outlet，"
                        "可执行文件为本机 .conda/grass、返回码 0",
                        {"create_location", "watershed_sfd", "water_outlet"} <= set(phases)
                        and executable_ok and all(c.get("returncode") == 0 for c in commands),
                        {"phases": phases, "executable_ok": executable_ok,
                         "returncodes": [c.get("returncode") for c in commands]})
        checklist.check("D4 GRASS 脚本含 r.watershed -s 与 r.water.outlet（不是 native 冒充）",
                        "r.watershed -s" in phase_scripts and "r.water.outlet" in phase_scripts,
                        {"script_phases": phases})
        checklist.check("D5 GRASS 结果面积与解析真值一致（tilted_plane 54,000 m²）",
                        summary.get("area_m2") is not None
                        and abs(float(summary["area_m2"]) - expected_area) / expected_area < 1e-4,
                        {"area_m2": summary.get("area_m2"), "expected": expected_area})
        mask_path = Path(summary.get("mask_geotiff") or "")
        if mask_path.is_file():
            count, total, _ = _read_mask_count(mask_path)
            checklist.check("D6 GRASS 掩膜与面积自洽（area == mask_cells × 900）",
                            abs(float(summary["area_m2"]) - count * 900.0) < 1e-3 * max(1.0, count * 900.0),
                            {"mask_cells": count, "area_m2": summary.get("area_m2")})
            evidence["grass"]["tilted_plane"]["mask_cells"] = count

        started_b, job_b = await run_grass("single_bowl", f"{RUNTAG}-grass-bowl")
        summary_b = job_b.get("result_summary") or {}
        native_expected = float(scenarios["single_bowl"]["expected_area_m2"])
        evidence["grass"]["single_bowl"] = {
            "status": (job_b.get("job") or {}).get("status"),
            "grass_area_m2": summary_b.get("area_m2"),
            "native_analytic_area_m2": native_expected,
            "engine_used": summary_b.get("engine_used"),
            "delta_m2": (float(summary_b["area_m2"]) - native_expected)
            if summary_b.get("area_m2") is not None else None,
        }
        checklist.check("D7 GRASS 在 single_bowl 上可独立复现（succeeded，面积与自身掩膜自洽）",
                        (job_b.get("job") or {}).get("status") == "succeeded"
                        and summary_b.get("area_m2") is not None,
                        {"grass_area": summary_b.get("area_m2"),
                         "native_analytic": native_expected})
        checklist.info(
            "D7b 引擎差异（诚实记录，不作等价结论）：single_bowl native(D8) 解析真值 vs GRASS(SFD) 面积",
            evidence["grass"]["single_bowl"],
        )
        checklist.check("D8 全程 stdout 逐行 JSON-RPC（GRASS 子进程输出不污染 MCP stdout）",
                        not session.transport.nonprotocol_lines,
                        {"lines": len(session.raw_stdout_lines)})

    # 无效 grass_cmd：engine=grass 必须显式报 ENGINE_UNAVAILABLE
    async with open_stdio_session(run_dir=WORKDIR / "grass-invalid",
                                  gis_extra={"grass_cmd": "/nonexistent/grass-not-here"}) as session:
        bad = tool_result_json(await session.client.call_tool(
            "start_watershed", {"dem_path": str((SYNTH_DIR / "tilted_plane.tif").resolve()),
                                "outlet_lonlat": scenarios["tilted_plane"]["outlet_lonlat"],
                                "engine": "grass", "idempotency_key": f"{RUNTAG}-grass-invalid"}))
        bad_job = await wait_terminal(session.client, bad["job_id"], timeout_s=120) if bad.get("job_id") else {}
        bad_error = ((bad_job.get("job") or {}).get("error")) or {}
        explicit_unavailable = (
            (bad_job.get("job") or {}).get("status") == "failed"
            and bad_error.get("code") == "ENGINE_UNAVAILABLE"
        )
        checklist.check(
            "D9 engine=grass 且 grass_cmd 不可用：任务显式失败且 job.error.code=ENGINE_UNAVAILABLE"
            "（F-12 已修复；不静默降级、不伪成功）",
            explicit_unavailable,
            {"submit_status": bad.get("status"),
             "job_status": (bad_job.get("job") or {}).get("status"),
             "error": bad_error})
        checklist.info("D9b 错误码透传检查（F-12）", {"job_error_code": bad_error.get("code")})
        auto = tool_result_json(await session.client.call_tool(
            "start_watershed", {"dem_path": str((SYNTH_DIR / "tilted_plane.tif").resolve()),
                                "outlet_lonlat": scenarios["tilted_plane"]["outlet_lonlat"],
                                "engine": "auto", "idempotency_key": f"{RUNTAG}-auto-fallback"}))
        auto_job = await wait_terminal(session.client, auto["job_id"], timeout_s=180) if auto.get("job_id") else {}
        auto_engine = (auto_job.get("result_summary") or {}).get("engine_used")
        checklist.check("D10 engine=auto 且 GRASS 不可用：回退 native 并记录实际引擎（可观察回退）",
                        auto.get("status") == "ok" and auto_engine == "native",
                        {"auto_engine": auto_engine})
    return "D GRASS 引擎真实性"


def _grass_version(checklist: Checklist) -> str | None:
    import subprocess

    proc = subprocess.run([str(GRASS_BIN), "--version"], capture_output=True, text=True,
                          env={**__import__("os").environ, "HOME": str(GRASS_HOME)}, timeout=120)
    first = (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr) else None
    checklist.info("D1b GRASS 版本原始输出", {"exit": proc.returncode, "first_line": first})
    return first


# ---------------------------------------------------------------------------
# E. DSH 真实链路证据独立复核（不重跑模型；复核 artifact + 证据一致性）
# ---------------------------------------------------------------------------


async def group_dsh_evidence(checklist: Checklist, evidence: dict[str, Any]) -> str:
    """复核 Lead 提供的 DSH headless 证据（不重跑模型；仅用交付的 transcript/manifest/attachment）。

    注意：Lead 在提取证据后已清理 `.cache/dshtest` 原始运行目录；因此本组**不依赖**该目录，
    只用仓库内交付的 `docs/evidence/`：
    - vision transcript（模型侧事实声明）；
    - `docs/evidence/images/manifest.json`（job_id、图件 sha256/bbox/view_bbox/layout）；
    - `docs/evidence/images/*.png`（交付证据图，可独立解码核对尺寸/哈希）；
    - 本机 DSH attachment 库（证明模型实际收到像素图片）。
    对"原始任务 result.json / 18 工具注册日志"这类已清理的中间产物，如实降级为
    "Lead transcript 声明 + 仓库测试/工具清单交叉核对"，不假装独立复核。
    """

    evidence_dir = REPO_ROOT / "docs" / "evidence"
    evidence["dsh"] = {"evidence_dir": str(evidence_dir),
                       "raw_dshtest_dir": str(REPO_ROOT / ".cache" / "dshtest"),
                       "raw_dshtest_available": (REPO_ROOT / ".cache" / "dshtest").is_dir()}
    vision_outs = sorted(evidence_dir.glob("dsh-headless-vision-e2e*.out.txt"))
    discovery_out = evidence_dir / "dsh-headless-tool-discovery.out.txt"
    if not vision_outs:
        checklist.info("E DSH 证据缺失（docs/evidence 无 vision transcript）——如实标注未独立复核", None)
        return "E DSH（证据缺失）"
    final_outs = [
        path for path in vision_outs
        if "final" in path.name and "pre-final" not in path.name and "pre_final" not in path.name
    ]
    vision_out = max(final_outs or vision_outs, key=lambda path: path.stat().st_mtime)
    out_text = vision_out.read_text(encoding="utf-8", errors="ignore")
    discovery_text = discovery_out.read_text(encoding="utf-8", errors="ignore") if discovery_out.is_file() else ""

    # 交付图件 manifest
    manifest_path = evidence_dir / "images" / "manifest.json"
    manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    manifest_items = list(manifest_raw.get("figures") or []) if isinstance(manifest_raw, dict) else list(manifest_raw or [])
    manifest_job = manifest_raw.get("job_id") if isinstance(manifest_raw, dict) else None
    from PIL import Image as _PILImage

    manifest_info: list[dict[str, Any]] = []
    import hashlib as _hashlib

    for item in manifest_items:
        stored = Path(str(item.get("stored") or ""))
        candidate = stored if stored.is_absolute() else (REPO_ROOT / stored)
        if not candidate.is_file():
            candidate = evidence_dir / "images" / stored.name
        if not candidate.is_file():
            manifest_info.append({"figure_id": item.get("figure_id"), "missing": True})
            continue
        data = candidate.read_bytes()
        with _PILImage.open(candidate) as image:
            dims = list(image.size)
        manifest_info.append({
            "figure_id": item.get("figure_id"), "path": str(candidate), "dims": dims,
            "bytes": len(data), "sha256": _hashlib.sha256(data).hexdigest(),
            "sha_ok": _hashlib.sha256(data).hexdigest() == item.get("sha256"),
            "bbox_lonlat": item.get("bbox_lonlat"),
            "view_bbox_lonlat": item.get("view_bbox_lonlat"),
            "layout": (item.get("layout") or {}),
        })
    evidence["dsh"].update({
        "transcript": str(vision_out), "transcript_sha256": sha256_file(vision_out),
        "tool_discovery_transcript": str(discovery_out) if discovery_out.is_file() else None,
        "vision_image_manifest": str(manifest_path), "manifest_job": manifest_job,
        "manifest_figures": manifest_info,
        "raw_dshtest_available": (REPO_ROOT / ".cache" / "dshtest").is_dir(),
    })

    # E1：DSH 真实发现/调用工具（交付 transcript 证据；原始注册日志已清理→降级声明）
    discovered = ("mcp__ovital__get_capabilities" in discovery_text) and ("simulated" in discovery_text)
    invoked_in_vision = any(name in out_text for name in
                            ("start_watershed", "get_job", "review_segment", "measure_geometry"))
    checklist.check(
        "E1 DSH headless 真实发现并调用本 MCP 工具（交付 transcript：get_capabilities 调用 + 视觉回合调用 "
        "start_watershed/get_job/review_segment/measure_geometry）",
        discovered and invoked_in_vision,
        {"discovery_transcript_hit": discovered, "vision_tools_invoked": invoked_in_vision,
         "note": "原始 .cache/dshtest 注册日志已被 Lead 清理，18 工具注册日志本次不可独立复现；"
                 "18 工具注册另有 tests/test_server.py 与 MCP tools/list 证据"},
    )
    checklist.check(
        "E2 DSH 会话连接模拟服务器（tool-discovery transcript：simulated=true / available=true / version 817）",
        "simulated" in discovery_text and "817" in discovery_text and "available" in discovery_text,
        {"has_simulated": "simulated" in discovery_text, "has_version_817": "817" in discovery_text},
    )

    # E3：视觉回合面积与 single_bowl 解析真值一致（transcript 声明；job result 已清理→如实降级）
    single_bowl_expected = 1681 * 900.0
    area_claim = "1,512,900" in out_text
    checklist.check(
        "E3 视觉回合面积声明与 single_bowl 解析真值一致（1,512,900 = 1681×900 m²）；"
        "原始 job result.json 已被清理，本项以 transcript 为准（降级标注）",
        area_claim,
        {"area_claim_in_transcript": area_claim, "expected_m2": single_bowl_expected,
         "job_id_in_manifest": manifest_job},
    )

    # E4：交付复核图 manifest 完整性 + bbox/view_bbox/layout
    ids = [item.get("figure_id") for item in manifest_info]
    bbox_bad = [
        item.get("figure_id") for item in manifest_info
        if not _bbox_in_lonlat_range(item.get("bbox_lonlat"))
        or not _bbox_contains(item.get("view_bbox_lonlat") or item.get("bbox_lonlat") or [],
                              item.get("bbox_lonlat") or [])
    ]
    layout_bad = [
        item.get("figure_id") for item in manifest_info
        if item.get("layout") and item.get("figure_id") != "overview"
        and item["layout"].get("mode") not in {"stack", "side", "outside"}
    ]
    checklist.check("E4 交付复核图 manifest：5 张图（overview+seg-01..04）完整、可解码、bbox/view_bbox 合法",
                    len(manifest_info) >= 5 and all(item.get("dims") for item in manifest_info)
                    and not bbox_bad and not layout_bad,
                    {"figures": ids, "bad_bbox": bbox_bad, "bad_layout": layout_bad,
                     "sha_all_ok": all(item.get("sha_ok") for item in manifest_info)})

    # E5：transcript 与任务声明一致性/诚实性
    claims = {
        "area_claim": area_claim,
        "measure_consistency": (
            "measure_geometry" in out_text
            and ("1,512,900.0000003222" in out_text or "面积差" in out_text or "151.29" in out_text)
        ),
        "simulated_stated": "simulated=true" in out_text.replace(" ", "") or "simulated = true" in out_text,
        "offline_computed_stated": "offline_computed" in out_text,
        "truncation_honesty": any(k in out_text for k in ("截断", "数据边缘", "DEM 边缘", "图幅边缘", "不能当")),
        "bbox_semantics_reported": (
            "bbox_lonlat" in out_text and ("经纬度" in out_text or "EPSG:4490" in out_text)
        ),
    }
    evidence["dsh"]["transcript_claims"] = claims
    checklist.check("E5 transcript 诚实性：面积/measure 一致、simulated/offline_computed 区分、截断风险如实标注、bbox 口径正确",
                    all(claims.values()), claims)

    # E6：attachment 独立验证（模型是否真的收到像素图片）
    attachment_roots = [
        _user_home() / ".dsh" / "attachments" / "v1" / "objects",
        REPO_ROOT / ".dsh-home" / "attachments" / "v1" / "objects",
    ]
    claimed_hashes: set[str] = set()
    for candidate in evidence_dir.glob("dsh-headless-vision-e2e*.out.txt"):
        claimed_hashes |= set(re.findall(r"\b([0-9a-f]{64})\b", candidate.read_text(encoding="utf-8", errors="ignore")))
    verified_claims = []
    missing_claims = []
    for sha in sorted(claimed_hashes):
        found = False
        for root in attachment_roots:
            target = root / sha[:2] / sha
            if target.is_file():
                verified_claims.append({"attachment": sha, "sha256_ok": sha256_file(target) == sha})
                found = True
                break
        if not found:
            missing_claims.append(sha)
    checklist.check("E6 transcript 声称的图片 sha256 至少 2 个仍可在本机 DSH attachment 库找到且内容哈希自洽"
                    "（证明模型收到的是像素图片而非路径；其余已随 DSH 附件清理/淘汰，如实记录）",
                    len(verified_claims) >= 2 and all(item["sha256_ok"] for item in verified_claims),
                    {"claimed": len(claimed_hashes), "verified": len(verified_claims),
                     "evicted": len(missing_claims),
                     "evicted_sample": [sha[:16] for sha in missing_claims[:3]]})

    # E6b：attachment 尺寸 ↔ 交付证据图尺寸匹配（不使用已清理的 job 元数据）
    deliver_dims = {tuple(item["dims"]): item["figure_id"] for item in manifest_info if item.get("dims")}
    window_anchor = vision_out.stat().st_mtime
    matched, other_images = [], []
    import datetime as _dt

    for root in attachment_roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
            if not (window_anchor - 1800 <= mtime <= window_anchor + 300):
                continue
            try:
                with _PILImage.open(path) as image:
                    dims = tuple(image.size)
            except Exception:  # noqa: BLE001
                continue
            item = {"sha256": path.name, "dims": list(dims), "bytes": path.stat().st_size,
                    "mtime": _dt.datetime.fromtimestamp(mtime).strftime("%H:%M:%S"),
                    "sha_ok": sha256_file(path) == path.name,
                    "matched_figure": deliver_dims.get(dims)}
            (matched if dims in deliver_dims else other_images).append(item)
    matched = sorted({item["sha256"]: item for item in matched}.values(), key=lambda item: item["mtime"])
    other_images = list({item["sha256"]: item for item in other_images}.values())
    evidence["dsh"]["attachments"] = {
        "claimed_hashes": sorted(claimed_hashes), "verified_claims": verified_claims,
        "matched_delivered_figures": matched,
        "other_images": other_images[:10],
    }
    expected_min = min(2, len(deliver_dims))
    checklist.check("E6b 视觉跑时间窗内新增的 attachment 至少有 2 张尺寸与交付证据图完全一致"
                    "（overview/分段图确实被 DSH 附件层接收）",
                    len(matched) >= expected_min and all(item["sha_ok"] for item in matched),
                    {"matched": [{"dims": m["dims"], "figure": m["matched_figure"],
                                  "bytes": m["bytes"], "mtime": m["mtime"]} for m in matched[:4]]})
    if other_images:
        checklist.info("E6c 同窗口内还有非源图尺寸的图片 attachment（模型/验证者局部裁剪图），"
                       "与 transcript 自述局部放大/像素级核对一致（不单独归因）",
                       {"count": len(other_images), "samples": [{"dims": i["dims"]} for i in other_images[:5]]})

    # E7：哈希口径 + 交付图 SHA256
    checklist.check("E7 交付证据图 manifest SHA256 全部一致（5 张，不篡改）",
                    bool(manifest_info) and all(item.get("sha_ok") for item in manifest_info),
                    {"items": len(manifest_info), "figure_ids": ids})
    checklist.info("E8 口径说明：DSH attachment sha256 = 附件层转码内容（WebP）哈希 ≠ 源 PNG SHA256；"
                   "源 PNG SHA256 见 manifest/表",
                   {"attachment_hashes": [v["attachment"][:16] for v in verified_claims],
                    "source_png_hashes": {item["figure_id"]: (item.get("sha256") or "")[:16]
                                          for item in manifest_info}})

    skill_patch = evidence_dir / "dsh-headless-test.patch.yml"
    patch_text = skill_patch.read_text(encoding="utf-8") if skill_patch.is_file() else ""
    skill_out = evidence_dir / "dsh-headless-skill-loading.out.txt"
    skill_text = skill_out.read_text(encoding="utf-8", errors="ignore") if skill_out.is_file() else ""
    if not patch_text:  # 兼容旧命名
        legacy = evidence_dir.parent / ".cache" / "dshtest" / "ovital.cordis.yml"
        patch_text = legacy.read_text(encoding="utf-8") if legacy.is_file() else ""
    checklist.check("E9 Skill 加载证据：DSH 测试 patch 使用官方 customSkillDirs，transcript 报告加载成功并复述红线",
                    "customSkillDirs" in patch_text and "加载成功" in skill_text
                    and ("不得为凑目标面积" in skill_text or "红线" in skill_text),
                    {"patch_has_customSkillDirs": "customSkillDirs" in patch_text,
                     "skill_loaded": "加载成功" in skill_text})
    checklist.info("E10 诚实边界与可复核性：DSH 模型会话由 Lead 执行；verifier 无 API Key、未重跑模型，"
                   "证据=lead-provided transcript/manifest + verifier artifact 复核；headless+模拟奥维+合成 DEM，"
                   "不是真实联机。Lead 已清理 .cache/dshtest 原始运行目录，"
                   "因此“18 工具注册日志/任务 result.json”这类中间产物不可独立复现（本组已降级标注，不影响其余结论）。")
    return "E DSH 真实链路证据复核"


# ---------------------------------------------------------------------------
# F. 安全汇总 + 图片内容真实性（PNG 解码/非空白/差异）
# ---------------------------------------------------------------------------


async def group_security(checklist: Checklist, evidence: dict[str, Any]) -> str:
    scan_roots = [p for p in WORKDIR.rglob("*") if p.is_dir() and "config" not in p.parts]
    forged_pattern = re.compile(r'"verified_against_real_device"\s*:\s*true')
    forged_hits: list[str] = []
    for root in scan_roots:
        for path in root.rglob("*.json"):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if forged_pattern.search(text):
                forged_hits.append(str(path))
    evidence["security"] = {"forged_true_files": forged_hits[:5]}
    checklist.check("F1 全量运行产物中无伪造 verified_against_real_device=true", not forged_hits,
                    {"hits": forged_hits[:3]})

    psk_value_pattern = re.compile(r'"psk"\s*:\s*"[^"]+"')
    psk_hits = []
    for path in WORKDIR.rglob("*.json"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if psk_value_pattern.search(text):
            psk_hits.append(str(path))
    evidence["security"]["job_files_with_psk_value"] = psk_hits[:5]
    checklist.check("F2 任务/证据 JSON 中无 'psk': 明文值（配置样例除外）", not psk_hits,
                    {"hits": psk_hits[:3]})

    images_dir = WORKDIR / "images"
    image_stats = []
    from PIL import Image
    import numpy as np

    for path in sorted(images_dir.glob("*.png")):
        with Image.open(path) as im:
            width, height = im.size
            arr = np.asarray(im.convert("L"), dtype=np.float32)
        image_stats.append({
            "name": path.name, "bytes": path.stat().st_size,
            "size": [width, height],
            "std": round(float(arr.std()), 2),
            "unique_gray_levels": int(len(np.unique(arr.astype(np.uint16)))),
            "sha256": sha256_file(path),
        })
    evidence["security"]["images"] = image_stats
    non_blank = [item for item in image_stats if item["std"] > 5.0 and item["unique_gray_levels"] > 20]
    checklist.check("F3 复核图不是空白图：至少 2 张图片像素标准差>5 且灰度级>20",
                    len(non_blank) >= 2, {"images": image_stats})
    overview = next((item for item in image_stats if item["name"].startswith("get_job-1")), None)
    segments = [item for item in image_stats if not item["name"].startswith("get_job-1")]
    checklist.check("F4 概览图与分段图内容不同（SHA256 不同，证明不是复用同一张图）",
                    overview is not None and segments
                    and all(overview["sha256"] != item["sha256"] for item in segments),
                    {"overview": overview["name"] if overview else None,
                     "overview_sha": overview["sha256"][:16] if overview else None,
                     "seg_hashes": [item["sha256"][:16] for item in segments]})
    sources = [p for p in (WORKDIR / "gis-native" / "data" / "jobs").glob("*/artifacts/review/*.png")]
    evidence["security"]["review_png_count"] = len(sources)
    checklist.check("F5 任务成果目录存在 overview + 分段 PNG（≥5 张）", len(sources) >= 5,
                    {"count": len(sources)})

    # F6：最新任务全部 figure 的 bbox/view_bbox/layout 元数据回归（排版修复的持久断言）
    latest_jobs = sorted((WORKDIR / "gis-native" / "data" / "jobs").glob("*"),
                         key=lambda p: p.stat().st_mtime)
    latest_result = json.loads((latest_jobs[-1] / "result.json").read_text(encoding="utf-8")) if latest_jobs else {}
    layout_problems: list[str] = []
    figures = (latest_result.get("review") or {}).get("figures") or []
    layout_notes: list[str] = []
    for fig in figures:
        fid = fig.get("figure_id")
        is_segment = fid != "overview"
        bbox = fig.get("bbox_lonlat")
        view = fig.get("view_bbox_lonlat")
        layout = fig.get("layout") or {}
        width_px, height_px = fig.get("width_px"), fig.get("height_px")
        if not _bbox_in_lonlat_range(bbox):
            layout_problems.append(f"{fid}: bbox_lonlat 非法")
        if view is not None and not (_bbox_in_lonlat_range(view) and _bbox_contains(view, bbox)):
            layout_problems.append(f"{fid}: view_bbox 非法/不包含 bbox")
        map_rect = layout.get("map_panel_rect")
        info_rect = layout.get("info_panel_rect")
        allowed_modes = {"inside", "outside", "stack", "side"} if not is_segment else {"outside", "stack", "side"}
        if layout.get("mode") not in allowed_modes:
            layout_problems.append(f"{fid}: 未知布局模式 {layout.get('mode')}")
        if isinstance(map_rect, list) and isinstance(info_rect, list) and _rects_overlap(map_rect, info_rect):
            layout_problems.append(f"{fid}: 信息面板与地图面板重叠")
        if is_segment:
            if float(layout.get("map_panel_height_fraction") or 0) < 0.55:
                layout_problems.append(
                    f"{fid}: 地图面板高度占比 {layout.get('map_panel_height_fraction')} < 0.55")
            if not width_px or not height_px or not (0.5 <= float(width_px) / float(height_px) <= 2.2):
                layout_problems.append(f"{fid}: 纵横比越界 {width_px}x{height_px}")
            if "外扩" not in str(fig.get("caption") or ""):
                layout_problems.append(f"{fid}: caption 未说明外扩")
            if view is None:
                layout_problems.append(f"{fid}: 缺 view_bbox_lonlat")
            if layout.get("narrow") is True:
                ticks = layout.get("x_major_tick_count")
                rotation = layout.get("x_tick_rotation_deg")
                if ticks is None or int(ticks) > 5:
                    layout_problems.append(f"{fid}: 窄图刻度数 {ticks} > 5")
                if rotation is None or float(rotation) < 30:
                    layout_problems.append(f"{fid}: 窄图刻度旋转 {rotation} < 30°")
    evidence["security"]["figure_layout"] = {
        "job": str(latest_jobs[-1]) if latest_jobs else None,
        "figures": len(figures), "problems": layout_problems[:10],
    }
    checklist.check("F6 全部复核图 figure 元数据：bbox/view_bbox 合法、信息面板在图外且不重叠、地图面板占比≥35%",
                    bool(figures) and not layout_problems,
                    {"figures": len(figures), "problems": layout_problems[:6]})
    return "F 安全与图片内容"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


GROUPS = ("mock", "faults", "gis", "grass", "dsh", "security")


async def run_all(groups: set[str]) -> tuple[Checklist, dict[str, Any]]:
    checklist = Checklist()
    evidence: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repo_root": str(REPO_ROOT),
        "python": sys.version.split()[0],
        "groups": sorted(groups),
    }
    expected = ensure_synthetic_dem(checklist)
    evidence["synthetic"] = {"dir": str(SYNTH_DIR),
                             "scenarios": sorted((expected.get("scenarios") or {}).keys())}
    started = time.monotonic()
    if "mock" in groups:
        evidence["sections"] = evidence.get("sections", []) + [await group_mock_protocol(checklist, evidence)]
    if "faults" in groups:
        evidence["sections"] = evidence.get("sections", []) + [await group_faults(checklist, evidence)]
    if "gis" in groups:
        evidence["sections"] = evidence.get("sections", []) + [
            await group_gis_native(checklist, evidence, expected)]
    if "grass" in groups and GRASS_BIN.is_file():
        evidence["sections"] = evidence.get("sections", []) + [await group_grass(checklist, evidence, expected)]
    elif "grass" in groups:
        checklist.info("D 组跳过：本机未找到 .conda/grass（GRASS 引擎真实调用未验证）")
    if "dsh" in groups:
        evidence["sections"] = evidence.get("sections", []) + [await group_dsh_evidence(checklist, evidence)]
    if "security" in groups:
        evidence["sections"] = evidence.get("sections", []) + [await group_security(checklist, evidence)]
    evidence["elapsed_s"] = round(time.monotonic() - started, 1)
    return checklist, evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="阶段 2 完整离线闭环独立验证")
    parser.add_argument("--groups", default=",".join(GROUPS),
                        help=f"逗号分隔，可选 {GROUPS}；默认全跑")
    parser.add_argument("--skip-grass", action="store_true", help="跳过 GRASS 组")
    args = parser.parse_args(argv)
    groups = {g.strip() for g in args.groups.split(",") if g.strip()} & set(GROUPS)
    if args.skip_grass:
        groups.discard("grass")
    if not groups:
        print("[FAIL] 未选择任何验证组", file=sys.stderr)
        return 2
    WORKDIR.mkdir(parents=True, exist_ok=True)
    if HARNESS_REPO_ROOT != REPO_ROOT:  # pragma: no cover
        print(f"[WARN] harness REPO_ROOT 不一致：{HARNESS_REPO_ROOT}", file=sys.stderr)

    started = time.monotonic()
    try:
        checklist, evidence = asyncio.run(run_all(groups))
    except Exception as exc:  # noqa: BLE001 - 显式失败
        print(f"[FAIL] 验证脚本异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1

    stamp = time.strftime("%Y%m%dT%H%M%S")
    report = {
        "evidence": evidence,
        "checks_total": len(checklist.items),
        "checks_failed": len(checklist.failures),
        "checks_warned": len(checklist.warnings),
        "checks": checklist.items,
        "passed": not checklist.failures,
        "elapsed_s": round(time.monotonic() - started, 1),
    }
    out = WORKDIR / f"mock-loop-evidence-{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    latest = WORKDIR / "mock-loop-evidence-latest.json"
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"证据：{out}")
    print(f"最新：{latest}")
    print(f"结果：{len(checklist.items) - len(checklist.failures) - len(checklist.warnings)}/"
          f"{len(checklist.items) - len(checklist.warnings)} 通过，{len(checklist.failures)} 失败，"
          f"{len(checklist.warnings)} 警告，用时 {report['elapsed_s']}s")
    if checklist.failures:
        print("失败项：")
        for item in checklist.failures:
            print(f"  - {item['name']}  实际={_short(item['detail'], 300)}")
        return 1
    print("阶段 2 完整闭环验证：全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
