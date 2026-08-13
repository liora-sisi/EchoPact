#!/usr/bin/env python3
"""Create or verify a non-overwriting Echo Pact Git recovery package."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.release_gate import (  # noqa: E402
    ReleaseGateError,
    create_recovery_package,
    verify_recovery_package,
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    actions = root.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="create and independently verify")
    create.add_argument("--repo", required=True)
    create.add_argument("--out", required=True)
    create.add_argument("--head", required=True)
    create.add_argument("--snapshot-name", required=True)
    create.add_argument("--remote", default=None)
    verify = actions.add_parser("verify", help="verify and restore in a temp repo")
    verify.add_argument("--package", required=True)
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.action == "create":
            result = create_recovery_package(
                args.repo, args.out, head_sha=args.head,
                snapshot_name=args.snapshot_name, remote=args.remote,
            )
        else:
            result = verify_recovery_package(args.package)
    except (ReleaseGateError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
