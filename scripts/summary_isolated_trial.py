#!/usr/bin/env python3
"""Frozen single-window A/B replay; synthetic-only, one attempt per job, no retries."""
from __future__ import annotations

import argparse
import copy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.memory import summary_faithfulness as original
from backend.memory import summary_faithfulness_v2 as revised
from backend.memory.caretaker_summary import SummaryError
from backend.memory.summary_trial_budget import BudgetLedger
from scripts import summary_faithfulness_trial as previous
from scripts import run_summary_comprehensive_comparison as transport
from scripts.prepare_summary_fidelity_comparison import digest

RECIPE = "echo-summary-single-window-ab-v1"
CASE_IDS = ("long-daily-01", "long-sparse-01", "long-dense-01")
MAX_OUTPUT = 6000
MAX_RESERVATION = Decimal("1.25")
LEDGER_CEILING = Decimal("12.00")
CONTRACTS = {"original": original, "revised": revised}


def build_suite():
    old = previous.build_suite()
    cases = {c["case_id"]: c for c in old["reviewer.json"]}
    writer = {c["case_id"]: c for c in json.loads(old["request.json"]["input"][1]["content"])["cases"]}
    jobs = []
    for index, cid in enumerate(CASE_IDS):
        # Fixed alternating order, with no answer passed to any later request.
        for arm in (("original", "revised") if index % 2 == 0 else ("revised", "original")):
            contract = CONTRACTS[arm]
            payload = copy.deepcopy(old["request.json"])
            prompt = contract.SYSTEM_PROMPT + "\n本次只有当前一道题，恰好作答一次。"
            payload["input"] = [{"role": "system", "content": prompt},
                                {"role": "user", "content": json.dumps({"cases": [writer[cid]]}, ensure_ascii=False)}]
            payload["max_output_tokens"] = MAX_OUTPUT
            response_cases = payload["text"]["format"]["schema"]["properties"]["cases"]
            response_cases.update(minItems=1, maxItems=1)
            response_cases["items"]["properties"]["case_id"]["enum"] = [cid]
            response_cases["items"]["properties"]["summary"] = copy.deepcopy(contract.SUMMARY_SCHEMA)
            jobs.append({"job_id": cid + "-" + arm, "case_id": cid, "arm": arm, "author_recipe": contract.RECIPE,
                         "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                         "payload_sha256": digest(payload), "request": payload})
    reviewer = [copy.deepcopy(cases[cid]) for cid in CASE_IDS]
    manifest = {"schema": RECIPE, "jobs": len(jobs), "case_ids": list(CASE_IDS), "source_kind": "synthetic_only",
        "unique_source_chars": sum(len(r["content"]) for c in reviewer for r in c["records"]),
        "transmitted_source_chars": 2 * sum(len(r["content"]) for c in reviewer for r in c["records"]),
        "jobs_sha256": digest(jobs), "reviewer_sha256": digest(reviewer), "production_accepted": False,
        "evaluation_kind": "known_development_cases_not_blind", "repetitions_per_arm_case": 1}
    return {"manifest.json": manifest, "jobs.json": jobs, "reviewer.json": reviewer}


def scope(suite):
    inp, out = transport.PROVIDERS["openai"]["prices"]
    amounts = {j["job_id"]: ((len(transport.encoded(j["request"])) + 2048) * inp
               + j["request"]["max_output_tokens"] * out) / 1_000_000 for j in suite["jobs.json"]}
    total = sum(amounts.values(), Decimal("0"))
    if total > MAX_RESERVATION:
        raise SummaryError("isolated_reservation_exceeds_batch_cap")
    return {"recipe": RECIPE, "jobs_sha256": suite["manifest.json"]["jobs_sha256"],
            "maximum_calls": len(amounts), "source_kind": "synthetic_only",
            "unique_source_chars": suite["manifest.json"]["unique_source_chars"],
            "transmitted_source_chars": suite["manifest.json"]["transmitted_source_chars"],
            "reserved_usd": str(total), "job_reserved_usd": {k: str(v) for k, v in amounts.items()},
            "ledger_ceiling_usd": str(LEDGER_CEILING)}


def freeze(directory, suite):
    current_scope = scope(suite)
    directory = transport.protected(directory)
    directory.mkdir(parents=True, exist_ok=False)
    for name, value in suite.items():
        transport.write_new(directory / name, value)
    transport.write_new(directory / "scope.json", current_scope)


def load_frozen(directory):
    directory = transport.protected(directory)
    expected = build_suite()
    actual = {name: json.loads((directory / name).read_text(encoding="utf-8")) for name in expected}
    if actual != expected or json.loads((directory / "scope.json").read_text(encoding="utf-8")) != scope(actual):
        raise SummaryError("isolated_frozen_material_changed")
    return actual


def inspect_final(final, job, case):
    if final.get("finish") != "complete":
        raise SummaryError("isolated_incomplete_first_answer")
    try:
        data = json.loads(final["final_content"])
        if (not isinstance(data, dict) or set(data) != {"cases"}
                or not isinstance(data["cases"], list) or len(data["cases"]) != 1):
            raise ValueError
        answer = data["cases"][0]
        if (not isinstance(answer, dict) or set(answer) != {"case_id", "summary"}
                or answer["case_id"] != job["case_id"] or case["case_id"] != job["case_id"]):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise SummaryError("isolated_wrong_answer_scope") from None
    result = CONTRACTS[job["arm"]].inspect_draft(answer["summary"], case["evidence_catalog"])
    return {"summary": answer["summary"], **result}


def reference_coverage(summary, case):
    """Citations locate attention, not semantic coverage of every fact in a turn."""
    if not original.inspect_draft(summary, case["evidence_catalog"])["mechanical_passed"]:
        # Bad dates may be diagnosed, but only schema/reference shape is needed below.
        from jsonschema import Draft202012Validator
        if list(Draft202012Validator(original.SUMMARY_SCHEMA).iter_errors(summary)):
            raise SummaryError("isolated_invalid_summary_shape")
    catalog = case["evidence_catalog"]
    cited = {}
    for i, fact in enumerate(summary["facts"], 1):
        refs = fact["evidence_ids"] + (fact["supersedes"]["evidence_ids"] if fact["supersedes"] else [])
        for sid in set(refs):
            if sid not in catalog:
                raise SummaryError("isolated_invalid_reference")
            cited.setdefault(catalog[sid]["record_id"], set()).add(f"f{i}")
    missing = [r["record_id"] for r in case["records"] if r["record_id"] not in cited]
    at_capacity = len(summary["facts"]) == original.SUMMARY_SCHEMA["properties"]["facts"]["maxItems"]
    hints = []
    if at_capacity:
        hints.append("fact_capacity_reached_check_grouping_or_partition")
    if missing:
        hints.append("uncited_records_require_semantic_review_not_automatic_failure")
    return {"assessment": "echo-summary-reference-coverage-v2", "semantic_coverage": None,
        "production_accepted": False, "fact_capacity_reached": at_capacity,
        "uncited_record_ids": missing, "review_hints": hints,
        "notice": "引用过整条记录不证明里面的所有事实已保留；未引用也不能单独证明遗漏。",
        "records": [{"record_id": r["record_id"], "fact_ids": sorted(cited.get(r["record_id"], []))}
                    for r in case["records"]]}


def run(suite, directory, key_file, ledger_path, *, authorized=False,
        key_reader=transport.read_dpapi_key, client_factory=transport.Client):
    if not authorized:
        raise SummaryError("isolated_explicit_live_enable_required")
    if suite != build_suite():
        raise SummaryError("isolated_current_frozen_suite_required")
    directory, key_file, ledger_path = map(transport.protected, (directory, key_file, ledger_path))
    current_scope = scope(suite)
    amount = Decimal(current_scope["reserved_usd"])
    run_id = RECIPE + ":" + current_scope["jobs_sha256"]
    directory.mkdir(parents=True, exist_ok=False)
    for name, value in (("manifest.json", suite["manifest.json"]), ("scope.json", current_scope)):
        transport.write_new(directory / name, value)
    report = {"recipe": RECIPE, "status": "not_run", "production_accepted": False,
              "model_calls_reserved": 0, "model_calls_attempted": 0, "outcomes": []}
    try:
        with BudgetLedger(ledger_path, maximum_limit=LEDGER_CEILING) as ledger:
            if ledger.data.get("provider") != "openai":
                raise SummaryError("wrong_provider_ledger")
            if any(r.get("run_id") == run_id for r in ledger.data["reservations"]):
                raise SummaryError("isolated_no_automatic_repeat")
            if ledger.total + amount > ledger.limit:
                raise SummaryError("trial_budget_exhausted")
            client = client_factory("openai", key_reader(key_file, provider="openai"))
            report["preflight"] = transport.preflight(client)
            ledger.reserve(amount=amount, run_id=run_id, model=transport.PROVIDERS["openai"]["model"],
                           case_id="three-single-windows-two-arms")
            report.update(model_calls_reserved=len(suite["jobs.json"]), reserved_usd=str(amount),
                          cumulative_reserved_usd=str(ledger.total))
            for job in suite["jobs.json"]:
                folder = directory / job["job_id"]
                folder.mkdir(exist_ok=False)
                transport.write_new(folder / "request.json", job["request"])
                transport.write_new(folder / "attempt.json", {k: v for k, v in job.items() if k != "request"})
                report["model_calls_attempted"] += 1
                started = time.monotonic()
                raw = client.request(transport.PROVIDERS["openai"]["endpoint"], job["request"])
                final = transport.final_only(raw, "openai")
                transport.write_new(folder / "final-response.json", final)
                case = next(c for c in suite["reviewer.json"] if c["case_id"] == job["case_id"])
                outcome = {"job_id": job["job_id"], "case_id": job["case_id"], "arm": job["arm"],
                    "elapsed_seconds": round(time.monotonic() - started, 3), "usage": final["usage"],
                    "estimated_usd_at_verified_ceiling": final["estimated_usd_at_verified_ceiling"],
                    **inspect_final(final, job, case)}
                outcome["reference_coverage"] = reference_coverage(outcome["summary"], case)
                transport.write_new(folder / "report.json", outcome)
                report["outcomes"].append(outcome)
                print(json.dumps({"job_id": job["job_id"], "status": "answer_preserved_requires_review"}), flush=True)
            report["status"] = "answered_requires_semantic_review"
    except SummaryError as error:
        report.update(status="stopped_no_retry", error_code=str(error))
    except Exception:
        report.update(status="stopped_no_retry", error_code="failure_details_suppressed")
    transport.write_new(directory / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("prepare-dir", "suite-dir", "output-dir", "key-file", "ledger"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--enable-api", action="store_true")
    args = parser.parse_args(argv)
    try:
        suite = load_frozen(args.suite_dir) if args.suite_dir else build_suite()
        if args.prepare_dir:
            freeze(args.prepare_dir, suite)
        if not args.enable_api:
            print(json.dumps({"status": "offline_only", **scope(suite)}))
            return 0
        if args.prepare_dir or not all((args.suite_dir, args.output_dir, args.key_file, args.ledger)):
            raise SummaryError("isolated_frozen_run_arguments_required")
        report = run(suite, args.output_dir, args.key_file, args.ledger, authorized=True)
        print(json.dumps({k: report[k] for k in ("status", "model_calls_attempted", "production_accepted")}))
        return 0 if report["status"] == "answered_requires_semantic_review" else 2
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "error", "code": "isolated_trial_failed_details_suppressed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
