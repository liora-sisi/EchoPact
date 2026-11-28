#!/usr/bin/env python3
"""Frozen synthetic author retest: one bounded request per window, no retries."""
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

from backend.memory.caretaker_summary import SummaryError
from backend.memory import summary_event_booklet as book
from backend.memory import summary_event_author_v2 as author
from backend.memory import summary_reading_card as cards
from backend.memory.summary_trial_budget import BudgetLedger
from scripts import summary_faithfulness_trial as previous
from scripts import run_summary_comprehensive_comparison as transport
from scripts.prepare_summary_fidelity_comparison import digest

RECIPE = "echo-summary-event-author-retest-v2"
CASE_IDS = ("long-daily-01", "long-sparse-01", "long-dense-01")
MAX_OUTPUT = 6000
MAX_RESERVATION = Decimal("0.80")
# Supporting a ceiling does not change the existing ledger or authorize spending.
LEDGER_CEILING = Decimal("20.00")


def provider_schema(schema):
    """Keep array uniqueness in the local validator, outside the provider subset."""
    schema = copy.deepcopy(schema)
    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == "array":
                value.pop("uniqueItems", None)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(schema)
    return schema


def build_suite(*, legacy_schema=False):
    cases = {case["case_id"]: case for case in previous.build_suite()["reviewer.json"]}
    jobs, reviewers = [], []
    for cid in CASE_IDS:
        case = copy.deepcopy(cases[cid])
        prepared = author.prepare_author_window(case["records"])
        case["evidence_catalog"] = prepared["catalog"]
        identity = {"source_namespace": "synthetic", "room_id": cid, "branch_id": "fixture"}
        payload = {"model": transport.PROVIDERS["openai"]["model"],
            "input": prepared["messages"], "store": False, "stream": False,
            "service_tier": "default", "reasoning": {"effort": "low"},
            "max_output_tokens": MAX_OUTPUT,
            "text": {"format": {"type": "json_schema", "name": "echo_event_booklet",
                                "strict": True, "schema": prepared["response_schema"] if legacy_schema else provider_schema(prepared["response_schema"])}}}
        jobs.append({"job_id": cid + "-event-v2", "case_id": cid, "author_recipe": author.RECIPE,
            "identity": identity, "source_window_sha256": prepared["source_window_sha256"],
            "prompt_sha256": hashlib.sha256(author.SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
            "payload_sha256": digest(payload), "request": payload})
        reviewers.append(case)
    manifest = {"schema": RECIPE, "author_recipe": author.RECIPE,
        "source_kind": "synthetic_only", "contains_private_excerpts": False,
        "case_ids": list(CASE_IDS), "jobs": len(jobs),
        "source_chars": sum(len(row["content"]) for case in reviewers for row in case["records"]),
        "jobs_sha256": digest(jobs), "reviewer_sha256": digest(reviewers),
        "evaluation_kind": "known_development_cases_not_blind",
        "repetitions_per_case": 1, "semantic_passed": None, "production_accepted": False}
    return {"manifest.json": manifest, "jobs.json": jobs, "reviewer.json": reviewers}


def scope(suite):
    inp, out = transport.PROVIDERS["openai"]["prices"]
    amounts = {job["job_id"]: ((len(transport.encoded(job["request"])) + 2048) * inp
        + job["request"]["max_output_tokens"] * out) / 1_000_000 for job in suite["jobs.json"]}
    total = sum(amounts.values(), Decimal("0"))
    if total > MAX_RESERVATION:
        raise SummaryError("event_author_batch_budget_exceeded")
    return {"recipe": RECIPE, "model": transport.PROVIDERS["openai"]["model"],
        "jobs_sha256": suite["manifest.json"]["jobs_sha256"],
        "source_kind": "synthetic_only", "source_chars": suite["manifest.json"]["source_chars"],
        "maximum_calls": len(amounts), "max_output_tokens_per_call": MAX_OUTPUT,
        "reserved_usd": str(total), "job_reserved_usd": {k: str(v) for k, v in amounts.items()},
        "ledger_ceiling_usd": str(LEDGER_CEILING), "automatic_retry": False}


def freeze(directory, suite):
    if suite != build_suite():
        raise SummaryError("event_author_current_suite_required")
    directory = transport.protected(directory)
    directory.mkdir(parents=True, exist_ok=False)
    for name, value in suite.items():
        transport.write_new(directory / name, value)
    transport.write_new(directory / "scope.json", scope(suite))


def load_frozen(directory):
    directory = transport.protected(directory)
    expected = build_suite()
    actual = {name: json.loads((directory / name).read_text(encoding="utf-8")) for name in expected}
    if actual != expected or json.loads((directory / "scope.json").read_text(encoding="utf-8")) != scope(actual):
        raise SummaryError("event_author_frozen_material_changed")
    return actual


def inspect_final(final, job, case):
    if final.get("finish") != "complete":
        raise SummaryError("event_author_incomplete_first_answer")
    if case["case_id"] != job["case_id"] or cards.fingerprint(case["records"]) != job["source_window_sha256"]:
        raise SummaryError("event_author_wrong_source_window")
    try:
        draft = json.loads(final["final_content"])
    except (ValueError, KeyError, TypeError):
        raise SummaryError("event_author_invalid_json") from None
    audit = book.make_audit(draft, records=case["records"], catalog=case["evidence_catalog"],
        identity=job["identity"], author={"provider": "openai", "model": final["model"],
            "recipe": job["author_recipe"], "prompt_sha256": job["prompt_sha256"]})
    booklet = book.build_booklet(audit)
    catalogue = book.catalogue(audit, booklet, current_records=case["records"], identity=job["identity"])
    # Verify every supplied fact/unknown/relation, not just the first card's first fact.
    item_ids = [f"{event['event_id']}.{kind}{n}" for event in draft["events"]
                for kind, key in (("f", "facts"), ("u", "uncertainties"))
                for n in range(1, len(event[key]) + 1)]
    item_ids += [f"{kind}{n}" for kind, key in (("r", "relations"), ("u", "uncertainties"))
                 for n in range(1, len(draft[key]) + 1)]
    rows = {row["record_id"]: row for row in case["records"]}
    for item_id in item_ids:
        expanded = book.expand(audit, booklet, item_id=item_id, current_records=case["records"],
                               identity=job["identity"], max_bytes=64000)
        if expanded["status"] != "unapproved_preview":
            raise SummaryError("event_author_expansion_incomplete")
        for evidence in expanded["evidence"].values():
            if rows[evidence["record_id"]]["content"][evidence["start"]:evidence["end"]] != evidence["quote"]:
                raise SummaryError("event_author_expansion_mismatch")
    record_uses = {row["record_id"]: [] for row in case["records"]}
    for event in draft["events"]:
        for i, fact in enumerate(event["facts"], 1):
            refs = fact["evidence_ids"] + (fact["supersedes"]["evidence_ids"] if fact["supersedes"] else [])
            for rid in {case["evidence_catalog"][sid]["record_id"] for sid in refs}:
                record_uses[rid].append(f"{event['event_id']}.f{i}")
    metrics = {"event_count": len(draft["events"]),
        "fact_count": sum(len(event["facts"]) for event in draft["events"]),
        "cards": len(booklet["cards"]), "layout_complete": booklet["layout_complete"],
        "blocked_events": booklet["blocked_events"], "author_status": draft["status"],
        "structural_errors": audit["review"]["structural_errors"],
        "text_date_errors": author.inspect_text_dates(draft, case["evidence_catalog"]),
        "original_body_utf8_bytes": sum(len(row["content"].encode("utf-8")) for row in case["records"]),
        "all_cards_utf8_bytes": sum(len(cards.compact(card)) for card in booklet["cards"]),
        "catalogue_utf8_bytes": len(cards.compact(catalogue)),
        "catalogue_status": catalogue["status"], "exact_expansion_items_checked": len(item_ids),
        "uncited_records": audit["review"]["uncited_records"],
        "fact_reference_coverage": record_uses,
        "semantic_passed": None, "production_accepted": False,
        "notice": "引用、分卡和回查通过均不能代替独立语义复核；大小不代表完整问答成本。"}
    return {"audit": audit, "booklet": booklet, "catalogue": catalogue, "metrics": metrics}


def run(suite, directory, key_file, ledger_path, *, authorized=False,
        key_reader=transport.read_dpapi_key, client_factory=transport.Client):
    if not authorized:
        raise SummaryError("event_author_explicit_live_enable_required")
    if suite != build_suite():
        raise SummaryError("event_author_current_suite_required")
    directory, key_file, ledger_path = map(transport.protected, (directory, key_file, ledger_path))
    current_scope = scope(suite)
    amount = Decimal(current_scope["reserved_usd"])
    run_id = RECIPE + ":" + current_scope["jobs_sha256"]
    directory.mkdir(parents=True, exist_ok=False)
    transport.write_new(directory / "manifest.json", suite["manifest.json"])
    transport.write_new(directory / "scope.json", current_scope)
    report = {"recipe": RECIPE, "status": "not_run", "production_accepted": False,
              "model_calls_reserved": 0, "model_calls_attempted": 0, "outcomes": []}
    try:
        with BudgetLedger(ledger_path, maximum_limit=LEDGER_CEILING) as ledger:
            if ledger.data.get("provider") != "openai":
                raise SummaryError("wrong_provider_ledger")
            if any(row.get("run_id") == run_id for row in ledger.data["reservations"]):
                raise SummaryError("event_author_no_automatic_repeat")
            if ledger.total + amount > ledger.limit:
                raise SummaryError("trial_budget_exhausted")
            client = client_factory("openai", key_reader(key_file, provider="openai"))
            report["preflight"] = transport.preflight(client)
            ledger.reserve(amount=amount, run_id=run_id, model=current_scope["model"], case_id="three-event-windows")
            report.update(model_calls_reserved=len(suite["jobs.json"]), reserved_usd=str(amount),
                          cumulative_reserved_usd=str(ledger.total))
            for job in suite["jobs.json"]:
                folder = directory / job["job_id"]
                folder.mkdir(exist_ok=False)
                transport.write_new(folder / "request.json", job["request"])
                transport.write_new(folder / "attempt.json", {k: v for k, v in job.items() if k != "request"})
                report["model_calls_attempted"] += 1
                # Durable attempted count before request; a crash cannot look like no request.
                transport.write_new(folder / "started.json", {"attempted": True, "automatic_retry": False})
                started = time.monotonic()
                raw = client.request(transport.PROVIDERS["openai"]["endpoint"], job["request"])
                final = transport.final_only(raw, "openai")
                transport.write_new(folder / "final-response.json", final)
                case = next(case for case in suite["reviewer.json"] if case["case_id"] == job["case_id"])
                outcome = {"job_id": job["job_id"], "case_id": job["case_id"],
                    "elapsed_seconds": round(time.monotonic() - started, 3), "usage": final["usage"],
                    "estimated_usd_at_verified_ceiling": final["estimated_usd_at_verified_ceiling"]}
                try:
                    inspected = inspect_final(final, job, case)
                    for name in ("audit", "booklet", "catalogue"):
                        transport.write_new(folder / (name + ".json"), inspected[name])
                    metrics = inspected["metrics"]
                    needs_repair = bool(metrics["structural_errors"] or metrics["text_date_errors"]
                                        or not metrics["layout_complete"] or metrics["author_status"] != "ok")
                    outcome.update(status="requires_draft_repair" if needs_repair else "requires_semantic_review",
                                   **metrics)
                except SummaryError as error:
                    # Preserve malformed/refused first answers; other frozen windows stay independent.
                    outcome.update(status="author_answer_rejected", error_code=str(error),
                                   semantic_passed=None, production_accepted=False)
                transport.write_new(folder / "report.json", outcome)
                report["outcomes"].append(outcome)
                print(json.dumps({"job_id": job["job_id"], "status": outcome["status"]}), flush=True)
            report["status"] = "answered_requires_semantic_review"
    except SummaryError as error:
        report.update(status="stopped_no_retry", error_code=str(error))
        if getattr(error, "provider_diagnostic", None):
            report["provider_diagnostic"] = error.provider_diagnostic
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
            raise SummaryError("event_author_frozen_run_arguments_required")
        report = run(suite, args.output_dir, args.key_file, args.ledger, authorized=True)
        print(json.dumps({k: report[k] for k in ("status", "model_calls_attempted", "production_accepted")}))
        return 0 if report["status"] == "answered_requires_semantic_review" else 2
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "error", "code": "event_author_trial_failed_details_suppressed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
