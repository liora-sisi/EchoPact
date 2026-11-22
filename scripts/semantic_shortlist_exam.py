"""Explicit frozen fictional two-call exam with one shared existing budget.

No arbitrary corpus is accepted. The second request is derived only from the
frozen fictional DB and validated shortlist, verified and saved before sending.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import socket
from unittest.mock import patch

from backend.memory import semantic_shortlist as core
from backend.memory import semantic_shortlist_trial as shortlist_api
from backend.memory import object_evidence_review_trial as review_api
from backend.memory import evidence_selector as selection
from backend.memory import evidence_packet as codec
from backend.memory.identity import register_agent
from backend.memory.records_v1 import COMPACT_RECORD_SCHEMA_VERSION, import_record_package
from backend.memory.semantic_shadow import file_digest
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory.venice_summary_trial import read_dpapi_key
from scripts import semantic_ordered_exam as previous
from scripts.assistant_recall_baseline import _private
from scripts.semantic_window_exam import write

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/semantic_shortlist_exam.json"
OWNER = "semantic-shortlist-synthetic-owner"
FIRST_MAX = Decimal("0.10")
SECOND_MAX = Decimal("0.20")
MAX_BATCH = Decimal("1.50")


def implementation():
    value = previous.source_fingerprints()
    value["source_files_sha256"]["scripts/semantic_shortlist_exam.py"] = file_digest(__file__)
    return value


def verify_fiction(prepared, case, fixture, *, first=False):
    value = prepared
    if first:
        value = {**prepared, "request": {**prepared["request"], "source_order_windows": []}}
    previous.verify_fictional_request(value, case, fixture)


def prepare(output):
    output = _private(output)
    output.mkdir(parents=True, exist_ok=False)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    records = previous.source_records(fixture)
    db, package = output / "synthetic.sqlite3", output / "synthetic.json"
    write(output / "frozen-fixture.json", fixture)
    write(package, {"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records})
    register_agent(OWNER, "Synthetic shortlist exam", actor="shortlist-exam", db_path=str(db))
    import_record_package(str(package), db_path=str(db), owner_agent_id=OWNER, actor="shortlist-exam")
    before = file_digest(db)
    plan = {"schema": "semantic-shortlist-exam-v1", "fixture_sha256": file_digest(FIXTURE),
            "source_sha256": before, "implementation": implementation(), "requests": {}, "cases": [],
            "scope": "fixed_seed_component_exam_not_retrieval_accuracy", "real_archive_records_sent": 0}
    with patch.object(socket.socket, "connect", side_effect=RuntimeError("offline preparation")):
        bridge = core.ShortlistBridge(db, OWNER, source_sha256=before)
        for case in fixture["cases"]:
            seed = bridge.prepare(previous.raw_seed(case, records), query=case["query"])
            verify_fiction(seed, case, fixture, first=True)
            write(output / (case["id"] + ".seed.json"), seed)
            amount = shortlist_api.reservation(seed["request"], "openai")
            if amount > FIRST_MAX:
                raise ValueError("first_call_ceiling_exceeded")
            plan["requests"][case["id"]] = {"seed_sha256": seed["prepared_sha256"],
                "payload_sha256": selection.digest(selection.encoded(shortlist_api.request_payload(seed["request"], "openai")).decode()),
                "first_reserved_usd": str(amount), "second_max_usd": str(SECOND_MAX)}
            eager = bridge.ordered.prepare_seed(seed)
            plan["cases"].append({"id": case["id"], "seed_cards": len(seed["local"]),
                "eager_context_deferred": eager["adjacency"].get("deferred") is True})
    plan["max_reserved_usd"] = str(sum((Decimal(row["first_reserved_usd"]) + SECOND_MAX
                                      for row in plan["requests"].values()), Decimal(0)))
    if Decimal(plan["max_reserved_usd"]) > MAX_BATCH:
        raise ValueError("batch_ceiling_exceeded")
    bridge.base.check_snapshot()
    write(output / "plan.json", plan)
    return plan


def score(case, result):
    accepted = result["status"] == "reviewed_requires_acceptance"
    expected = case["expected"]
    if expected["kind"] == "partial":
        brief = codec.expand(result["brief"])
        evidence = bool(brief.get("items") and brief["needs_more_evidence"]
                        and brief.get("model_sufficiency") == "partial" and "missing_answer" in brief.get("gaps", []))
    else:
        evidence = previous.score(case, {"status": "selected_requires_review" if accepted else "failed",
                                         "brief": result["brief"]})["expected_evidence"]
    seed = result["audit"]["seed"]
    draft = result["audit"].get("shortlist", {"inspect": [], "defer": [], "discard": []})
    chosen = {field: {seed["local"][cid]["record_id"] for cid in draft[field]} for field in core.FIELDS}
    routing = (set(expected.get("must_inspect", [])) <= chosen["inspect"]
               and set(expected.get("must_discard", [])) <= chosen["discard"]
               and not set(expected.get("must_not_discard", [])) & chosen["discard"]
               and (not expected.get("must_defer") or bool(draft["defer"])))
    return {"accepted_contract": accepted, "expected_evidence": bool(evidence), "expected_shortlist": bool(routing),
            "passed": accepted and bool(evidence) and bool(routing), "calls": result["calls"],
            "brief_bytes": len(selection.encoded(result["brief"]))}


def run_model(output, key_file, ledger_path):
    output = _private(output)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    if (plan["fixture_sha256"] != file_digest(FIXTURE) or plan["implementation"] != implementation()
            or Decimal(plan["max_reserved_usd"]) > MAX_BATCH):
        raise ValueError("frozen_exam_changed")
    bridge = core.ShortlistBridge(output / "synthetic.sqlite3", OWNER, source_sha256=plan["source_sha256"])
    work = []
    for case in fixture["cases"]:
        seed = json.loads((output / (case["id"] + ".seed.json")).read_text(encoding="utf-8"))
        bridge.base.verify(seed)
        verify_fiction(seed, case, fixture, first=True)
        if seed["prepared_sha256"] != plan["requests"][case["id"]]["seed_sha256"]:
            raise ValueError("frozen_seed_changed")
        work.append((case, seed))
    report = {"model": review_api.model("openai"), "new_model_calls": 0, "cases": [], "real_archive_records_sent": 0,
              "production_accepted": False, "scope": plan["scope"], "max_reserved_usd": plan["max_reserved_usd"]}
    with BudgetLedger(_private(ledger_path), maximum_limit=Decimal("10.00")) as ledger:
        if ledger.data.get("provider") != "openai" or ledger.total + Decimal(plan["max_reserved_usd"]) > ledger.limit:
            raise ValueError("existing_budget_unavailable")
        write(output / "model-started.json", {"status": "started_no_automatic_retry", "max_reserved_usd": plan["max_reserved_usd"]})
        key = read_dpapi_key(_private(key_file), provider="openai")
        client = review_api.openai.OpenAIClient(key)
        del key
        review_api.openai.preflight(client)
        for case, seed in work:
            cid = case["id"]
            first_response = {}
            def reserve(stage, ceiling, item):
                amount = Decimal(item["reserved_usd"])
                if amount > ceiling:
                    raise ValueError("call_ceiling_exceeded")
                ledger.reserve(amount=amount, run_id=output.name, model=review_api.model("openai"), case_id=cid + "." + stage)
            def first(request):
                nonlocal first_response
                if request != seed["request"]:
                    raise ValueError("first_request_changed")
                report["new_model_calls"] += 1
                first_response = shortlist_api.run_case(client, request, provider="openai",
                    frozen_request_sha256=plan["requests"][cid]["payload_sha256"],
                    before_request=lambda item: reserve("shortlist", FIRST_MAX, item))
                write(output / (cid + ".shortlist.response.json"), first_response)
                return first_response["draft"]
            def second(request):
                focused = core.focused_seed(seed, first_response["draft"])
                prepared = bridge.ordered.prepare_seed(focused)
                if request != prepared["request"]:
                    raise ValueError("derived_context_request_changed")
                verify_fiction(prepared, case, fixture)
                payload_sha = selection.digest(selection.encoded(review_api.request_payload(request, "openai")).decode())
                write(output / (cid + ".derived-review-request.json"), {"prepared": prepared, "payload_sha256": payload_sha,
                      "derivation": "frozen_fiction_and_validated_first_response", "max_reserved_usd": str(SECOND_MAX)})
                report["new_model_calls"] += 1
                response = review_api.run_case(client, request, provider="openai", frozen_request_sha256=payload_sha,
                    before_request=lambda item: reserve("review", SECOND_MAX, item))
                write(output / (cid + ".review.response.json"), response)
                return response["draft"]
            result = bridge.run(seed, shortlister=first, reviewer=second)
            write(output / (cid + ".result.json"), result)
            entry = {"id": cid, "status": result["status"], **score(case, result)}
            report["cases"].append(entry)
            print(json.dumps(entry), flush=True)
        report.update(cumulative_reserved_usd=str(ledger.total), budget_limit_usd=str(ledger.limit))
    if plan["implementation"] != implementation():
        raise ValueError("implementation_changed_during_exam")
    bridge.base.check_snapshot()
    write(output / "model-report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run-model"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--budget-ledger", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        result = prepare(args.output)
        print(json.dumps({"status": "prepared_offline", "max_reserved_usd": result["max_reserved_usd"], "cases": result["cases"]}))
    else:
        if not args.key_file or not args.budget_ledger:
            parser.error("explicit key and existing ledger required")
        result = run_model(args.output, args.key_file, args.budget_ledger)
        print(json.dumps({"status": "finished", "new_model_calls": result["new_model_calls"],
                          "cumulative_reserved_usd": result["cumulative_reserved_usd"]}))


if __name__ == "__main__":
    main()
