#!/usr/bin/env python3
"""Freeze and optionally run six synthetic fidelity cases; API calls are opt-in."""
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
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory import summary_faithfulness as contract
from scripts import prepare_summary_comprehensive_comparison as sources
from scripts import prepare_summary_round2 as round2
from scripts import run_summary_comprehensive_comparison as transport
from scripts.prepare_summary_fidelity_comparison import digest

FIXTURE = ROOT / "tests/fixtures/summary_faithfulness_long_synthetic.json"
PROVIDER = "openai"
MAX_OUTPUT = 14000
MAX_RESERVATION = Decimal("0.60")
LEDGER_CEILING = Decimal("12.00")
REGRESSION_IDS = ("daily-01", "long-daily-01", "long-intimacy-01")


def _validate_case(case):
    rows = case["records"]
    contract.validate_synthetic_window(rows)
    known = {r["record_id"] for r in rows}
    if any(not group or not set(group) <= known for group in case.get("review_evidence_groups", [])):
        raise SummaryError("faithfulness_invalid_review_evidence")


def build_suite():
    previous = {c["case_id"]: c for c in round2.load_cases()}
    cases = [dict(copy.deepcopy(previous[cid]), cohort="regression") for cid in REGRESSION_IDS]
    long_cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if (len(long_cases) != 2 or any(not 2000 <= sum(len(r["content"]) for r in c["records"]) <= 24000
                                  or len(c["records"]) < 16 for c in long_cases)):
        raise SummaryError("faithfulness_two_multi_turn_long_cases_required")
    cases += [dict(c, cohort="new_long_development_not_blind") for c in long_cases]
    cases.append({"case_id": "known-negative-01", "cohort": "new_development_not_blind", "category": "daily",
        "focus": "明确没发生与无法确定、转述与现实核验分开",
        "records": [{"record_id": "kn-1", "recorded_at": "2036-10-20T20:00:00+08:00",
                     "source_kind": "synthetic", "content": "温祁：2036年10月18日我已经把信寄出去了，邮局给了收据。"},
                    {"record_id": "kn-2", "recorded_at": "2036-10-20T20:01:00+08:00",
                     "source_kind": "synthetic", "content": "程溪：温祁说信已经寄出，我还没看到收据。到今天我明确没有收到信；邮局有没有尝试投递，我不知道。"}],
        "review_targets": ["温祁自述已于10月18日寄信，按资料叙述标已发生；不凭未见收据改成未知",
                           "程溪转述寄信与温祁直接陈述区分；明确未收到与是否尝试投递未知区分"],
        "forbidden_claims": ["信已收到", "确定没有尝试投递", "寄信已获独立现实核验"],
        "review_evidence_groups": [["kn-1"], ["kn-2"]]})
    if len({c["case_id"] for c in cases}) != 6:
        raise SummaryError("faithfulness_unique_six_cases_required")
    writer = []
    for case in cases:
        _validate_case(case)
        case["evidence_catalog"] = sources._catalog(case["records"])
        writer.append({"case_id": case["case_id"], "records": [
            {"record_id": row["record_id"], "recorded_at": row["recorded_at"], "source_kind": "synthetic",
             "spans": [{"id": sid, "text": span["quote"]} for sid, span in case["evidence_catalog"].items()
                       if span["record_id"] == row["record_id"]]} for row in case["records"]]})
    response_schema = {"type": "object", "additionalProperties": False, "required": ["cases"],
        "properties": {"cases": {"type": "array", "minItems": 6, "maxItems": 6, "items": {
            "type": "object", "additionalProperties": False, "required": ["case_id", "summary"],
            "properties": {"case_id": {"type": "string", "enum": [c["case_id"] for c in cases]},
                           "summary": contract.SUMMARY_SCHEMA}}}}}
    instructions = contract.SYSTEM_PROMPT + "\n本批六题彼此独立，每个 case_id 恰好作答一次。"
    payload = {"model": transport.PROVIDERS[PROVIDER]["model"],
        "input": [{"role": "system", "content": instructions},
                  {"role": "user", "content": json.dumps({"cases": writer}, ensure_ascii=False)}],
        "store": False, "stream": False, "service_tier": "default", "reasoning": {"effort": "low"},
        "max_output_tokens": MAX_OUTPUT,
        "text": {"format": {"type": "json_schema", "name": "echo_faithfulness", "strict": True,
                            "schema": response_schema}}}
    manifest = {"schema": "echo-summary-faithfulness-suite-v1", "recipe": contract.RECIPE,
        "case_count": len(cases), "source_chars": sum(len(r["content"]) for c in cases for r in c["records"]),
        "source_kind": "synthetic_only", "contains_private_excerpts": False,
        "model": payload["model"], "prompt_sha256": hashlib.sha256(instructions.encode()).hexdigest(),
        "payload_sha256": digest(payload), "reviewer_sha256": digest(cases),
        "production_accepted": False, "evaluation_kind": "regression_and_development_not_blind"}
    return {"manifest.json": manifest, "request.json": payload, "reviewer.json": cases}


def scope(suite):
    payload = suite["request.json"]
    input_price, output_price = transport.PROVIDERS[PROVIDER]["prices"]
    # The old runner reserves a fixed 8,000 output tokens. This contract has its
    # own ceiling; charge the actual requested maximum, not that older constant.
    amount = ((len(transport.encoded(payload)) + 2048) * input_price
              + payload["max_output_tokens"] * output_price) / 1_000_000
    if amount > MAX_RESERVATION:
        raise SummaryError("faithfulness_reservation_exceeds_batch_cap")
    return {"recipe": contract.RECIPE, "model": suite["manifest.json"]["model"],
            "cases": 6, "source_chars": suite["manifest.json"]["source_chars"], "source_kind": "synthetic_only",
            "payload_sha256": suite["manifest.json"]["payload_sha256"], "maximum_calls": 1,
            "reserved_usd": str(amount), "ledger_ceiling_usd": str(LEDGER_CEILING)}


def freeze(directory, suite):
    directory = transport.protected(directory)
    directory.mkdir(parents=True, exist_ok=False)
    for name, data in suite.items():
        transport.write_new(directory / name, data)
    transport.write_new(directory / "scope.json", scope(suite))


def load_frozen(directory):
    directory = transport.protected(directory)
    expected = build_suite()
    actual = {name: json.loads((directory / name).read_text(encoding="utf-8")) for name in expected}
    # JSON embedded in request strings is preserved: compare canonical data, not pretty layout.
    if actual != expected or json.loads((directory / "scope.json").read_text(encoding="utf-8")) != scope(actual):
        raise SummaryError("faithfulness_frozen_material_changed")
    return actual


def inspect_final(final, suite):
    if final.get("finish") != "complete":
        raise SummaryError("faithfulness_incomplete_first_answer")
    try:
        data = json.loads(final["final_content"])
        if not isinstance(data, dict) or set(data) != {"cases"} or not isinstance(data["cases"], list):
            raise ValueError
        if any(not isinstance(c, dict) or set(c) != {"case_id", "summary"} for c in data["cases"]):
            raise ValueError
        answers = {c["case_id"]: c["summary"] for c in data["cases"]}
        if len(answers) != len(data["cases"]) or set(answers) != {c["case_id"] for c in suite["reviewer.json"]}:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise SummaryError("faithfulness_batch_format_or_coverage_invalid") from None
    return [{"case_id": case["case_id"], "cohort": case["cohort"], "summary": answers[case["case_id"]],
             **contract.inspect_draft(answers[case["case_id"]], case["evidence_catalog"])}
            for case in suite["reviewer.json"]]


def run(suite, directory, key_file, ledger_path, *, authorized=False,
        key_reader=transport.read_dpapi_key, client_factory=transport.Client):
    if not authorized:
        raise SummaryError("faithfulness_explicit_live_enable_required")
    if suite != build_suite():
        raise SummaryError("faithfulness_current_frozen_suite_required")
    directory, key_file, ledger_path = map(transport.protected, (directory, key_file, ledger_path))
    current_scope = scope(suite)
    amount = Decimal(current_scope["reserved_usd"])
    run_id = contract.RECIPE + ":" + current_scope["payload_sha256"]
    directory.mkdir(parents=True, exist_ok=False)
    transport.write_new(directory / "request.json", suite["request.json"])
    transport.write_new(directory / "scope.json", current_scope)
    report = {"recipe": contract.RECIPE, "status": "not_run", "production_accepted": False,
              "model_calls_reserved": 0, "reserved_usd": str(amount)}
    try:
        with BudgetLedger(ledger_path, maximum_limit=LEDGER_CEILING) as ledger:
            if ledger.data.get("provider") != PROVIDER:
                raise SummaryError("wrong_provider_ledger")
            if any(r.get("run_id") == run_id for r in ledger.data["reservations"]):
                raise SummaryError("faithfulness_no_automatic_repeat")
            if ledger.total + amount > ledger.limit:
                raise SummaryError("trial_budget_exhausted")
            client = client_factory(PROVIDER, key_reader(key_file, provider=PROVIDER))
            report["preflight"] = transport.preflight(client)
            ledger.reserve(amount=amount, run_id=run_id, model=current_scope["model"], case_id="synthetic-six-v1")
            report["model_calls_reserved"] = 1
            transport.write_new(directory / "attempt.json", {"run_id": run_id, "scope": current_scope})
            started = time.monotonic()
            raw = client.request(transport.PROVIDERS[PROVIDER]["endpoint"], suite["request.json"])
            final = transport.final_only(raw, PROVIDER)
            transport.write_new(directory / "final-response.json", final)
            report.update(status="answered_requires_semantic_review", outcomes=inspect_final(final, suite),
                elapsed_seconds=round(time.monotonic() - started, 3), usage=final["usage"],
                estimated_usd_at_verified_ceiling=final["estimated_usd_at_verified_ceiling"],
                cumulative_reserved_usd=str(ledger.total))
    except SummaryError as error:
        report.update(status="stopped_no_retry", error_code=str(error))
    except Exception:
        report.update(status="stopped_no_retry", error_code="failure_details_suppressed")
    transport.write_new(directory / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-dir", type=Path)
    parser.add_argument("--suite-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--ledger", type=Path)
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
            raise SummaryError("faithfulness_frozen_run_arguments_required")
        report = run(suite, args.output_dir, args.key_file, args.ledger, authorized=True)
        print(json.dumps({k: report[k] for k in ("status", "model_calls_reserved", "reserved_usd", "production_accepted")}))
        return 0 if report["status"] == "answered_requires_semantic_review" else 2
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "error", "code": "faithfulness_trial_failed_details_suppressed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
