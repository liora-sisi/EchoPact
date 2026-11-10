"""Question-only OpenAI adapter; independent of summary writing and source data."""
from __future__ import annotations

import copy
from decimal import Decimal
import json

from . import openai_summary_trial as transport
from . import query_caretaker as contract
from .caretaker_summary import SummaryError

MAX_OUTPUT_TOKENS = 700  # Small plan, including reasoning; preserves the shared trial budget.


def request_payload(query):
    contract.validate_question(query)
    return {"model": transport.MODEL, "store": False, "stream": False, "service_tier": "default",
            "reasoning": {"effort": "low"}, "max_output_tokens": MAX_OUTPUT_TOKENS,
            "input": [{"role": "system", "content": contract.SYSTEM_PROMPT},
                      {"role": "user", "content": json.dumps({"question": query}, ensure_ascii=False)}],
            "text": {"format": {"type": "json_schema", "name": "echo_query_plan", "strict": True,
                                "schema": copy.deepcopy(contract.SCHEMA)}}}


def reservation(query):
    size = len(json.dumps(request_payload(query), ensure_ascii=False).encode("utf-8")) + 2048
    return (size * transport.INPUT_PRICE + MAX_OUTPUT_TOKENS * transport.OUTPUT_PRICE) / Decimal(1_000_000)


def parse_response(response, query):
    if response.get("model") != transport.MODEL or response.get("status") != "completed" or response.get("error"):
        raise SummaryError("query_response_incomplete_or_changed_model")
    output = response.get("output")
    if not isinstance(output, list) or not 1 <= len(output) <= 8:
        raise SummaryError("invalid_query_response")
    texts = []
    for item in output:
        if not isinstance(item, dict):
            raise SummaryError("invalid_query_response")
        if item.get("type") == "reasoning":
            continue
        if (item.get("type") != "message" or item.get("role") != "assistant"
                or item.get("status") != "completed" or not isinstance(item.get("content"), list)):
            raise SummaryError("unexpected_query_response_item")
        for part in item["content"]:
            if not isinstance(part, dict) or part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                raise SummaryError("query_refused_or_invalid")
            texts.append(part["text"])
    if len(texts) != 1:
        raise SummaryError("invalid_query_response")
    try:
        if len(texts[0]) > 4000:
            raise ValueError
        draft = json.loads(texts[0])
    except (ValueError, TypeError):
        raise SummaryError("invalid_query_final_json") from None
    try:
        plan = contract.validate_plan(draft, query)
    except (ValueError, TypeError):
        error = SummaryError("query_plan_failed_local_validation")
        error.rejected_plan = draft
        raise error from None
    usage = response.get("usage") or {}
    if not isinstance(usage, dict):
        raise SummaryError("invalid_query_usage")
    usage = {k: usage[k] for k in ("input_tokens", "output_tokens") if type(usage.get(k)) is int and usage[k] >= 0}
    result = {"plan": plan, "usage": usage, "model": transport.MODEL, "provider": "openai", "store": False}
    if len(usage) == 2:
        result["estimated_usd"] = str((usage["input_tokens"] * transport.INPUT_PRICE
                                       + usage["output_tokens"] * transport.OUTPUT_PRICE) / Decimal(1_000_000))
    return result


def run_query(client, query, *, before_request):
    payload = request_payload(query)
    before_request({"reserved_usd": str(reservation(query))})
    return parse_response(client.request("/responses", payload), query)
