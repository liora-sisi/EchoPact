"""Frozen synthetic window-delivery exam; prepare offline, optional explicit API.

Only this checked-in fictional corpus may enter the API route. Real old cases
use the bridge locally in a separate caller; there is no arbitrary-corpus flag.
"""
from __future__ import annotations

import argparse
import copy
from decimal import Decimal
import json
from pathlib import Path
import socket
from unittest.mock import patch

from backend.memory import semantic_evidence as bridge_core
from backend.memory import evidence_selector as selection
from backend.memory import evidence_selector_trial as api
from backend.memory import evidence_packet as codec
from backend.memory.semantic_shadow import LocalEncoder, HybridGateway, build_index, file_digest, canonical_hash
from backend.memory.identity import register_agent
from backend.memory.records_v1 import COMPACT_RECORD_SCHEMA_VERSION, import_record_package
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory.venice_summary_trial import read_dpapi_key
from scripts.assistant_recall_baseline import _private, implementation

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/semantic_window_exam.json"
OWNER = "semantic-window-synthetic-owner"
MAX_BATCH_USD = Decimal("0.75")


def write(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def source_records(fixture):
    return [{"record_id": r["id"], "source_kind": "synthetic", "source_ref": "synthetic://window-exam",
             "conversation_id": "window-demo", "message_id": r["id"], "role": "assistant",
             "content": r["prefix"] + r["repeat"] * r["times"] + r["suffix"],
             "created_at": "2035-05-01T12:00:00Z", "verified": False, "authority": "synthetic",
             "source_cutoff_at": "2035-05-01T12:00:00Z",
             "branch_memberships": [{"branch_id": "main", "position": i}]}
            for i, r in enumerate(fixture["records"])]


def source_fingerprints():
    state = implementation()
    state["source_files_sha256"]["scripts/semantic_window_exam.py"] = file_digest(__file__)
    return state


def prepare(directory, model):
    directory = _private(directory)
    directory.mkdir(parents=True, exist_ok=False)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    write(directory / "frozen-fixture.json", fixture)
    records = source_records(fixture)
    package, db = directory / "synthetic.json", directory / "synthetic.sqlite3"
    write(package, {"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records})
    register_agent(OWNER, "Synthetic window exam", actor="synthetic-window", db_path=str(db))
    import_record_package(str(package), db_path=str(db), owner_agent_id=OWNER, actor="synthetic-window")
    before = file_digest(db)
    plan = {"schema": "semantic-window-exam-v1", "fixture_sha256": file_digest(FIXTURE),
            "source_sha256": before, "implementation": source_fingerprints(), "requests": {}, "results": [],
            "production_accepted": False, "real_archive_records_sent": 0}
    with patch.object(socket.socket, "connect", side_effect=RuntimeError("offline preparation")):
        encoder = LocalEncoder(model)
        plan["index"] = build_index(db, OWNER, directory / "semantic.sqlite3", encoder)
        hybrid = HybridGateway(db, OWNER, directory / "semantic.sqlite3", encoder)
        bridge = bridge_core.WindowBridge(db, OWNER, source_sha256=before)
        for case in fixture["cases"]:
            arguments = {"query": case["query"], "as_of": fixture["reference_at"], "limit": 10}
            for arm in ("lexical", "hybrid"):
                rid = case["id"] + "." + arm
                raw = (hybrid.gateway if arm == "lexical" else hybrid).recall(arguments)
                prepared = bridge.prepare(raw, query=case["query"])
                preview = bridge.preview(prepared)
                write(directory / (rid + ".prepared.json"), prepared)
                write(directory / (rid + ".local-audit.json"), {"raw": raw, "preview": preview})
                payload = api.request_payload(prepared["request"], "openai")
                plan["requests"][rid] = {"payload_sha256": selection.digest(selection.encoded(payload).decode()),
                    "prepared_sha256": prepared["prepared_sha256"],
                    "reserved_usd": str(api.reservation(prepared["request"], "openai"))}
                reached = case["target_id"] is None or any(row["record_id"] == case["target_id"]
                    and case["required"] in row["text"] for row in prepared["local"].values())
                plan["results"].append({"id": rid, "target_window_reached": reached,
                    "candidate_count": len(prepared["local"]), "omitted_candidates": prepared["omitted_candidates"],
                    "raw_bytes": len(selection.encoded(raw)), "request_bytes": len(selection.encoded(prepared["request"])),
                    "preview_bytes": len(selection.encoded(preview))})
    plan["reserved_usd"] = str(sum((Decimal(r["reserved_usd"]) for r in plan["requests"].values()), Decimal(0)))
    if file_digest(db) != before:
        raise ValueError("synthetic_source_changed")
    write(directory / "plan.json", plan)
    return plan


def verify_fictional_request(prepared, case, fixture):
    """Even a frozen output directory cannot substitute private archive text."""
    bridge_core.validate_ordered_request({**prepared["request"], "source_order_windows": []})
    if prepared["request"]["question"] != case["query"]:
        raise ValueError("synthetic_question_changed")
    sources = {r["record_id"]: r for r in source_records(fixture)}
    for card in prepared["request"]["candidates"]:
        row = prepared["local"][card["candidate_id"]]
        source = sources[row["record_id"]]
        start, text = row["excerpt_start"], row["text"]
        if (source["content"][start:start + len(text)] != text or card["text"] != text
                or selection.digest(source["content"]) != row["observed_content_sha256"]
                or selection._metadata(source) != selection._metadata(row)
                or card["source_kind"] != source["source_kind"] or card["role"] != source["role"]):
            raise ValueError("request_not_frozen_fiction")


def score(case, result):
    accepted = result["status"] == "selected_requires_review"
    brief = codec.expand(result["brief"])
    answers = [item for item in brief["items"] if item["relation"] == "answers"]
    if case["target_id"] is None:
        correct = (not brief["items"] and brief.get("model_sufficiency") == "insufficient"
                   and brief.get("gap") == "missing_answer")
    else:
        correct = (any(case["required"] in item["text"] and any(ref["record_id"] == case["target_id"]
                       for ref in item["references"]) for item in answers)
                   and all(all(ref["record_id"] == case["target_id"] for ref in item["references"]) for item in answers)
                   and not any(case.get("forbidden_answer", "\0") in item["text"] for item in answers))
    return {"accepted_contract": accepted, "expected_evidence": bool(correct), "passed": accepted and bool(correct),
            "brief_bytes": len(selection.encoded(result["brief"]))}


def run_model(directory, key_file, budget_ledger):
    directory = _private(directory)
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if (plan["fixture_sha256"] != file_digest(FIXTURE) or plan["implementation"] != source_fingerprints()
            or Decimal(plan["reserved_usd"]) > MAX_BATCH_USD):
        raise ValueError("frozen_exam_or_budget_changed")
    db = directory / "synthetic.sqlite3"
    bridge = bridge_core.WindowBridge(db, OWNER, source_sha256=plan["source_sha256"])
    work = []
    for case in fixture["cases"]:
        for arm in ("lexical", "hybrid"):
            rid = case["id"] + "." + arm
            prepared = json.loads((directory / (rid + ".prepared.json")).read_text(encoding="utf-8"))
            bridge.verify(prepared)
            verify_fictional_request(prepared, case, fixture)
            if prepared["prepared_sha256"] != plan["requests"][rid]["prepared_sha256"]:
                raise ValueError("frozen_prepared_changed")
            work.append((rid, case, prepared))
    report = {"model": api.model("openai"), "new_model_calls": 0, "cases": [], "production_accepted": False,
              "real_archive_records_sent": 0, "max_batch_usd": str(MAX_BATCH_USD)}
    with BudgetLedger(_private(budget_ledger), maximum_limit=Decimal("10.00")) as ledger:
        if ledger.data.get("provider") != "openai" or ledger.total + Decimal(plan["reserved_usd"]) > ledger.limit:
            raise ValueError("existing_budget_unavailable")
        write(directory / "model-started.json", {"status": "started_no_automatic_retry", "reserved_usd": plan["reserved_usd"]})
        key = read_dpapi_key(_private(key_file), provider="openai")
        client = api.openai.OpenAIClient(key)
        del key
        api.openai.preflight(client)
        for rid, case, prepared in work:
            def select(request):
                def reserve(item):
                    ledger.reserve(amount=Decimal(item["reserved_usd"]), run_id=directory.name, model=api.model("openai"), case_id=rid)
                report["new_model_calls"] += 1
                response = api.run_case(client, request, provider="openai",
                    frozen_request_sha256=plan["requests"][rid]["payload_sha256"], before_request=reserve)
                write(directory / (rid + ".response.json"), response)
                return response["draft"]
            result = bridge.review(prepared, selector=select)
            write(directory / (rid + ".review.json"), result)
            entry = {"id": rid, **score(case, result)}
            report["cases"].append(entry)
            print(json.dumps(entry), flush=True)
        report.update(cumulative_reserved_usd=str(ledger.total), budget_limit_usd=str(ledger.limit))
    if plan["implementation"] != source_fingerprints():
        raise ValueError("implementation_changed_during_exam")
    bridge.check_snapshot()
    write(directory / "model-report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run-model"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model-directory", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--budget-ledger", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        if not args.model_directory:
            parser.error("--model-directory required for offline preparation")
        report = prepare(args.output, args.model_directory)
        print(json.dumps({"status": "prepared_offline", "reserved_usd": report["reserved_usd"], "results": report["results"]}))
    else:
        if not args.key_file or not args.budget_ledger:
            parser.error("explicit key and existing ledger required")
        report = run_model(args.output, args.key_file, args.budget_ledger)
        print(json.dumps({"status": "finished", "new_model_calls": report["new_model_calls"],
                          "cumulative_reserved_usd": report["cumulative_reserved_usd"]}))


if __name__ == "__main__":
    main()
