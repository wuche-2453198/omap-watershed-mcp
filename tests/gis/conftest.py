"""GIS 测试 conftest：路径、matplotlib 缓存目录、GRASS 可用性。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
for path in (ROOT, HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# matplotlib 不得写 ~/.matplotlib（沙箱/只读 HOME 会报错）
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".cache" / "mpl"))


def _grass_availability() -> tuple[bool, str]:
    try:
        from ovital_mcp.gis import get_engine

        engine = get_engine("grass")
        return engine.available()
    except Exception as exc:  # noqa: BLE001 - 检测失败按不可用处理
        return False, str(exc)


_GRASS_OK, _GRASS_REASON = _grass_availability()

requires_grass = pytest.mark.skipif(
    not _GRASS_OK,
    reason=f"GRASS 不可用（跳过真实 GRASS 验证）：{_GRASS_REASON}",
)


@pytest.fixture(scope="session")
def grass_reason() -> str:
    return _GRASS_REASON


@pytest.fixture(scope="session")
def expected_fixture() -> dict:
    from helpers import load_expected

    return load_expected()


@pytest.fixture(scope="session")
def synthetic_dir() -> Path:
    from helpers import FIXTURES

    return FIXTURES
