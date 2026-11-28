#!/usr/bin/env python3
"""Review pinned synthetic V5 first answers once; save proposals separately."""
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
from backend.memory import summary_event_author_v5 as author
from backend.memory import summary_event_review as review
from backend.memory import summary_reading_card as cards
from backend.memory.summary_trial_budget import BudgetLedger
from scripts import summary_event_author_trial as base
from scripts import run_summary_comprehensive_comparison as transport
from scripts.prepare_summary_fidelity_comparison import digest

RECIPE = "echo-summary-time-state-review-trial-v1"
FIRST_HASHES = {
    "long-daily-01": "b94e1defe636a58861912c6bb2144f5ac418bbbf08fcbfa0c19db3ecbcc6cd57",
    "long-sparse-01": "3a680d42976dc28fbcbfe6082aab133137af53faf3acc35e6583ce5b37299879",
    "long-dense-01": "ba48b0421f509222af7ba1e896f03a831e9a59b195615295748209ef04cecd33"}
MAX_OUTPUT = 6500
MAX_RESERVATION = Decimal("1.80")


def control_case():
    texts = [
        "林清：音乐会原定2041年2月3日举行，主办方在2041年2月3日决定取消。原定日与取消决定日确实相同。",
        "林清：我的票已申请退款，但是否到账没有查，不能确认收到，也不能确认没收到。",
        "方岸：我的另一张票已经核对过银行和订单，截至这段记录退款明确尚未到账。",
        "林清：方岸送我的手套已经在冬天收到了，具体哪一天没有记清。",
        "方岸：我还没有做包裹签收确认。至于快递是否已送到，我没查，不能由没点确认推断没送到。",
        "林清：相册程序已经测试通过，但明确还未发布，没有人替我发布。",
        "方岸：2041年3月8日我们确定了2041年4月9日的返程安排；现在仍照这个计划，还没有返程。",
        "林清：我喜欢现在的相册布局，看到它会觉得高兴。"]
    records = [{"record_id": f"control-{i}", "content": text, "source_kind": "synthetic",
                "recorded_at": "2041-12-01T12:00:00+08:00"} for i, text in enumerate(texts, 1)]
    def fact(text, event, ref):
        return {"text": text, "event": event, "evidence_ids": [ref], "statement_role": "direct",
                "supersedes": None, "time_evidence": [], "history_time_evidence": []}
    facts = [
        fact("音乐会已取消，原定活动与取消决定是同一天。", {"status": "cancelled", "planned_for_ref": "t1", "cancelled_at_ref": "t2"}, "s1"),
        fact("林清的票退款是否到账未知。", {"status": "unknown"}, "s2"),
        fact("方岸的另一张票退款明确尚未到账。", {"status": "not_happened"}, "s3"),
        fact("林清已在冬天收到方岸送的手套，具体日期未知。", {"status": "happened", "occurred_at_ref": None}, "s4"),
        fact("方岸尚未做包裹签收确认。", {"status": "not_happened"}, "s5"),
        fact("包裹是否已送到尚未核实。", {"status": "unknown"}, "s5"),
        fact("相册程序明确尚未发布。", {"status": "not_happened"}, "s6"),
        fact("两人计划按已确定的日期返程，该安排仍有效。", {"status": "planned", "planned_for_ref": "t4", "plan_scope": "current", "replaced_by": None}, "s7"),
        fact("林清喜欢现在的相册布局，看着会高兴。", {"status": "not_applicable"}, "s8")]
    raw = {"status": "ok", "events": [{"event_id": "e1", "kind": "mixed", "topic": "正确时间与状态对照", "facts": facts,
        "uncertainties": []}], "uncertainties": [], "relations": [], "gaps": [], "refusal_reason": None}
    return {"case_id": "time-state-positive-control", "records": records,
            "review_targets": ["原稿九项在本次日期状态范围均应保留；核对动作与所核对事件不能混淆。"],
            "forbidden_claims": ["同日取消与原定活动必须拆成不同日期", "明确未到账改成未知", "未知送达改成未送达"],
            "cohort": "hand_authored_positive_control_not_author_quality_exam"}, raw


def build_suite(first_directory, *, review_version="v3"):
    first_directory = transport.protected(first_directory)
    original = base.build_suite(author_version="v5")
    cases, jobs = [], []
    inputs = []
    for case in original["reviewer.json"]:
        path = first_directory / (case["case_id"] + "-event-v5") / "final-response.json"
        blob = path.read_bytes()
        if hashlib.sha256(blob).hexdigest() != FIRST_HASHES[case["case_id"]]:
            raise SummaryError("summary_review_unrecognized_first_answer")
        final = json.loads(blob)
        if final["finish"] != "complete":
            raise SummaryError("summary_review_incomplete_original")
        old_job = next(j for j in original["jobs.json"] if j["case_id"] == case["case_id"])
        inputs.append((case, json.loads(final["final_content"]), old_job["prompt_sha256"], FIRST_HASHES[case["case_id"]]))
    control, raw = control_case()
    inputs.append((control, raw, digest("hand-authored-positive-control-v1"), None))
    for case, raw, first_prompt, first_file_hash in inputs:
        prepared = review.prepare_review(case["records"], raw, version=review_version)
        case = copy.deepcopy(case)
        case["evidence_catalog"] = author.prepare_author_window(case["records"])["catalog"]
        case["first_draft"] = raw
        payload = {"model": transport.PROVIDERS["openai"]["model"], "input": prepared["messages"],
            "store": False, "stream": False, "service_tier": "default", "reasoning": {"effort": "low" if review_version == "v1" else "medium"},
            "max_output_tokens": MAX_OUTPUT,
            "text": {"format": {"type": "json_schema", "name": "echo_time_state_review", "strict": True,
                                  "schema": base.provider_schema(prepared["response_schema"])}}}
        jobs.append({"job_id": case["case_id"] + "-review-" + review_version, "case_id": case["case_id"],
            "author_recipe": author.RECIPE, "review_binding": prepared["binding"],
            "identity": {"source_namespace": "synthetic", "room_id": case["case_id"], "branch_id": "fixture"},
            "source_window_sha256": cards.fingerprint(case["records"]),
            "first_response_sha256": first_file_hash, "first_prompt_sha256": first_prompt,
            "prompt_sha256": hashlib.sha256(prepared["messages"][0]["content"].encode()).hexdigest(),
            "payload_sha256": digest(payload), "request": payload})
        cases.append(case)
    return {"manifest.json": {"schema": RECIPE, "jobs_sha256": digest(jobs), "reviewer_sha256": digest(cases),
        "source_chars": sum(len(r["content"]) for c in cases for r in c["records"]), "jobs": len(jobs),
        "source_kind": "synthetic_only", "evaluation_kind": "reused_development_plus_positive_controls_not_blind",
        "production_accepted": False}, "jobs.json": jobs, "reviewer.json": cases}


def scope(suite):
    inp, out = transport.PROVIDERS["openai"]["prices"]
    amounts = {j["job_id"]: ((len(transport.encoded(j["request"])) + 2048) * inp + MAX_OUTPUT * out) / 1_000_000
               for j in suite["jobs.json"]}
    total = sum(amounts.values(), Decimal("0"))
    if total > MAX_RESERVATION:
        raise SummaryError("summary_review_batch_budget_exceeded")
    return {"recipe": RECIPE, "jobs_sha256": suite["manifest.json"]["jobs_sha256"],
            "maximum_calls": len(amounts), "max_output_tokens_per_call": MAX_OUTPUT, "reserved_usd": str(total),
            "job_reserved_usd": {k: str(v) for k, v in amounts.items()}, "ledger_ceiling_usd": str(base.LEDGER_CEILING),
            "automatic_retry": False, "source_kind": "synthetic_only"}


def freeze(directory, suite):
    directory = transport.protected(directory)
    directory.mkdir(parents=True, exist_ok=False)
    for name, value in {**suite, "scope.json": scope(suite)}.items():
        transport.write_new(directory / name, value)


def load_frozen(directory, first_directory, *, review_version="v3"):
    expected = build_suite(first_directory, review_version=review_version)
    directory = transport.protected(directory)
    actual = {name: json.loads((directory / name).read_text(encoding="utf-8")) for name in expected}
    if actual != expected or json.loads((directory / "scope.json").read_text(encoding="utf-8")) != scope(actual):
        raise SummaryError("summary_review_frozen_material_changed")
    return actual


def inspect_final(final, job, case):
    if final.get("finish") != "complete":
        raise SummaryError("summary_review_incomplete_answer")
    if job["case_id"] != case["case_id"] or cards.fingerprint(case["records"]) != job["source_window_sha256"]:
        raise SummaryError("summary_review_wrong_source")
    try:
        response = json.loads(final["final_content"])
    except (KeyError, ValueError, TypeError):
        raise SummaryError("summary_review_invalid_json") from None
    revised = review.apply_review(case["records"], case["first_draft"], response,
                                  expected_binding=job["review_binding"])
    result = {"review": response, "receipt": revised["receipt"]}
    if revised["candidate"] is None:
        result["metrics"] = {"status": "review_incomplete_no_candidate", "semantic_passed": None, "production_accepted": False}
        return result
    # The candidate is a derived two-step artifact, not the unmodified author's first answer.
    candidate_job = copy.deepcopy(job)
    candidate_job["prompt_sha256"] = digest({"author": job["first_prompt_sha256"], "review": job["prompt_sha256"]})
    inspected = base.inspect_final({"finish": "complete", "model": final["model"],
        "final_content": json.dumps(revised["candidate"], ensure_ascii=False)}, candidate_job, case)
    result.update(inspected, **{"candidate-raw": revised["candidate"]})
    result["receipt"].update(first_response_sha256=job["first_response_sha256"],
        pipeline_prompt_sha256=candidate_job["prompt_sha256"], candidate_audit_sha256=cards.fingerprint(inspected["audit"]))
    m = result["metrics"]
    has_errors = bool(m["structural_errors"] or m["text_date_errors"] or m["date_witness_errors"] or not m["layout_complete"])
    m.update(status="candidate_requires_repair" if has_errors else "candidate_requires_semantic_review",
             revisions=len(revised["receipt"]["changes"]), reviewed_facts=len(response["decisions"]))
    return result


def replay_saved(suite_directory, first_directory, prior_directory, output_directory):
    """Reinspect saved answers after host fixes; no key, client or ledger is opened."""
    suite_directory, prior_directory, output_directory = map(transport.protected,
        (suite_directory, prior_directory, output_directory))
    old = {name: json.loads((suite_directory / name).read_text(encoding="utf-8"))
           for name in ("manifest.json", "jobs.json", "reviewer.json")}
    version = review.version_for_recipe(old["jobs.json"][0]["review_binding"]["recipe"])
    expected = build_suite(first_directory, review_version=version)
    if (old["reviewer.json"] != expected["reviewer.json"]
            or old["manifest.json"]["jobs_sha256"] != digest(old["jobs.json"])
            or old["manifest.json"]["reviewer_sha256"] != digest(old["reviewer.json"])
            or len(old["jobs.json"]) != len(expected["jobs.json"])
            or {**old["manifest.json"], "jobs_sha256": expected["manifest.json"]["jobs_sha256"]} != expected["manifest.json"]
            or json.loads((suite_directory / "scope.json").read_text(encoding="utf-8")) != scope(old)
            or json.loads((prior_directory / "manifest.json").read_text(encoding="utf-8")) != old["manifest.json"]):
        raise SummaryError("summary_review_replay_material_changed")
    migrations = []
    for prior, current, case in zip(old["jobs.json"], expected["jobs.json"], expected["reviewer.json"]):
        migration = None
        if prior["review_binding"] != current["review_binding"]:
            migration = review.rebind_saved_request(case["records"], case["first_draft"],
                prior["request"]["input"], prior["review_binding"])
            if migration["binding"] != current["review_binding"]:
                raise SummaryError("summary_review_replay_material_changed")
        normalized = {**prior, "review_binding": current["review_binding"]}
        if normalized != current:
            raise SummaryError("summary_review_replay_material_changed")
        folder = prior_directory / prior["job_id"]
        if (json.loads((folder / "request.json").read_text(encoding="utf-8")) != prior["request"]
                or json.loads((folder / "attempt.json").read_text(encoding="utf-8")) != {k: v for k, v in prior.items() if k != "request"}):
            raise SummaryError("summary_review_replay_material_changed")
        migrations.append(migration)
    output_directory.mkdir(parents=True, exist_ok=False)
    report = {"recipe": RECIPE, "offline_replay": True, "model_calls_attempted": 0,
              "production_accepted": False, "outcomes": []}
    for job, case, migration in zip(expected["jobs.json"], expected["reviewer.json"], migrations):
        prior = prior_directory / job["job_id"]
        final_path = prior / "final-response.json"
        response_bytes = final_path.read_bytes()
        folder = output_directory / job["job_id"]
        folder.mkdir(exist_ok=False)
        transport.write_new(folder / "replay-origin.json", {
            "first_review_response_sha256": hashlib.sha256(response_bytes).hexdigest(),
            "prior_report_sha256": hashlib.sha256((prior / "report.json").read_bytes()).hexdigest(),
            "binding_migration": migration, "model_calls_attempted": 0})
        try:
            inspected = inspect_final(json.loads(response_bytes), job, case)
            for name, value in inspected.items():
                if name != "metrics": transport.write_new(folder / (name + ".json"), value)
            outcome = {"job_id": job["job_id"], "case_id": case["case_id"], **inspected["metrics"]}
        except SummaryError as error:
            outcome = {"job_id": job["job_id"], "status": "review_rejected", "error_code": str(error),
                       "semantic_passed": None, "production_accepted": False}
        if final_path.read_bytes() != response_bytes:
            raise SummaryError("summary_review_original_changed_during_replay")
        transport.write_new(folder / "report.json", outcome)
        report["outcomes"].append(outcome)
    transport.write_new(output_directory / "report.json", report)
    return report


def run(suite, first_directory, directory, key_file, ledger_path, *, authorized=False,
        key_reader=transport.read_dpapi_key, client_factory=transport.Client):
    if not authorized:
        raise SummaryError("summary_review_explicit_live_enable_required")
    version = review.version_for_recipe(suite["jobs.json"][0]["review_binding"]["recipe"])
    if suite != build_suite(first_directory, review_version=version):
        raise SummaryError("summary_review_current_suite_required")
    directory, key_file, ledger_path = map(transport.protected, (directory, key_file, ledger_path))
    limits = scope(suite)
    directory.mkdir(parents=True, exist_ok=False)
    transport.write_new(directory / "manifest.json", suite["manifest.json"])
    transport.write_new(directory / "scope.json", limits)
    report = {"recipe": RECIPE, "status": "not_run", "model_calls_attempted": 0, "outcomes": [], "production_accepted": False}
    try:
        with BudgetLedger(ledger_path, maximum_limit=base.LEDGER_CEILING) as ledger:
            run_id = RECIPE + ":" + limits["jobs_sha256"]
            if ledger.data.get("provider") != "openai":
                raise SummaryError("wrong_provider_ledger")
            if any(r.get("run_id") == run_id for r in ledger.data["reservations"]):
                raise SummaryError("summary_review_no_automatic_repeat")
            amount = Decimal(limits["reserved_usd"])
            if ledger.total + amount > ledger.limit:
                raise SummaryError("trial_budget_exhausted")
            client = client_factory("openai", key_reader(key_file, provider="openai"))
            report["preflight"] = transport.preflight(client)
            ledger.reserve(amount=amount, run_id=run_id, model=transport.PROVIDERS["openai"]["model"], case_id="date-state-review-four-windows")
            report.update(reserved_usd=str(amount), cumulative_reserved_usd=str(ledger.total))
            for job in suite["jobs.json"]:
                folder = directory / job["job_id"]
                folder.mkdir(exist_ok=False)
                transport.write_new(folder / "request.json", job["request"])
                transport.write_new(folder / "attempt.json", {k: v for k, v in job.items() if k != "request"})
                report["model_calls_attempted"] += 1
                transport.write_new(folder / "started.json", {"attempted": True, "automatic_retry": False})
                started = time.monotonic()
                final = transport.final_only(client.request(transport.PROVIDERS["openai"]["endpoint"], job["request"]), "openai")
                transport.write_new(folder / "final-response.json", final)
                case = next(c for c in suite["reviewer.json"] if c["case_id"] == job["case_id"])
                outcome = {"job_id": job["job_id"], "case_id": job["case_id"], "usage": final["usage"],
                    "estimated_usd_at_verified_ceiling": final["estimated_usd_at_verified_ceiling"],
                    "elapsed_seconds": round(time.monotonic() - started, 3)}
                try:
                    inspected = inspect_final(final, job, case)
                    for name, value in inspected.items():
                        if name != "metrics":
                            transport.write_new(folder / (name + ".json"), value)
                    outcome.update(inspected["metrics"])
                except SummaryError as error:
                    outcome.update(status="review_rejected", error_code=str(error), semantic_passed=None, production_accepted=False)
                transport.write_new(folder / "report.json", outcome)
                report["outcomes"].append(outcome)
                print(json.dumps({"job_id": job["job_id"], "status": outcome["status"]}), flush=True)
            report["status"] = "answered_requires_semantic_review"
    except SummaryError as error:
        report.update(status="stopped_no_retry", error_code=str(error))
    except Exception:
        report.update(status="stopped_no_retry", error_code="failure_details_suppressed")
    transport.write_new(directory / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first-answer-dir", type=Path, required=True)
    parser.add_argument("--review-version", choices=("v1", "v2", "v3"), default="v3")
    for name in ("prepare-dir", "suite-dir", "output-dir", "key-file", "ledger", "prior-run-dir", "replay-output-dir"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--enable-api", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.replay_output_dir:
            if args.enable_api or args.prepare_dir or not args.suite_dir or not args.prior_run_dir:
                raise SummaryError("summary_review_offline_replay_arguments_required")
            report = replay_saved(args.suite_dir, args.first_answer_dir, args.prior_run_dir, args.replay_output_dir)
            print(json.dumps({"status": "offline_replay_complete", "model_calls_attempted": 0,
                "outcomes": [{"job_id": r["job_id"], "status": r["status"]} for r in report["outcomes"]]}))
            return 0
        suite = load_frozen(args.suite_dir, args.first_answer_dir, review_version=args.review_version) if args.suite_dir else build_suite(args.first_answer_dir, review_version=args.review_version)
        if args.prepare_dir:
            freeze(args.prepare_dir, suite)
        if not args.enable_api:
            print(json.dumps({"status": "offline_only", **scope(suite)}))
            return 0
        if args.prepare_dir or not all((args.suite_dir, args.output_dir, args.key_file, args.ledger)):
            raise SummaryError("summary_review_frozen_run_arguments_required")
        report = run(suite, args.first_answer_dir, args.output_dir, args.key_file, args.ledger, authorized=True)
        print(json.dumps({k: report[k] for k in ("status", "model_calls_attempted", "production_accepted")}))
        return 0 if report["status"] == "answered_requires_semantic_review" else 2
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "error", "code": "summary_review_trial_failed_details_suppressed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
