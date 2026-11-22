"""Frozen fictional adjacency exam, not a retrieval or whole-pipeline benchmark.

Deliberately supply an old seed, then ask the real selector to distinguish
adjacent revisions, conditions, retellings and branches. No arbitrary corpus
or private query can enter the explicit API command.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import socket
from unittest.mock import patch

from backend.memory import semantic_ordered_evidence as core
from backend.memory import evidence_selector as selection
from backend.memory import evidence_packet as codec
from backend.memory import object_evidence_review_trial as api
from backend.memory.identity import register_agent
from backend.memory.records_v1 import COMPACT_RECORD_SCHEMA_VERSION, import_record_package
from backend.memory.semantic_shadow import file_digest, VERSION as INDEX_VERSION
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory.venice_summary_trial import read_dpapi_key
from backend.mcp.readonly_server import _bounded_result
from scripts.assistant_recall_baseline import _private, implementation
from scripts.semantic_window_exam import write

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/semantic_ordered_exam.json"
OWNER = "semantic-order-synthetic-owner"
MAX_BATCH_USD = Decimal("1.00")


def source_records(fixture):
    result = []
    for case in fixture["cases"]:
        for i, row in enumerate(case["records"]):
            result.append({"record_id": row["id"], "source_kind": "synthetic", "source_ref": "synthetic://ordered-exam",
                "conversation_id": case["id"], "message_id": row["id"], "role": row.get("role", "assistant"),
                "content": row.get("prefix", "") + row.get("repeat", "") * row.get("times", 0) + row["text"],
                "created_at": "2035-05-01T12:00:00Z", "verified": False, "authority": "synthetic",
                "source_cutoff_at": "2035-05-01T12:00:00Z",
                "branch_memberships": row.get("memberships", [{"branch_id": "main", "position": i}])})
    return result


def source_fingerprints():
    state = implementation()
    for script in (Path(__file__), ROOT / "scripts/semantic_window_exam.py"):
        state["source_files_sha256"][script.relative_to(ROOT).as_posix()] = file_digest(script)
    return state


def raw_seed(case, records):
    sources = {row["record_id"]: row for row in records}
    packet = _bounded_result({"memories": [sources[rid] for rid in case["seeds"]], "coverage": {"coverage_gap": False}})
    hits = {}
    for spec in case["records"]:
        if spec["id"] not in case["seeds"] or not spec.get("span_quotes"):
            continue
        text = sources[spec["id"]]["content"]
        hits[spec["id"]] = {"content_sha256": selection.digest(text), "spans": [
            {"start": text.index(quote), "end": text.index(quote) + len(quote)} for quote in spec["span_quotes"]]}
    if hits:
        packet["semantic_shadow"] = {"version": INDEX_VERSION, "status": "hybrid_candidates_only", "selected": hits}
    return packet


def verify_fictional_request(prepared, case, fixture):
    core.ordered.validate_request(prepared["request"])
    if prepared["request"]["question"] != case["query"]:
        raise ValueError("synthetic_question_changed")
    sources = {row["record_id"]: row for row in source_records(fixture)}
    for card in prepared["request"]["candidates"]:
        row = prepared["local"][card["candidate_id"]]
        source = sources[row["record_id"]]
        start, text = row["excerpt_start"], row["text"]
        if (source["conversation_id"] != case["id"] or source["content"][start:start + len(text)] != text
                or card["text"] != text or selection.digest(source["content"]) != row["observed_content_sha256"]
                or selection._metadata(source) != selection._metadata(row)
                or card["role"] != source["role"] or card["source_kind"] != source["source_kind"]):
            raise ValueError("request_not_frozen_fiction")


def prepare(directory):
    directory = _private(directory)
    directory.mkdir(parents=True, exist_ok=False)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    records = source_records(fixture)
    package, db = directory / "synthetic.json", directory / "synthetic.sqlite3"
    write(directory / "frozen-fixture.json", fixture)
    write(package, {"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records})
    register_agent(OWNER, "Synthetic order exam", actor="synthetic-order", db_path=str(db))
    import_record_package(str(package), db_path=str(db), owner_agent_id=OWNER, actor="synthetic-order")
    before = file_digest(db)
    plan = {"schema": "semantic-ordered-exam-v1", "fixture_sha256": file_digest(FIXTURE),
            "source_sha256": before, "implementation": source_fingerprints(), "requests": {}, "cases": [],
            "scope": "fixed_seed_component_exam_not_retrieval_accuracy", "production_accepted": False,
            "real_archive_records_sent": 0}
    with patch.object(socket.socket, "connect", side_effect=RuntimeError("offline preparation")):
        bridge = core.OrderedWindowBridge(db, OWNER, source_sha256=before)
        for case in fixture["cases"]:
            prepared = bridge.prepare(raw_seed(case, records), query=case["query"])
            verify_fictional_request(prepared, case, fixture)
            preview = bridge.preview(prepared)
            write(directory / (case["id"] + ".prepared.json"), prepared)
            write(directory / (case["id"] + ".preview.json"), preview)
            plan["requests"][case["id"]] = {
                "payload_sha256": selection.digest(selection.encoded(api.request_payload(prepared["request"], "openai")).decode()),
                "prepared_sha256": prepared["prepared_sha256"],
                "reserved_usd": str(api.reservation(prepared["request"], "openai"))}
            plan["cases"].append({"id": case["id"], "seed_cards": len(prepared["seed"]["local"]),
                "candidate_cards": len(prepared["local"]), "adjacency": prepared["adjacency"],
                "request_bytes": len(selection.encoded(prepared["request"])), "preview_bytes": len(selection.encoded(preview))})
    plan["reserved_usd"] = str(sum((Decimal(row["reserved_usd"]) for row in plan["requests"].values()), Decimal(0)))
    if Decimal(plan["reserved_usd"]) > MAX_BATCH_USD:
        raise ValueError("batch_ceiling_exceeded")
    bridge.check_snapshot()
    write(directory / "plan.json", plan)
    return plan


def score(case, result):
    brief = codec.expand(result["brief"])
    expected = case["expected"]
    answers = [item for item in brief["items"] if item["relation"] == "answers"]
    allowed = {item["id"] for item in expected["items"]}
    if expected["kind"] == "missing":
        correct = not answers and "missing_answer" in brief.get("gaps", [])
    else:
        relation = "answers" if expected["kind"] == "answers" else "uncertain"
        correct = all(any(item["relation"] == relation and wanted["contains"] in item["text"]
            and any(ref["record_id"] == wanted["id"] for ref in item["references"])
            for item in brief["items"]) for wanted in expected["items"])
        correct &= all(ref["record_id"] in allowed for item in answers for ref in item["references"])
        if expected["kind"] == "ambiguous":
            correct &= not answers and "conflicting_evidence" in brief.get("gaps", [])
    accepted = result["status"] == "selected_requires_review"
    return {"accepted_contract": accepted, "expected_evidence": bool(correct), "passed": accepted and bool(correct),
            "brief_bytes": len(selection.encoded(result["brief"]))}


def run_model(directory, key_file, budget_ledger):
    directory = _private(directory)
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if (plan["fixture_sha256"] != file_digest(FIXTURE) or plan["implementation"] != source_fingerprints()
            or Decimal(plan["reserved_usd"]) > MAX_BATCH_USD):
        raise ValueError("frozen_exam_or_budget_changed")
    bridge = core.OrderedWindowBridge(directory / "synthetic.sqlite3", OWNER, source_sha256=plan["source_sha256"])
    work = []
    for case in fixture["cases"]:
        prepared = json.loads((directory / (case["id"] + ".prepared.json")).read_text(encoding="utf-8"))
        bridge.verify(prepared)
        verify_fictional_request(prepared, case, fixture)
        if prepared["prepared_sha256"] != plan["requests"][case["id"]]["prepared_sha256"]:
            raise ValueError("frozen_prepared_changed")
        work.append((case, prepared))
    report = {"model": api.model("openai"), "new_model_calls": 0, "cases": [], "production_accepted": False,
              "real_archive_records_sent": 0, "scope": plan["scope"], "max_batch_usd": str(MAX_BATCH_USD)}
    with BudgetLedger(_private(budget_ledger), maximum_limit=Decimal("10.00")) as ledger:
        if ledger.data.get("provider") != "openai" or ledger.total + Decimal(plan["reserved_usd"]) > ledger.limit:
            raise ValueError("existing_budget_unavailable")
        write(directory / "model-started.json", {"status": "started_no_automatic_retry", "reserved_usd": plan["reserved_usd"]})
        key = read_dpapi_key(_private(key_file), provider="openai")
        client = api.openai.OpenAIClient(key)
        del key
        api.openai.preflight(client)
        for case, prepared in work:
            case_id = case["id"]
            def select(request):
                def reserve(item):
                    ledger.reserve(amount=Decimal(item["reserved_usd"]), run_id=directory.name,
                                   model=api.model("openai"), case_id=case_id)
                report["new_model_calls"] += 1
                response = api.run_case(client, request, provider="openai",
                    frozen_request_sha256=plan["requests"][case_id]["payload_sha256"], before_request=reserve)
                write(directory / (case_id + ".response.json"), response)
                return response["draft"]
            result = bridge.review(prepared, selector=select)
            write(directory / (case_id + ".review.json"), result)
            entry = {"id": case_id, **score(case, result)}
            report["cases"].append(entry)
            print(json.dumps(entry), flush=True)
        report.update(cumulative_reserved_usd=str(ledger.total), budget_limit_usd=str(ledger.limit))
    if plan["implementation"] != source_fingerprints():
        raise ValueError("implementation_changed_during_exam")
    bridge.check_snapshot()
    write(directory / "model-report.json", report)
    return report


def replay(directory):
    """Audit saved first answers after a local validator fix; never a new exam."""
    directory = _private(directory)
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if plan["fixture_sha256"] != file_digest(FIXTURE):
        raise ValueError("frozen_fixture_changed")
    bridge = core.OrderedWindowBridge(directory / "synthetic.sqlite3", OWNER, source_sha256=plan["source_sha256"])
    report = {"new_model_calls": 0, "kind": "saved_first_answers_local_validation_replay",
              "original_implementation": plan["implementation"], "replay_implementation": source_fingerprints(),
              "request_payloads_unchanged": True, "cases": [], "production_accepted": False}
    for case in fixture["cases"]:
        case_id = case["id"]
        prepared = json.loads((directory / (case_id + ".prepared.json")).read_text(encoding="utf-8"))
        bridge.verify(prepared)
        verify_fictional_request(prepared, case, fixture)
        if selection.digest(selection.encoded(api.request_payload(prepared["request"], "openai")).decode()) != plan["requests"][case_id]["payload_sha256"]:
            raise ValueError("request_payload_changed_not_same_exam")
        response_path = directory / (case_id + ".response.json")
        response = json.loads(response_path.read_text(encoding="utf-8"))
        previous = json.loads((directory / (case_id + ".review.json")).read_text(encoding="utf-8"))
        with patch.object(socket.socket, "connect", side_effect=RuntimeError("offline replay")):
            result = bridge.review(prepared, selector=lambda _: response["draft"])
        report["cases"].append({"id": case_id, **score(case, result), "first_status": previous["status"],
            "same_as_first": result == previous, "response_sha256": file_digest(response_path), "result": result})
    bridge.check_snapshot()
    write(directory / "validation-replay.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run-model", "replay"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--budget-ledger", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        report = prepare(args.output)
        print(json.dumps({"status": "prepared_offline", "reserved_usd": report["reserved_usd"], "cases": report["cases"]}))
    elif args.action == "replay":
        report = replay(args.output)
        print(json.dumps({"new_model_calls": 0, "cases": [{k: v for k, v in row.items() if k != "result"}
                                                         for row in report["cases"]]}))
    else:
        if not args.key_file or not args.budget_ledger:
            parser.error("explicit key and existing ledger required")
        report = run_model(args.output, args.key_file, args.budget_ledger)
        print(json.dumps({"status": "finished", "new_model_calls": report["new_model_calls"],
                          "cumulative_reserved_usd": report["cumulative_reserved_usd"]}))


if __name__ == "__main__":
    main()
