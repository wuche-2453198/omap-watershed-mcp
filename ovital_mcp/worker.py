"""GIS 后台 worker：由 ``JobManager`` 以独立子进程启动。

用法::

    python -m ovital_mcp.worker <job_dir>

职责：
1. 读取 ``<job_dir>/job.json`` 中的计算参数；
2. 调用确定性 GIS 引擎（``ovital_mcp.gis.engine``）计算集水区；
3. 生成复核图（若 GIS 复核模块可用）；
4. 把结构化结果写到 ``result.json``，并更新 ``job.json`` 的终态。

本进程只做 GIS 计算，不需要也不读取奥维凭据。
"""

from __future__ import annotations

import dataclasses
import math
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

from .jobs import JobManager
from .store import atomic_write_json, utc_now


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return _jsonable(value.item())
        if isinstance(value, np.ndarray):
            return value.tolist()
    except ImportError:  # pragma: no cover
        pass
    return str(value)


def write_result(job_dir: Path, payload: dict[str, Any]) -> Path:
    path = job_dir / "result.json"
    atomic_write_json(path, _jsonable(payload))
    return path


# ---------------------------------------------------------------------------
# 心跳
# ---------------------------------------------------------------------------


class Heartbeat(threading.Thread):
    def __init__(self, job_dir: Path, interval_s: float = 5.0) -> None:
        super().__init__(name="ovital-job-heartbeat", daemon=True)
        self.job_dir = job_dir
        self.interval_s = interval_s
        self._stop = threading.Event()
        self.stage = "starting"
        self.message = "worker 启动中"
        self.counter = 0

    def run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self.counter += 1
            try:
                current = JobManager.read_job_file(self.job_dir)
                if current.get("status") in {"succeeded", "failed", "cancelled"}:
                    return
                JobManager.update_job_file(
                    self.job_dir,
                    progress={
                        "stage": self.stage,
                        "message": self.message,
                        "heartbeats": self.counter,
                    },
                )
            except Exception:  # noqa: BLE001 - 心跳失败不应掩盖主任务错误
                continue

    def set_stage(self, stage: str, message: str) -> None:
        self.stage = stage
        self.message = message
        try:
            JobManager.update_job_file(
                self.job_dir,
                progress={"stage": stage, "message": message, "heartbeats": self.counter},
            )
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _build_watershed_params(job: dict[str, Any]):
    """把 job.json 中的参数转成 ``WatershedParams``。"""

    from .gis.engine import WatershedParams  # 延迟导入：缺依赖时给出清晰错误

    params = dict(job.get("params") or {})
    dem_path = params.get("dem_path")
    if not dem_path:
        raise ValueError("job.params.dem_path 缺失")
    outlet = params.get("outlet_lonlat")
    if not outlet:
        raise ValueError("job.params.outlet_lonlat 缺失")
    if isinstance(outlet, dict):
        outlet = (float(outlet["lon"]), float(outlet["lat"]))
    else:
        outlet = (float(outlet[0]), float(outlet[1]))
    return WatershedParams(
        dem_path=str(dem_path),
        outlet_lonlat=outlet,
        engine=str(params.get("engine") or "auto"),
        snap_radius_m=float(params.get("snap_radius_m") or 90.0),
        metric_crs=params.get("metric_crs"),
        target_res_m=(float(params["target_res_m"]) if params.get("target_res_m") else None),
        min_accum_cells=int(params.get("min_accum_cells") or 3),
        simplify_tolerance_m=float(params.get("simplify_tolerance_m") or 0.0),
        extra={
            "grass_cmd": params.get("grass_cmd") or "grass",
            "max_cells": params.get("max_cells"),
            "grass_home": os.environ.get("OVITAL_MCP_GRASS_HOME"),
        },
    )


def _render_review(result: Any, job: dict[str, Any], artifacts_dir: Path) -> dict[str, Any]:
    """生成复核图包；GIS 复核模块不可用时显式记录警告而不是伪造图片。"""

    # review 包自身会在 out_dir 下建立 review/ 子目录（engine 概览图也写在这里），
    # 因此这里传 artifacts_dir，最终清单位于 artifacts/review/manifest.json。
    max_pixels = int((job.get("params") or {}).get("review_max_pixels") or 1600)
    try:
        from .gis.review import render_review_pack
    except Exception as exc:  # noqa: BLE001 - 复核模块缺失/导入失败
        return {
            "ok": False,
            "warnings": [f"复核图模块不可用：{type(exc).__name__}: {exc}"],
            "figures": [],
        }
    try:
        manifest = render_review_pack(result, artifacts_dir, max_pixels=max_pixels)
        return _jsonable(manifest)
    except Exception as exc:  # noqa: BLE001 - 复核失败不应让计算成果丢失
        return {
            "ok": False,
            "warnings": [f"复核图生成失败：{type(exc).__name__}: {exc}"],
            "figures": [],
        }


def _check_cancel(job_dir: Path) -> bool:
    return bool(JobManager.read_job_file(job_dir).get("cancel_requested"))


def run_job(job_dir: Path) -> int:
    job = JobManager.read_job_file(job_dir)
    if not job:
        print(f"[worker] job.json 缺失：{job_dir}", file=sys.stderr)
        return 2
    if job.get("cancel_requested"):
        JobManager.update_job_file(
            job_dir,
            status="cancelled",
            error={"code": "JOB_CANCELLED", "message": "任务在启动前被取消"},
        )
        return 0
    if job.get("status") in {"succeeded", "failed", "cancelled"}:
        return 0

    heartbeat = Heartbeat(job_dir)
    heartbeat.start()
    try:
        JobManager.update_job_file(
            job_dir,
            status="running",
            progress={"stage": "starting", "message": "worker 已接手任务"},
        )
        heartbeat.set_stage("parsing_params", "解析任务参数")
        params = _build_watershed_params(job)
        if _check_cancel(job_dir):
            raise _Cancelled()

        artifacts_dir = job_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        heartbeat.set_stage(
            "dem_inspect",
            f"检查 DEM 与出口（engine={params.engine}，snap_radius={params.snap_radius_m} m）",
        )

        from .gis.engine import run_watershed

        heartbeat.set_stage("computing", "计算流向/汇流/集水区（确定性引擎）")
        result = run_watershed(params, artifacts_dir)
        if _check_cancel(job_dir):
            raise _Cancelled()

        result_dict = _jsonable(result)
        if isinstance(result_dict, dict):
            result_dict.pop("_raw", None)

        heartbeat.set_stage("rendering_review", "生成概览与分段复核图")
        review = _render_review(result, job, artifacts_dir)

        ok = bool(getattr(result, "ok", False))
        payload = {
            "ok": ok,
            "job_id": job.get("job_id"),
            "kind": job.get("kind", "watershed"),
            "task_id": job.get("task_id"),
            "result": result_dict,
            "review": review,
            "finished_at": utc_now(),
            "warnings": list(getattr(result, "warnings", []) or []),
            "simulated": bool(job.get("simulated")),
            "verified_against_real_device": bool(job.get("verified_against_real_device")),
        }
        error = None if ok else {
            # 保留引擎给出的具体错误码（如 ENGINE_UNAVAILABLE），避免把
            # “GRASS 未安装”这类可恢复问题退化成笼统的 ENGINE_FAILED。
            "code": getattr(result, "error_code", None) or "ENGINE_FAILED",
            "message": str(getattr(result, "error", None) or "GIS 引擎返回失败"),
        }
        write_result(job_dir, payload)
        JobManager.update_job_file(
            job_dir,
            status="succeeded" if ok else "failed",
            error=error,
            result={
                "ok": ok,
                "boundary_geojson": result_dict.get("boundary_geojson"),
                "mask_geotiff": result_dict.get("mask_geotiff"),
                "area_m2": result_dict.get("area_m2"),
                "engine_used": result_dict.get("engine_used"),
                "engine_version": result_dict.get("engine_version"),
                "warnings": result_dict.get("warnings"),
                "review_figures": [
                    {
                        "figure_id": fig.get("figure_id"),
                        "path": fig.get("path"),
                        "title": fig.get("title"),
                        "segment_id": fig.get("segment_id"),
                    }
                    for fig in (review.get("figures") or [])
                ],
            },
            progress={
                "stage": "done" if ok else "failed",
                "message": "计算完成" if ok else "计算失败",
            },
        )
        return 0 if ok else 1
    except _Cancelled:
        write_result(job_dir, {"ok": False, "error": {"code": "JOB_CANCELLED", "message": "任务被取消"}})
        JobManager.update_job_file(
            job_dir,
            status="cancelled",
            error={"code": "JOB_CANCELLED", "message": "任务被取消"},
            progress={"stage": "cancelled", "message": "任务被取消"},
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - 顶层兜底：任何异常都落终态
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        error = {
            "code": getattr(exc, "code", "WORKER_ERROR"),
            "message": f"{type(exc).__name__}: {exc}",
            "traceback_tail": tb.strip().splitlines()[-20:],
        }
        write_result(
            job_dir,
            {
                "ok": False,
                "error": error,
                "finished_at": utc_now(),
                "verified_against_real_device": bool(job.get("verified_against_real_device")),
                "simulated": bool(job.get("simulated")),
            },
        )
        JobManager.update_job_file(
            job_dir,
            status="failed",
            error=error,
            progress={"stage": "failed", "message": error["message"]},
        )
        return 1
    finally:
        heartbeat.stop()


class _Cancelled(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("用法：python -m ovital_mcp.worker <job_dir>", file=sys.stderr)
        return 2
    job_dir = Path(args[0]).resolve()
    return run_job(job_dir)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
