"""Explicit synthetic transport for shortlist IDs; reuse the existing provider."""
import copy
from decimal import Decimal

from . import evidence_selector_trial as base
from . import semantic_shortlist as contract
from .caretaker_summary import SummaryError
from .object_evidence_review import validate_request

MAX_OUTPUT_TOKENS = 768


def request_payload(request, provider):
    # Strict card validation, without sending fabricated source-order windows.
    if not isinstance(request, dict) or set(request) != {"question", "candidates"}:
        raise SummaryError("invalid_shortlist_request")
    validate_request({**request, "source_order_windows": []})
    payload = base.request_payload(request, provider)
    messages = payload["input"] if provider == "openai" else payload["messages"]
    messages[0]["content"] = contract.SYSTEM_PROMPT
    spec = payload["text"]["format"] if provider == "openai" else payload["response_format"]["json_schema"]
    spec.update(name="echo_candidate_shortlist", schema=copy.deepcopy(contract.SCHEMA))
    payload["max_output_tokens" if provider == "openai" else "max_tokens"] = MAX_OUTPUT_TOKENS
    return payload


def reservation(request, provider):
    inp, out = base.prices(provider)
    size = len(contract.selection.encoded(request_payload(request, provider))) + 2048
    return (size * inp + MAX_OUTPUT_TOKENS * out) / Decimal(1_000_000)


def run_case(client, request, *, provider, frozen_request_sha256, before_request):
    payload = request_payload(request, provider)
    if contract.selection.digest(contract.selection.encoded(payload).decode()) != frozen_request_sha256:
        raise SummaryError("frozen_shortlist_request_changed")
    before_request({"reserved_usd": str(reservation(request, provider))})
    return base.parse_response(client.request("/responses" if provider == "openai" else "/chat/completions", payload), provider)
