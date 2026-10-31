"""GRASS 引擎真实运行测试（GRASS 不可用时整组 skip 并记录原因）。

验证口径（与 native 不同，必须分开写）：

- native：严格 D8 最陡下降 → 解析真值逐像元一致（见 test_gis_native_engine）；
- GRASS：``r.watershed -s`` 的 SFD 用 A^T **最小代价路径**，不是严格 D8
  最陡下降。因此在"无并列坡度且坡向单一/收敛"的解析地形上两者应当一致
  （tilted_plane 60 像元、valley_converge 1200 像元）；在径向对称碗地形上
  两种算法会产生不同流向（实测 61/1681 像元方向不同 → 流域被分解），
  这属于算法差异，不能用 native 真值硬套 GRASS。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from PIL import Image

from conftest import requires_grass
from helpers import (
    FIXTURES,
    analytic_mask,
    assert_close,
    read_mask,
    run_scenario,
    scenario,
    warnings_text,
)

from ovital_mcp.gis import WatershedParams, get_engine, run_watershed
from ovital_mcp.gis.crs import prepare_analysis_dem
from ovital_mcp.gis.grass import (
    GRASS_DIRECTION_OFFSETS,
    GrassEngine,
    dir_to_receiver,
    reset_availability_cache,
)
from ovital_mcp.gis.native import compute_flow_direction, upstream_mask


@requires_grass
def test_grass_available_and_selected_by_auto() -> None:
    engine = get_engine("grass")
    ok, reason = engine.available()
    assert ok, reason
    assert engine.version is not None and engine.version.startswith("GRASS")
    auto = get_engine("auto")
    assert auto.name == "grass", "GRASS 可用时 auto 应优先 GRASS"


@requires_grass
def test_grass_tilted_plane_matches_analytic_truth(tmp_path: Path) -> None:
    """无并列坡度的倾斜平面：GRASS 应精确复现解析真值 60 像元 / 54,000 m²。"""

    spec = scenario("tilted_plane")
    result = run_scenario(tmp_path / "grass_plane", "tilted_plane", engine="grass")
    assert result.ok, result.error
    assert result.engine_used == "grass"
    assert result.engine_version.startswith("GRASS")

    mask = read_mask(result)
    expected = analytic_mask(spec["mask_rule"], *mask.shape)
    assert expected is not None
    assert np.array_equal(mask, expected), "GRASS 掩膜与解析真值不一致"
    assert int(mask.sum()) == 60
    assert_close(result.area_m2, 54000.0, rel=1e-6)
    assert result.geometry_valid is True
    assert result.hole_count == 0
    # 边界矢量面积与掩膜像元面积一致（GRASS r.to.vect 精确贴像元边界）
    assert abs(result.area_m2 - int(mask.sum()) * 900.0) <= 1e-3

    # 记录 GRASS 版本与命令（provenance 证据）
    provenance = json.loads(Path(result.artifacts["provenance_json"]).read_text())
    grass = provenance["grass"]
    assert grass["version"].startswith("GRASS")
    assert [command["phase"] for command in grass["commands"]] == [
        "create_location",
        "watershed_sfd",
        "water_outlet",
    ]
    assert "r.watershed -s" in grass["commands"][1]["script"]
    assert provenance["verified_against_real_device"] is False
    assert provenance["offline_computed"] is True


@requires_grass
def test_grass_direction_encoding_empirically_verified(tmp_path: Path) -> None:
    """倾斜平面（东向下降）内部像元 GRASS drainage 必须全为 8（East）。"""

    result = run_scenario(tmp_path / "grass_dir", "tilted_plane", engine="grass")
    assert result.ok, result.error
    with rasterio.open(result.artifacts["flow_direction_tif"]) as ds:
        direction = ds.read(1)
    assert np.all(direction[7, 0:59] == 8), "GRASS drainage 编码 8 应表示 East"
    # 出口在东边界外流 → 负值（-8）
    assert direction[7, 59] == -8
    provenance = json.loads(Path(result.artifacts["provenance_json"]).read_text())
    encoding = provenance["grass"]["drainage_encoding"]
    assert encoding["8"] == [0, 1]  # (行偏移, 列偏移) = 东
    assert set(GRASS_DIRECTION_OFFSETS) == {1, 2, 3, 4, 5, 6, 7, 8}


@requires_grass
def test_grass_valley_converge_2d_analytic(tmp_path: Path) -> None:
    """二维收敛谷（无并列坡度）：GRASS 与解析真值一致（1200 像元 / 1,080,000 m²）。"""

    spec = scenario("valley_converge")
    result = run_scenario(tmp_path / "grass_valley", "valley_converge", engine="grass")
    assert result.ok, result.error
    mask = read_mask(result)
    expected = analytic_mask(spec["mask_rule"], *mask.shape)
    assert expected is not None
    assert np.array_equal(mask, expected), "GRASS 二维收敛谷结果与解析真值不一致"
    assert int(mask.sum()) == 1200
    assert_close(result.area_m2, 1080000.0, rel=1e-6)
    assert result.geometry_valid is True


@requires_grass
def test_grass_determinism_two_runs_identical(tmp_path: Path) -> None:
    first = run_scenario(tmp_path / "grass_a", "valley_converge", engine="grass")
    second = run_scenario(tmp_path / "grass_b", "valley_converge", engine="grass")
    assert first.ok and second.ok
    det_a = first.provenance["determinism"]
    det_b = second.provenance["determinism"]
    for key in ("fingerprint_sha256", "mask_sha256", "boundary_wkb_sha256"):
        assert det_a[key] == det_b[key], key
    assert np.array_equal(read_mask(first), read_mask(second))


@requires_grass
def test_grass_least_cost_differs_from_strict_d8_on_radial_bowl(tmp_path: Path) -> None:
    """径向对称碗：记录并解释 GRASS（A^T 最小代价）与 native（严格 D8）的差异。

    这是**算法差异**而非适配器 bug：用 GRASS 自己的 drainage 图做反向 BFS，
    必须精确复现 GRASS 自己的 basin（证明我们读取/对齐/编码都正确）。
    """

    spec = scenario("single_bowl")
    grass_result = run_scenario(tmp_path / "grass_bowl", "single_bowl", engine="grass")
    native_result = run_scenario(tmp_path / "native_bowl", "single_bowl", engine="native")
    assert grass_result.ok and native_result.ok

    grass_mask = read_mask(grass_result)
    native_mask = read_mask(native_result)
    assert int(native_mask.sum()) == 1681  # native 严格 D8 与解析真值一致
    grass_cells = int(grass_mask.sum())
    assert 0 < grass_cells < 1681, "GRASS 结果应为 native 真值的真子集（算法差异）"
    assert not np.any(grass_mask & ~native_mask), "GRASS 集水区不应超出解析真值"

    # 适配器正确性：GRASS basin == 对 GRASS drainage 图从出口做反向 BFS
    analysis = prepare_analysis_dem(
        str(Path(native_result.dem_info["path"])),
        tmp_path / "grass_bowl" / "analysis_check.tif",
        metric_crs=native_result.metric_crs,
        read_array=True,
    )
    with rasterio.open(grass_result.artifacts["flow_direction_tif"]) as ds:
        direction = ds.read(1).astype(np.int64)
    valid = analysis.ensure_valid_mask()
    receiver = dir_to_receiver(direction, valid)
    snapped = grass_result.provenance["outlet_snap"]["snapped_cell"]
    start = int(snapped[0]) * analysis.width + int(snapped[1])
    bfs_mask = upstream_mask(receiver.ravel(), start, shape=(analysis.height, analysis.width))
    assert np.array_equal(bfs_mask, grass_mask), "GRASS basin 与 GRASS drainage 反向 BFS 不一致"

    # 方向差异量化：GRASS 最小代价与严格 D8 在部分像元上流向不同
    z = analysis.ensure_array()
    _fd, native_receiver = compute_flow_direction(
        z, valid, analysis.res_x, analysis.res_y
    )
    both = (native_receiver.ravel() >= 0) & (receiver.ravel() >= 0)
    agreement = float(
        np.count_nonzero(
            both & (native_receiver.ravel() == receiver.ravel())
        )
    ) / float(np.count_nonzero(both))
    assert agreement > 0.9, f"两种算法方向一致率异常低：{agreement:.3f}"
    assert agreement < 1.0, "预期 GRASS 最小代价与严格 D8 存在可量化的方向差异"
    # 面积/几何仍然自洽
    assert_close(grass_result.area_m2, grass_cells * 900.0, rel=1e-6)
    assert grass_result.geometry_valid is True
    assert "GRASS" in warnings_text(grass_result)


@requires_grass
def test_grass_unavailable_when_command_missing(tmp_path: Path) -> None:
    reset_availability_cache()
    engine = GrassEngine(grass_cmd="/nonexistent/definitely-not-grass")
    ok, reason = engine.available()
    assert ok is False
    assert "未找到 GRASS" in reason or "不可用" in reason
    outlet = scenario("tilted_plane")["outlet_lonlat"]
    result = run_watershed(
        WatershedParams(
            dem_path=str(FIXTURES / scenario("tilted_plane")["dem"]),
            outlet_lonlat=tuple(outlet),
            engine="grass",
            extra={"grass_cmd": "/nonexistent/definitely-not-grass"},
        ),
        tmp_path / "no_grass",
    )
    assert result.ok is False
    assert result.error_code == "ENGINE_UNAVAILABLE"


@requires_grass
def test_grass_review_pack_decodable(tmp_path: Path) -> None:
    from ovital_mcp.gis.review import render_review_pack

    result = run_scenario(tmp_path / "grass_review", "valley_converge", engine="grass")
    assert result.ok, result.error
    pack = render_review_pack(result, Path(result.task_dir), max_pixels=800)
    assert pack["ok"] is True, pack["warnings"]
    assert len(pack["figures"]) >= 2
    for figure in pack["figures"]:
        with Image.open(figure["path"]) as image:
            image.verify()
        assert Path(figure["path"]).stat().st_size <= 4 * 1024 * 1024
        assert figure["crs"] == "EPSG:4490"
