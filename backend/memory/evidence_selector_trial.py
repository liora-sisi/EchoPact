"""Explicit synthetic selector trials using existing bounded provider transports."""
from __future__ import annotations

import copy
from decimal import Decimal
import json

from . import evidence_selector as contract
from . import openai_summary_trial as openai
from . import venice_summary_trial as venice
from .caretaker_summary import SummaryError

MISTRAL = "mistral-small-3-2-24b-instruct"
MAX_OUTPUT_TOKENS = 1800


def model(provider):
    if provider not in ("venice", "openai"):
        raise SummaryError("unsupported_selector_provider")
    return MISTRAL if provider == "venice" else openai.MODEL


def request_payload(request, provider):
    if (not isinstance(request, dict) or set(request) != {"question", "candidates"}
            or len(contract.encoded(request)) > contract.MAX_INPUT_BYTES
            or not isinstance(request["candidates"], list)
            or not 1 <= len(request["candidates"]) <= contract.MAX_CANDIDATES):
        raise SummaryError("invalid_selection_request")
    messages = [{"role": "system", "content": contract.SYSTEM_PROMPT},
                {"role": "user", "content": contract.encoded(request).decode("utf-8")}]
    common = {"model": model(provider), "store": False, "stream": False}
    if provider == "openai":
        return {**common, "input": messages, "service_tier": "default", "reasoning": {"effort": "low"},
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "text": {"format": {"type": "json_schema", "name": "echo_evidence_selection", "strict": True,
                                "schema": copy.deepcopy(contract.SCHEMA)}}}
    return {**common, "messages": messages, "temperature": 0, "n": 1, "max_tokens": MAX_OUTPUT_TOKENS,
        "response_format": {"type": "json_schema", "json_schema": {"name": "echo_evidence_selection",
                            "strict": True, "schema": copy.deepcopy(contract.SCHEMA)}},
        "venice_parameters": {"include_venice_system_prompt": False, "disable_thinking": True,
            "strip_thinking_response": True, "enable_web_search": "off", "enable_web_scraping": False,
            "enable_web_citations": False}}


def prices(provider):
    model(provider)
    return ((openai.INPUT_PRICE, openai.OUTPUT_PRICE) if provider == "openai"
            else venice.MODEL_PRICE_CEILINGS[MISTRAL])


def reservation(request, provider):
    inp, out = prices(provider)
    size = len(contract.encoded(request_payload(request, provider))) + 2048
    return (size * inp + MAX_OUTPUT_TOKENS * out) / Decimal(1_000_000)


def parse_response(response, provider):
    if not isinstance(response, dict) or response.get("model") != model(provider):
        raise SummaryError("selector_response_model_changed")
    if provider == "openai":
        if response.get("status") != "completed" or response.get("error"):
            raise SummaryError("selector_response_incomplete")
        items = response.get("output")
        if not isinstance(items, list) or not 1 <= len(items) <= 8:
            raise SummaryError("invalid_selector_response")
        texts = []
        for item in items:
            if not isinstance(item, dict):
                raise SummaryError("invalid_selector_response")
            if item.get("type") == "reasoning":
                continue
            if (item.get("type") != "message" or item.get("role") != "assistant"
                    or item.get("status") != "completed" or not isinstance(item.get("content"), list)):
                raise SummaryError("unexpected_selector_response_item")
            for part in item["content"]:
                if not isinstance(part, dict) or part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                    raise SummaryError("selector_refused_or_invalid")
                texts.append(part["text"])
        if len(texts) != 1:
            raise SummaryError("invalid_selector_response")
        text = texts[0]
        names = ("input_tokens", "output_tokens")
    else:
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise SummaryError("invalid_selector_response")
        choice = choices[0]
        message = choice.get("message")
        if choice.get("finish_reason") != "stop" or not isinstance(message, dict):
            raise SummaryError("selector_response_incomplete")
        if message.get("role") != "assistant" or message.get("tool_calls") or message.get("refusal"):
            raise SummaryError("unexpected_selector_response_item")
        text = message.get("content")
        names = ("prompt_tokens", "completion_tokens")
    try:
        if not isinstance(text, str) or len(text) > 12_000:
            raise ValueError
        draft = json.loads(text)
    except (ValueError, TypeError):
        raise SummaryError("invalid_selector_final_json") from None
    raw_usage = response.get("usage") or {}
    if not isinstance(raw_usage, dict):
        raise SummaryError("invalid_selector_usage")
    usage = {key: raw_usage[key] for key in names if type(raw_usage.get(key)) is int and raw_usage[key] >= 0}
    inp, out = prices(provider)
    estimated = str((usage[names[0]] * inp + usage[names[1]] * out) / Decimal(1_000_000)) if len(usage) == 2 else None
    return {"draft": draft, "usage": usage, "estimated_usd": estimated,
            "model": model(provider), "provider": provider, "store": False}


def run_case(client, request, *, provider, frozen_request_sha256, before_request):
    payload = request_payload(request, provider)
    if contract.digest(contract.encoded(payload).decode()) != frozen_request_sha256:
        raise SummaryError("frozen_selector_request_changed")
    before_request({"reserved_usd": str(reservation(request, provider))})
    response = client.request("/responses" if provider == "openai" else "/chat/completions", payload)
    return parse_response(response, provider)
