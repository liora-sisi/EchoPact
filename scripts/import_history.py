#!/usr/bin/env python3
"""Import versioned Echo Pact record packages into a local SQLite database.

The supported interchange formats are legacy ``echo-pact-records-v1`` and
compact ``echo-pact-records-v2``. The source file is read-only. Import batches
are transactional and idempotent by record_id plus exact content.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.memory.records_v1 import (  # noqa: E402
    RecordPackageError,
    check_records_index_consistency,
    import_record_package,
    load_record_package,
    migrate_records_db,
    rebuild_records_index,
)


# Retained for callers of the pre-V1 scaffold.  V1 recovery does not depend on
# this file: committed transactional batches are discovered by record_id.
CHECKPOINT_FILE = "/opt/echo-pact/import_checkpoint.json"


def load_checkpoint() -> Dict[str, int]:
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, encoding="utf-8") as checkpoint:
            return json.load(checkpoint)
    return {"last_index": 0, "total": 0}


def save_checkpoint(index: int, total: int) -> None:
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as checkpoint:
        json.dump({"last_index": index, "total": total}, checkpoint)


def parse_conversations(filepath: str):
    """Compatibility name: parse a standard V1 package into normalized records."""

    return load_record_package(filepath).records


def import_memories(
    filepath: str,
    agent_id: str = "default",
    *,
    db_path: Optional[str] = None,
    batch_size: int = 100,
) -> Dict[str, Any]:
    """Compatibility entry point backed by the real V1 importer.

    ``agent_id`` is intentionally ignored because V1 identity is expressed by
    conversation_id and branch_id rather than a process-wide agent label.
    """

    del agent_id
    if not Path(filepath).is_file():
        summary = {
            "schema_version": "echo-pact-records-v1",
            "added": 0,
            "skipped": 0,
            "failed": 0,
            "knowledge_cutoff_at": None,
            "latest_record_at": None,
            "notice": f"record package not found: {filepath}",
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary
    summary = import_record_package(
        filepath, db_path=db_path, batch_size=batch_size
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import Echo Pact records-v1/v2 packages without network access."
    )
    parser.add_argument("input", nargs="?", help="V1/V2 JSON or V1 JSONL record package")
    parser.add_argument(
        "--db",
        dest="db_path",
        help="SQLite target path (back up a real database before migration)",
    )
    parser.add_argument("--batch-size", type=int, default=100)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--migrate-only", action="store_true")
    action.add_argument("--check-index", action="store_true")
    action.add_argument("--repair-index", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.migrate_only:
            result = migrate_records_db(args.db_path)
        elif args.check_index:
            result = check_records_index_consistency(args.db_path)
        elif args.repair_index:
            result = rebuild_records_index(args.db_path)
        else:
            if not args.input:
                _build_parser().error("input is required unless an index action is used")
            result = import_record_package(
                args.input,
                db_path=args.db_path,
                batch_size=args.batch_size,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 3
    except RecordPackageError as exc:
        output = dict(exc.summary)
        output["error"] = str(exc)
        print(json.dumps(output, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {"failed": 1, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
