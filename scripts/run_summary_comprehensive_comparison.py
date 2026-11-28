#!/usr/bin/env python3
"""One fixed twelve-case batch per provider, explicitly authorized and budgeted."""
from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jsonschema import Draft202012Validator
from backend.memory.caretaker_summary import SummaryError, _object
from backend.memory.caretaker_summary_v2 import _explicit_dates
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory.venice_summary_trial import _NoRedirect, read_dpapi_key, preflight as venice_preflight
from backend.memory.openai_summary_trial import preflight as openai_preflight
from scripts import prepare_summary_comprehensive_comparison as exam
from scripts.prepare_summary_fidelity_comparison import digest, encoded

PROVIDERS = {
    "openai": {"model": "gpt-5.6-sol", "base": "https://api.openai.com/v1", "endpoint": "/responses",
               "prices": (Decimal("4"), Decimal("20")), "ceiling": Decimal("10.00")},
    "venice": {"model": "mistral-small-3-2-24b-instruct", "base": "https://api.venice.ai/api/v1",
               "endpoint": "/chat/completions", "prices": (Decimal("0.09375"), Decimal("0.25")),
               "ceiling": Decimal("0.10")},
    "deepseek": {"model": "deepseek-flash", "base": "https://api.deepseek.com",
                 "endpoint": "/chat/completions", "prices": (Decimal("0.30"), Decimal("1.20")),
                 "ceiling": Decimal("0.05")},
}
RECIPE = "echo-comprehensive-batch-v1"
MAX_OUTPUT = 8000
MAX_RESPONSE_BYTES = 2_000_000
PRICE_RECHECK_AFTER = date(2026, 11, 30)
ASSESSMENT_REVISION = "comprehensive-structure-v2-complete-date-forms"


def http_error_diagnostic(raw, secret):
    """Only known error categories, never echoed prompts, headers or arbitrary text."""
    if not isinstance(raw, bytes) or len(raw) > 16384 or secret.encode() in raw:
        return {}
    try:
        error = json.loads(raw).get("error", {})
        if not isinstance(error, dict):
            return {}
    except (ValueError, TypeError, AttributeError):
        return {}
    result = {}
    for field, allowed in {
        "code": {"invalid_json_schema", "invalid_value", "unsupported_value", "invalid_api_key", "insufficient_quota"},
        "type": {"invalid_request_error", "authentication_error", "insufficient_quota"},
        "param": {"text.format", "text.format.schema", "response_format", "model", "max_output_tokens"},
    }.items():
        value = error.get(field)
        if isinstance(value, str) and value in allowed:
            result[field] = value
    message = error.get("message")
    if isinstance(message, str) and "schema" in message.lower():
        keywords = [word for word in ("uniqueItems", "additionalProperties", "required", "minItems", "maxItems", "pattern")
                    if "'" + word + "'" in message or '"' + word + '"' in message]
        if keywords:
            result["mentioned_schema_keywords"] = keywords
    return result


def protected(path):
    path, root = Path(path).resolve(), exam.PRIVATE_ROOT.resolve()
    if path == root or root not in path.parents:
        raise SummaryError("protected_trial_path_required")
    return path


def write_new(path, value):
    with Path(path).open("xb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())


def load_frozen(directory):
    directory = protected(directory)
    bundle = {name: json.loads((directory / name).read_text(encoding="utf-8"))
              for name in ("manifest.json", "writer_inputs.json", "reviewer_sheet.json")}
    private = json.loads(exam.PRIVATE_FIXTURE.read_text(encoding="utf-8"))
    if bundle != exam.build_bundle(exam.load_cases(), private):
        raise SummaryError("frozen_exam_changed")
    return bundle


def batch_input(bundle):
    cases = bundle["writer_inputs.json"]["cases"]
    if len(cases) != 12 or len({c["case_id"] for c in cases}) != 12:
        raise SummaryError("twelve_case_exam_required")
    if any(c["messages"][0] != cases[0]["messages"][0]
           or c["response_schema"] != cases[0]["response_schema"] for c in cases):
        raise SummaryError("common_prompt_and_schema_required")
    schema = _object({"cases": {"type": "array", "minItems": 12, "maxItems": 12,
        "items": _object({"case_id": {"type": "string", "enum": [c["case_id"] for c in cases]},
                          "summary": copy.deepcopy(cases[0]["response_schema"])})}})
    instruction = ("\n本次有十二道彼此独立的题，逐题摘要，每个 case_id 恰好返回一次。"
        "各题的片段编号只在该题内有效，不得跨题拼接证据、人物、日期或经历。"
        "即使某题无法处理，也单独返回该题状态，继续处理其他题。"
        "仅输出最终 JSON，不输出推理。以下 JSON Schema 只规定格式，不是任何题的答案：\n")
    messages = [{"role": "system", "content": cases[0]["messages"][0]["content"] + instruction
                 + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))},
                {"role": "user", "content": json.dumps({"cases": [
                    {"case_id": c["case_id"], **json.loads(c["messages"][1]["content"])}
                    for c in cases]}, ensure_ascii=False, separators=(",", ":"))}]
    return messages, schema


def payload_for(bundle, provider):
    messages, schema = batch_input(bundle)
    model = PROVIDERS[provider]["model"]
    if provider == "openai":
        return {"model": model, "input": messages, "store": False, "stream": False,
                "service_tier": "default", "reasoning": {"effort": "low"}, "max_output_tokens": MAX_OUTPUT,
                "text": {"format": {"type": "json_schema", "name": "echo_comprehensive",
                                    "strict": True, "schema": schema}}}
    payload = {"model": model, "messages": messages, "stream": False, "max_tokens": MAX_OUTPUT,
               "temperature": 0, "n": 1}
    if provider == "deepseek":
        payload.update(thinking={"type": "disabled"}, response_format={"type": "json_object"})
    else:
        payload.update(store=False,
            response_format={"type": "json_schema", "json_schema": {
                "name": "echo_comprehensive", "strict": True, "schema": schema}},
            venice_parameters={"include_venice_system_prompt": False, "disable_thinking": True,
                "strip_thinking_response": True, "enable_web_search": "off",
                "enable_web_scraping": False, "enable_web_citations": False})
    return payload


def reservation(payload, provider):
    inp, out = PROVIDERS[provider]["prices"]
    # Conservative UTF-8 byte bound; no credit for caching or off-peak discounts.
    return ((len(encoded(payload)) + 2048) * inp + MAX_OUTPUT * out) / 1_000_000


@dataclass(repr=False)
class Client:
    provider: str
    api_key: str = field(repr=False)

    def request(self, endpoint, payload=None):
        if self.provider not in PROVIDERS:
            raise SummaryError("provider_not_allowed")
        allowed = {"openai": ("/models/gpt-5.6-sol", "/responses"),
                   "venice": ("/api_keys/rate_limits", "/models?type=text", "/chat/completions"),
                   "deepseek": ("/models", "/user/balance", "/chat/completions")}
        if endpoint not in allowed[self.provider]:
            raise SummaryError("endpoint_not_allowed")
        if (not isinstance(self.api_key, str) or not 32 <= len(self.api_key) <= 1024
                or not self.api_key.isascii() or any(c.isspace() for c in self.api_key)):
            raise SummaryError("invalid_key_format")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if body is not None and self.api_key.encode() in body:
            raise SummaryError("key_in_payload_blocked")
        req = urllib.request.Request(PROVIDERS[self.provider]["base"] + endpoint, data=body, headers={
            "Authorization": "Bearer " + self.api_key, "Content-Type": "application/json",
            "Accept": "application/json", "User-Agent": "EchoPact-bounded-comprehensive-trial/1"})
        try:
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=180) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            failure = SummaryError(f"{self.provider}_http_{error.code}")
            try:
                failure.provider_diagnostic = http_error_diagnostic(error.read(16385), self.api_key)
            except Exception:
                failure.provider_diagnostic = {}
            raise failure from None
        except SummaryError:
            raise
        except Exception:
            raise SummaryError(f"{self.provider}_network_failure_no_retry") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SummaryError("provider_response_too_large")
        if self.api_key.encode() in raw:
            raise SummaryError("provider_echoed_secret_suppressed")
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise SummaryError("provider_invalid_json") from None


def preflight(client):
    if date.today() > PRICE_RECHECK_AFTER:
        raise SummaryError("trial_prices_require_reverification")
    if client.provider == "openai":
        return openai_preflight(client)
    if client.provider == "venice":
        return venice_preflight(client, PROVIDERS["venice"]["model"])
    models = client.request("/models").get("data", [])
    if not any(isinstance(m, dict) and m.get("id") == PROVIDERS["deepseek"]["model"] for m in models):
        raise SummaryError("selected_model_not_available")
    if client.request("/user/balance").get("is_available") is not True:
        raise SummaryError("deepseek_balance_unavailable")
    return {"model": PROVIDERS["deepseek"]["model"], "authentication_verified": True,
            "balance_available": True, "data_retention_not_claimed_zero": True}


def final_only(response, provider):
    """Keep final content and billing counters, never reasoning or entire raw responses."""
    model = response.get("model")
    matches_model = model == PROVIDERS[provider]["model"]
    if not isinstance(model, str) or len(model) > 120 or not all(c.isalnum() or c in ".-_" for c in model):
        model = "unexpected_model_id_suppressed"
    usage = response.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    keys = ("input_tokens", "output_tokens") if provider == "openai" else ("prompt_tokens", "completion_tokens")
    counters = {k: usage[k] for k in keys if type(usage.get(k)) is int and usage[k] >= 0}
    if provider == "openai":
        parts, refusal = [], False
        for item in response.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for part in item.get("content", []):
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif part.get("type") == "refusal":
                    refusal = True
        finish = "refused" if refusal else ("complete" if response.get("status") == "completed" else "incomplete")
        content = "\n".join(parts)
    else:
        choices = response.get("choices", [])
        if not isinstance(choices, list) or len(choices) != 1:
            raise SummaryError("summary_response_invalid")
        message = choices[0].get("message", {})
        content = message.get("content") or ""
        if not isinstance(content, str):
            raise SummaryError("summary_response_invalid")
        finish = ("refused" if message.get("refusal") else
                  "complete" if choices[0].get("finish_reason") == "stop" else "incomplete")
    inp, out = PROVIDERS[provider]["prices"]
    cost = str((counters[keys[0]] * inp + counters[keys[1]] * out) / 1_000_000) if len(counters) == 2 and matches_model else None
    return {"model": model, "finish": finish if matches_model else "model_changed", "final_content": content, "usage": counters,
            "estimated_usd_at_verified_ceiling": cost}


def canonical_complete_date(value):
    """Compare the two full-date forms allowed by the frozen prompt, without rewriting evidence."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
    if match is None:
        match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", value)
    if match is None:
        return None
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None


def inspect_summary(summary, catalog, *, summary_schema=None):
    schema = exam.SUMMARY_SCHEMA if summary_schema is None else summary_schema
    if list(Draft202012Validator(schema).iter_errors(summary)):
        return ["schema_invalid"]
    errors = []
    if summary["status"] in ("refused", "insufficient") and summary["facts"]:
        errors.append("non_ok_with_facts")
    if summary["status"] == "refused" and not summary["refusal_reason"]:
        errors.append("refusal_reason_missing")
    if summary["status"] == "ok" and (not summary["facts"] or summary["refusal_reason"] is not None):
        errors.append("ok_status_inconsistent")
    for fact in summary["facts"]:
        refs = fact["evidence_ids"]
        if len(refs) != len(set(refs)) or any(r not in catalog for r in refs):
            errors.append("invalid_evidence_ids")
            continue
        if not fact["text"].strip():
            errors.append("blank_fact")
        event, planned = fact["event_date"], fact["planned_date"]
        if event is not None and fact["event_status"] != "happened":
            errors.append("event_date_requires_happened")
        if planned is not None and fact["event_status"] not in ("planned", "cancelled"):
            errors.append("planned_date_requires_plan")
        supported = set().union(*(_explicit_dates(catalog[r]["quote"]) for r in refs))
        for value in (event, planned):
            if value is not None:
                canonical = canonical_complete_date(value)
                if canonical is None:
                    errors.append("invalid_complete_date_format")
                elif canonical not in supported:
                    errors.append("date_without_source_support")
        history = fact["supersedes"]
        if fact["statement_role"] == "correction":
            if history is None or not history["text"].strip():
                errors.append("correction_history_required")
            elif any(r not in catalog for r in history["evidence_ids"]):
                errors.append("invalid_history_evidence")
        elif history is not None:
            errors.append("unexpected_history")
    return sorted(set(errors))


def inspect_final(final, bundle, *, summary_schema=None):
    reviews = bundle["reviewer_sheet.json"]["cases"]
    common_error = None
    try:
        parsed = json.loads(final["final_content"])
        rows = parsed["cases"]
        if not isinstance(parsed, dict) or set(parsed) != {"cases"} or not isinstance(rows, list):
            raise ValueError
        if any(not isinstance(r, dict) or set(r) != {"case_id", "summary"} for r in rows):
            raise ValueError
        ids = [r["case_id"] for r in rows]
        if len(ids) != len(set(ids)) or set(ids) != {r["case_id"] for r in reviews}:
            raise ValueError
        answers = {r["case_id"]: r["summary"] for r in rows}
    except (ValueError, TypeError, KeyError):
        answers = {}
        common_error = "blank_output" if not final["final_content"].strip() else "batch_format_or_coverage_invalid"
    if final["finish"] != "complete":
        common_error = "provider_" + final["finish"]
    outcomes = []
    for case in reviews:
        summary = answers.get(case["case_id"])
        errors = [common_error] if common_error else inspect_summary(
            summary, case["evidence_catalog"], summary_schema=summary_schema)
        outcomes.append({"case_id": case["case_id"], "category": case["category"],
            "summary": summary, "structural_errors": errors, "structural_passed": not errors,
            "assessment_revision": ASSESSMENT_REVISION,
            "semantic_passed": None, "review_status": "requires_separate_semantic_review"})
    return outcomes


def scope_for(bundle):
    return {"schema": "echo-comprehensive-authorized-scope-v1", "recipe": RECIPE,
            "exam_manifest_sha256": digest(bundle["manifest.json"]),
            "provider_requests_sha256": {p: digest(payload_for(bundle, p)) for p in PROVIDERS},
            "calls_per_provider": 1, "case_count": 12, "source_chars": bundle["manifest.json"]["source_chars"],
            "private_source_chars": sum(len(r["content"]) for c in bundle["reviewer_sheet.json"]["cases"]
                if c["category"] == "explicit_adult_excerpt" for r in c["records"]),
            "purpose": "fixed_summary_fidelity_comparison", "original_databases_read_only": True}


def run(bundle, provider, directory, key_file, ledger_path, authorization, *, key_reader=read_dpapi_key,
        client_factory=Client):
    directory, key_file, ledger_path = map(protected, (directory, key_file, ledger_path))
    if (not isinstance(authorization, dict) or set(authorization) != {"scope", "user_authorized", "authorization_id"}
            or authorization["user_authorized"] is not True or authorization["scope"] != scope_for(bundle)
            or authorization["authorization_id"] != "liora-comprehensive-20261125"):
        raise SummaryError("exact_authorized_scope_required")
    payload = payload_for(bundle, provider)
    amount = reservation(payload, provider)
    directory.mkdir(parents=True, exist_ok=False)
    write_new(directory / "request.json", payload)
    write_new(directory / "scope.json", authorization)
    report = {"provider": provider, "model": PROVIDERS[provider]["model"], "recipe": RECIPE,
              "status": "not_run", "model_calls_reserved": 0, "production_accepted": False,
              "payload_sha256": digest(payload), "reserved_usd": str(amount), "outcomes": []}
    run_id = authorization["authorization_id"] + ":" + provider
    try:
        with BudgetLedger(ledger_path, maximum_limit=PROVIDERS[provider]["ceiling"]) as ledger:
            if any(r.get("run_id") == run_id for r in ledger.data["reservations"]):
                raise SummaryError("batch_already_attempted_no_automatic_retry")
            if ledger.total + amount > ledger.limit:
                raise SummaryError("trial_budget_exhausted")
            if provider == "openai" and ledger.data.get("provider") != "openai":
                raise SummaryError("wrong_provider_ledger")
            if provider == "deepseek" and ledger.data.get("provider") != "deepseek":
                raise SummaryError("wrong_provider_ledger")
            if provider == "venice" and not (ledger.data.get("provider") == "venice"
                    or (ledger.data.get("provider") is None and ledger_path.name == "summary-budget-20261110.json")):
                raise SummaryError("wrong_provider_ledger")
            client = client_factory(provider, key_reader(key_file, provider=provider))
            report["preflight"] = preflight(client)
            ledger.reserve(amount=amount, run_id=run_id, model=PROVIDERS[provider]["model"], case_id="twelve-case-batch-v1")
            report["model_calls_reserved"] = 1
            write_new(directory / "attempt.json", {"run_id": run_id, "reserved_usd": str(amount),
                      "payload_sha256": digest(payload), "no_automatic_retry": True})
            start = time.monotonic()
            response = client.request(PROVIDERS[provider]["endpoint"], payload)
            report["elapsed_seconds"] = round(time.monotonic() - start, 3)
            final = final_only(response, provider)
            write_new(directory / "final-response.json", final)
            report["outcomes"] = inspect_final(final, bundle)
            report["usage"] = final["usage"]
            report["estimated_usd_at_verified_ceiling"] = final["estimated_usd_at_verified_ceiling"]
            report["status"] = "answered_requires_semantic_review"
            report["cumulative_reserved_usd"] = str(ledger.total)
    except SummaryError as error:
        report.update(status="stopped_no_retry", error_code=str(error))
    except Exception:
        report.update(status="stopped_no_retry", error_code="unexpected_failure_details_suppressed")
    write_new(directory / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", type=Path, required=True)
    parser.add_argument("--provider", choices=tuple(PROVIDERS))
    parser.add_argument("--allow-live-api", action="store_true")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--budget-ledger", type=Path)
    args = parser.parse_args(argv)
    if args.allow_live_api and not all((args.provider, args.authorization, args.output_dir, args.key_file, args.budget_ledger)):
        parser.error("live batch requires exact authorization, provider, new private output, key and existing ledger")
    try:
        bundle = load_frozen(args.suite_dir)
        if not args.allow_live_api:
            print(json.dumps({"status": "dry_run", "case_count": 12, "model_calls": 0,
                "one_batch_per_provider": True, "reserved_usd_by_provider": {
                    p: str(reservation(payload_for(bundle, p), p)) for p in PROVIDERS}}))
            return 0
        authorization = json.loads(protected(args.authorization).read_text(encoding="utf-8"))
        report = run(bundle, args.provider, args.output_dir, args.key_file, args.budget_ledger, authorization)
        print(json.dumps({k: report[k] for k in ("provider", "status", "model_calls_reserved", "production_accepted")}
                         | {"error_code": report.get("error_code"), "cases_returned": len(report["outcomes"])}))
        return int(report["status"] == "stopped_no_retry")
    except (OSError, ValueError, KeyError):
        print(json.dumps({"status": "error", "code": "comprehensive_batch_precondition_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
