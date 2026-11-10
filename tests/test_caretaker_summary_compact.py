import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from backend.memory.caretaker_summary import SummaryError
from backend.memory.caretaker_summary_v3 import validate_summary

RECORDS = [{"record_id": "a", "recorded_at": "2028-01-09T10:00:00+08:00", "source_kind": "synthetic",
            "content": "2028年1月7日，我们把台灯修好了。"},
           {"record_id": "b", "recorded_at": "2028-01-10T10:00:00+08:00", "source_kind": "synthetic",
            "content": "具体花了几分钟没有记。"}]


def summary():
    return {"status": "ok", "facts": [{"text": "2028年1月7日修好台灯。", "event_status": "happened",
        "statement_role": "direct", "event_date": "2028-01-07", "planned_date": None,
        "evidence": [{"record_id": "a", "quote": RECORDS[0]["content"]}]}],
        "uncertainties": ["耗时未记录。"], "refusal_reason": None}


def test_host_computes_coverage_and_date_citation_without_rewriting_model_text():
    original = summary()
    packet = validate_summary(original, RECORDS)
    assert packet["schema_version"].endswith("v3") and packet["requires_human_review"]
    assert packet["model_summary"] == original
    assert packet["summary"]["omitted_source_ids"] == ["b"]
    assert packet["summary"]["facts"][0]["date_evidence"] == original["facts"][0]["evidence"][0]
    assert packet["source_accounting"] == packet["date_citation_selection"] == "computed_by_host"
    assert "omitted_source_ids" not in original


@pytest.mark.parametrize("change", [
    lambda s: s["facts"][0].update(event_date="2028-01-09"),
    lambda s: s["facts"][0]["evidence"][0].update(record_id="b"),
    lambda s: s["facts"][0]["evidence"][0].update(quote="原文没有这句话"),
    lambda s: s["facts"][0].update(event_status="planned"),
])
def test_host_derivation_does_not_hide_unsupported_dates_or_quotes(change):
    draft = summary()
    change(draft)
    with pytest.raises(SummaryError):
        validate_summary(draft, RECORDS)


def test_refusal_remains_refusal_with_explicit_host_omission_accounting():
    packet = validate_summary({"status": "refused", "facts": [], "uncertainties": [],
                               "refusal_reason": "无法处理。"}, RECORDS)
    assert packet["summary"]["status"] == "refused"
    assert packet["summary"]["omitted_source_ids"] == ["a", "b"]
    assert packet["model_summary"]["facts"] == []


def test_compact_probe_is_explicit_and_offline_by_default():
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, str(root / "scripts/summary_model_comparison.py"),
        "--compact-contract-probe"], check=True, capture_output=True, text=True, encoding="utf-8")
    report = json.loads(done.stdout)
    assert report["contract"] == "v3" and report["model_calls"] == 0 and report["planned_calls"] == 4
