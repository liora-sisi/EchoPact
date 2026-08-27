#!/usr/bin/env python3
"""Start Echo Pact MCP from a verified active cloud-snapshot pointer."""

from __future__ import annotations

import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.mcp.cloud_snapshot import (  # noqa: E402
    CloudSnapshotError,
    resolve_active_snapshot,
)
from backend.mcp.readonly_server import main as readonly_main  # noqa: E402


def main() -> int:
    pointer = os.getenv("ECHO_PACT_MCP_SNAPSHOT_POINTER", "").strip()
    agent_id = os.getenv("ECHO_PACT_MCP_AGENT_ID", "").strip()
    if not pointer or not agent_id:
        print("Echo Pact cloud MCP configuration is incomplete", file=sys.stderr)
        return 2
    try:
        active = resolve_active_snapshot(pointer, expected_agent_id=agent_id)
    except (CloudSnapshotError, OSError, ValueError):
        print("Echo Pact cloud MCP snapshot validation failed", file=sys.stderr)
        return 2
    os.environ["ECHO_PACT_MCP_DB_PATH"] = active["database_path"]
    return readonly_main()


if __name__ == "__main__":
    raise SystemExit(main())
