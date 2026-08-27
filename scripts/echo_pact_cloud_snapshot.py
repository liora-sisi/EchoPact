#!/usr/bin/env python3
"""Manage verified, versioned read-only Echo Pact cloud snapshots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.mcp.cloud_snapshot import (  # noqa: E402
    CloudSnapshotError,
    activate_release,
    create_snapshot,
    resolve_active_snapshot,
    rollback_active,
    verify_release,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create and verify immutable read-only Echo Pact snapshots. "
            "The source database is never migrated or modified."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create one new snapshot release")
    create.add_argument("--source-db", required=True)
    create.add_argument("--release-root", required=True)
    create.add_argument("--agent-id", required=True)

    verify = commands.add_parser("verify", help="verify one snapshot release")
    verify.add_argument("--release-dir", required=True)
    verify.add_argument("--agent-id", required=True)

    activate = commands.add_parser("activate", help="atomically activate a release")
    activate.add_argument("--release-dir", required=True)
    activate.add_argument("--pointer", required=True)
    activate.add_argument("--agent-id", required=True)

    show = commands.add_parser("show", help="verify and show the active release")
    show.add_argument("--pointer", required=True)
    show.add_argument("--agent-id", required=True)

    rollback = commands.add_parser("rollback", help="swap active and previous release")
    rollback.add_argument("--pointer", required=True)
    rollback.add_argument("--agent-id", required=True)
    return parser


def _public_result(result):
    """Keep CLI output metadata-only and independent of private record text."""

    if "manifest" in result:
        manifest = result["manifest"]
        return {
            "release_dir": result.get("release_dir"),
            "database_path": result.get("database_path"),
            "snapshot_id": manifest["snapshot_id"],
            "database_sha256": manifest["database_sha256"],
            "database_size": manifest["database_size"],
            "agent_id": manifest["agent_id"],
            "visible_record_count": manifest["visible_record_count"],
            "coverage": manifest["coverage"],
            "sqlite_quick_check": manifest["sqlite_quick_check"],
        }
    return result


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            result = create_snapshot(args.source_db, args.release_root, args.agent_id)
        elif args.command == "verify":
            result = verify_release(
                args.release_dir, expected_agent_id=args.agent_id
            )
        elif args.command == "activate":
            result = activate_release(
                args.release_dir,
                args.pointer,
                expected_agent_id=args.agent_id,
            )
        elif args.command == "show":
            result = resolve_active_snapshot(
                args.pointer, expected_agent_id=args.agent_id
            )
        else:
            result = rollback_active(
                args.pointer, expected_agent_id=args.agent_id
            )
    except (CloudSnapshotError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {"ok": True, **_public_result(result)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
