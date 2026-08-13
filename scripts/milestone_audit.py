#!/usr/bin/env python3
"""Audit the fixed M4.5/M5 commit range, schema, routes, and regressions."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.release_gate import ReleaseGateError, audit_milestone  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--expect", required=True)
    parser.add_argument("--from-sha", required=True)
    parser.add_argument("--to-sha", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        result = audit_milestone(
            args.repo, args.expect, args.out, from_sha=args.from_sha,
            to_sha=args.to_sha, run_validation=True,
        )
    except (ReleaseGateError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
