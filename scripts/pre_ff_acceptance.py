#!/usr/bin/env python3
"""Run the seven fail-closed gates before a separately authorised FF push."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.release_gate import ReleaseGateError, pre_ff_acceptance  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--expect", required=True)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--baseline-package", required=True)
    parser.add_argument("--target-package", required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--protected-ref", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        result = pre_ff_acceptance(
            args.repo, args.expect, args.audit, args.baseline_package,
            args.target_package, args.out, remote=args.remote,
            protected_ref=args.protected_ref, target_sha=args.target,
        )
    except (ReleaseGateError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
