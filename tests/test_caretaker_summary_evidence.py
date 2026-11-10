"""Citation integrity, explicit history and bounded Mistral-only trial checks."""
import copy
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

import pytest

from backend.memory.caretaker_summary import SummaryError
from backend.memory.caretaker_summary_v4 import build_messages, evidence_catalog, validate_summary
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory.venice_summary_trial import MODEL_PRICE_CEILINGS
from scripts.summary_model_comparison import MISTRAL, load_cases, restore_attempted, run_comparison

RECORDS = [
    {"record_id": "old", "recorded_at": "2030-01-05T08:00:00+08:00", "source_kind": "synthetic",
     "content": "我记得2030年1月2日修好了陶碗。"},
    {"record_id": "new", "recorded_at": "2030-01-06T08:00:00+08:00", "source_kind": "synthetic",
     "content": "更正：修好陶碗实际是2030年1月3日。"},
    {"record_id": "other", "recorded_at": "2030-01-07T08:00:00+08:00", "source_kind": "synthetic",
     "content": "工具借给邻居了。"},
]


def draft():
    return {"status": "ok", "facts": [{"text": "据更正，2030年1月3日修好了陶碗。",
        "event_status": "happened", "statement_role": "correction",
        "event_date": "2030-01-03", "planned_date": None, "evidence_ids": ["s2"],
        "supersedes": {"text": "原说2030年1月2日修好了陶碗。", "evidence_ids": ["s1"]}}],
        "uncertainties": [], "refusal_reason": None}


def test_correction_history_is_separate_cited_and_counts_as_retained_source():
    source, output = copy.deepcopy(RECORDS), draft()
    original = copy.deepcopy(output)
    packet = validate_summary(output, source)
    fact = packet["summary"]["facts"][0]
    assert packet["model_summary"] == original == output
    assert source == RECORDS
    assert fact["supersedes"]["evidence"] == [{"record_id": "old", "quote": RECORDS[0]["content"]}]
    assert fact["evidence"] == [{"record_id": "new", "quote": RECORDS[1]["content"]}]
    assert fact["date_evidence"] == fact["evidence"][0]
    assert packet["summary"]["omitted_source_ids"] == ["other"]
    assert packet["evidence_resolution"] == "exact_source_slices_by_host"
    assert packet["requires_human_review"] and packet["schema_version"].endswith("v4")


@pytest.mark.parametrize("change,code", [
    (lambda f: f.update(supersedes=None), "correction_history_required"),
    (lambda f: f["supersedes"].update(text=""), "correction_history_required"),
    (lambda f: f["supersedes"].update(evidence_ids=["missing"]), "invalid_evidence_ids"),
    (lambda f: f.update(evidence_ids=["missing"]), "invalid_evidence_ids"),
    (lambda f: f.update(evidence_ids=["s2", "s2"]), "invalid_evidence_ids"),
    (lambda f: f.update(evidence_ids=[]), "invalid_evidence_ids"),
    (lambda f: f.update(evidence_ids=[{}]), "invalid_evidence_ids"),
    (lambda f: f.update(statement_role="direct"), "unexpected_correction_history"),
    (lambda f: f.update(event_date="2030-01-06"), "date_evidence_not_cited"),
    (lambda f: f.update(event_date="2030-01-02"), "date_evidence_not_cited"),
    (lambda f: f.update(event_status="planned"), "event_date_requires_happened"),
])
def test_missing_history_unknown_references_and_borrowed_history_date_fail(change, code):
    output = draft()
    change(output["facts"][0])
    with pytest.raises(SummaryError, match=code):
        validate_summary(output, RECORDS)


def test_cross_record_cancellation_needs_plan_date_in_current_claim_refs():
    records = copy.deepcopy(RECORDS[:2])
    records[0]["content"] = "原定2030年1月4日去划船。"
    records[1]["content"] = "划船取消了，决定日期没写。"
    output = draft()
    output["facts"][0].update(text="原定1月4日的划船计划取消。", event_status="cancelled",
        statement_role="direct", event_date=None, planned_date="2030-01-04",
        supersedes=None, evidence_ids=["s2"])
    with pytest.raises(SummaryError, match="date_evidence_not_cited"):
        validate_summary(output, records)
    output["facts"][0]["evidence_ids"] = ["s1", "s2"]
    fact = validate_summary(output, records)["summary"]["facts"][0]
    assert fact["event_date"] is None
    assert fact["date_evidence"]["record_id"] == "old"
    assert [c["record_id"] for c in fact["evidence"]] == ["old", "new"]


def test_catalog_preserves_unicode_offsets_and_bounds_even_without_punctuation():
    records = copy.deepcopy(RECORDS[:1])
    records[0]["content"] = "  第一行。\n" + "长句🙂" * 180 + "！后半句?\n"
    catalog = evidence_catalog(records)
    for span in catalog.values():
        assert span["quote"] == records[0]["content"][span["start"]:span["end"]]
        assert 1 <= len(span["quote"]) <= 240
    nonspace = lambda s: "".join(s.split())
    assert nonspace("".join(c["quote"] for c in catalog.values())) == nonspace(records[0]["content"])
    assert evidence_catalog(records) == catalog


def test_real_sources_rejected_and_review_targets_never_sent():
    records = copy.deepcopy(RECORDS)
    records[0]["source_kind"] = "chatgpt_official"
    with pytest.raises(SummaryError, match="synthetic_only"):
        build_messages(records)
    cases = load_cases(repair=True)
    for case in cases:
        message = json.loads(build_messages(case["records"])[1]["content"])
        assert set(message) == {"synthetic_records"}
        assert "review_targets" not in json.dumps(message)
        assert all("spans" in r and "content" not in r for r in message["synthetic_records"])


def test_wrong_semantic_claim_is_not_silently_rewritten_or_called_proven():
    output = draft()
    output["facts"][0]["text"] = "据更正，同日修好了别人的木门。"
    packet = validate_summary(output, RECORDS)
    assert packet["model_summary"] == output
    assert packet["summary"]["facts"][0]["text"] == output["facts"][0]["text"]
    assert packet["semantic_validation"] == "requires_human_review"


def test_refusal_retained_and_sources_accounted():
    packet = validate_summary({"status": "refused", "facts": [], "uncertainties": [],
        "refusal_reason": "无法处理。"}, RECORDS)
    assert packet["summary"]["omitted_source_ids"] == ["old", "new", "other"]
    assert packet["model_summary"]["status"] == "refused"


def test_insufficient_cannot_coexist_with_extracted_facts():
    output = draft()
    output["status"] = "insufficient"
    with pytest.raises(SummaryError, match="insufficient_with_facts"):
        validate_summary(output, RECORDS)


def test_reference_overlap_flags_review_without_deleting_or_rewriting_claims():
    output = draft()
    output["facts"].append({**copy.deepcopy(output["facts"][0]), "statement_role": "direct",
        "text": "原先日期仍当成有效结论。", "event_date": "2030-01-02",
        "evidence_ids": ["s1"], "supersedes": None})
    output["facts"].append({**copy.deepcopy(output["facts"][0]), "statement_role": "direct",
        "text": "同一段里另一件可能独立的事。", "supersedes": None})
    packet = validate_summary(output, RECORDS)
    assert packet["model_summary"] == output and len(packet["summary"]["facts"]) == 3
    assert {f["code"] for f in packet["review_findings"]} == {
        "possible_superseded_claim_still_current", "shared_evidence_check_for_duplicate_claims"}


class RepairClient:
    def __init__(self, path):
        self.path, self.calls = path, []

    def request(self, endpoint, payload=None):
        if endpoint == "/api_keys/rate_limits":
            return {"data": {"accessPermitted": True}}
        if endpoint.startswith("/models"):
            inp, out = MODEL_PRICE_CEILINGS[MISTRAL]
            return {"data": [{"id": MISTRAL, "model_spec": {"privacy": "private",
                "capabilities": {"supportsResponseSchema": True},
                "pricing": {"input": {"usd": str(inp)}, "output": {"usd": str(out)}}}}]}
        self.calls.append(payload)
        ledger = json.loads(self.path.read_text(encoding="utf-8"))
        assert len(ledger["reservations"]) == len(self.calls)
        assert payload["model"] == MISTRAL
        record = json.loads(payload["messages"][1]["content"])["synthetic_records"][0]
        answer = {"status": "ok", "facts": [{"text": "测试用合成摘录。",
            "event_status": "unknown", "statement_role": "direct", "event_date": None,
            "planned_date": None, "evidence_ids": [record["spans"][0]["id"]], "supersedes": None}],
            "uncertainties": [], "refusal_reason": None}
        return {"model": MISTRAL, "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(answer)}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50}}


def test_repair_runs_only_four_mistral_cells_with_existing_cumulative_budget(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({"schema": "echo-summary-budget-v1", "limit_usd": "0.05",
        "prior_reserved_usd": "0.03913783125", "reservations": []}), encoding="utf-8")
    client = RepairClient(path)
    report = {"run_id": "repair", "contract": "v4", "models": [MISTRAL], "cases": []}
    cases = load_cases(repair=True)
    with BudgetLedger(path) as ledger:
        run_comparison(client, cases, report, tmp_path, ledger)
        assert Decimal("0.03913783125") < ledger.total < Decimal("0.05")
    assert len(client.calls) == len(report["cases"]) == 4
    assert report["status"] == "awaiting_human_review"
    assert all(r["result"]["schema_version"].endswith("v4") for r in report["cases"])
    with BudgetLedger(path) as ledger:
        previous = {**report, "status": "stopped_no_retry"}
        changed = {**report, "models": list(MODEL_PRICE_CEILINGS)}
        with pytest.raises(SummaryError, match="mistral_only"):
            restore_attempted(previous, changed, cases, ledger)


def test_repair_cli_dry_run_does_not_open_credentials_or_network():
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, str(root / "scripts/summary_model_comparison.py"),
        "--evidence-repair-probe", "--key-file", "does-not-exist"], check=True,
        capture_output=True, text=True, encoding="utf-8")
    report = json.loads(done.stdout)
    assert report["models"] == [MISTRAL] and report["contract"] == "v4"
    assert report["model_calls"] == 0 and report["planned_calls"] == 4
