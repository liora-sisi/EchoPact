#!/usr/bin/env python3
"""Dry-run or convert one single-room GuiJian JSON export."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.adapters.guijian_v1 import (  # noqa: E402
    GuijianAdapterError,
    dry_run_guijian_export,
    write_converted_package,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate one offline GuiJian ChatGPT room export and convert it "
            "to compact echo-pact-records-v2 without network access."
        )
    )
    parser.add_argument("input", help="one GuiJian single-room JSON export")
    top_confirmation = parser.add_mutually_exclusive_group()
    top_confirmation.add_argument(
        "--top-witness",
        help=(
            "optional older same-room export that confirmed the visible top; "
            "used only to validate a matching leading fingerprint sequence"
        ),
    )
    top_confirmation.add_argument(
        "--attest-visible-top",
        action="store_true",
        help=(
            "record an explicit human observation that this capture reached "
            "the visible top and its first message was recognized"
        ),
    )
    parser.add_argument(
        "--repair-redundant-segments",
        action="store_true",
        help=(
            "select one raw segment only when it order-preservingly contains "
            "every other segment; retain repair evidence in the package"
        ),
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report only; never write a formal record package",
    )
    action.add_argument(
        "--output",
        help="create a new echo-pact-records-v2 JSON file atomically",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.dry_run:
        report = dry_run_guijian_export(
            args.input,
            top_witness_path=args.top_witness,
            manual_top_attestation=args.attest_visible_top,
            repair_redundant_segments=args.repair_redundant_segments,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["can_convert"] else 2
    try:
        result = write_converted_package(
            args.input,
            args.output,
            top_witness_path=args.top_witness,
            manual_top_attestation=args.attest_visible_top,
            repair_redundant_segments=args.repair_redundant_segments,
        )
    except GuijianAdapterError as exc:
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
