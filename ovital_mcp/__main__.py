"""``python -m ovital_mcp`` 入口（stdio MCP 服务）。"""

from __future__ import annotations

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
