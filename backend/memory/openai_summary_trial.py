"""Opt-in synthetic comparison, same v4 evidence contract as the Venice trial."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import json
import urllib.error
import urllib.request

from .caretaker_summary import SummaryError
from . import caretaker_summary_v4 as contract
from .venice_summary_trial import _NoRedirect, MAX_RESPONSE_BYTES

MODEL = "gpt-5.6-sol"
BASE_URL = "https://api.openai.com/v1"
MAX_OUTPUT_TOKENS = 2500  # Includes reasoning; Venice uses 1500 without reasoning.
MAX_BUDGET_USD = Decimal("0.50")
INPUT_PRICE = Decimal("4")
OUTPUT_PRICE = Decimal("20")
PRICE_VERIFIED_ON = "2026-11-10"
PRICE_RECHECK_AFTER = date(2026, 11, 30)


def request_payload(records):
    return {"model": MODEL, "input": contract.build_messages(records), "store": False,
            "stream": False, "service_tier": "default", "reasoning": {"effort": "low"},
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "text": {"format": {"type": "json_schema", "name": "echo_summary",
                                "strict": True, "schema": copy.deepcopy(contract.SUMMARY_SCHEMA)}}}


def reservation(records):
    # Overestimate input using UTF-8 bytes plus framing; never assume cache savings.
    size = len(json.dumps(request_payload(records), ensure_ascii=False).encode("utf-8")) + 2048
    return (size * INPUT_PRICE + MAX_OUTPUT_TOKENS * OUTPUT_PRICE) / 1_000_000


@dataclass(repr=False)
class OpenAIClient:
    api_key: str = field(repr=False)

    def request(self, endpoint, payload=None):
        if endpoint not in (f"/models/{MODEL}", "/responses"):
            raise SummaryError("endpoint_not_allowed")
        if (not isinstance(self.api_key, str) or not self.api_key.startswith("sk-")
                or not 32 <= len(self.api_key) <= 1024 or any(c.isspace() for c in self.api_key)
                or not self.api_key.isascii()):
            raise SummaryError("invalid_key_format")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if body is not None and self.api_key.encode() in body:
            raise SummaryError("key_in_payload_blocked")
        request = urllib.request.Request(BASE_URL + endpoint, data=body, headers={
            "Authorization": "Bearer " + self.api_key, "Content-Type": "application/json",
            "Accept": "application/json", "User-Agent": "EchoPact-synthetic-comparison/1"})
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=60) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            # Do not log error bodies, echoed keys, headers or account metadata.
            raise SummaryError(f"openai_http_{error.code}") from None
        except SummaryError:
            raise
        except Exception:
            raise SummaryError("openai_network_failure_no_retry") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SummaryError("provider_response_too_large")
        if self.api_key.encode() in raw:
            raise SummaryError("provider_echoed_secret_suppressed")
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, UnicodeError):
            raise SummaryError("provider_invalid_json") from None


def preflight(client, *, today=None):
    if (today or date.today()) > PRICE_RECHECK_AFTER:
        raise SummaryError("openai_pricing_requires_reverification")
    if client.request(f"/models/{MODEL}").get("id") != MODEL:
        raise SummaryError("selected_model_not_available")
    return {"model": MODEL, "input_usd_per_million": str(INPUT_PRICE),
            "output_usd_per_million": str(OUTPUT_PRICE), "verified_on": PRICE_VERIFIED_ON,
            "account_credit_balance_verified": False}


def parse_response(response, records):
    if response.get("model") != MODEL:
        raise SummaryError("response_model_changed")
    if response.get("status") != "completed" or response.get("error"):
        raise SummaryError("summary_incomplete")
    output = response.get("output")
    if not isinstance(output, list) or not 1 <= len(output) <= 8:
        raise SummaryError("summary_response_invalid")
    texts = []
    for item in output:
        if not isinstance(item, dict):
            raise SummaryError("summary_response_invalid")
        if item.get("type") == "reasoning":
            continue  # Do not retain reasoning items, encrypted contents or summaries.
        if (item.get("type") != "message" or item.get("role") != "assistant"
                or item.get("status") != "completed" or not isinstance(item.get("content"), list)):
            raise SummaryError("unexpected_response_item")
        for part in item["content"]:
            if not isinstance(part, dict):
                raise SummaryError("summary_response_invalid")
            if part.get("type") == "refusal":
                raise SummaryError("summary_provider_refused")
            if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                raise SummaryError("summary_response_invalid")
            texts.append(part["text"])
    if len(texts) != 1:
        raise SummaryError("summary_response_invalid")
    try:
        summary = json.loads(texts[0])
    except (ValueError, TypeError):
        raise SummaryError("summary_response_invalid") from None
    usage = response.get("usage", {})
    if not isinstance(usage, dict):
        raise SummaryError("summary_response_invalid")
    usage = {k: usage[k] for k in ("input_tokens", "output_tokens")
             if type(usage.get(k)) is int and usage[k] >= 0}
    try:
        result = contract.validate_summary(summary, records)
    except SummaryError as error:
        error.rejected_draft = {"accepted": False, "final_content": summary}
        error.trial_usage = usage
        raise
    result.update(model=MODEL, provider="openai", usage=usage, store=False)
    if len(usage) == 2:
        result["estimated_usd"] = str((usage["input_tokens"] * INPUT_PRICE
                                      + usage["output_tokens"] * OUTPUT_PRICE) / 1_000_000)
    return result


def run_case(client, records, *, before_request):
    payload = request_payload(records)  # Synthetic guard runs before the receipt or API.
    amount = reservation(records)
    before_request({"reserved_usd": str(amount)})  # Must persist before sending.
    return parse_response(client.request("/responses", payload), records)
