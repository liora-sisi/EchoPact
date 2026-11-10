#!/usr/bin/env python3
"""Frozen synthetic suites; historical comparison and bounded repair probes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.memory.caretaker_summary import SummaryError, validate_records
from backend.memory.caretaker_summary_v2 import SYSTEM_PROMPT, SUMMARY_SCHEMA, build_messages
from backend.memory import caretaker_summary_v2, caretaker_summary_v3, caretaker_summary_v4
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory.venice_summary_trial import (
    MODEL_PRICE_CEILINGS, TrialBudget, VeniceClient, preflight, read_dpapi_key, run_case,
)


MISTRAL = "mistral-small-3-2-24b-instruct"
CONTRACTS = {"v2": caretaker_summary_v2, "v3": caretaker_summary_v3, "v4": caretaker_summary_v4}


def load_cases(compact=False, repair=False):
    cases = []
    if repair:
        files = (("caretaker_summary_compact_probe.json", "regression"),
                 ("caretaker_summary_evidence_repair.json", "held_out_before_first_run"))
    elif compact:
        files = (("caretaker_summary_compact_probe.json", "compact_new_before_first_run"),)
    else:
        files = (("caretaker_summary_synthetic.json", "regression"),
                 ("caretaker_summary_holdout.json", "held_out_before_first_run"))
    for name, group in files:
        for case in json.loads((ROOT / "tests/fixtures" / name).read_text(encoding="utf-8")):
            validate_records(case["records"])
            cases.append({**case, "group": group})
    expected_count = 4 if repair else (2 if compact else 6)
    if len(cases) != expected_count or len({c["case_id"] for c in cases}) != expected_count:
        raise SummaryError("comparison_suite_changed")
    return cases


def selected_models(report):
    models = report.get("models", list(MODEL_PRICE_CEILINGS))
    if (not isinstance(models, list) or not models
            or any(not isinstance(m, str) or m not in MODEL_PRICE_CEILINGS for m in models)
            or len(set(models)) != len(models)):
        raise SummaryError("invalid_comparison_models")
    if report.get("contract") == "v4" and models != [MISTRAL]:
        raise SummaryError("repair_probe_requires_mistral_only")
    return models


def write_json(path, data):
    with path.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def restore_attempted(previous, report, cases, ledger):
    """Continue only unsent cells; never retry a timeout or overwrite prior evidence."""
    for name in ("schema", "contract", "prompt_sha256", "schema_sha256", "input_sha256"):
        if previous.get(name) != report.get(name):
            raise SummaryError("continuation_contract_or_input_changed")
    if selected_models(previous) != selected_models(report):
        raise SummaryError("continuation_models_changed")
    if previous.get("status") != "stopped_no_retry" or previous.get("synthetic_only") is not True:
        raise SummaryError("continuation_requires_stopped_synthetic_run")
    lineage = [*previous.get("lineage", []), previous["run_id"]]
    expected = {(c["case_id"], m) for c in cases for m in selected_models(report)}
    rows = previous.get("cases", [])
    attempted = [(r["case_id"], r["model"]) for r in rows]
    reserved = [(r["case_id"], r["model"]) for r in ledger.data["reservations"] if r["run_id"] in lineage]
    if (len(set(attempted)) != len(attempted) or not set(attempted) <= expected
            or len(reserved) != len(attempted) or set(reserved) != set(attempted)):
        raise SummaryError("continuation_receipts_need_reconciliation")
    report.update(lineage=lineage, cases=rows)


def run_comparison(client, cases, report, output_dir, ledger):
    version = report.get("contract", "v2")
    contract = CONTRACTS[version]
    prices = {model: preflight(client, model) for model in selected_models(report)}
    report["preflight"] = prices
    report["inherited_reserved_usd"] = str(ledger.total)
    budget = TrialBudget(spent_reservations=ledger.total, max_calls=12)
    attempted = {(r["case_id"], r["model"]) for r in report["cases"]}
    # Check feasibility for ALL remaining cells before making any model request.
    preview = TrialBudget(spent_reservations=ledger.total, max_calls=12)
    for case in cases:
        for model in prices:
            if (case["case_id"], model) in attempted:
                continue
            preview.reserve(contract.build_messages(case["records"]), prices[model], contract.SUMMARY_SCHEMA)
    report["projected_cumulative_reserved_usd"] = str(preview.spent_reservations)
    write_json(output_dir / "report.json", report)

    for case in cases:
        for model in prices:
            if (case["case_id"], model) in attempted:
                continue
            def reserve(item):
                ledger.reserve(amount=Decimal(item["reserved_usd"]), run_id=report["run_id"],
                               model=model, case_id=case["case_id"])
            row = {"model": model, "case_id": case["case_id"], "group": case["group"],
                   "review_targets": case["review_targets"]}
            try:
                result = run_case(client, case["records"], prices=prices[model], budget=budget,
                                  before_request=reserve, model=model, contract_version=version)
                row.update(status="validated_structure", result=result)
            except SummaryError as error:
                row.update(status="rejected", error_code=str(error))
                if not hasattr(error, "rejected_draft"):
                    report["cases"].append(row)
                    raise
                row.update(rejected_draft=error.rejected_draft, usage=error.trial_usage)
                # A received invalid final draft is an observed model result. Preserve it,
                # never repair/retry it, and continue other independent comparison cells.
            report["cases"].append(row)
            report["cumulative_reserved_usd"] = str(ledger.total)
            write_json(output_dir / "report.json", report)
            print(json.dumps({k: row[k] for k in ("model", "case_id", "status")}), flush=True)
    has_missing = any(r.get("error_code") and "rejected_draft" not in r for r in report["cases"])
    report["status"] = "awaiting_human_review_with_missing" if has_missing else "awaiting_human_review"


def main(argv=None):
    parser = argparse.ArgumentParser(description="同题对照；真实 API 默认关闭，累计预算不重置。")
    parser.add_argument("--allow-live-api", action="store_true")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--budget-ledger", type=Path)
    parser.add_argument("--continue-unattempted-from", type=Path)
    probes = parser.add_mutually_exclusive_group()
    probes.add_argument("--compact-contract-probe", action="store_true")
    probes.add_argument("--evidence-repair-probe", action="store_true")
    args = parser.parse_args(argv)
    cases = load_cases(args.compact_contract_probe, args.evidence_repair_probe)
    version = "v4" if args.evidence_repair_probe else ("v3" if args.compact_contract_probe else "v2")
    contract = CONTRACTS[version]
    models = [MISTRAL] if args.evidence_repair_probe else list(MODEL_PRICE_CEILINGS)
    if not args.allow_live_api:
        preview = TrialBudget(max_calls=12)
        for case in cases:
            for model in models:
                inp, out = MODEL_PRICE_CEILINGS[model]
                preview.reserve(contract.build_messages(case["records"]), {
                    "input_usd_per_million": str(inp), "output_usd_per_million": str(out)}, contract.SUMMARY_SCHEMA)
        print(json.dumps({"mode": "dry_run", "models": models, "cases": len(cases),
            "model_calls": 0, "contract": version, "planned_calls": len(cases)*len(models), "new_reservation_ceiling_usd": str(preview.spent_reservations),
            "cumulative_budget_usd": "0.05", "requires_existing_budget_ledger": True}))
        return 0
    if not all((args.key_file, args.output_dir, args.budget_ledger)):
        parser.error("live comparison requires key-file, a new output-dir and existing budget-ledger")
    if args.output_dir.exists():
        parser.error("output directory must be new; no implicit retries")
    args.output_dir.mkdir(parents=True)
    report = {"schema": "echo-summary-model-comparison-v1", "run_id": args.output_dir.name,
        "at": datetime.now(timezone.utc).isoformat(), "synthetic_only": True, "contract": version, "models": models,
        "prompt_sha256": hashlib.sha256(contract.SYSTEM_PROMPT.encode()).hexdigest(),
        "schema_sha256": hashlib.sha256(json.dumps(contract.SUMMARY_SCHEMA, sort_keys=True).encode()).hexdigest(),
        "input_sha256": hashlib.sha256(json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "status": "running", "requires_human_review": True, "cases": []}
    write_json(args.output_dir / "synthetic-input.json", cases)
    # Freeze instructions and schema as well as inputs. Gold review targets are never sent.
    write_json(args.output_dir / "contract.json", {"system_prompt": contract.SYSTEM_PROMPT, "schema": contract.SUMMARY_SCHEMA})
    try:
        with BudgetLedger(args.budget_ledger) as ledger:
            report["cumulative_reserved_usd"] = str(ledger.total)
            try:
                if args.continue_unattempted_from:
                    previous = json.loads(args.continue_unattempted_from.read_text(encoding="utf-8"))
                    restore_attempted(previous, report, cases, ledger)
                client = VeniceClient(read_dpapi_key(args.key_file))
                run_comparison(client, cases, report, args.output_dir, ledger)
            finally:
                report["cumulative_reserved_usd"] = str(ledger.total)
    except SummaryError as error:
        report.update(status="stopped_no_retry", error_code=str(error))
    except Exception:
        report.update(status="stopped_no_retry", error_code="unexpected_trial_failure_details_suppressed")
    write_json(args.output_dir / "report.json", report)
    print(json.dumps({"status": report["status"], "cells_attempted": len(report["cases"]),
        "cumulative_reserved_usd": report.get("cumulative_reserved_usd"), "error_code": report.get("error_code")}), flush=True)
    return 0 if report["status"].startswith("awaiting_human_review") else 1


if __name__ == "__main__":
    raise SystemExit(main())
