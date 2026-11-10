"""Synthetic, no-network tests for the isolated model summary boundary."""

import copy
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error

import pytest

from backend.memory.caretaker_summary import SummaryError, build_messages, validate_summary
from backend.memory.venice_summary_trial import (
    MODEL, TrialBudget, VeniceClient, _NoRedirect, preflight, read_dpapi_key, run_case,
)

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "tests/fixtures/caretaker_summary_synthetic.json").read_text(encoding="utf-8"))
PRICES = {"input_usd_per_million": "0.15", "output_usd_per_million": "1.00"}


def valid_summary():
    return {"status": "ok", "facts": [{"text": "后来更正：10月25日修书架，26日买螺丝。",
        "kind": "correction", "event_date": "2026-10-25",
        "evidence": [{"record_id": "date-2", "quote": "修书架是2026年10月25日，26日只是买了螺丝。"}]}],
        "uncertainties": ["先前说法与后来纠正不同。"], "omitted_source_ids": ["date-1"],
        "refusal_reason": None}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.spec = {"privacy": "private", "offline": False,
            "capabilities": {"supportsResponseSchema": True},
            "pricing": {"input": {"usd": 0.15}, "output": {"usd": 1.0}}}
        self.allowed = True
        self.bundled = 0
        self.model = MODEL
        self.summary = valid_summary()
        self.fail = None

    def request(self, endpoint, payload=None):
        self.calls.append((endpoint, payload))
        if endpoint == "/api_keys/rate_limits":
            return {"data": {"accessPermitted": self.allowed, "balances": {"BUNDLED_CREDITS": self.bundled}}}
        if endpoint.startswith("/models"):
            return {"data": [{"id": self.model, "model_spec": self.spec}]}
        if self.fail:
            raise SummaryError(self.fail)
        return {"model": MODEL, "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(self.summary), "reasoning_content": "must not be retained"}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 120}}


def test_summary_is_separate_cited_copy_not_original_or_proven_truth():
    records = copy.deepcopy(CASES[0]["records"])
    before = copy.deepcopy(records)
    summary = valid_summary()
    result = validate_summary(summary, records)
    assert records == before
    assert result["requires_human_review"] is True
    assert result["generation_kind"] == "model_summary_not_original_record"
    summary["facts"][0]["text"] = "changed after validation"
    assert result["summary"]["facts"][0]["text"] != "changed after validation"


@pytest.mark.parametrize("change,code", [
    (lambda s: s["facts"][0]["evidence"][0].update(record_id="unknown"), "unverifiable_quote"),
    (lambda s: s["facts"][0]["evidence"][0].update(quote="原文没有这句话"), "unverifiable_quote"),
    (lambda s: s.update(omitted_source_ids=[]), "unaccounted_sources"),
    (lambda s: s.update(omitted_source_ids=["date-1", "date-2"]), "unaccounted_sources"),
    (lambda s: s["facts"][0].update(event_date="2026-02-30"), "invalid_event_date"),
    (lambda s: s["facts"][0].update(text="x" * 241), "invalid_fact"),
    (lambda s: s.update(facts=[]), "empty_success"),
    (lambda s: s.update(extra="untrusted instruction"), "invalid_summary_fields"),
])
def test_rejects_unverifiable_or_unbounded_output(change, code):
    summary = valid_summary()
    change(summary)
    with pytest.raises(SummaryError, match=code):
        validate_summary(summary, CASES[0]["records"])


def test_refusal_is_explicit_not_absence_of_memories():
    result = validate_summary({"status": "refused", "facts": [], "uncertainties": [],
        "refusal_reason": "无法处理此输入", "omitted_source_ids": ["date-1", "date-2"]}, CASES[0]["records"])
    assert result["summary"]["status"] == "refused"


def test_real_sources_and_input_overflow_rejected_before_request():
    for change, code in [
        (lambda r: r[0].update(source_kind="chatgpt_official"), "synthetic_only"),
        (lambda r: r[0].update(content="x" * 8001), "input_budget_exceeded"),
    ]:
        records = copy.deepcopy(CASES[0]["records"])
        change(records)
        client = FakeClient()
        with pytest.raises(SummaryError, match=code):
            run_case(client, records, prices=PRICES, budget=TrialBudget(), before_request=lambda _: None)
        assert client.calls == []


def test_gold_review_targets_not_sent_to_model():
    for case in CASES:
        messages = build_messages(case["records"])
        assert set(json.loads(messages[1]["content"])) == {"synthetic_records"}


def test_preflight_checks_authentication_before_public_catalog():
    client = FakeClient()
    client.allowed = False
    with pytest.raises(SummaryError, match="inference_not_permitted"):
        preflight(client)
    assert len(client.calls) == 1


def test_positive_bundled_credit_does_not_override_access_denial():
    client = FakeClient()
    client.allowed, client.bundled = False, 0.05
    with pytest.raises(SummaryError, match="inference_not_permitted"):
        preflight(client)
    assert all(path != "/chat/completions" for path, _ in client.calls)
    client.bundled = 0
    with pytest.raises(SummaryError, match="inference_not_permitted"):
        preflight(client)
    client.bundled, client.allowed = 0.05, None
    with pytest.raises(SummaryError, match="inference_not_permitted"):
        preflight(client)


def test_rejected_final_draft_is_quarantined_for_review_without_hidden_reasoning():
    client = FakeClient()
    client.summary["omitted_source_ids"] = []
    with pytest.raises(SummaryError) as caught:
        run_case(client, CASES[0]["records"], prices=PRICES, budget=TrialBudget(), before_request=lambda _: None)
    assert caught.value.rejected_draft["accepted"] is False
    assert caught.value.rejected_draft["final_content"] == client.summary
    assert "reasoning_content" not in json.dumps(caught.value.rejected_draft)
    assert caught.value.trial_usage["completion_tokens"] == 120


@pytest.mark.parametrize("change,code", [
    (lambda c: setattr(c, "model", "different-model"), "not_available"),
    (lambda c: c.spec.update(privacy="anonymous"), "not_private_or_online"),
    (lambda c: c.spec.update(offline=True), "not_private_or_online"),
    (lambda c: c.spec["capabilities"].update(supportsResponseSchema=False), "schema_unsupported"),
    (lambda c: c.spec["pricing"]["input"].update(usd=20), "price_exceeds"),
    (lambda c: c.spec["pricing"]["input"].update(usd="NaN"), "invalid_model_price"),
])
def test_preflight_fails_closed_without_provider_or_model_fallback(change, code):
    client = FakeClient()
    change(client)
    with pytest.raises(SummaryError, match=code):
        preflight(client)
    assert all(endpoint != "/chat/completions" for endpoint, _ in client.calls)


def test_real_request_shape_and_receipt_precede_model_call():
    client = FakeClient()
    receipts = []
    def receipt(item):
        assert client.calls == []
        receipts.append(item)
    result = run_case(client, CASES[0]["records"], prices=PRICES, budget=TrialBudget(), before_request=receipt)
    body = client.calls[0][1]
    assert body["model"] == MODEL and body["store"] is False
    assert not {"tools", "fallbacks"} & set(body)
    assert body["venice_parameters"]["enable_web_search"] == "off"
    assert body["venice_parameters"]["include_venice_system_prompt"] is False
    assert Decimal(receipts[0]["reserved_usd"]) >= Decimal(result["estimated_usd"])
    assert "reasoning_content" not in json.dumps(result)


def test_budget_rejects_fifth_call_and_reserves_timeouts_without_retry():
    client, budget = FakeClient(), TrialBudget()
    client.fail = "provider_network_failure_no_retry"
    for _ in range(4):
        with pytest.raises(SummaryError, match="no_retry"):
            run_case(client, CASES[0]["records"], prices=PRICES, budget=budget, before_request=lambda _: None)
    with pytest.raises(SummaryError, match="budget_exhausted"):
        run_case(client, CASES[0]["records"], prices=PRICES, budget=budget, before_request=lambda _: None)
    assert len(client.calls) == budget.calls == 4
    assert budget.spent_reservations > 0


def test_budget_blocks_spend_before_request():
    client = FakeClient()
    with pytest.raises(SummaryError, match="budget_exhausted"):
        run_case(client, CASES[0]["records"], prices=PRICES,
                 budget=TrialBudget(spent_reservations=Decimal("0.0499")), before_request=lambda _: None)
    assert not client.calls


def test_redirect_and_key_in_payload_are_blocked():
    with pytest.raises(SummaryError, match="redirect_blocked"):
        _NoRedirect().redirect_request(None, None, 302, None, None, "https://other.example/")
    client = VeniceClient("synthetic-secret-not-a-real-key")
    assert "synthetic-secret" not in repr(client)
    with pytest.raises(SummaryError, match="key_in_payload_blocked"):
        client.request("/chat/completions", {"content": client.api_key})


def test_http_error_does_not_echo_provider_body_or_key(monkeypatch):
    secret = "synthetic-secret-not-a-real-key"
    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError("https://example.invalid", 401, secret, {}, None)
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    with pytest.raises(SummaryError) as error:
        VeniceClient(secret).request("/api_keys/rate_limits")
    assert str(error.value) == "provider_http_401"


def test_payment_error_reports_only_classified_reason(monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            body = io.BytesIO(json.dumps({"error": "Insufficient balance for request with private details"}).encode())
            raise urllib.error.HTTPError("https://example.invalid", 402, "payment", {}, body)
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    with pytest.raises(SummaryError) as error:
        VeniceClient("synthetic-secret-not-a-real-key").request("/api_keys/rate_limits")
    assert str(error.value) == "provider_http_402_INSUFFICIENT_BALANCE"


def test_cli_defaults_to_no_key_no_network():
    done = subprocess.run([sys.executable, str(ROOT / "scripts/summary_api_trial.py")],
                          capture_output=True, text=True, encoding="utf-8", check=True)
    output = json.loads(done.stdout)
    assert output["mode"] == "dry_run" and output["model_calls"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI interop")
def test_powershell_envelope_decrypts_without_printing_key(tmp_path):
    path = tmp_path / "synthetic.dpapi"
    secret = "synthetic-dpapi-value-not-a-real-key"
    script = """
Add-Type -AssemblyName System.Security
$data = [Text.Encoding]::UTF8.GetBytes('synthetic-dpapi-value-not-a-real-key')
$entropy = [Text.Encoding]::UTF8.GetBytes('EchoPact.Venice.SummaryTrial.v1')
$cipher = [Security.Cryptography.ProtectedData]::Protect($data, $entropy, 'CurrentUser')
$sealed = @{schema='echo-caretaker-key-v1'; provider='venice';
 protection='Windows-DPAPI-CurrentUser'; ciphertext_b64=[Convert]::ToBase64String($cipher)} | ConvertTo-Json
[IO.File]::WriteAllText($env:ECHO_TEST_DPAPI_PATH, $sealed)
"""
    env = dict(os.environ, ECHO_TEST_DPAPI_PATH=str(path))
    done = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                          env=env, capture_output=True, timeout=20, check=True)
    assert secret.encode() not in done.stdout + done.stderr
    assert read_dpapi_key(path) == secret
