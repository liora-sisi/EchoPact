#!/usr/bin/env python3
"""Capture explicitly selected local Codex threads through app-server."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.adapters.codex_app_server_capture import (  # noqa: E402
    CodexCaptureError,
    capture_codex_threads,
    write_codex_rollout_snapshot,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read exact local Codex thread ids through thread/read and atomically "
            "create one private, checksummed rollout snapshot."
        )
    )
    parser.add_argument(
        "--thread-id",
        action="append",
        required=True,
        help="exact reviewed Codex thread id; repeat to capture more than one",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="new private snapshot path outside the repository",
    )
    parser.add_argument(
        "--codex-executable",
        default="codex",
        help="Codex executable name or path (default: codex)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=30,
        help="per-request timeout from 1 to 300 seconds",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        snapshot = capture_codex_threads(
            args.thread_id,
            codex_executable=args.codex_executable,
            timeout_seconds=args.timeout_seconds,
        )
        result = write_codex_rollout_snapshot(snapshot, args.output)
    except (CodexCaptureError, FileExistsError, FileNotFoundError, OSError) as exc:
        print(
            json.dumps(
                {"captured": False, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    result["captured"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
