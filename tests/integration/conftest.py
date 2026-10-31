"""集成测试公共件：把 ``scripts/verify_stdio_harness.py`` 的验证 harness 暴露给 pytest。

写作用域：``tests/integration/**``（独立验证者 D）。不修改业务代码。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture()
def stdio_run_dir(tmp_path: Path) -> Path:
    """每次集成测试独立的 MCP 运行目录（配置/数据目录都在内，不污染仓库）。"""

    run_dir = tmp_path / "stdio-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir
