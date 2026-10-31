"""工具层公共件：统一响应、provenance、错误转换、几何/图片读取。

约定（docs/architecture.md §6）：
- 每个工具返回的 dict 至少包含 ``status`` / ``tool`` / ``warnings`` / ``provenance``；
- 领域错误转成 ``status="error"`` 的结构化返回，并带稳定 ``code``；
- 不把异常伪装成空数据或成功；错误信息不含凭据。
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import inspect
import json
import logging
import math
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..app import AppContext
from ..errors import OmapError

logger = logging.getLogger("ovital_mcp.tools")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def jsonable(value: Any) -> Any:
    """把 dataclass / Path / numpy / tuple 等转成 JSON 友好结构。"""

    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return jsonable(value.item())
        if isinstance(value, np.ndarray):
            return value.tolist()
    except ImportError:  # pragma: no cover
        pass
    return str(value)


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------

MODE_SIMULATED = "offline_simulated"
MODE_REAL_DEVICE = "real_device"
MODE_OFFLINE_COMPUTE = "offline_computed"
MODE_DOCUMENT = "official_document"


def provenance(
    mode: str,
    *,
    verified_against_real_device: bool = False,
    simulated: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "server": "ovital-watershed-mcp",
        "server_version": __version__,
        "mode": mode,
        "evidence_level": mode,
        "simulated": simulated,
        "verified_against_real_device": verified_against_real_device,
        "generated_at": utc_now(),
        "policy": (
            "本机无 Windows 奥维客户端；凡未经真实设备读写验证的结果，"
            "verified_against_real_device 恒为 false，不得表述为工程验收。"
        ),
    }
    if extra:
        data["extra"] = jsonable(extra)
    return data


def effective_real_device_verified(ctx: AppContext, *, simulated: bool | None) -> bool:
    """是否可声称"已在真实设备验证"。

    只有同时满足：①连接可用；②不是模拟服务器；③运维显式设置
    ``ovital.real_device_verified=true``，才返回 True。程序不得自行推断。
    """

    if simulated is not False:
        return False
    return bool(getattr(ctx.config.ovital, "real_device_verified", False))


def ovital_provenance(ctx: AppContext, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """奥维操作 provenance。

    注意：连接到"非模拟服务器"不等于完成真实设备验证。本构建中
    ``verified_against_real_device`` 只在运维人员显式设置
    ``ovital.real_device_verified: true`` 后才为 true；否则恒 false。
    """

    simulated = ctx.is_simulated_connection()
    manually_verified = effective_real_device_verified(ctx, simulated=simulated)
    return provenance(
        MODE_SIMULATED if simulated else MODE_REAL_DEVICE,
        verified_against_real_device=manually_verified,
        simulated=bool(simulated),
        extra=extra,
    )


def compute_provenance(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return provenance(MODE_OFFLINE_COMPUTE, extra=extra)


# ---------------------------------------------------------------------------
# 统一响应
# ---------------------------------------------------------------------------


def ok(tool: str, payload: dict[str, Any] | None = None, *, warnings: list[str] | None = None,
       prov: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "status": "ok",
        "tool": tool,
        "warnings": warnings or [],
        "provenance": prov or provenance(MODE_DOCUMENT),
    }
    if payload:
        response.update(jsonable(payload))
    return response


def error(
    tool: str,
    exc: OmapError | Exception,
    *,
    warnings: list[str] | None = None,
    prov: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(exc, OmapError):
        err = exc.to_dict()
    else:
        err = {
            "code": "INTERNAL_ERROR",
            "message": f"{type(exc).__name__}: {exc}",
            "details": {"traceback_tail": traceback.format_exc().strip().splitlines()[-8:]},
            "retryable": False,
        }
    if details:
        err["details"] = {**(err.get("details") or {}), "context": jsonable(details)}
    return {
        "status": "error",
        "tool": tool,
        "error": err,
        "warnings": warnings or [],
        "provenance": prov or provenance(MODE_DOCUMENT),
    }


def partial(
    tool: str,
    payload: dict[str, Any] | None = None,
    *,
    warnings: list[str],
    prov: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = ok(tool, payload, warnings=warnings, prov=prov)
    response["status"] = "partial"
    return response


# ---------------------------------------------------------------------------
# 工具装饰器
# ---------------------------------------------------------------------------


def guarded(tool: str, ctx: AppContext) -> Callable[[Callable], Callable]:
    """统一捕获领域异常/未预期异常，返回结构化 error；同步/异步均支持。"""

    def decorator(fn: Callable) -> Callable:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - 统一出口
                    logger.warning("工具 %s 失败：%s", tool, exc, exc_info=not isinstance(exc, OmapError))
                    return error(tool, exc, prov=provenance(MODE_DOCUMENT))

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.warning("工具 %s 失败：%s", tool, exc, exc_info=not isinstance(exc, OmapError))
                return error(tool, exc, prov=provenance(MODE_DOCUMENT))

        return sync_wrapper

    return decorator


# ---------------------------------------------------------------------------
# 几何 / 结果读取
# ---------------------------------------------------------------------------


def parse_geojson_input(value: Any) -> dict[str, Any]:
    """接受 GeoJSON 字符串、dict、文件路径（.geojson/.json）。"""

    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{"):
            return json.loads(text)
        path = Path(text)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        raise ValueError("geojson 参数既不是 JSON 文本，也不是存在的文件路径")
    raise ValueError(f"无法解析 geojson 参数类型：{type(value)!r}")


def geometry_from_geojson(value: Any):
    from shapely.geometry import shape  # 延迟导入

    data = parse_geojson_input(value)
    if data.get("type") == "FeatureCollection":
        features = data.get("features") or []
        if len(features) != 1:
            raise ValueError(f"期望单个要素，收到 {len(features)} 个")
        return shape(features[0]["geometry"])
    if data.get("type") == "Feature":
        return shape(data["geometry"])
    return shape(data)


def load_job_result(ctx: AppContext, job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回 ``(job_record_dict, engine_result_dict)``。

    worker 写出的 ``result.json`` 外层是任务包装（ok/review/...），内层 ``result``
    才是 GIS 引擎成果；这里统一返回内层，调用方不要再解包一次。
    终态非 succeeded 时抛错（不把失败伪装成空成果）。
    """

    record = ctx.jobs.load(job_id)
    if record.status != "succeeded":
        raise OmapError(
            f"任务 {job_id} 尚未成功完成（status={record.status}），不能读取成果",
            details={"job_id": job_id, "status": record.status, "error": record.error},
        )
    outer = ctx.jobs.result(job_id) or {}
    if not outer.get("ok"):
        raise OmapError(f"任务 {job_id} 的 result 未标记成功", details={"job_id": job_id})
    inner = outer.get("result")
    if not isinstance(inner, dict):
        raise OmapError(
            f"任务 {job_id} 的成果结构异常（缺少内层 result 对象）",
            details={"job_id": job_id, "keys": sorted(outer.keys())},
        )
    return record.to_dict(), inner


def boundary_from_job(ctx: AppContext, job_id: str) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """载入任务边界：返回 (shapely 几何 EPSG:4490, result_dict, job_dict)。"""

    job_dict, inner = load_job_result(ctx, job_id)
    boundary_path = inner.get("boundary_geojson")
    if not boundary_path:
        raise OmapError(f"任务 {job_id} 没有 boundary_geojson 成果", details={"job_id": job_id})
    geom = geometry_from_geojson(boundary_path)
    return geom, inner, job_dict


# ---------------------------------------------------------------------------
# 图片
# ---------------------------------------------------------------------------


def read_image(path: str | Path, *, max_bytes: int) -> tuple[bytes, str]:
    from ..errors import ReviewFigureError

    target = Path(path)
    if not target.is_file():
        raise ReviewFigureError(f"图片不存在：{target}")
    data = target.read_bytes()
    if len(data) > max_bytes:
        raise ReviewFigureError(
            f"图片 {target.name} 超过 MCP 返回上限 {max_bytes} 字节（实际 {len(data)}）",
            details={"path": str(target), "size_bytes": len(data)},
        )
    if not data.startswith(b"\x89PNG"):
        raise ReviewFigureError(f"{target} 不是 PNG 文件")
    return data, "image/png"


def image_object(path: str | Path, *, max_bytes: int):
    """返回 MCP ``Image`` 内容块（延迟导入，避免启动时加载 mcp 之外依赖）。"""

    from mcp.server.mcpserver import Image

    data, fmt = read_image(path, max_bytes=max_bytes)
    return Image(data=data, format=fmt.split("/")[-1])


# ---------------------------------------------------------------------------
# 杂项
# ---------------------------------------------------------------------------

OBJECT_TYPE_NAMES: dict[int, str] = {
    7: "标签",
    8: "轨迹",
    11: "导航线路",
    13: "图形",
    30: "文件夹",
}

RET_ERROR_NAMES: dict[int, str] = {
    -1: "没有找到奥维地图程序窗口",
    -2: "向奥维地图程序注册失败",
    -3: "奥维地图程序版本太低",
    -4: "参数错误",
    -5: "当前奥维地图绑定用户不是VIP用户，不支持此API接口",
    -6: "消息处理失败",
    -7: "当前奥维地图绑定用户不是SVIP，不支持此API接口",
}


def type_name(type_id: int | None) -> str:
    if type_id is None:
        return "未知"
    return OBJECT_TYPE_NAMES.get(int(type_id), f"未知类型({type_id})")


def ret_name(ret: int | None) -> str:
    if ret is None:
        return "未知"
    return RET_ERROR_NAMES.get(int(ret), f"ret={ret}")


def bbox_list(value: Any) -> list[float]:
    """校验并规范化 bbox_lonlat=[minx,miny,maxx,maxy]。"""

    if isinstance(value, str):
        parts = [p for p in value.replace("，", ",").split(",") if p.strip()]
        value = [float(p) for p in parts]
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("bbox_lonlat 必须是 [minx,miny,maxx,maxy]")
    minx, miny, maxx, maxy = (float(v) for v in value)
    if not (-180 <= minx < maxx <= 180 and -90 <= miny < maxy <= 90):
        raise ValueError(f"bbox_lonlat 数值非法：{[minx, miny, maxx, maxy]}")
    return [minx, miny, maxx, maxy]


def run_sync_in_thread(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """把重 CPU/阻塞函数放到线程，避免阻塞事件循环（工具内使用）。"""

    async def _runner() -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    return _runner()
