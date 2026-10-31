"""复核图测试：PNG 真实可解码、尺寸/大小约束、经纬度 bbox 口径、分段图布局。

F-11 回归重点：``figure.bbox_lonlat`` 必须是 EPSG:4490 经纬度，且与
``segments.geojson`` 里同一段的 ``bbox_lonlat`` 一致；``figure.crs`` 口径统一。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from helpers import FIXTURES, run_scenario

from ovital_mcp.gis.review import (
    MAX_PNG_BYTES,
    render_hillshade_contours,
    render_overview,
    render_review_pack,
    render_segment,
)


@pytest.fixture(scope="module")
def engine_result(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("review_engine")
    result = run_scenario(out, "two_bowl_ridge", render_overview=True)
    assert result.ok, result.error
    return result


@pytest.fixture(scope="module")
def engine_result_tall(tmp_path_factory: pytest.TempPathFactory):
    """南北向长条 DEM（600 m×1800 m）：触发 side 布局与窄图刻度修复。"""

    out = tmp_path_factory.mktemp("review_engine_tall")
    result = run_scenario(out, "valley_converge_tall", render_overview=False)
    assert result.ok, result.error
    return result


def _decode(path: str | Path) -> tuple[int, int]:
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        assert image.format == "PNG"
        return image.size


def _is_lonlat_bbox(bbox: list[float]) -> bool:
    return (
        len(bbox) == 4
        and -180.0 <= bbox[0] <= bbox[2] <= 180.0
        and -90.0 <= bbox[1] <= bbox[3] <= 90.0
    )


def _segment_bboxes(result) -> dict[int, list[float]]:
    data = json.loads(Path(result.artifacts["segments_geojson"]).read_text(encoding="utf-8"))
    return {
        int(feature["properties"]["segment_id"]): feature["properties"]["bbox_lonlat"]
        for feature in data["features"]
    }


def test_review_pack_generates_decodable_pngs(engine_result) -> None:
    task_dir = Path(engine_result.task_dir)
    pack = render_review_pack(engine_result, task_dir, max_pixels=1200)
    assert pack["ok"] is True, pack["warnings"]
    assert pack["warnings"] == []
    figures = pack["figures"]
    assert len(figures) >= 5  # overview + 4 段
    figure_ids = {item["figure_id"] for item in figures}
    assert "overview" in figure_ids
    assert any(item.startswith("seg-") for item in figure_ids)

    for figure in figures:
        path = Path(figure["path"])
        assert path.is_file()
        width, height = _decode(path)
        assert width > 400 and height > 250
        assert width == figure["width_px"] and height == figure["height_px"]
        assert path.stat().st_size <= MAX_PNG_BYTES
        assert figure["crs"] == "EPSG:4490"
        assert figure["pixel_size_m"] == pytest.approx(30.0)
        assert _is_lonlat_bbox(figure["bbox_lonlat"]), figure["bbox_lonlat"]
        assert figure["title"]
        assert "offline computed" in figure["caption"]
        assert "verified_against_real_device=false" in figure["caption"]
        assert figure["data_sources"], "每张图必须记录数据来源"
        # 图像不是空白（有实际地形/线条内容）
        with Image.open(path) as image:
            grey = np.asarray(image.convert("L"), dtype=np.float64)
        assert grey.std() > 5.0, f"{figure['figure_id']} 图像过于空白"

    manifest = json.loads((task_dir / "review" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["ok"] is True
    assert manifest["verified_against_real_device"] is False
    assert manifest["evidence_level"] == "offline_computed"
    assert len(manifest["figures"]) == len(figures)


def test_segment_figure_bbox_lonlat_matches_segment_f11(engine_result) -> None:
    """F-11 回归：分段图 bbox 必须与 segment.bbox_lonlat 一致（经纬度）。"""

    task_dir = Path(engine_result.task_dir)
    segment_bboxes = _segment_bboxes(engine_result)
    pack = render_review_pack(engine_result, task_dir, max_pixels=1000)
    assert pack["ok"] is True
    checked = 0
    for figure in pack["figures"]:
        segment_id = figure["segment_id"]
        if segment_id is None:
            # overview 使用 analysis_extent_lonlat，也必须是经纬度
            assert _is_lonlat_bbox(figure["bbox_lonlat"])
            assert figure["bbox_lonlat"] == pytest.approx(
                list(engine_result.analysis_extent_lonlat), abs=1e-9
            )
            continue
        expected = segment_bboxes[int(segment_id)]
        assert _is_lonlat_bbox(figure["bbox_lonlat"]), (
            f"seg-{segment_id} bbox 不是经纬度：{figure['bbox_lonlat']}"
        )
        assert figure["bbox_lonlat"] == pytest.approx(expected, abs=1e-6)
        # 显示范围（外扩）也必须是经纬度，且包含段真实 bbox
        view = figure["view_bbox_lonlat"]
        assert view is not None and _is_lonlat_bbox(view)
        assert view[0] <= figure["bbox_lonlat"][0] + 1e-6
        assert view[1] <= figure["bbox_lonlat"][1] + 1e-6
        assert view[2] >= figure["bbox_lonlat"][2] - 1e-6
        assert view[3] >= figure["bbox_lonlat"][3] - 1e-6
        checked += 1
    assert checked >= 4


def test_render_segment_single_figure(engine_result) -> None:
    task_dir = Path(engine_result.task_dir)
    segment_bboxes = _segment_bboxes(engine_result)
    item = render_segment(engine_result, 1, task_dir, max_pixels=1000)
    assert item["ok"] is True, item["warnings"]
    assert len(item["figures"]) == 1
    figure = item["figures"][0]
    assert figure["segment_id"] == 1
    assert figure["figure_id"] == "seg-01"
    assert figure["crs"] == "EPSG:4490"
    assert figure["bbox_lonlat"] == pytest.approx(segment_bboxes[1], abs=1e-6)
    width, height = _decode(figure["path"])
    assert width == figure["width_px"] and height == figure["height_px"]


def test_render_segment_missing_returns_explicit_failure(engine_result) -> None:
    task_dir = Path(engine_result.task_dir)
    item = render_segment(engine_result, 99, task_dir)
    assert item["ok"] is False
    assert item["figures"] == []
    assert item["warnings"]
    assert "不存在" in item["warnings"][0]


def _rects_overlap(a: list[float], b: list[float], *, eps: float = 1e-6) -> bool:
    return not (
        a[0] + a[2] <= b[0] + eps
        or b[0] + b[2] <= a[0] + eps
        or a[1] + a[3] <= b[1] + eps
        or b[1] + b[3] <= a[1] + eps
    )


def _rect_contains(outer: list[float], inner: list[float], *, eps: float = 1e-6) -> bool:
    return (
        inner[0] >= outer[0] - eps
        and inner[1] >= outer[1] - eps
        and inner[0] + inner[2] <= outer[0] + outer[2] + eps
        and inner[1] + inner[3] <= outer[1] + outer[3] + eps
    )


def _assert_segment_layout(pack: dict, *, expected_modes: set[str] | None = None) -> dict:
    """分段图排版统一断言：地图占比、纵横比、面板不遮挡、刻度可读。"""

    assert pack["ok"] is True, pack["warnings"]
    segment_figures = [item for item in pack["figures"] if item["segment_id"] is not None]
    assert segment_figures, "没有分段图"
    ratios: dict[str, float] = {}
    modes: set[str] = set()
    for figure in segment_figures:
        width, height = _decode(figure["path"])
        ratio = width / height
        ratios[figure["figure_id"]] = ratio
        layout = figure["layout"]
        modes.add(layout["mode"])
        assert layout["mode"] in {"stack", "side"}
        # 图幅纵横比（含 bbox_inches="tight" 裁剪余量）
        assert 0.5 <= ratio <= 2.2, f"{figure['figure_id']} 纵横比异常：{ratio:.2f}"
        # 地图面板高度占比 ≥60%
        fraction = layout["map_panel_height_fraction"]
        assert fraction is not None and fraction >= 0.60, (
            f"{figure['figure_id']} 地图面板只占 {fraction:.1%}"
        )
        map_rect = layout["map_panel_rect"]
        info_rect = layout["info_panel_rect"]
        scale_rect = layout["scale_bar_rect"]
        north_rect = layout["north_arrow_rect"]
        # 信息面板与地图/比例尺/指北针均不重叠
        assert not _rects_overlap(info_rect, map_rect), "信息面板压住地图面板"
        assert not _rects_overlap(info_rect, scale_rect), "信息面板遮挡比例尺"
        assert not _rects_overlap(info_rect, north_rect), "信息面板遮挡指北针"
        assert _rect_contains(map_rect, scale_rect), "比例尺必须在地图面板内部"
        assert _rect_contains(map_rect, north_rect), "指北针必须在地图面板内部"
        # x 轴主刻度：窄图 ≤5 且旋转；宽图 ≥3
        tick_count = layout["x_major_tick_count"]
        assert isinstance(tick_count, int) and tick_count >= 2
        if layout["narrow"]:
            assert tick_count <= 5, f"{figure['figure_id']} 窄图 x 刻度 {tick_count} 个，粘连"
            assert layout["x_tick_rotation_deg"] >= 30, "窄图刻度应旋转 30–45°"
        # 显示范围外扩且包含段真实 bbox；图注说明外扩
        view = figure["view_bbox_lonlat"]
        assert view is not None and _is_lonlat_bbox(view)
        assert view[0] <= figure["bbox_lonlat"][0] + 1e-6
        assert view[1] <= figure["bbox_lonlat"][1] + 1e-6
        assert view[2] >= figure["bbox_lonlat"][2] - 1e-6
        assert view[3] >= figure["bbox_lonlat"][3] - 1e-6
        assert "外扩" in figure["caption"] or "padded" in figure["caption"]
    if expected_modes is not None:
        assert modes <= expected_modes, f"布局模式不符合预期：{modes}"
    return ratios


def test_segment_figure_layout_horizontal(engine_result) -> None:
    """横版/近水平分段图：地图占比 ≥60%、纵横比 ≤2.2、面板不遮挡、刻度可读。"""

    task_dir = Path(engine_result.task_dir)
    pack = render_review_pack(engine_result, task_dir, max_pixels=1200)
    ratios = _assert_segment_layout(pack, expected_modes={"stack"})
    assert ratios["seg-01"] > 1.0, f"东西向长条段图幅被压扁：ratio={ratios['seg-01']:.2f}"


def test_segment_figure_layout_vertical_narrow(engine_result_tall) -> None:
    """竖版/窄图：side 布局、地图占比 ≥60%、x 轴主刻度 ≤5 且旋转。"""

    task_dir = Path(engine_result_tall.task_dir)
    pack = render_review_pack(engine_result_tall, task_dir, max_pixels=1200)
    _assert_segment_layout(pack)
    narrow_figures = [
        item
        for item in pack["figures"]
        if item["segment_id"] is not None and item["layout"].get("narrow")
    ]
    assert narrow_figures, "长条 DEM 上应出现窄图并触发刻度修复"
    for figure in narrow_figures:
        layout = figure["layout"]
        assert layout["mode"] == "side"
        assert layout["x_major_tick_count"] <= 5
        assert layout["x_tick_rotation_deg"] >= 30


def test_render_overview_returns_review_figure(engine_result) -> None:
    figure = render_overview(engine_result, max_pixels=1000)
    assert figure.image_path.endswith("overview.png")
    assert figure.crs == "EPSG:4490"
    assert figure.segment_id is None
    # bbox_lonlat 是分析范围（EPSG:4490）；显示范围外扩 4%
    assert figure.bbox_lonlat == pytest.approx(
        tuple(engine_result.analysis_extent_lonlat), abs=1e-9
    )
    assert figure.layout["view_padding_fraction"] == pytest.approx(0.04)
    view = figure.view_bbox_lonlat
    assert view is not None
    extent = figure.bbox_lonlat
    assert view[0] <= extent[0] + 1e-6 and view[1] <= extent[1] + 1e-6
    assert view[2] >= extent[2] - 1e-6 and view[3] >= extent[3] - 1e-6
    # 概览信息面板补充关键警告行
    key_warnings = figure.layout.get("key_warnings") or []
    assert key_warnings, "概览图必须记录关键警告行"
    assert any("DSM/DTM" in item or "DSM" in item for item in key_warnings)
    assert any("垂直基准" in item or "vertical datum" in item for item in key_warnings)
    assert any("truncated=true" in item for item in key_warnings)
    width, height = _decode(figure.image_path)
    assert width == figure.width_px and height == figure.height_px
    payload = figure.to_dict()
    assert payload["path"] == figure.image_path
    assert payload["crs"] == "EPSG:4490"
    assert payload["view_bbox_lonlat"] is not None


def test_render_hillshade_contours(tmp_path: Path) -> None:
    out = tmp_path / "hillshade.png"
    figure = render_hillshade_contours(
        FIXTURES / "two_bowl_ridge.tif",
        out,
        title="测试地形阴影",
        max_pixels=800,
    )
    assert Path(figure.image_path).is_file()
    width, height = _decode(figure.image_path)
    assert width > 300 and height > 200
    assert Path(figure.image_path).stat().st_size <= MAX_PNG_BYTES
    assert figure.bbox_lonlat is not None and _is_lonlat_bbox(list(figure.bbox_lonlat))
