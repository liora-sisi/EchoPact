#!/usr/bin/env python3
"""Local M22 shadow with explicit, budgeted question-only API opt-in."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.mcp.readonly_server import ReadonlyGateway
from backend.memory import query_caretaker as core
from backend.memory import openai_query_trial as api
from backend.memory.assistant_baseline import validate_suite, text_sha, serialized, measure_packet, _score, _sources
from backend.memory.caretaker_summary import SummaryError
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory.venice_summary_trial import read_dpapi_key
from scripts.assistant_recall_baseline import _private, file_sha, validate_targets, implementation
from scripts.summary_cabinet_trial import write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读问句小管家试跑；默认只准备，真实 API 仅发送问句。")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-offline", action="store_true")
    parser.add_argument("--allow-query-only-api", action="store_true")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--budget-ledger", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if args.run_offline and args.allow_query_only_api:
        parser.error("choose offline or live")
    output, db, cases = map(_private, (args.output_dir, args.db, args.cases))
    if cases.stat().st_size > 100000:
        raise core.PlannerError("cases_too_large")
    suite = json.loads(cases.read_text(encoding="utf-8-sig"))
    validate_suite(suite)
    if len(suite["cases"]) > 4:
        raise core.PlannerError("trial_maximum_four_cases")
    before = file_sha(db)
    if before != suite["corpus_sha256"] or any(Path(str(db) + s).exists() for s in ("-wal", "-journal")):
        raise core.PlannerError("frozen_corpus_required")
    validate_targets(db, suite, args.agent_id)
    prepared = {c["case_id"]: api.request_payload(c["steps"][0]["query"]) for c in suite["cases"]
                if core.eligibility(c["steps"][0]["query"], suite["reference_at"]) == "eligible"}
    code = implementation()
    code["source_files_sha256"]["scripts/query_caretaker_trial.py"] = file_sha(Path(__file__))
    manifest = {"schema": core.VERSION + "-trial", "suite_sha256": text_sha(serialized(suite)),
        "agent_id_sha256": text_sha(args.agent_id), "corpus_sha256": before, "implementation": code,
        "requests": {key: text_sha(serialized(value)) for key, value in prepared.items()}}
    if output.exists():
        if not args.resume or json.loads((output / "manifest.json").read_text(encoding="utf-8")) != manifest:
            raise core.PlannerError("existing_run_requires_matching_resume")
        if json.loads((output / "cases.private.json").read_text(encoding="utf-8")) != suite:
            raise core.PlannerError("frozen_cases_changed")
        for cid, request in prepared.items():
            if json.loads((output / (cid + ".request.json")).read_text(encoding="utf-8")) != request:
                raise core.PlannerError("frozen_request_changed")
    else:
        if args.resume:
            raise core.PlannerError("nothing_to_resume")
        output.mkdir(parents=True)
        write_json(output / "manifest.json", manifest)
        write_json(output / "cases.private.json", suite)
        for cid, request in prepared.items():
            write_json(output / (cid + ".request.json"), request)
    if (output / "report.json").exists():
        print(json.dumps({"status": "existing_report_reused", "new_model_calls": 0, "new_retrieval_calls": 0}))
        return 0
    reserve_all = sum((api.reservation(c["steps"][0]["query"]) for c in suite["cases"]
                       if c["case_id"] in prepared), Decimal(0))
    if not args.allow_query_only_api and not args.run_offline:
        print(json.dumps({"status": "prepared_no_api", "eligible_questions": len(prepared),
                          "maximum_reservation_usd": str(reserve_all), "model_calls": 0}))
        return 0
    live = bool(args.allow_query_only_api and args.key_file and args.key_file.is_file() and args.budget_ledger)
    report = {"schema": core.VERSION + "-report", "production_accepted": False,
              "only_questions_sent": True, "archive_records_sent": 0, "model_calls_reserved": 0,
              "provider_status": "disabled" if not args.allow_query_only_api else "missing_key_or_budget",
              "cases": [], "implementation": code}
    gateway = ReadonlyGateway(str(db), args.agent_id)
    gateway.validate()
    with ExitStack() as stack:
        ledger = None
        if live:
            try:
                ledger = stack.enter_context(BudgetLedger(args.budget_ledger, maximum_limit=api.transport.MAX_BUDGET_USD))
            except Exception:
                report["provider_status"] = "unavailable_original_only"
        initial_count = len(ledger.data["reservations"]) if ledger else 0
        client = None
        if ledger is not None:
            try:
                if ledger.data.get("provider") != "openai" or ledger.data.get("trial_id") != "openai-summary-comparison-20261110":
                    raise SummaryError("existing_openai_ledger_required")
                pending = [c for c in suite["cases"] if c["case_id"] in prepared
                    and not (output / (c["case_id"] + ".started.json")).exists()]
                if ledger.total + sum((api.reservation(c["steps"][0]["query"]) for c in pending), Decimal(0)) > ledger.limit:
                    raise SummaryError("trial_budget_exhausted")
                if pending:
                    client = api.transport.OpenAIClient(read_dpapi_key(args.key_file, provider="openai"))
                    api.transport.preflight(client)
                report["provider_status"] = "available_or_reused"
            except Exception:
                report["provider_status"] = "unavailable_original_only"
                client = None
        for case in suite["cases"]:
            cid, query = case["case_id"], case["steps"][0]["query"]
            result_path = output / (cid + ".case.json")
            if result_path.exists():
                report["cases"].append(json.loads(result_path.read_text(encoding="utf-8")))
                continue
            def planner(question):
                response_path = output / (cid + ".response.json")
                started_path = output / (cid + ".started.json")
                if response_path.exists():
                    if not started_path.exists():
                        raise SummaryError("orphan_response_requires_review")
                    saved = json.loads(response_path.read_text(encoding="utf-8"))
                    if "plan" in saved:
                        return core.validate_plan(saved["plan"], question)
                    raise SummaryError("previous_failure_no_retry")
                if started_path.exists() or client is None:
                    raise SummaryError("unavailable_or_interrupted_no_retry")
                write_json(started_path, {"question_sha256": text_sha(question), "status": "attempt_started"})
                def reserve(item):
                    ledger.reserve(amount=Decimal(item["reserved_usd"]), run_id=output.name,
                                   model=api.transport.MODEL, case_id="query-" + cid)
                try:
                    saved = api.run_query(client, question, before_request=reserve)
                except Exception as error:
                    write_json(response_path, {"status": "failed_no_retry", "details_suppressed": True,
                        "error_type": "provider_or_validation_failure",
                        "rejected_plan": getattr(error, "rejected_plan", None)})
                    raise
                write_json(response_path, saved)
                return saved["plan"]
            result = core.run_shadow(query, as_of=suite["reference_at"], limit=10, runner=gateway.recall,
                                     planner=planner if args.allow_query_only_api else None)
            evidence = [row for packet in [result["original"], *result["passes"]] for group in _sources(packet) for row in group]
            entry = {"case_id": cid, "planner_status": result["planner_status"], "plan": result["plan"],
                "queries": result["queries"], "retrieval_calls": result["retrieval_calls"],
                "original": measure_packet(result["original"], query=query, targets=case["targets"], limit=10),
                "all_pass_evidence": _score(case["targets"], evidence),
                "brief_evidence": _score(case["targets"], [("brief", x) for x in result["brief"]["items"]], text_field="text"),
                "brief_chars": len(serialized(result["brief"])), "brief": result["brief"],
                "internal_payload_chars": sum(len(serialized(p)) for p in [result["original"], *result["passes"]])}
            write_json(result_path, entry)
            report["cases"].append(entry)
        if ledger:
            report.update(model_calls_reserved=len(ledger.data["reservations"]) - initial_count,
                          cumulative_reserved_usd=str(ledger.total), budget_limit_usd=str(ledger.limit))
    if file_sha(db) != before or any(Path(str(db) + s).exists() for s in ("-wal", "-journal")):
        raise core.PlannerError("corpus_changed_during_trial")
    after_code = implementation()
    after_code["source_files_sha256"]["scripts/query_caretaker_trial.py"] = file_sha(Path(__file__))
    if after_code != code:
        raise core.PlannerError("implementation_changed_during_trial")
    report["database_unchanged"] = True
    write_json(output / "report.json", report)
    print(json.dumps({"status": "shadow_trial_finished_not_production", "model_calls_reserved": report["model_calls_reserved"],
                      "cases": len(report["cases"]), "archive_records_sent": 0, "database_unchanged": True}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (core.PlannerError, SummaryError) as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit("query_trial_failed_details_suppressed") from None
