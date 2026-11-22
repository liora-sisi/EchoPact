"""Offline actual-embedding experiment, with an unchanged lexical control arm.

Outputs may contain private source packets: --output must be outside this checkout.
No planner, selector, answering API, upload, production registration or retries.
"""
from __future__ import annotations

import argparse
import copy
import json
import socket
import time
from pathlib import Path
from unittest.mock import patch

from backend.memory.semantic_shadow import LocalEncoder, HybridGateway, build_index, file_digest, canonical_hash
from backend.memory.identity import register_agent
from backend.memory.records_v1 import COMPACT_RECORD_SCHEMA_VERSION, import_record_package
from scripts.assistant_recall_baseline import _private, implementation

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/semantic_shadow_trial.json"
REFERENCE = "2035-05-02T00:00:00Z"
OWNER = "semantic-trial-owner"


def create_synthetic(output):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    records = [{"record_id": rid, "source_kind": "synthetic", "source_ref": "synthetic://semantic-shadow",
                "conversation_id": "semantic-demo", "message_id": rid, "role": "user", "content": text,
                "created_at": "2035-05-01T12:00:00Z", "verified": False, "authority": "synthetic",
                "source_cutoff_at": "2035-05-01T12:00:00Z",
                "branch_memberships": [{"branch_id": "main", "position": i}]}
               for i, (rid, text) in enumerate(fixture["records"])]
    package, db = output / "synthetic.json", output / "synthetic.sqlite3"
    package.write_text(json.dumps({"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records},
                                   ensure_ascii=False), encoding="utf-8")
    register_agent(OWNER, "Local semantic trial", actor="semantic-trial", db_path=str(db))
    import_record_package(str(package), db_path=str(db), owner_agent_id=OWNER, actor="semantic-trial")
    return db, fixture["cases"], REFERENCE, file_digest(FIXTURE)


def compare(db, cases, reference, encoder, output, *, limit=3):
    output = _private(Path(output))
    frozen = copy.deepcopy(cases)
    provenance = implementation()
    provenance["source_files_sha256"]["scripts/semantic_shadow_trial.py"] = file_digest(__file__)
    with (output / "frozen-cases.private.json").open("x", encoding="utf-8") as stream:
        json.dump({"cases": frozen, "reference_at": reference, "limit": limit}, stream, ensure_ascii=False, indent=2)
    with patch.object(socket.socket, "connect", side_effect=RuntimeError("offline experiment")):
        report = _compare(db, frozen, reference, encoder, output, limit=limit)
    report.update(implementation=provenance, cases_sha256=canonical_hash(frozen), reference_at=reference)
    return report


def _compare(db, cases, reference, encoder, output, *, limit):
    start = time.perf_counter()
    before = file_digest(db)
    meta = build_index(db, OWNER, output / "semantic.sqlite3", encoder)
    build_seconds = time.perf_counter() - start
    runner = HybridGateway(db, OWNER, output / "semantic.sqlite3", encoder)
    results = []
    for case in cases:
        args = {"query": case["query"], "limit": limit, "as_of": reference}
        a = time.perf_counter()
        baseline = runner.gateway.recall(args)
        baseline_seconds = time.perf_counter() - a
        a = time.perf_counter()
        hybrid = runner.recall(args)
        hybrid_seconds = time.perf_counter() - a
        left = [r["record_id"] for r in baseline["memories"]]
        right = [r["record_id"] for r in hybrid["memories"]]
        without_trace = copy.deepcopy(hybrid)
        trace = without_trace.pop("semantic_shadow")
        targets = case.get("targets", [case["target"]] if "target" in case else [])
        results.append({"case_id": case["id"], "baseline_ids": left, "hybrid_ids": right,
                        "targets": targets, "baseline_hits": len(set(left) & set(targets)),
                        "hybrid_hits": len(set(right) & set(targets)),
                        "unchanged_required": case.get("unchanged", False),
                        "unchanged": baseline == without_trace, "no_answer": case.get("no_answer", False),
                        "status": trace["status"], "baseline_seconds": baseline_seconds,
                        "hybrid_seconds": hybrid_seconds,
                        "baseline_bytes": len(json.dumps(baseline, ensure_ascii=False).encode()),
                        "hybrid_bytes": len(json.dumps(hybrid, ensure_ascii=False).encode())})
        (output / (case["id"] + ".packets.json")).write_text(
            json.dumps({"baseline": baseline, "hybrid": hybrid}, ensure_ascii=False, indent=2), encoding="utf-8")
    if file_digest(db) != before:
        raise RuntimeError("source database changed")
    report = {"schema": "echo-semantic-first-experiment-v1", "model": encoder.manifest,
              "source_sha256": before, "index": meta, "index_seconds": build_seconds, "limit": limit,
              "results": results, "target_count": sum(len(c["targets"]) for c in results),
              "baseline_hits": sum(c["baseline_hits"] for c in results),
              "hybrid_hits": sum(c["hybrid_hits"] for c in results),
              "controls_unchanged": all(c["unchanged"] for c in results if c["unchanged_required"]),
              "paid_model_calls": 0, "network_connections_allowed": False,
              "source_unchanged": True, "production_accepted": False,
              "limitations": ["candidate retrieval only; no final answer or downstream selector exam",
                              "RRF may return unrelated candidates on no-answer questions",
                              "constrained questions use unchanged lexical route",
                              "record-count budget equal; candidate packet byte lengths are measured, not equalized"]}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-directory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output = _private(args.output)
    args.output.mkdir(parents=True, exist_ok=False)
    db, cases, reference, fixture_hash = create_synthetic(args.output)
    with patch.object(socket.socket, "connect", side_effect=RuntimeError("offline experiment")):
        start = time.perf_counter()
        encoder = LocalEncoder(args.model_directory)
        loaded = time.perf_counter() - start
        report = compare(db, cases, reference, encoder, args.output)
        report.update(model_load_seconds=loaded, fixture_sha256=fixture_hash, corpus="synthetic_development")
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ["target_count", "baseline_hits", "hybrid_hits", "controls_unchanged",
                                            "model_load_seconds", "index_seconds", "production_accepted"]}))


if __name__ == "__main__":
    main()
