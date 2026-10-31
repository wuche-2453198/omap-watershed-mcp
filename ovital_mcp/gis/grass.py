"""GRASS GIS 引擎适配器（GRASS 8.x，SFD/D8）。

对应契约 ``docs/architecture.md`` §5.2 的 grass 部分。实测配方（macOS +
conda-forge GRASS 8.5.0）::

    grass -c <dem.tif> -e <gisdbase>/<location>          # 建临时 location
    grass <location>/PERMANENT --exec bash <script.sh>    # 执行模块脚本

脚本内命令::

    r.in.gdal input=<dem.tif> output=dem --overwrite
    g.region raster=dem -a
    r.watershed -s elevation=dem accumulation=acc drainage=dir --overwrite
    r.out.gdal -f input=acc output=<acc.tif> type=Int32 --overwrite
    r.out.gdal -f input=dir output=<dir.tif> type=Int16 --overwrite
    r.water.outlet input=dir output=basin coordinates=<east>,<north> --overwrite
    r.to.vect input=basin output=basin_vect type=area --overwrite
    v.out.ogr input=basin_vect output=<boundary.geojson> format=GeoJSON type=area --overwrite
    r.out.gdal -f input=basin output=<basin.tif> type=Byte --overwrite

本机兼容坑（已实测）：

- GRASS 默认配置目录是 ``~/Library/GRASS/GRASS8``（macOS）/``~/.grass8``；
  沙箱或只读 HOME 下会报 "Failed to create configuration directory"。
  必须给子进程显式设置可写的 ``HOME``（本项目用 ``<repo>/.cache/grasshome``），
  并额外设置 ``GRASS_CONFIG_DIR`` 兜底。
- **路径不能含空格**：GRASS 命令行/脚本解析对空格敏感（本项目路径含
  "omap mcp"），因此所有 GRASS 输入输出都放在无空格的临时暂存目录里，
  算完再拷回任务目录。
- ``r.out.gdal`` 从 DCELL 导出 Int32/Int16 会被精度检查拒绝，需要 ``-f``。
- GRASS ``accumulation`` 对"可能来自分析范围外"的像元给**负值**（绝对值是
  保守估计）；``drainage`` 方向编码 1=NE,2=N,3=NW,4=W,5=SW,6=S,7=SE,8=E，
  -1=洼地、其他负值=流向分析范围外。这些编码都会写入 provenance。
- ``r.watershed`` 用最小代价算法，**不要求预先填洼**（与 native 的"不填洼"
  一致）；NoData 空洞不会被填补，需要显式警告。
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.warp import Resampling, reproject
from shapely.geometry import shape

from ..errors import EngineError, EngineUnavailableError
from . import engine as engine_module
from . import geometry as geometry_module
from .crs import transform_xy_to_lonlat
from .native import build_stream_network, snap_outlet, write_single_band

#: GRASS ``r.watershed`` 的 SFD 方向编码 -> (行偏移, 列偏移)。
#: 1=NE, 2=N, 3=NW, 4=W, 5=SW, 6=S, 7=SE, 8=E（已用倾斜平面实测验证）。
GRASS_DIRECTION_OFFSETS: dict[int, tuple[int, int]] = {
    1: (-1, 1),
    2: (-1, 0),
    3: (-1, -1),
    4: (0, -1),
    5: (1, -1),
    6: (1, 0),
    7: (1, 1),
    8: (0, 1),
}

#: 子进程 stderr 保留长度（GRASS 很吵，只留摘要）。
_STDERR_TAIL_CHARS = 4000
#: 默认单阶段超时（秒）。
DEFAULT_GRASS_TIMEOUT_S = 1800.0

#: 可用性检测缓存：resolved cmd -> (ok, reason, version)。
_AVAIL_CACHE: dict[str, tuple[bool, str, str | None]] = {}


# ---------------------------------------------------------------------------
# 可执行文件解析与环境
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def grass_home_dir() -> Path:
    """GRASS 用户配置目录（必须可写；本机默认 ``~/Library/GRASS`` 不可写）。"""

    path = _repo_root() / ".cache" / "grasshome"
    path.mkdir(parents=True, exist_ok=True)
    (path / "grassconfig").mkdir(parents=True, exist_ok=True)
    return path


def grass_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """构造 GRASS 子进程环境：显式可写 HOME + GRASS_CONFIG_DIR。"""

    env = dict(base if base is not None else os.environ)
    home = grass_home_dir()
    env["HOME"] = str(home)
    env["GRASS_CONFIG_DIR"] = str(home / "grassconfig")
    env.setdefault("GRASS_PYTHON", str(Path(os.sys.executable)))
    return env


def resolve_grass_executable(grass_cmd: str | None = None) -> tuple[str | None, list[str]]:
    """按优先级解析 GRASS 可执行文件；返回 ``(path|None, tried)``。"""

    tried: list[str] = []
    candidates: list[str] = []
    # 显式配置的 grass_cmd 是**权威**的：找不到就报不可用，绝不悄悄回退到
    # PATH 里的其它 GRASS（否则用户以为指定了 A，实际跑的是 B）。
    if grass_cmd:
        candidates = [grass_cmd]
    elif os.environ.get("OVITAL_GRASS_CMD"):
        candidates = [str(os.environ["OVITAL_GRASS_CMD"])]
    else:
        candidates = ["grass"]
        repo_candidate = _repo_root() / ".conda" / "grass" / "bin" / "grass"
        candidates.append(str(repo_candidate))
    for candidate in candidates:
        tried.append(candidate)
        if os.path.sep in candidate or candidate.startswith("."):
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path.resolve()), tried
            continue
        found = shutil.which(candidate)
        if found:
            return found, tried
    return None, tried


def detect_grass_version(executable: str, *, timeout_s: float = 120.0) -> str:
    """运行 ``grass --version`` 并返回首行（例如 ``GRASS 8.5.0``）。"""

    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=grass_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EngineUnavailableError(f"无法执行 {executable} --version：{exc}") from exc
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        reason = tail[-1] if tail else f"退出码 {result.returncode}"
        raise EngineUnavailableError(f"GRASS 不可用（{executable}）：{reason}")
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise EngineUnavailableError(f"GRASS {executable} --version 没有输出")
    return lines[0]


def reset_availability_cache() -> None:
    """清空可用性缓存（测试/配置变更后调用）。"""

    _AVAIL_CACHE.clear()


def _availability(grass_cmd: str | None = None) -> tuple[bool, str, str | None, str | None]:
    """检测 GRASS 是否可用；返回 ``(ok, reason, executable, version)``。"""

    executable, tried = resolve_grass_executable(grass_cmd)
    if executable is None:
        return (
            False,
            "未找到 GRASS 可执行文件（尝试过：" + ", ".join(tried) + "）；"
            "可用 OVITAL_GRASS_CMD 或 extra.grass_cmd 指定",
            None,
            None,
        )
    cached = _AVAIL_CACHE.get(executable)
    if cached is not None:
        ok, reason, version = cached
        return ok, reason, executable, version
    try:
        version = detect_grass_version(executable)
    except EngineUnavailableError as exc:
        _AVAIL_CACHE[executable] = (False, str(exc), None)
        return False, str(exc), executable, None
    reason = f"GRASS 可用：{version}（{executable}）"
    _AVAIL_CACHE[executable] = (True, reason, version)
    return True, reason, executable, version


# ---------------------------------------------------------------------------
# 子进程 / 脚本工具
# ---------------------------------------------------------------------------


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_s: float,
    label: str,
) -> tuple[int, str, str]:
    """执行子进程并返回 ``(rc, stdout, stderr)``；超时/启动失败显式报错。"""

    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=grass_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise EngineError(
            f"GRASS {label} 超时（>{timeout_s:g}s）：{' '.join(command)}"
        ) from exc
    except OSError as exc:
        raise EngineError(f"GRASS {label} 无法启动：{exc}") from exc
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    return completed.returncode, stdout, stderr


def _stderr_tail(stderr: str) -> str:
    text = stderr.strip()
    if len(text) <= _STDERR_TAIL_CHARS:
        return text
    return "…" + text[-_STDERR_TAIL_CHARS:]


def _write_script(path: Path, lines: list[str]) -> str:
    path.write_text("set -e\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _read_raster(path: str | Path) -> tuple[np.ndarray, Any, float | None, bool]:
    """读单波段栅格：返回 ``(array float64, transform, nodata, valid)``。"""

    with rasterio.open(path) as ds:
        array = ds.read(1).astype(np.float64)
        transform = ds.transform
        nodata = ds.nodata
    if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
        valid = array != nodata
    else:
        valid = np.isfinite(array)
    array = np.where(valid, array, np.nan)
    return array, transform, nodata, valid


def _same_grid(transform: Any, shape_hw: tuple[int, int], analysis: Any) -> bool:
    if tuple(shape_hw) != (analysis.height, analysis.width):
        return False
    values_a = (transform.a, transform.b, transform.c, transform.d, transform.e, transform.f)
    values_b = (
        analysis.transform.a,
        analysis.transform.b,
        analysis.transform.c,
        analysis.transform.d,
        analysis.transform.e,
        analysis.transform.f,
    )
    return all(abs(float(x) - float(y)) <= 1e-6 for x, y in zip(values_a, values_b))


def _align_to_analysis(
    array: np.ndarray,
    transform: Any,
    analysis: Any,
    *,
    resampling: Resampling = Resampling.nearest,
) -> np.ndarray:
    """把 GRASS 输出栅格对齐到分析网格（网格不一致时最近邻重投影）。"""

    if _same_grid(transform, array.shape, analysis):
        return array
    destination = np.full((analysis.height, analysis.width), np.nan, dtype=np.float64)
    reproject(
        source=array,
        destination=destination,
        src_transform=transform,
        src_crs=analysis.crs,
        src_nodata=np.nan,
        dst_transform=analysis.transform,
        dst_crs=analysis.crs,
        dst_nodata=np.nan,
        resampling=resampling,
    )
    return destination


def dir_to_receiver(dir_array: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """把 GRASS ``drainage`` 编码转成下游像元平坦索引（-1 = 无出流）。

    GRASS 编码：1=NE,2=N,3=NW,4=W,5=SW,6=S,7=SE,8=E；-1=洼地，其他负值=
    流向分析范围外。后两者在本实现里都表示"链在此终止"。
    """

    dir_array = np.asarray(dir_array, dtype=np.int64)
    valid = np.asarray(valid, dtype=bool)
    height, width = dir_array.shape
    rows = np.arange(height, dtype=np.int64)[:, None]
    cols = np.arange(width, dtype=np.int64)[None, :]
    receiver = np.full((height, width), -1, dtype=np.int64)
    for code, (dr, dc) in GRASS_DIRECTION_OFFSETS.items():
        target_rows = rows + dr
        target_cols = cols + dc
        in_bounds = (
            (target_rows >= 0)
            & (target_rows < height)
            & (target_cols >= 0)
            & (target_cols < width)
        )
        flat_target = np.broadcast_to(target_rows * width + target_cols, (height, width))
        select = valid & (dir_array == code) & np.broadcast_to(in_bounds, (height, width))
        if select.any():
            receiver[select] = flat_target[select]
    receiver[~valid] = -1
    return receiver


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class GrassEngine:
    """GRASS 8.x 的 ``r.watershed -s`` + ``r.water.outlet`` 引擎。"""

    name = "grass"

    def __init__(self, grass_cmd: str | None = None) -> None:
        self.grass_cmd = grass_cmd
        self.executable: str | None = None
        self.version: str | None = None
        self._reason: str = "尚未检测"

    # -- 可用性 -------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        ok, reason, executable, version = _availability(self.grass_cmd)
        self._reason = reason
        if ok:
            self.executable = executable
            self.version = version
        return ok, reason

    # -- 主流程 -------------------------------------------------------
    def run(
        self, params: engine_module.WatershedParams, out_dir: str | Path
    ) -> engine_module.WatershedResult:
        out_dir = Path(out_dir)
        ok, reason = self.available()
        if not ok or self.executable is None:
            raise EngineUnavailableError(f"GRASS 引擎不可用：{reason}")

        ctx = engine_module.prepare_common(params, out_dir)
        analysis = ctx.analysis
        metric_crs = ctx.metric_crs
        warnings = list(ctx.warnings)
        extra = params.extra or {}
        timeout_s = float(extra.get("grass_timeout_s", DEFAULT_GRASS_TIMEOUT_S))
        keep_staging = bool(extra.get("grass_keep_staging", False))

        # GRASS 对含空格的路径敏感：全部工作放在无空格暂存目录
        staging = Path(tempfile.mkdtemp(prefix="omap-grass-"))
        commands: list[dict[str, Any]] = []
        try:
            staged_dem = staging / "analysis_dem.tif"
            shutil.copyfile(analysis.path, staged_dem)
            gisdbase = staging / "gdb"
            location = gisdbase / "loc"
            mapset = location / "PERMANENT"

            # 1) 建临时 location（从分析 DEM 的 CRS 创建，只建投影不导数据）
            create_cmd = [
                str(self.executable),
                "-c",
                str(staged_dem),
                "-e",
                str(location),
            ]
            rc, stdout, stderr = _run_command(
                create_cmd, cwd=staging, timeout_s=timeout_s, label="建 location"
            )
            commands.append(
                {
                    "phase": "create_location",
                    "command": create_cmd,
                    "returncode": rc,
                    "stderr_tail": _stderr_tail(stderr),
                }
            )
            if rc != 0:
                raise EngineError(
                    f"GRASS 建 location 失败（rc={rc}）：{_stderr_tail(stderr)}"
                )

            # 2) Phase A：导入 DEM -> region -> r.watershed -s -> 导出 acc/dir
            acc_tif = staging / "acc.tif"
            dir_tif = staging / "dir.tif"
            script_a = _write_script(
                staging / "phase_a.sh",
                [
                    f"r.in.gdal input={staged_dem} output=dem --overwrite",
                    "g.region raster=dem -a",
                    "r.watershed -s elevation=dem accumulation=acc drainage=dir --overwrite",
                    f"r.out.gdal -f input=acc output={acc_tif} type=Int32 --overwrite",
                    f"r.out.gdal -f input=dir output={dir_tif} type=Int16 --overwrite",
                ],
            )
            phase_a_cmd = [
                str(self.executable),
                str(mapset),
                "--exec",
                "bash",
                script_a,
            ]
            rc, stdout, stderr = _run_command(
                phase_a_cmd, cwd=staging, timeout_s=timeout_s, label="r.watershed"
            )
            commands.append(
                {
                    "phase": "watershed_sfd",
                    "command": phase_a_cmd,
                    "script": Path(script_a).read_text(encoding="utf-8"),
                    "returncode": rc,
                    "stdout_tail": _stderr_tail(stdout),
                    "stderr_tail": _stderr_tail(stderr),
                }
            )
            if rc != 0:
                raise EngineError(
                    f"GRASS r.watershed 阶段失败（rc={rc}）：{_stderr_tail(stderr)}"
                )

            # 3) 读 acc/dir 并对齐分析网格；出口吸附在 Python 侧完成（复用 native）
            acc_raw, acc_transform, _acc_nodata, acc_valid_raw = _read_raster(acc_tif)
            dir_raw, dir_transform, _dir_nodata, dir_valid_raw = _read_raster(dir_tif)
            acc_on_grid = _align_to_analysis(acc_raw, acc_transform, analysis)
            dir_on_grid = _align_to_analysis(dir_raw, dir_transform, analysis)
            valid = analysis.ensure_valid_mask() & np.isfinite(acc_on_grid)
            abs_acc = np.abs(acc_on_grid)
            abs_acc_int = np.where(valid, np.rint(abs_acc), 0).astype(np.int64)
            dir_int = np.where(np.isfinite(dir_on_grid), np.rint(dir_on_grid), 0).astype(
                np.int64
            )
            if not _same_grid(acc_transform, acc_raw.shape, analysis):
                warnings.append(
                    "GRASS 输出网格与分析网格不一致，已用最近邻重投影对齐"
                    "（可能带来 1 像元级边界差异）"
                )

            snap = snap_outlet(
                abs_acc_int,
                valid,
                analysis.transform,
                ctx.outlet_xy[0],
                ctx.outlet_xy[1],
                float(params.snap_radius_m),
                min_accum_cells=int(params.min_accum_cells),
            )
            if snap.snapping_disabled:
                warnings.append(
                    f"出口吸附已关闭（snap_radius_m=0）：使用输入点所在像元 "
                    f"(行 {snap.row}, 列 {snap.col})，该像元 GRASS 累积绝对值 "
                    f"{snap.accum_cells} 像元"
                )
            else:
                warnings.append(
                    f"出口吸附（GRASS）：输入点 ({ctx.outlet_xy[0]:.2f}, {ctx.outlet_xy[1]:.2f}) "
                    f"→ 像元 (行 {snap.row}, 列 {snap.col})，距离 {snap.distance_m:.2f} m，"
                    f"半径内最大累积绝对值 {snap.accum_cells} 像元"
                )
            if snap.accum_below_min:
                warnings.append(
                    f"出口所在像元累积 {snap.accum_cells} < min_accum_cells="
                    f"{params.min_accum_cells}：出口可能位于坡面而非河道"
                )
            if snap.ambiguous:
                warnings.append(
                    "半径内存在多个累积相近的候选栅格，出口吸附可能不唯一；"
                    "请复核 provenance.outlet_snap.candidates"
                )

            # 4) Phase B：r.water.outlet -> r.to.vect -> v.out.ogr -> 导出 basin
            boundary_geojson = staging / "boundary.geojson"
            basin_tif = staging / "basin.tif"
            coordinates = f"{snap.x:.6f},{snap.y:.6f}"
            script_b = _write_script(
                staging / "phase_b.sh",
                [
                    f"r.water.outlet input=dir output=basin coordinates={coordinates} --overwrite",
                    "r.to.vect input=basin output=basin_vect type=area --overwrite",
                    f"v.out.ogr input=basin_vect output={boundary_geojson} "
                    "format=GeoJSON type=area --overwrite",
                    f"r.out.gdal -f input=basin output={basin_tif} type=Byte --overwrite",
                ],
            )
            phase_b_cmd = [
                str(self.executable),
                str(mapset),
                "--exec",
                "bash",
                script_b,
            ]
            rc, stdout, stderr = _run_command(
                phase_b_cmd, cwd=staging, timeout_s=timeout_s, label="r.water.outlet"
            )
            commands.append(
                {
                    "phase": "water_outlet",
                    "command": phase_b_cmd,
                    "script": Path(script_b).read_text(encoding="utf-8"),
                    "returncode": rc,
                    "stdout_tail": _stderr_tail(stdout),
                    "stderr_tail": _stderr_tail(stderr),
                }
            )
            if rc != 0:
                raise EngineError(
                    f"GRASS r.water.outlet 阶段失败（rc={rc}）：{_stderr_tail(stderr)}"
                )

            # 5) 读取 basin 掩膜与矢量边界（都对齐到分析网格）
            basin_raw, basin_transform, basin_nodata, basin_valid_raw = _read_raster(basin_tif)
            basin_on_grid = _align_to_analysis(basin_raw, basin_transform, analysis)
            basin_valid = np.isfinite(basin_on_grid)
            mask = basin_valid & (np.rint(basin_on_grid) == 1)
            mask_cell_count = int(np.count_nonzero(mask))
            if mask_cell_count == 0:
                raise EngineError(
                    "GRASS r.water.outlet 返回空集水区（出口可能落在无出流/NoData 像元）；"
                    "请检查出口坐标、吸附半径与 DEM 覆盖"
                )
            if not boundary_geojson.is_file():
                raise EngineError("GRASS v.out.ogr 没有生成边界 GeoJSON")
            import json

            geojson_data = json.loads(boundary_geojson.read_text(encoding="utf-8"))
            features = geojson_data.get("features") or []
            if not features:
                raise EngineError(
                    "GRASS v.out.ogr 生成的 GeoJSON 没有要素（集水区矢量化失败）"
                )
            boundary_metric = shape(features[0]["geometry"])
            boundary_metric = geometry_module.as_polygonal(boundary_metric)
            if boundary_metric.is_empty:
                raise EngineError("GRASS 边界几何为空")
            if not boundary_metric.is_valid:
                warnings.append(
                    "GRASS 矢量边界无效，已用 make_valid 受控修复（报告 topology_repaired=true）"
                )
                from shapely import make_valid

                boundary_metric = geometry_module.as_polygonal(
                    make_valid(boundary_metric)
                )

            # 6) 河网 / 出口出流状态 / 外部来水警告
            receiver = dir_to_receiver(dir_int, valid)
            stream_threshold = extra.get("stream_threshold_cells")
            if stream_threshold is None:
                stream_threshold = max(
                    int(params.min_accum_cells),
                    int(round(0.005 * max(mask_cell_count, 1))),
                )
            stream_threshold = int(stream_threshold)
            stream_lines, stream_threshold, stream_skip_reason = build_stream_network(
                receiver,
                abs_acc_int,
                valid,
                analysis.transform,
                stream_threshold,
                mask=mask,
            )
            if stream_skip_reason:
                warnings.append(stream_skip_reason)
            else:
                warnings.append(
                    f"河网：GRASS 累积 ≥ {stream_threshold} 像元的河道，"
                    f"链化后 {len(stream_lines)} 条折线（仅用于复核展示）"
                )
            dir_at_outlet = int(dir_int[snap.row, snap.col])
            outlet_is_sink = dir_at_outlet <= 0
            negative_acc_cells = int(np.count_nonzero(np.isfinite(acc_on_grid) & (acc_on_grid < 0)))
            if negative_acc_cells:
                warnings.append(
                    f"GRASS 累积有 {negative_acc_cells} 个像元为负值：文档说明这些像元"
                    "可能有分析范围外的来水，累积绝对值为保守估计；分析范围可能不足"
                )
            if ctx.dem_info.nodata_fraction > 0:
                warnings.append(
                    "DEM 含 NoData：GRASS 文档要求分析区内部空洞先填补（如 r.fillnulls），"
                    "本实现不填补，仅显式警告；空洞会阻断汇流"
                )
            grass_version = self.version or detect_grass_version(str(self.executable))

            # 7) 中间栅格落盘（GRASS 编码写入 provenance）
            flow_accum_path = write_single_band(
                out_dir / engine_module.FLOW_ACCUM_TIF,
                np.where(np.isfinite(acc_on_grid), np.rint(acc_on_grid), -2147483648),
                crs=metric_crs,
                transform=analysis.transform,
                nodata=-2147483648,
                dtype="int32",
            )
            flow_dir_path = write_single_band(
                out_dir / engine_module.FLOW_DIRECTION_TIF,
                np.where(np.isfinite(dir_on_grid), np.rint(dir_on_grid), -32768),
                crs=metric_crs,
                transform=analysis.transform,
                nodata=-32768,
                dtype="int16",
            )

            outlet_used = (
                (float(params.outlet_lonlat[0]), float(params.outlet_lonlat[1]))
                if snap.snapping_disabled
                else transform_xy_to_lonlat(snap.x, snap.y, metric_crs)
            )
            provenance_extra = {
                "grass": {
                    "version": grass_version,
                    "executable": str(self.executable),
                    "location": str(mapset),
                    "staging_dir": str(staging),
                    "staging_removed": not keep_staging,
                    "commands": commands,
                    "home_override": str(grass_home_dir()),
                    "config_dir_override": str(grass_home_dir() / "grassconfig"),
                    "drainage_encoding": {
                        str(code): list(offset)
                        for code, offset in GRASS_DIRECTION_OFFSETS.items()
                    },
                    "drainage_negative_meaning": (
                        "GRASS: -1=洼地, 其他负值=流向分析范围外（链在此终止）"
                    ),
                    "accumulation_negative_meaning": (
                        "GRASS: 负值表示可能有分析范围外来水，绝对值为保守估计"
                    ),
                    "accumulation_at_outlet": int(round(float(acc_on_grid[snap.row, snap.col]))),
                    "basin_cell_count": mask_cell_count,
                    "dir_at_outlet": dir_at_outlet,
                    "negative_accumulation_cells": negative_acc_cells,
                    "fill_depressions": False,
                    "nodata_filled": False,
                    "stream_threshold_cells": stream_threshold,
                }
            }
            extra_artifacts = {
                "analysis_dem_tif": analysis.path,
                "flow_direction_tif": flow_dir_path,
                "flow_accum_tif": flow_accum_path,
            }
            return engine_module.assemble_result(
                out_dir=out_dir,
                params=params,
                dem_info=ctx.dem_info,
                metric_crs=metric_crs,
                analysis=analysis,
                mask=mask,
                boundary_metric=boundary_metric,
                outlet_used_lonlat=outlet_used,
                snap_distance_m=snap.distance_m,
                flow_accum_cells=snap.accum_cells,
                engine_name=self.name,
                engine_version=str(grass_version),
                warnings=warnings,
                provenance_extra=provenance_extra,
                stream_network=stream_lines,
                extra_artifacts=extra_artifacts,
                outlet_is_sink=outlet_is_sink,
                topology_repaired=not boundary_metric.is_valid,
                snap_evidence=snap.to_evidence(),
            )
        finally:
            if not keep_staging:
                shutil.rmtree(staging, ignore_errors=True)
