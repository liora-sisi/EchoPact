"""Explicit synthetic object-review trials; existing provider transport only."""
from decimal import Decimal

from . import object_evidence_review as contract
from . import evidence_selector_trial as base
from .evidence_selector_trial import MISTRAL, model, prices, parse_response, openai, venice
from .caretaker_summary import SummaryError


def request_payload(request, provider):
    contract.validate_request(request)
    plain = {k: request[k] for k in ("question", "candidates")}
    payload = base.request_payload(plain, provider)
    messages = payload["input"] if provider == "openai" else payload["messages"]
    messages[0]["content"] = contract.SYSTEM_PROMPT
    messages[1]["content"] = contract.selection.encoded(request).decode("utf-8")
    return payload


def reservation(request, provider):
    inp, out = prices(provider)
    size = len(contract.selection.encoded(request_payload(request, provider))) + 2048
    return (size * inp + base.MAX_OUTPUT_TOKENS * out) / Decimal(1_000_000)


def run_case(client, request, *, provider, frozen_request_sha256, before_request):
    payload = request_payload(request, provider)
    if contract.selection.digest(contract.selection.encoded(payload).decode()) != frozen_request_sha256:
        raise SummaryError("frozen_ordered_request_changed")
    before_request({"reserved_usd": str(reservation(request, provider))})
    return parse_response(client.request("/responses" if provider == "openai" else "/chat/completions", payload), provider)
