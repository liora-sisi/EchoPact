"""Explicit, budgeted Venice API trial. Not imported by production recall or Bot."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from .caretaker_summary import SummaryError, SUMMARY_SCHEMA, build_messages, validate_summary


MODEL = "qwen3-5-35b-a3b"
BASE_URL = "https://api.venice.ai/api/v1"
MAX_OUTPUT_TOKENS = 1500
MAX_BUDGET_USD = Decimal("0.05")
MAX_INPUT_PRICE = Decimal("0.15")
MAX_OUTPUT_PRICE = Decimal("1.00")
MODEL_PRICE_CEILINGS = {
    MODEL: (MAX_INPUT_PRICE, MAX_OUTPUT_PRICE),
    "mistral-small-3-2-24b-instruct": (Decimal("0.09375"), Decimal("0.25")),
}
MAX_RESPONSE_BYTES = 2_000_000
SAFE_PROVIDER_CODES = {
    "AUTHENTICATION_FAILED", "AUTHENTICATION_FAILED_INACTIVE_KEY", "PRO_ONLY_MODEL",
    "INSUFFICIENT_BALANCE", "API_KEY_DIEM_SPEND_LIMIT_EXCEEDED", "API_KEY_USD_SPEND_LIMIT_EXCEEDED",
    "UNAUTHORIZED", "API_ACCESS_DISABLED", "INVALID_REQUEST", "INVALID_MODEL", "MODEL_NOT_FOUND",
}


def read_dpapi_key(path: str | Path) -> str:
    if os.name != "nt":
        raise SummaryError("windows_key_store_required")
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 16384:
            raise ValueError
        envelope = json.loads(raw)
        if (envelope.get("schema") != "echo-caretaker-key-v1" or envelope.get("provider") != "venice"
                or envelope.get("protection") != "Windows-DPAPI-CurrentUser"):
            raise ValueError
        encrypted = base64.b64decode(envelope["ciphertext_b64"], validate=True)
    except Exception:
        raise SummaryError("invalid_protected_key_file") from None

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    def blob(data):
        buffer = ctypes.create_string_buffer(data)
        return Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

    source, source_buffer = blob(encrypted)
    entropy, entropy_buffer = blob(b"EchoPact.Venice.SummaryTrial.v1")
    destination = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not crypt.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(entropy), None, None,
                                   1, ctypes.byref(destination)):
        raise SummaryError("key_decryption_failed")
    try:
        key = ctypes.string_at(destination.pbData, destination.cbData).decode("utf-8")
    except Exception:
        raise SummaryError("invalid_key_encoding") from None
    finally:
        ctypes.memset(destination.pbData, 0, destination.cbData)
        kernel.LocalFree(destination.pbData)
    if not 32 <= len(key) <= 1024 or any(c.isspace() for c in key) or not key.isascii():
        raise SummaryError("invalid_key_format")
    return key


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SummaryError("redirect_blocked")


@dataclass(repr=False)
class VeniceClient:
    api_key: str = field(repr=False)

    def request(self, endpoint: str, payload: dict | None = None) -> dict:
        if endpoint not in ("/models?type=text", "/api_keys/rate_limits", "/billing/balance", "/chat/completions"):
            raise SummaryError("endpoint_not_allowed")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if body is not None and self.api_key.encode() in body:
            raise SummaryError("key_in_payload_blocked")
        request = urllib.request.Request(BASE_URL + endpoint, data=body, headers={
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "EchoPact-summary-trial/1",
        })
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=60) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            code = None
            try:
                details = json.loads(error.read(8192))
                candidate = details.get("code")
                message = details.get("error")
                if isinstance(message, dict):
                    candidate = message.get("code", candidate)
                    message = message.get("message")
                if candidate in SAFE_PROVIDER_CODES:
                    code = candidate
                elif isinstance(message, str):
                    known_messages = {
                        "insufficient": "INSUFFICIENT_BALANCE",
                        "api key diem spend limit": "API_KEY_DIEM_SPEND_LIMIT_EXCEEDED",
                        "api key usd spend limit": "API_KEY_USD_SPEND_LIMIT_EXCEEDED",
                    }
                    for prefix, safe_code in known_messages.items():
                        if message.lower().startswith(prefix):
                            code = safe_code
                            break
            except Exception:
                pass
            suffix = f"_{code}" if code else ""
            raise SummaryError(f"provider_http_{error.code}{suffix}") from None
        except SummaryError:
            raise
        except Exception:
            raise SummaryError("provider_network_failure_no_retry") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SummaryError("provider_response_too_large")
        if self.api_key.encode() in raw:
            raise SummaryError("provider_echoed_secret_suppressed")
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError
            return parsed
        except (ValueError, UnicodeError):
            raise SummaryError("provider_invalid_json") from None


def _price(value) -> Decimal:
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise SummaryError("invalid_model_price") from None


def preflight(client, model: str = MODEL) -> dict:
    if model not in MODEL_PRICE_CEILINGS:
        raise SummaryError("model_not_in_trial_allowlist")
    # The catalog is public; authentication must be checked separately.
    access = client.request("/api_keys/rate_limits").get("data", {})
    if not isinstance(access, dict) or access.get("accessPermitted") is not True:
        raise SummaryError("inference_not_permitted")
    catalog = client.request("/models?type=text").get("data", [])
    matches = [m for m in catalog if isinstance(m, dict) and m.get("id") == model]
    if len(matches) != 1:
        raise SummaryError("selected_model_not_available")
    spec = matches[0].get("model_spec", {})
    if spec.get("offline") is True or spec.get("privacy") != "private":
        raise SummaryError("selected_model_not_private_or_online")
    if spec.get("capabilities", {}).get("supportsResponseSchema") is not True:
        raise SummaryError("selected_model_schema_unsupported")
    pricing = spec.get("pricing", {})
    input_price = _price(pricing.get("input", {}).get("usd"))
    output_price = _price(pricing.get("output", {}).get("usd"))
    ceiling_in, ceiling_out = MODEL_PRICE_CEILINGS[model]
    if input_price > ceiling_in or output_price > ceiling_out:
        raise SummaryError("model_price_exceeds_trial_ceiling")
    return {"model": model, "privacy": "private", "input_usd_per_million": str(input_price),
            "output_usd_per_million": str(output_price), "authentication_verified": True,
            "rate_limits_access_permitted": access.get("accessPermitted")}


@dataclass
class TrialBudget:
    spent_reservations: Decimal = Decimal("0")
    calls: int = 0
    max_calls: int = 4

    def __post_init__(self):
        if (not self.spent_reservations.is_finite() or not 0 <= self.spent_reservations <= MAX_BUDGET_USD
                or type(self.max_calls) is not int or not 1 <= self.max_calls <= 12
                or type(self.calls) is not int or not 0 <= self.calls <= self.max_calls):
            raise SummaryError("invalid_trial_budget")

    def reserve(self, messages: list[dict], prices: dict, schema: dict = SUMMARY_SCHEMA) -> Decimal:
        # UTF-8 byte count plus framing is conservative for this byte-level tokenizer.
        input_bound = (len(json.dumps(messages, ensure_ascii=False).encode())
                       + len(json.dumps(schema, ensure_ascii=False).encode()) + 2048)
        amount = (Decimal(input_bound) * _price(prices["input_usd_per_million"])
                  + MAX_OUTPUT_TOKENS * _price(prices["output_usd_per_million"])) / 1_000_000
        if self.calls >= self.max_calls or self.spent_reservations + amount > MAX_BUDGET_USD:
            raise SummaryError("trial_budget_exhausted")
        self.calls += 1
        self.spent_reservations += amount
        return amount


def run_case(client, records: list[dict], *, prices: dict, budget: TrialBudget,
             before_request, model: str = MODEL, contract_version: str = "v1") -> dict:
    if model not in MODEL_PRICE_CEILINGS or prices.get("model", model) != model:
        raise SummaryError("model_not_in_trial_allowlist")
    if contract_version == "v1":
        builder, schema, validator = build_messages, SUMMARY_SCHEMA, validate_summary
    elif contract_version == "v2":
        from . import caretaker_summary_v2
        builder, schema, validator = (caretaker_summary_v2.build_messages,
            caretaker_summary_v2.SUMMARY_SCHEMA, caretaker_summary_v2.validate_summary)
    elif contract_version == "v3":
        from . import caretaker_summary_v3
        builder, schema, validator = (caretaker_summary_v3.build_messages,
            caretaker_summary_v3.SUMMARY_SCHEMA, caretaker_summary_v3.validate_summary)
    elif contract_version == "v4":
        from . import caretaker_summary_v4
        builder, schema, validator = (caretaker_summary_v4.build_messages,
            caretaker_summary_v4.SUMMARY_SCHEMA, caretaker_summary_v4.validate_summary)
    else:
        raise SummaryError("unknown_summary_contract")
    messages = builder(records)
    reserved = budget.reserve(messages, prices, schema)
    # Durable receipt first; a timeout is never retried automatically.
    before_request({"status": "reserved", "reserved_usd": str(reserved)})
    response = client.request("/chat/completions", {
        "model": model, "messages": messages, "temperature": 0,
        "max_tokens": MAX_OUTPUT_TOKENS, "stream": False, "n": 1, "store": False,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "echo_summary", "strict": True, "schema": schema}},
        "venice_parameters": {"include_venice_system_prompt": False,
            "disable_thinking": True, "strip_thinking_response": True,
            "enable_web_search": "off", "enable_web_scraping": False, "enable_web_citations": False},
    })
    if response.get("model") != model:
        raise SummaryError("response_model_changed")
    try:
        choice = response["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise SummaryError("summary_incomplete")
        content = choice["message"]["content"]
        summary = json.loads(content)
    except SummaryError:
        raise
    except (KeyError, IndexError, TypeError, ValueError):
        raise SummaryError("summary_response_invalid") from None
    try:
        result = validator(summary, records)
    except SummaryError as error:
        # Keep a bounded, quarantined final draft for review. It is never accepted
        # as memory or silently repaired. The client has already suppressed key echoes.
        error.rejected_draft = {"accepted": False, "final_content": summary}
        usage = response.get("usage", {})
        error.trial_usage = {k: usage[k] for k in ("prompt_tokens", "completion_tokens")
                             if type(usage.get(k)) is int and usage[k] >= 0}
        raise
    result.update({"model": model, "privacy": "private", "reserved_usd": str(reserved)})
    usage = response.get("usage", {})
    inp, out = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if type(inp) is int and inp >= 0 and type(out) is int and out >= 0:
        result["usage"] = {"prompt_tokens": inp, "completion_tokens": out}
        result["estimated_usd"] = str((inp * _price(prices["input_usd_per_million"])
            + out * _price(prices["output_usd_per_million"])) / 1_000_000)
    else:
        result["usage"] = None
        result["estimated_usd"] = None
    return result
