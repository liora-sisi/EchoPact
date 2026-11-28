"""Direct-reading traces retain failed/repeated delivery and exact evidence access."""
import copy
import json

import pytest

from backend.memory import summary_event_booklet as books
from backend.memory import summary_reading_card as reading
from backend.memory.caretaker_summary import SummaryError
from scripts import summary_event_booklet_trial as fixtures
from scripts import summary_event_reading_trial as trial


def case():
    sample = fixtures.relation_fixture()
    audit = books.make_audit(**sample)
    return {"case_id": "fixture", "records": sample["records"], "audit": audit,
            "booklet": books.build_booklet(audit)}


def take(c, rows, operation, **kwargs):
    row = trial.step(c, rows, {"operation": operation, **kwargs})
    rows.append(row)
    return row


def test_real_reading_counts_catalogue_repeats_source_and_receipts_without_auto_grading():
    c, rows = case(), []
    original = copy.deepcopy(c)
    cat = take(c, rows, "catalogue")
    eid = cat["response"]["events"][0]["event_id"]
    chosen = take(c, rows, "events", event_ids=[eid])
    repeated = take(c, rows, "events", event_ids=[eid])
    assert chosen["response"] == repeated["response"]
    assert chosen["counterfactual_card_response_bytes"] > chosen["response_bytes"]
    expanded = take(c, rows, "expand", item_id=eid + ".f1")
    answer = take(c, rows, "answer", text="仅就已读窗口回答；未证明完整历史。", evidence_ids=[eid + ".f1"])
    assert answer["cumulative_response_bytes"] == sum(trial.size(row["response"]) for row in rows)
    assert "not_semantically_approved" in answer["response"]["status"]
    for span in expanded["response"]["evidence"].values():
        r = next(r for r in c["records"] if r["record_id"] == span["record_id"])
        assert span["quote"] == r["content"][span["start"]:span["end"]]
    assert c == original


@pytest.mark.parametrize("action", [
    {"operation": "events", "event_ids": ["e1"]},
    {"operation": "expand", "item_id": "e1.f1"},
    {"operation": "answer", "text": "我已经知道答案", "evidence_ids": ["e1.f1"]},
])
def test_cannot_pick_unseen_events_expand_unread_items_or_cite_unopened_originals(action):
    with pytest.raises(SummaryError):
        trial.step(case(), [], action)


def test_budget_withheld_originals_do_not_become_citable_and_are_still_counted():
    c = case()
    full = trial.source_packet(c)
    row = trial.step(c, [], {"operation": "originals"}, total_limit=1024)
    assert trial.size(full) > 1024
    assert row["response"]["status"] == "session_budget_exceeded"
    assert row["response_bytes"] == trial.size(row["response"])
    assert "records" not in row["response"]
    with pytest.raises(SummaryError, match="needs_read_original_evidence"):
        trial.step(c, [row], {"operation": "answer", "text": "没找到", "evidence_ids": ["record:" + c["records"][0]["record_id"]]})


def test_original_fallback_is_explicit_bounded_window_and_stale_input_is_rejected():
    c, rows = case(), []
    row = take(c, rows, "originals")
    assert row["response"]["records"] == c["records"]
    assert "绝对第一次" in row["response"]["notice"]
    take(c, rows, "answer", text="这份窗口不能确认第一次。", evidence_ids=["record:" + c["records"][0]["record_id"]])
    c["records"][0]["content"] += " changed"
    with pytest.raises(SummaryError):
        trial.step(c, rows, {"operation": "originals"})


def test_total_budget_includes_previous_catalogue_and_repeat_costs():
    c, rows = case(), []
    cat = take(c, rows, "catalogue")
    budget = max(1024, cat["response_bytes"] * 2 - 1)
    second = trial.step(c, rows, {"operation": "catalogue"}, total_limit=budget)
    assert second["response"]["status"] == "session_budget_exceeded"
    assert second["cumulative_response_bytes"] == cat["response_bytes"] + second["response_bytes"]
    assert second["cumulative_response_bytes"] <= budget
    assert "events" not in second["response"]


def test_saved_walk_replays_first_answers_rejects_trace_edits_and_never_calls_api(tmp_path, monkeypatch):
    from scripts import prepare_summary_comprehensive_comparison as sources
    monkeypatch.setattr(sources, "PRIVATE_ROOT", tmp_path)
    c = case()
    monkeypatch.setattr(trial.delivery, "load_cases", lambda **kwargs: [copy.deepcopy(c)])
    def no_api(*args, **kwargs):
        pytest.fail("direct reading must not access credentials or call a model")
    monkeypatch.setattr(trial.delivery.review_trial.transport, "Client", no_api)
    monkeypatch.setattr(trial.delivery.review_trial.transport, "read_dpapi_key", no_api)
    directory = tmp_path / "walk"
    inputs = {"suite_directory": tmp_path / "suite", "first_directory": tmp_path / "first", "prior_directory": tmp_path / "prior"}
    questions = [{"query_id": "q1", "case_id": "fixture", "question": "后来发生什么了？"}]
    trial.create(directory, inputs, questions=questions)
    manifest_bytes = (directory / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        trial.create(directory, inputs, questions=questions)
    assert (directory / "manifest.json").read_bytes() == manifest_bytes
    trial.act(directory, "q1", {"operation": "originals"})
    evidence = ["record:" + c["records"][0]["record_id"]]
    trial.act(directory, "q1", {"operation": "answer", "text": "第一份答案。", "evidence_ids": evidence})
    trial.act(directory, "q1", {"operation": "answer", "text": "后续修订也必须另存。", "evidence_ids": evidence})
    report = trial.report(directory)
    q = report["questions"][0]
    assert [a["text"] for a in q["answers"]] == ["第一份答案。", "后续修订也必须另存。"]
    assert q["steps"] == 3 and q["whole_window_reads"] == 1
    assert report["semantic_passed"] is None and report["production_accepted"] is False
    row_file = directory / "step-001.json"
    row = json.loads(row_file.read_text(encoding="utf-8"))
    row["response_bytes"] = 1
    row_file.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(SummaryError, match="changed_trace"):
        trial.report(directory)
