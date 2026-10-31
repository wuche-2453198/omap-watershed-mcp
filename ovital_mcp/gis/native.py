"""native 引擎：numpy + rasterio 的确定性 D8 集水区计算。

对应契约 ``docs/architecture.md`` §5.2 的 native 部分：

1. DEM 重投影到米制 CRS（:mod:`ovital_mcp.gis.crs`，与 GRASS 引擎共用网格）；
2. D8 单流向（8 邻域最陡下降，固定邻域顺序 + 严格 ``>`` 比较保证确定性）；
3. 汇流累积（在按高程降序排列的严格下三角稀疏矩阵上做三角求解，
   比逐像元 Python 循环快且逐位确定；scipy 不可用时退回 Kahn 拓扑排序）；
4. 出口吸附：半径内取汇流累积最大的有效栅格（并列时取距输入点最近、
   再取索引最小），记录吸附前后距离/累积/候选点；
5. 从吸附出口反向 BFS 求上游掩膜；
6. ``rasterio.features.shapes`` 矢量化并取含出口的连通部件；
7. shapely 校验（可选受控简化）→ 回投 EPSG:4490（由 engine.assemble_result 完成）。

**不填洼**：与 GRASS ``r.watershed`` 的最小代价算法一致；平缓/洼地会显式
警告，绝不静默修改地形。NoData 空洞不填补，并显式警告。

D8 编码（``flow_direction.tif`` 与 provenance 中记录）：

======  ================  ====================
代码    方向（行,列偏移）   说明
======  ================  ====================
0       (-1, -1)          西北 NW
1       (-1,  0)          北 N
2       (-1, +1)          东北 NE
3       ( 0, -1)          西 W
4       ( 0, +1)          东 E
5       (+1, -1)          西南 SW
6       (+1,  0)          南 S
7       (+1, +1)          东南 SE
-1      —                 无出流（洼地/平地/边界）
======  ================  ====================
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio import features
from shapely.geometry import LineString, Point, shape

from ..errors import EngineError
from . import engine as engine_module
from . import geometry as geometry_module
from .crs import AnalysisRaster, transform_xy_to_lonlat, xy_to_rowcol

#: 固定 8 邻域顺序（行偏移, 列偏移）。顺序即 D8 编码，**不要重排**。
D8_NEIGHBORS: tuple[tuple[int, int], ...] = (
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, -1),
    (0, 1),
    (1, -1),
    (1, 0),
    (1, 1),
)

#: 河网折线（链）数量上限：超过则跳过河网并显式告警，绝不拖垮计算。
MAX_STREAM_LINES = 20_000
#: 河网折线总顶点数上限（同样的保护）。
MAX_STREAM_VERTICES = 2_000_000
#: 河网像元数上限：超过则跳过（极端大流域）。
MAX_STREAM_CELLS = 4_000_000


# ---------------------------------------------------------------------------
# D8 核心
# ---------------------------------------------------------------------------


def compute_flow_direction(
    z: np.ndarray,
    valid: np.ndarray,
    res_x: float,
    res_y: float,
) -> tuple[np.ndarray, np.ndarray]:
    """计算 D8 流向。

    返回 ``(fd, recv)``：``fd`` 为 int8 编码（见模块 docstring，-1 无出流）；
    ``recv`` 为 int64 平坦索引（-1 表示无出流）。邻域顺序固定、比较用严格
    ``>``，因此完全确定。
    """

    z = np.asarray(z, dtype=np.float64)
    valid = np.asarray(valid, dtype=bool)
    if z.shape != valid.shape:
        raise EngineError(f"高程 {z.shape} 与有效掩膜 {valid.shape} 形状不一致")
    height, width = z.shape
    best_slope = np.zeros((height, width), dtype=np.float64)
    fd = np.full((height, width), -1, dtype=np.int8)
    rows = np.arange(height, dtype=np.int64)[:, None]
    cols = np.arange(width, dtype=np.int64)[None, :]

    for code, (dr, dc) in enumerate(D8_NEIGHBORS):
        # 邻居高程：nb[r, c] = z[r + dr, c + dc]（越界为 NaN）
        # 源索引 = 目标索引 + 偏移；按偏移正负裁掉越界的一条边。
        nb = np.full((height, width), np.nan, dtype=np.float64)
        if dr < 0:
            r_dst, r_src = slice(-dr, height), slice(0, height + dr)
        else:
            r_dst, r_src = slice(0, height - dr), slice(dr, height)
        if dc < 0:
            c_dst, c_src = slice(-dc, width), slice(0, width + dc)
        else:
            c_dst, c_src = slice(0, width - dc), slice(dc, width)
        nb[r_dst, c_dst] = z[r_src, c_src]
        distance = math.hypot(dr * float(res_x), dc * float(res_y))
        if distance <= 0:  # pragma: no cover - 邻居偏移不可能为 0
            continue
        slope = (z - nb) / distance
        candidate = valid & np.isfinite(slope) & (slope > best_slope)
        if not candidate.any():
            continue
        best_slope[candidate] = slope[candidate]
        fd[candidate] = code

    # 一次性把编码转成接收像元平坦索引
    dr_arr = np.array([item[0] for item in D8_NEIGHBORS], dtype=np.int64)
    dc_arr = np.array([item[1] for item in D8_NEIGHBORS], dtype=np.int64)
    safe = np.where(fd >= 0, fd, 0).astype(np.int64)
    recv = np.where(
        (fd >= 0) & valid,
        (rows + dr_arr[safe]) * width + (cols + dc_arr[safe]),
        -1,
    )
    recv = recv.astype(np.int64, copy=False)
    recv[~valid] = -1
    return fd, recv


def _accumulate_kahn(valid_flat: np.ndarray, recv_flat: np.ndarray) -> np.ndarray:
    """Kahn 拓扑排序的汇流累积（scipy 三角求解不可用时的确定性后备）。"""

    n = valid_flat.size
    acc = np.zeros(n, dtype=np.int64)
    acc[valid_flat] = 1
    indegree = np.zeros(n, dtype=np.int32)
    receivers = recv_flat[valid_flat]
    upstream = receivers[receivers >= 0]
    np.add.at(indegree, upstream, 1)
    from collections import deque

    queue = deque(int(i) for i in np.flatnonzero(valid_flat & (indegree == 0)))
    processed = 0
    while queue:
        cell = queue.popleft()
        processed += 1
        target = int(recv_flat[cell])
        if target >= 0:
            acc[target] += acc[cell]
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if processed != int(np.count_nonzero(valid_flat)):
        raise EngineError("汇流累积失败：流向图存在环（不应发生）")
    return acc


def compute_flow_accumulation(
    z: np.ndarray,
    valid: np.ndarray,
    recv: np.ndarray,
) -> np.ndarray:
    """汇流累积：每个像元的上游像元数（含自身，与 GRASS accumulation 口径一致）。

    利用"D8 只流向严格更低像元"这一性质，把问题变成按高程降序排列后的
    严格下三角线性系统 ``(I - A) acc = 1``，用 ``scipy.sparse.linalg.
    spsolve_triangular`` 求解；结果全部为整数（float64 可精确表示 ≤2^53）。
    """

    z = np.asarray(z, dtype=np.float64)
    valid = np.asarray(valid, dtype=bool)
    height, width = z.shape
    n = height * width
    z_flat = z.ravel()
    valid_flat = valid.ravel()
    recv_flat = recv.ravel().astype(np.int64, copy=False)

    flat_index = np.arange(n, dtype=np.int64)
    # 按高程降序：-z 升序；并列时按像元索引，保证确定性
    order = np.lexsort((flat_index, -np.where(valid_flat, z_flat, -np.inf)))
    pos = np.empty(n, dtype=np.int32)
    pos[order] = np.arange(n, dtype=np.int32)

    src = np.flatnonzero(valid_flat & (recv_flat >= 0))
    if src.size == 0:
        acc = np.zeros(n, dtype=np.int64)
        acc[valid_flat] = 1
        return acc.reshape(height, width)

    dst = recv_flat[src]
    rows = pos[dst].astype(np.int32, copy=False)
    cols = pos[src].astype(np.int32, copy=False)
    if not np.all(rows > cols):
        # 理论上不可能（严格更低高程 => 更晚位置）；出现则显式失败而不是算错
        raise EngineError("汇流累积失败：流向不满足严格下降（DEM 或流向实现异常）")

    try:
        from scipy.sparse import csc_matrix
        from scipy.sparse.linalg import spsolve_triangular

        matrix = csc_matrix(
            (-np.ones(src.size, dtype=np.float64), (rows, cols)),
            shape=(n, n),
        )
        solution = spsolve_triangular(
            matrix,
            np.ones(n, dtype=np.float64),
            lower=True,
            unit_diagonal=True,
        )
        acc_position = np.empty(n, dtype=np.float64)
        acc_position[order] = solution
        acc_flat = np.where(valid_flat, np.rint(acc_position), 0.0).astype(np.int64)
        return acc_flat.reshape(height, width)
    except EngineError:
        raise
    except Exception:  # noqa: BLE001 - scipy 求解异常时退回纯 Python 拓扑排序
        acc_flat = _accumulate_kahn(valid_flat, recv_flat)
        return acc_flat.reshape(height, width)


def upstream_mask(
    recv: np.ndarray,
    start_flat: int,
    shape: tuple[int, int] | None = None,
) -> np.ndarray:
    """从出口像元反向 BFS 求上游掩膜（bool，形状同流向图）。"""

    recv_flat = np.asarray(recv, dtype=np.int64).ravel()
    n = recv_flat.size
    if not (0 <= int(start_flat) < n):
        raise EngineError(f"出口平坦索引 {start_flat} 越界（共 {n} 个像元）")
    valid_cells = np.flatnonzero(recv_flat >= 0)
    receiver_of_valid = recv_flat[valid_cells]
    sort_order = np.argsort(receiver_of_valid, kind="stable")
    pred_cells = valid_cells[sort_order]
    pred_receiver = receiver_of_valid[sort_order]

    mask = np.zeros(n, dtype=bool)
    mask[int(start_flat)] = True
    frontier = np.array([int(start_flat)], dtype=np.int64)
    while frontier.size:
        lo = np.searchsorted(pred_receiver, frontier, side="left")
        hi = np.searchsorted(pred_receiver, frontier, side="right")
        counts = (hi - lo).astype(np.int64)
        total = int(counts.sum())
        if total == 0:
            break
        offsets = np.repeat(lo - (np.cumsum(counts) - counts), counts)
        gather = np.arange(total, dtype=np.int64) + offsets
        new_cells = pred_cells[gather]
        new_cells = new_cells[~mask[new_cells]]
        mask[new_cells] = True
        frontier = new_cells
    if shape is not None:
        return mask.reshape(shape)
    return mask


def vectorize_mask(
    mask: np.ndarray,
    transform: Any,
    outlet_xy: tuple[float, float],
) -> Any:
    """把上游掩膜矢量化，并取含出口的连通部件（米制 CRS 面几何）。"""

    data = np.asarray(mask, dtype=np.uint8)
    polygons: list[Any] = []
    for geom_dict, value in features.shapes(
        data, mask=mask, transform=transform, connectivity=8
    ):
        if int(value) != 1:
            continue
        polygons.append(shape(geom_dict))
    if not polygons:
        raise EngineError("上游掩膜矢量化结果为空（掩膜没有像元）")
    merged = polygons[0] if len(polygons) == 1 else _union(polygons)
    return geometry_module.pick_component_containing(merged, outlet_xy)


def _union(polygons: list[Any]) -> Any:
    import shapely

    return shapely.union_all(polygons)


def build_stream_network(
    recv: np.ndarray,
    acc: np.ndarray,
    valid: np.ndarray,
    transform: Any,
    threshold_cells: int,
    *,
    mask: np.ndarray | None = None,
    max_lines: int = MAX_STREAM_LINES,
    max_vertices: int = MAX_STREAM_VERTICES,
) -> tuple[list[LineString], int, str | None]:
    """由 D8 流向提取河网，并**合并成链**（headwater/交汇点之间的折线）。

    逐像元画"像元中心→下游中心"会产生与像元数同量级的线段（小流域上千条、
    30 m 全国 DEM 上亿条），既拖慢计算也撑爆复核图。这里按图论链化：

    - 河网像元 = ``valid & recv>=0 & acc>=threshold``（可再用流域掩膜过滤）；
    - 节点 = 入度 ≠ 1 的河网像元（headwater 入度 0，交汇点入度 ≥2）；
    - 从每个节点顺流走到下一个节点（或流出河网），得到一条链；
    - 每条链按半像元容差简化，仅用于展示。

    返回 ``(lines, threshold, skipped_reason)``。超过数量/顶点/像元上限时
    返回空列表并给出原因（调用方写 warning），保证不会拖垮主计算。
    """

    recv_flat = np.asarray(recv, dtype=np.int64).ravel()
    acc_flat = np.asarray(acc, dtype=np.int64).ravel()
    valid_flat = np.asarray(valid, dtype=bool).ravel()
    height, width = np.asarray(recv).shape
    threshold = max(1, int(threshold_cells))
    selected = valid_flat & (recv_flat >= 0) & (acc_flat >= threshold)
    if mask is not None:
        selected &= np.asarray(mask, dtype=bool).ravel()
    cells = np.flatnonzero(selected)
    if cells.size == 0:
        return [], threshold, None
    if cells.size > MAX_STREAM_CELLS:
        return (
            [],
            threshold,
            f"河网像元 {cells.size} 超过上限 {MAX_STREAM_CELLS}，已跳过河网提取",
        )

    recv_of = recv_flat[cells]
    # 下游像元是否也在河网内（用 searchsorted 定位，避免 n 大小索引数组）
    insertion = np.searchsorted(cells, recv_of)
    insertion_clipped = np.clip(insertion, 0, cells.size - 1)
    downstream_in_network = cells[insertion_clipped] == recv_of
    indegree = np.zeros(cells.size, dtype=np.int32)
    if downstream_in_network.any():
        np.add.at(indegree, insertion[downstream_in_network], 1)

    position = {int(flat): index for index, flat in enumerate(cells.tolist())}
    starts = np.flatnonzero(indegree != 1)
    chains: list[np.ndarray] = []
    total_vertices = 0
    for start_index in starts.tolist():
        path: list[int] = [int(cells[start_index])]
        current = start_index
        while True:
            target = int(recv_of[current])
            path.append(target)
            next_index = position.get(target)
            if next_index is None:
                break  # 下游离开河网（进入非河道像元），链在此结束
            current = next_index
            if indegree[current] != 1:
                break  # 到达下一个节点（交汇点/出口）
            if len(path) > cells.size + 2:  # pragma: no cover - 防御死循环
                raise EngineError("河网链化失败：检测到环（D8 流向不应有环）")
        chains.append(np.asarray(path, dtype=np.int64))
        total_vertices += len(path)
        if len(chains) > max_lines:
            return (
                [],
                threshold,
                f"河网链数超过上限 {max_lines}，已跳过河网提取（可提高 extra.stream_threshold_cells）",
            )
        if total_vertices > max_vertices:
            return (
                [],
                threshold,
                f"河网顶点数超过上限 {max_vertices}，已跳过河网提取（可提高 extra.stream_threshold_cells）",
            )

    # 链 -> 坐标 -> LineString（一次性向量化坐标换算）
    flat_all = np.concatenate(chains)
    rows, cols = np.divmod(flat_all, width)
    x = transform.c + (cols + 0.5) * transform.a
    y = transform.f + (rows + 0.5) * transform.e
    simplify_tolerance = 0.5 * min(
        abs(float(transform.a)), abs(float(transform.e))
    )
    lines: list[LineString] = []
    offset = 0
    for chain in chains:
        count = len(chain)
        segment = np.stack([x[offset : offset + count], y[offset : offset + count]], axis=1)
        offset += count
        line = LineString(segment)
        if count > 2 and simplify_tolerance > 0:
            line = line.simplify(simplify_tolerance, preserve_topology=False)
        lines.append(line)
    return lines, threshold, None


# ---------------------------------------------------------------------------
# 出口吸附
# ---------------------------------------------------------------------------


@dataclass
class OutletSnap:
    """出口吸附结果与证据（供复核与 provenance 使用）。"""

    row: int
    col: int
    x: float
    y: float
    distance_m: float
    accum_cells: int
    requested_x: float
    requested_y: float
    max_accum_in_radius: int
    ambiguous: bool = False
    accum_below_min: bool = False
    snapping_disabled: bool = False
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_evidence(self) -> dict[str, Any]:
        return {
            "mode": "disabled" if self.snapping_disabled else "max_accum_in_radius",
            "requested_xy": [self.requested_x, self.requested_y],
            "snapped_cell": [self.row, self.col],
            "snapped_xy": [self.x, self.y],
            "distance_m": self.distance_m,
            "accum_cells": self.accum_cells,
            "max_accum_in_radius": self.max_accum_in_radius,
            "ambiguous": self.ambiguous,
            "accum_below_min_accum_cells": self.accum_below_min,
            "candidates": self.candidates,
        }


def snap_outlet(
    acc: np.ndarray,
    valid: np.ndarray,
    transform: Any,
    x: float,
    y: float,
    radius_m: float,
    *,
    min_accum_cells: int = 1,
    max_candidates: int = 5,
) -> OutletSnap:
    """把输入出口吸附到半径内汇流累积最大的有效像元。

    - ``radius_m=0``：**关闭吸附**，直接使用出口所在像元（snap_distance_m=0，
      outlet_used=outlet_input），但仍报告该像元的汇流累积；
    - 输入点在分析范围外：显式报错（不猜最近点）；
    - 半径内没有有效像元：显式报错并给出最近有效像元的距离；
    - 并列时取距输入点更近者，再取平坦索引更小者（确定性）；
    - 记录吸附前后距离、累积值、最大累积与候选点，供"是否误吸附"复核。
    """

    acc = np.asarray(acc)
    valid = np.asarray(valid, dtype=bool)
    if acc.shape != valid.shape:
        raise EngineError(f"累积 {acc.shape} 与有效掩膜 {valid.shape} 形状不一致")
    height, width = acc.shape
    if radius_m < 0:
        raise EngineError(f"吸附半径不能为负：{radius_m}")

    row_f, col_f = xy_to_rowcol(transform, x, y)
    row0, col0 = int(math.floor(row_f)), int(math.floor(col_f))
    if not (0 <= row0 < height and 0 <= col0 < width):
        raise EngineError(
            f"出口不在 DEM 分析范围内（行列 {row0},{col0} 越界于 {height}×{width}）；"
            "请检查出口坐标或 DEM 覆盖范围"
        )

    if float(radius_m) == 0.0:
        # 关闭吸附：严格使用出口所在像元；NoData 显式报错而不是悄悄挪动出口
        if not bool(valid[row0, col0]):
            raise EngineError(
                f"出口所在像元 (行 {row0}, 列 {col0}) 为 NoData；"
                "snap_radius_m=0 时不会吸附，请检查出口坐标或改用吸附半径"
            )
        cell_x = float(transform.c + (col0 + 0.5) * transform.a)
        cell_y = float(transform.f + (row0 + 0.5) * transform.e)
        cell_accum = int(round(float(abs(acc[row0, col0]))))
        return OutletSnap(
            row=row0,
            col=col0,
            x=cell_x,
            y=cell_y,
            distance_m=0.0,
            accum_cells=cell_accum,
            requested_x=float(x),
            requested_y=float(y),
            max_accum_in_radius=cell_accum,
            ambiguous=False,
            accum_below_min=cell_accum < int(min_accum_cells),
            snapping_disabled=True,
            candidates=[
                {
                    "row": row0,
                    "col": col0,
                    "accum_cells": cell_accum,
                    "distance_m": 0.0,
                }
            ],
        )

    res_x = abs(float(transform.a))
    res_y = abs(float(transform.e))
    row_radius = max(1, int(math.ceil(radius_m / max(res_y, 1e-9))))
    col_radius = max(1, int(math.ceil(radius_m / max(res_x, 1e-9))))
    r_lo, r_hi = max(0, row0 - row_radius), min(height, row0 + row_radius + 1)
    c_lo, c_hi = max(0, col0 - col_radius), min(width, col0 + col_radius + 1)
    acc_win = np.abs(acc[r_lo:r_hi, c_lo:c_hi].astype(np.float64))
    valid_win = valid[r_lo:r_hi, c_lo:c_hi]

    rows = np.arange(r_lo, r_hi, dtype=np.int64)[:, None]
    cols = np.arange(c_lo, c_hi, dtype=np.int64)[None, :]
    cx = transform.c + (cols + 0.5) * transform.a
    cy = transform.f + (rows + 0.5) * transform.e
    dist = np.hypot(cx - float(x), cy - float(y))
    candidate = valid_win & (dist <= float(radius_m)) & np.isfinite(acc_win)
    if not candidate.any():
        # 给出最近有效像元距离，帮助用户判断半径是否过小
        valid_any = valid & np.isfinite(np.abs(acc.astype(np.float64)))
        if not valid_any.any():
            raise EngineError("DEM 分析范围内没有任何有效像元，无法吸附出口")
        rr, cc = np.nonzero(valid_any)
        all_x = transform.c + (cc + 0.5) * transform.a
        all_y = transform.f + (rr + 0.5) * transform.e
        nearest = float(np.min(np.hypot(all_x - float(x), all_y - float(y))))
        raise EngineError(
            f"出口半径 {radius_m:g} m 内没有有效栅格（最近有效像元 {nearest:.1f} m）；"
            "请增大 snap_radius_m 或检查出口是否落在 DEM 覆盖/NoData 区域"
        )

    max_accum = float(acc_win[candidate].max())
    tie_mask = candidate & (np.isclose(acc_win, max_accum, rtol=0.0, atol=1e-9))
    tie_rows, tie_cols = np.nonzero(tie_mask)
    tie_dist = dist[tie_rows, tie_cols]
    # 先按距离，再按平坦索引（行优先）确定唯一结果
    tie_order = np.lexsort(
        (
            (tie_rows + r_lo) * width + (tie_cols + c_lo),
            tie_dist,
        )
    )
    chosen = int(tie_order[0])
    row = int(tie_rows[chosen] + r_lo)
    col = int(tie_cols[chosen] + c_lo)
    chosen_x = float(transform.c + (col + 0.5) * transform.a)
    chosen_y = float(transform.f + (row + 0.5) * transform.e)
    chosen_distance = float(math.hypot(chosen_x - float(x), chosen_y - float(y)))
    chosen_accum = int(round(max_accum))

    # 候选点证据（累积 >= 50% 最大值的像元，按累积降序、距离升序取前 N）
    cand_rows, cand_cols = np.nonzero(candidate)
    cand_accum = acc_win[cand_rows, cand_cols]
    cand_dist = dist[cand_rows, cand_cols]
    order = np.lexsort(
        (
            (cand_rows + r_lo) * width + (cand_cols + c_lo),
            cand_dist,
            -cand_accum,
        )
    )
    candidates: list[dict[str, Any]] = []
    for idx in order[:max_candidates]:
        candidates.append(
            {
                "row": int(cand_rows[idx] + r_lo),
                "col": int(cand_cols[idx] + c_lo),
                "accum_cells": int(round(float(cand_accum[idx]))),
                "distance_m": float(cand_dist[idx]),
            }
        )
    ambiguous = False
    if len(candidates) > 1:
        second = candidates[1]
        if (
            second["accum_cells"] >= 0.9 * max(chosen_accum, 1)
            and second["distance_m"] > max(res_x, res_y) * 2.0
            and math.hypot(
                second["row"] - row, second["col"] - col
            )
            > 2.0
        ):
            ambiguous = True

    return OutletSnap(
        row=row,
        col=col,
        x=chosen_x,
        y=chosen_y,
        distance_m=chosen_distance,
        accum_cells=chosen_accum,
        requested_x=float(x),
        requested_y=float(y),
        max_accum_in_radius=chosen_accum,
        ambiguous=ambiguous,
        accum_below_min=chosen_accum < int(min_accum_cells),
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# 栅格写出
# ---------------------------------------------------------------------------


def write_single_band(
    path: str | Path,
    array: np.ndarray,
    *,
    crs: str,
    transform: Any,
    nodata: float | int,
    dtype: str,
) -> str:
    """写单波段 GeoTIFF（不压缩、无时间戳，保证同输入同字节）。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "dtype": dtype,
        "count": 1,
        "width": array.shape[1],
        "height": array.shape[0],
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(array.astype(dtype, copy=False), 1)
    return str(path)


# ---------------------------------------------------------------------------
# native 引擎
# ---------------------------------------------------------------------------


class NativeEngine:
    """numpy/rasterio 的确定性 D8 引擎（契约 §5.2）。"""

    name = "native"

    def available(self) -> tuple[bool, str]:
        try:
            import scipy  # noqa: F401

            return True, (
                f"内置 D8 引擎可用（numpy {np.__version__}，"
                f"rasterio {rasterio.__version__}）"
            )
        except Exception as exc:  # noqa: BLE001 - 依赖缺失时给出原因
            return False, f"numpy/rasterio/scipy 依赖不可用：{exc}"

    def run(
        self, params: engine_module.WatershedParams, out_dir: str | Path
    ) -> engine_module.WatershedResult:
        out_dir = Path(out_dir)
        ctx = engine_module.prepare_common(params, out_dir)
        warnings = list(ctx.warnings)
        analysis: AnalysisRaster = ctx.analysis
        metric_crs = ctx.metric_crs

        z = analysis.ensure_array()
        valid = analysis.ensure_valid_mask()
        if not valid.any():
            raise EngineError("分析栅格内没有有效高程像元")

        fd, recv = compute_flow_direction(z, valid, analysis.res_x, analysis.res_y)
        acc = compute_flow_accumulation(z, valid, recv)

        snap = snap_outlet(
            acc,
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
                f"(行 {snap.row}, 列 {snap.col})，该像元汇流累积 {snap.accum_cells} 像元"
            )
        else:
            warnings.append(
                f"出口吸附（native）：输入点 ({ctx.outlet_xy[0]:.2f}, {ctx.outlet_xy[1]:.2f}) "
                f"→ 像元 (行 {snap.row}, 列 {snap.col})，距离 {snap.distance_m:.2f} m，"
                f"半径内最大汇流累积 {snap.accum_cells} 像元"
            )
            if snap.distance_m > 1e-6:
                warnings.append(
                    f"出口已从输入点吸附到汇流累积最大的栅格（{snap.distance_m:.2f} m）："
                    "计算使用吸附后的出口；原始出口与吸附出口均记录在 provenance"
                )
        if snap.accum_below_min:
            warnings.append(
                f"出口所在像元汇流累积 {snap.accum_cells} < min_accum_cells="
                f"{params.min_accum_cells}：出口可能位于坡面而非河道，集水区可能呈细长条"
            )
        if snap.ambiguous:
            warnings.append(
                "半径内存在多个汇流累积相近的候选栅格，出口吸附可能不唯一；"
                "请复核候选点（provenance.outlet_snap.candidates）"
            )

        start_flat = snap.row * analysis.width + snap.col
        mask = upstream_mask(recv, start_flat, shape=z.shape)
        mask_cell_count = int(np.count_nonzero(mask))
        if mask_cell_count != snap.accum_cells:
            warnings.append(
                f"反向 BFS 掩膜 {mask_cell_count} 像元与出口累积 {snap.accum_cells} "
                "不一致（D8 不变量被破坏，请检查实现/输入）"
            )

        boundary_metric = vectorize_mask(
            mask, analysis.transform, (snap.x, snap.y)
        )
        if snap.snapping_disabled:
            # 关闭吸附时 outlet_used 必须与输入完全一致（不引入任何像元中心位移）
            outlet_used = (float(params.outlet_lonlat[0]), float(params.outlet_lonlat[1]))
        else:
            outlet_used = transform_xy_to_lonlat(snap.x, snap.y, metric_crs)

        # 中间成果（稳定文件名，供复核图/工具/交叉校验使用）
        fd_path = write_single_band(
            out_dir / engine_module.FLOW_DIRECTION_TIF,
            fd,
            crs=metric_crs,
            transform=analysis.transform,
            nodata=-128,
            dtype="int8",
        )
        acc_max = int(acc.max()) if acc.size else 0
        acc_path = write_single_band(
            out_dir / engine_module.FLOW_ACCUM_TIF,
            acc,
            crs=metric_crs,
            transform=analysis.transform,
            nodata=-1,
            dtype="int32" if acc_max < 2**31 - 1 else "float64",
        )

        stream_threshold = (params.extra or {}).get("stream_threshold_cells")
        if stream_threshold is None:
            stream_threshold = max(
                int(params.min_accum_cells),
                int(round(0.005 * max(mask_cell_count, 1))),
            )
        stream_threshold = int(stream_threshold)
        stream_lines, stream_threshold, stream_skip_reason = build_stream_network(
            recv, acc, valid, analysis.transform, stream_threshold, mask=mask
        )
        if stream_skip_reason:
            warnings.append(stream_skip_reason)
        else:
            warnings.append(
                f"河网：汇流累积 ≥ {stream_threshold} 像元的河道，"
                f"链化后 {len(stream_lines)} 条折线（仅用于复核展示）"
            )

        provenance_extra = {
            "native": {
                "algorithm": "D8 steepest descent + flow accumulation + reverse BFS",
                "fill_depressions": False,
                "flow_direction_encoding": {
                    str(code): list(offset)
                    for code, offset in enumerate(D8_NEIGHBORS)
                },
                "flow_direction_nodata": -128,
                "sink_code": -1,
                "stream_threshold_cells": stream_threshold,
                "max_cells": ctx.max_cells,
            }
        }
        extra_artifacts = {
            "analysis_dem_tif": analysis.path,
            "flow_direction_tif": fd_path,
            "flow_accum_tif": acc_path,
        }
        outlet_is_sink = bool(fd[snap.row, snap.col] == -1)
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
            engine_version=f"native-d8/1 (numpy {np.__version__})",
            warnings=warnings,
            provenance_extra=provenance_extra,
            stream_network=stream_lines,
            extra_artifacts=extra_artifacts,
            outlet_is_sink=outlet_is_sink,
            snap_evidence=snap.to_evidence(),
        )
