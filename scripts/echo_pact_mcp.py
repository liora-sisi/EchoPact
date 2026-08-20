#!/usr/bin/env python3
"""Location-independent launcher for the local Echo Pact MCP server."""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.mcp.readonly_server import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
