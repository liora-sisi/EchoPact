#!/usr/bin/env python3
"""Dry-run or convert one Room Ferry full-backup v1 JSON file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.adapters.room_ferry_v1 import (  # noqa: E402
    FerryAdapterError,
    dry_run_ferry_backup,
    write_converted_package,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a UTF-8 Room Ferry full-backup v1 JSON and convert it "
            "to echo-pact-records-v1 without network access."
        )
    )
    parser.add_argument("input", help="one Room Ferry full-backup JSON file")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report only; never write a formal record package",
    )
    action.add_argument(
        "--output",
        help="create a new echo-pact-records-v1 JSON file atomically",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.dry_run:
        report = dry_run_ferry_backup(args.input)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["can_convert"] else 2
    try:
        result = write_converted_package(args.input, args.output)
    except FerryAdapterError as exc:
        output = exc.report or {"can_convert": False, "error": str(exc)}
        print(json.dumps(output, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except (FileExistsError, FileNotFoundError, OSError) as exc:
        print(
            json.dumps(
                {"can_convert": False, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
