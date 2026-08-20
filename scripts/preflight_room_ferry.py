#!/usr/bin/env python3
"""Create a redacted, read-only acceptance preflight report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.adapters.room_ferry_acceptance import (  # noqa: E402
    run_room_ferry_preflight,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate one Room Ferry backup without conversion, database writes, "
            "network access, or disclosure of message content and source IDs."
        )
    )
    parser.add_argument("input", help="one Room Ferry full-backup JSON file")
    parser.add_argument(
        "--report",
        required=True,
        help="create a new redacted JSON report; an existing path is never overwritten",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_room_ferry_preflight(args.input, args.report)
    except FileNotFoundError as exc:
        if exc.filename is not None:
            print(
                "error: a required file became unavailable during acceptance preflight",
                file=sys.stderr,
            )
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except (FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print(
            "error: acceptance preflight could not safely read or write a file",
            file=sys.stderr,
        )
        return 1

    summary = {
        "schema": report["schema"],
        "input_sha256": report["source"]["sha256"],
        "input_unchanged": report["source"]["input_unchanged"],
        "warnings": sum(item["count"] for item in report["issues"]["warnings"]),
        "fatal": sum(item["count"] for item in report["issues"]["fatal"]),
        "can_proceed_to_conversion": report["decision"]["can_proceed_to_conversion"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["can_proceed_to_conversion"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
