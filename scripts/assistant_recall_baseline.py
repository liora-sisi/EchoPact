#!/usr/bin/env python3
"""Read-only local assistant recall evaluation; no model, network, or credentials."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.mcp.readonly_server import ReadonlyGateway
from backend.memory.assistant_baseline import BaselineError, run_suite, text_sha, validate_suite
from backend.memory.identity import can_read_record
from backend.memory.records_v1 import _connect_readonly
from backend.memory.recall_debug import load_speech_query_policy
from scripts.summary_cabinet_trial import write_json


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _private(path):
    resolved = path.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise BaselineError("private_artifacts_must_stay_outside_repository")
    return resolved


def implementation():
    paths = {Path(__file__).resolve(), ROOT / "scripts/summary_cabinet_trial.py"}
    for directory in ("backend/memory", "backend/mcp"):
        paths.update((ROOT / directory).glob("*.py"))
    return {"source_files_sha256": {p.relative_to(ROOT).as_posix(): file_sha(p) for p in sorted(paths)},
            "python_version": sys.version.split()[0], "sqlite_version": sqlite3.sqlite_version}


def validate_targets(db, suite, agent_id):
    with closing(_connect_readonly(str(db))) as conn:
        for case in suite["cases"]:
            for target in case["targets"]:
                row = conn.execute("SELECT id, content, source_kind, conversation_id FROM records_v1 WHERE record_id = ?",
                                   (target["record_id"],)).fetchone()
                if row is None or not can_read_record(conn, agent_id, row["id"]):
                    raise BaselineError("review_target_missing_or_not_visible")
                if (text_sha(row["content"]) != target["content_sha256"]
                        or target["quote"] not in row["content"]
                        or any(row[k] != target[k] for k in ("source_kind", "conversation_id"))):
                    raise BaselineError("review_target_source_changed")


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地只读验收：自然问法、追查轮次、上下文大小与原文是否仍在。")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--speech-policy", type=Path)
    parser.add_argument("--limit", type=int, choices=range(1, 11), default=10)
    args = parser.parse_args(argv)
    output = _private(args.output_dir)
    cases_path = _private(args.cases)
    db = _private(args.db)
    if output.exists():
        raise BaselineError("new_output_directory_required")
    if cases_path.stat().st_size > 100000:
        raise BaselineError("case_file_too_large")
    suite = json.loads(cases_path.read_text(encoding="utf-8-sig"))
    validate_suite(suite)
    # A frozen offline generation is required. A live WAL can change its visible
    # contents without changing the main file's hash; do not benchmark that way.
    if any(Path(str(db) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise BaselineError("frozen_database_without_wal_required")
    before = file_sha(db)
    if before != suite["corpus_sha256"]:
        raise BaselineError("database_does_not_match_frozen_cases")
    validate_targets(db, suite, args.agent_id)
    policy = load_speech_query_policy(str(args.speech_policy)) if args.speech_policy else None
    policy_sha = file_sha(args.speech_policy) if args.speech_policy else None
    code = implementation()
    gateway = ReadonlyGateway(str(db), args.agent_id, speech_query_policy=policy)
    gateway.validate()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "cases.private.json", suite)
    write_json(output / "started.json", {"database_sha256": before,
        "agent_id_sha256": text_sha(args.agent_id), "speech_policy_sha256": policy_sha,
        "implementation": code, "read_only": True, "model_calls": 0})
    try:
        report = run_suite(suite, gateway.recall, limit=args.limit)
        after = file_sha(db)
        if after != before or any(Path(str(db) + s).exists() for s in ("-wal", "-journal")):
            raise BaselineError("database_changed_during_evaluation")
        if implementation() != code or (args.speech_policy and file_sha(args.speech_policy) != policy_sha):
            raise BaselineError("implementation_or_policy_changed_during_evaluation")
        report.update(database_unchanged=True, agent_id_sha256=text_sha(args.agent_id),
                      speech_policy_sha256=policy_sha, implementation=code)
        write_json(output / "report.json", report)
    except Exception as error:
        write_json(output / "failed.json", {"status": "incomplete_not_a_pass",
            "error_code": str(error) if isinstance(error, BaselineError) else "failure_details_suppressed"})
        raise BaselineError("baseline_incomplete_see_private_receipt") from None
    print(json.dumps({"status": "measured_not_production_acceptance", "cases": len(report["cases"]),
                      "retrieval_calls": report["retrieval_calls"], "model_calls": 0,
                      "database_unchanged": True}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaselineError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit("baseline_failed_details_suppressed") from None
