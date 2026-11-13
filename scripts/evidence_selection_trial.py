#!/usr/bin/env python3
"""Fixed synthetic selector exam; no DB or arbitrary case-file input exists."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.memory import evidence_selector as core
from backend.memory import evidence_selector_trial as api
from backend.memory import object_evidence_review as object_core
from backend.memory import object_evidence_review_trial as object_api
from scripts import object_correction_exam
from backend.memory.caretaker_summary import SummaryError
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory.venice_summary_trial import read_dpapi_key
from scripts.assistant_recall_baseline import _private, file_sha
from scripts.summary_cabinet_trial import write_json

FIXTURE = ROOT / "tests/fixtures/evidence_selection_synthetic.json"


def load_cases(partition):
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [c for c in cases if partition == "all" or c["partition"] == partition]


def prepare_case(case):
    rows = []
    for row in case["records"]:
        # Expected labels and required quotes never enter the request.
        rows.append({"record_id": row["id"], "source_kind": row.get("source_kind", "synthetic"),
            "conversation_id": "synthetic-room", "role": row.get("role", "assistant"),
            "created_at": "2031-05-06T00:00:00Z", "content": row["content"],
            "conflict_group_id": row.get("conflict_group_id")})
    shadow = {"original": {"memories": rows, "coverage": {"coverage_gap": False}}, "passes": [],
              "plan": {"status": "planned", "intent": "topic", "topic_terms": case["terms"]}}
    return core.prepare(shadow, query=case["query"])


def score(case, prepared, draft, brief):
    gold = {r["id"]: r for r in case["records"]}
    mismatches = []
    for judgment in draft["judgments"]:
        rid = prepared["local"][judgment["candidate_id"]]["record_id"]
        if judgment["relation"] != gold[rid]["relation"]:
            mismatches.append({"record_id": rid, "field": "relation"})
        if any(text not in judgment["quote"] for text in gold[rid].get("required", [])):
            mismatches.append({"record_id": rid, "field": "required_quote"})
    for field in ("sufficiency", "gap"):
        if draft[field] != case[field]:
            mismatches.append({"field": field})
    if "expected_items" in case and len(brief["items"]) != case["expected_items"]:
        mismatches.append({"field": "display_group_count"})
    return {"passed": not mismatches, "mismatches": mismatches}


def main(argv=None):
    parser = argparse.ArgumentParser(description="小管家证据筛选：只用固定虚构题，默认准备、不调用模型。")
    parser.add_argument("--provider", choices=("venice", "openai"), default="venice")
    parser.add_argument("--partition", choices=("development", "holdout", "fresh", "all"), default="development")
    parser.add_argument("--exam", choices=("general", "objects", "object-contrasts"), default="general")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-synthetic-api", action="store_true")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--budget-ledger", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    output = _private(args.output_dir)
    is_object_exam = args.exam in ("objects", "object-contrasts")
    exam_api = object_api if is_object_exam else api
    exam_core = object_core if is_object_exam else core
    cases = object_correction_exam.load_cases(args.partition, contrast=args.exam == "object-contrasts") if is_object_exam else load_cases(args.partition)
    if not cases:
        raise SummaryError("selected_partition_is_empty")
    prepare = object_correction_exam.prepare_case if is_object_exam else prepare_case
    prepared = {c["case_id"]: prepare(c) for c in cases}
    payloads = {cid: exam_api.request_payload(item["request"], args.provider) for cid, item in prepared.items()}
    code_paths = (Path(__file__), ROOT / "backend/memory/evidence_selector.py",
                  ROOT / "backend/memory/evidence_selector_trial.py", FIXTURE,
                  ROOT / "backend/memory/query_caretaker.py",
                  ROOT / "backend/memory/summary_trial_budget.py",
                  ROOT / "backend/memory/venice_summary_trial.py",
                  ROOT / "backend/memory/openai_summary_trial.py")
    if is_object_exam:
        code_paths += (ROOT / "backend/memory/object_evidence_review.py",
                       ROOT / "backend/memory/object_evidence_review_trial.py",
                       ROOT / "backend/memory/object_recall_shadow.py", ROOT / "backend/memory/named_collection.py",
                       ROOT / "scripts/object_correction_exam.py", object_correction_exam.FIXTURE)
        if args.exam == "object-contrasts":
            code_paths += (object_correction_exam.CONTRAST_FIXTURE,)
    manifest = {"schema": exam_core.VERSION + "-trial", "provider": args.provider, "exam": args.exam,
        "model": api.model(args.provider), "partition": args.partition, "synthetic_only": True,
        "source_sha256": {p.relative_to(ROOT).as_posix(): file_sha(p) for p in code_paths},
        "requests": {cid: core.digest(core.encoded(p).decode()) for cid, p in payloads.items()}}
    if output.exists():
        if not args.resume or json.loads((output / "manifest.json").read_text(encoding="utf-8")) != manifest:
            raise SummaryError("existing_run_requires_matching_resume")
        for cid, payload in payloads.items():
            if json.loads((output / (cid + ".request.json")).read_text(encoding="utf-8")) != payload:
                raise SummaryError("frozen_request_changed")
    else:
        if args.resume:
            raise SummaryError("nothing_to_resume")
        output.mkdir(parents=True)
        write_json(output / "manifest.json", manifest)
        for cid, payload in payloads.items():
            write_json(output / (cid + ".request.json"), payload)
    report_path = output / "report.json"
    if report_path.exists():
        print(json.dumps({"status": "existing_report_reused", "new_model_calls": 0}))
        return 0
    total = sum((exam_api.reservation(p["request"], args.provider) for p in prepared.values()), Decimal(0))
    if not args.allow_synthetic_api:
        print(json.dumps({"status": "prepared_no_api", "cases": len(cases),
                          "maximum_reservation_usd": str(total), "model_calls": 0}))
        return 0
    if not args.key_file or not args.budget_ledger:
        parser.error("live run requires the existing protected key and cumulative ledger")
    # 2026-11-13: approved $1 amendment followed by delegated small adjustments.
    # This only permits reading that amended ledger; it never raises its limit,
    # refunds reservations or changes older trial callers' $0.50 ceiling.
    # Object review is a separately bounded continuation under the delegated
    # small-budget amendment. Older general trials retain their $2 ceiling.
    ceiling = (Decimal("3.00") if is_object_exam else Decimal("2.00")) if args.provider == "openai" else api.venice.MAX_SUPPORTED_BUDGET_USD
    report = {"schema": exam_core.VERSION + "-report", "exam": args.exam, "provider": args.provider, "model": api.model(args.provider),
              "synthetic_only": True, "archive_records_sent": 0, "production_accepted": False,
              "new_model_calls": 0, "cases": []}
    with BudgetLedger(_private(args.budget_ledger), maximum_limit=ceiling) as ledger:
        if ledger.data.get("provider", "venice") != args.provider:
            raise SummaryError("budget_provider_mismatch")
        pending = [c for c in cases if not (output / (c["case_id"] + ".started.json")).exists()]
        amount = sum((exam_api.reservation(prepared[c["case_id"]]["request"], args.provider) for c in pending), Decimal(0))
        if ledger.total + amount > ledger.limit:
            raise SummaryError("trial_budget_exhausted")
        client = None
        if pending:
            key = read_dpapi_key(_private(args.key_file), provider=args.provider)
            client = api.openai.OpenAIClient(key) if args.provider == "openai" else api.venice.VeniceClient(key)
            del key
            if args.provider == "openai":
                api.openai.preflight(client)
            else:
                api.venice.preflight(client, api.MISTRAL)
        for case in cases:
            cid = case["case_id"]
            response_path = output / (cid + ".response.json")
            started_path = output / (cid + ".started.json")
            if response_path.exists():
                if not started_path.exists():
                    raise SummaryError("orphan_response_requires_review")
                response = json.loads(response_path.read_text(encoding="utf-8"))
            elif started_path.exists():
                response = {"status": "interrupted_no_retry"}
            else:
                if any(row.get("run_id") == output.name and row.get("case_id") == cid
                       for row in ledger.data["reservations"]):
                    raise SummaryError("orphan_reservation_requires_review")
                write_json(started_path, {"status": "started", "request_sha256": manifest["requests"][cid]})
                def reserve(item):
                    ledger.reserve(amount=Decimal(item["reserved_usd"]), run_id=output.name,
                                   model=api.model(args.provider), case_id=cid)
                report["new_model_calls"] += 1
                try:
                    response = exam_api.run_case(client, prepared[cid]["request"], provider=args.provider,
                        frozen_request_sha256=manifest["requests"][cid], before_request=reserve)
                except Exception:
                    response = {"status": "failed_no_retry", "details_suppressed": True}
                write_json(response_path, response)
            entry = {"case_id": cid, "accepted_contract": False, "quality": {"passed": False}}
            if "draft" in response:
                try:
                    brief = exam_core.make_brief(response["draft"], prepared[cid])
                    entry.update(accepted_contract=True, brief=brief,
                        quality=score(case, prepared[cid], response["draft"], brief),
                        estimated_usd=response.get("estimated_usd"), usage=response.get("usage"))
                except (core.SelectionError, object_core.ReviewError) as error:
                    entry["validation_error"] = str(error)
            report["cases"].append(entry)
        report.update(cumulative_reserved_usd=str(ledger.total), budget_limit_usd=str(ledger.limit))
    if manifest["source_sha256"] != {p.relative_to(ROOT).as_posix(): file_sha(p) for p in code_paths}:
        raise SummaryError("implementation_changed_during_trial")
    write_json(report_path, report)
    print(json.dumps({"status": "synthetic_selector_exam_finished", "new_model_calls": report["new_model_calls"],
        "passed": sum(c["quality"]["passed"] for c in report["cases"]), "cases": len(cases),
        "archive_records_sent": 0, "production_accepted": False}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SummaryError, core.SelectionError) as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit("selector_trial_failed_details_suppressed") from None
