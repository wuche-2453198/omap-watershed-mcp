"""公共测试 fixtures。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ovital_mcp.config import AppConfig, RuntimePaths
from ovital_mcp.store import TaskStore

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def config(data_root: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "storage": {"data_root": str(data_root)},
            "ovital": {"endpoint": "ws://127.0.0.1:7699", "psk": "test-psk-not-real"},
            "gis": {"engine": "native"},
        }
    )


@pytest.fixture()
def paths(config: AppConfig) -> RuntimePaths:
    return RuntimePaths.build(config, base_dir=REPO_ROOT).ensure()


@pytest.fixture()
def store(config: AppConfig, paths: RuntimePaths) -> TaskStore:
    return TaskStore(config, paths).ensure()


@pytest.fixture()
def sample_polygon_geojson(data_root: Path) -> Path:
    """一个北京附近的小多边形（EPSG:4490 经纬度）。"""

    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [116.30, 39.95],
                            [116.32, 39.95],
                            [116.32, 39.97],
                            [116.30, 39.97],
                            [116.30, 39.95],
                        ]
                    ],
                },
                "properties": {"name": "sample"},
            }
        ],
    }
    path = data_root / "sample.geojson"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
