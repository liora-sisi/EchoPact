"""Atomic events, dependency-aware delivery, exact sources and explicit overflow."""
import copy
import json

import pytest

from backend.memory import summary_event_booklet as books
from backend.memory import summary_reading_card as reading
from backend.memory.caretaker_summary import SummaryError
from scripts import summary_event_booklet_trial as trial


@pytest.fixture
def sample():
    return trial.relation_fixture()


def built(sample, budget=800):
    audit = books.make_audit(**sample)
    return audit, books.build_booklet(audit, card_bytes=budget)


def test_more_than_twenty_four_facts_and_late_life_content_survive_layout():
    sample = trial.capacity_fixture()
    audit, view = built(sample, 1800)
    assert sum(len(e["facts"]) for e in audit["draft"]["events"]) == 32
    assert len(view["cards"]) > 1 and view["layout_complete"]
    ids = [e["event_id"] for c in view["cards"] for e in c["events"]]
    assert ids == [e["event_id"] for e in sample["draft"]["events"]]
    bodies = "\n".join(e["body"] for c in view["cards"] for e in c["events"])
    assert all(t in bodies for t in ("小河灯", "互相表达爱意", "仍疲惫", "尚未检查备份盘", "具体检查日期未定"))
    assert all(len(reading.compact(c)) <= 1800 for c in view["cards"])
    index = books.catalogue(audit, view, current_records=sample["records"], identity=sample["identity"], max_bytes=64000)
    assert "cards" not in index and "body" not in str(index)
    assert index["semantic_passed"] is None and not index["production_accepted"]


def test_author_input_retains_long_record_tail_and_has_no_injected_answers():
    sample = trial.capacity_fixture()
    sample["records"][0]["content"] = "前段文字🎐e\u0301\r\n" * 60 + "末尾更正不能被截断。"
    before = copy.deepcopy(sample["records"])
    prepared = books.prepare_author_window(sample["records"])
    payload = json.loads(prepared["messages"][1]["content"])
    assert len(payload["records"]) == len(before)
    assert "events" not in payload and "review_targets" not in payload
    for raw, sent in zip(before, payload["records"]):
        assert "".join(span["text"] for span in sent["spans"]) == raw["content"]
    for span in prepared["catalog"].values():
        raw = next(r for r in before if r["record_id"] == span["record_id"])
        assert raw["content"][span["start"]:span["end"]] == span["quote"]
    assert prepared["model_calls"] == 0 and sample["records"] == before
    prepared["response_schema"]["properties"]["events"]["maxItems"] = 1
    assert books.DRAFT_SCHEMA["properties"]["events"]["maxItems"] == 32


def test_consumer_edits_cannot_mutate_source_audit_or_booklet(sample):
    sample["draft"]["status"] = "partial"
    sample["draft"]["gaps"] = [{"record_ids": ["row-5"], "reason": "这段尚未完成整理。"}]
    before_sample = copy.deepcopy(sample)
    audit, view = built(sample)
    before_audit = copy.deepcopy(audit)
    index = books.catalogue(audit, view, current_records=sample["records"], identity=sample["identity"])
    index["source"]["room_id"] = "consumer-edit"
    index["declared_gaps"][0]["reason"] = "changed"
    view["cards"][0]["source"]["room_id"] = "consumer-edit"
    assert audit == before_audit and sample == before_sample
    blocked = books.build_booklet(audit, card_bytes=512)
    before_blocked = copy.deepcopy(blocked)
    index = books.catalogue(audit, blocked, current_records=sample["records"], identity=sample["identity"])
    assert index["blocked_events"]
    index["blocked_events"][0]["reason"] = "changed"
    assert blocked == before_blocked and audit == before_audit


@pytest.mark.parametrize("kind", ["updates", "corrects", "conflicts"])
def test_old_card_requires_later_correction_update_or_unresolved_conflict(sample, kind):
    sample["draft"]["relations"][0]["kind"] = kind
    audit, view = built(sample)
    old, new = view["event_to_card"]["e1"], view["event_to_card"]["e3"]
    assert old != new
    index = books.catalogue(audit, view, current_records=sample["records"], identity=sample["identity"])
    assert index["relations"][0]["read_together"] is True
    selected = books.read_cards(audit, view, card_ids=[old], current_records=sample["records"], identity=sample["identity"])
    assert old in selected["included_card_ids"] and new in selected["included_card_ids"]
    assert selected["status"] == "unapproved_preview"
    reverse = books.read_cards(audit, view, card_ids=[new], current_records=sample["records"], identity=sample["identity"])
    assert old in reverse["included_card_ids"]
    needed = len(reading.compact(selected))
    withheld = books.read_cards(audit, view, card_ids=[old], current_records=sample["records"], identity=sample["identity"], max_bytes=needed - 1)
    assert withheld["status"] == "budget_exceeded" and "cards" not in withheld
    assert withheld["required_bytes"] == needed


def test_ordinary_related_event_does_not_force_unbounded_followup(sample):
    sample["draft"]["relations"][0]["kind"] = "related"
    audit, view = built(sample)
    old = view["event_to_card"]["e1"]
    packet = books.read_cards(audit, view, card_ids=[old], current_records=sample["records"], identity=sample["identity"])
    assert packet["included_card_ids"] == [old]


def test_required_context_follows_the_whole_update_chain(sample):
    sample["draft"]["relations"].append({"from_event": "e2", "to_event": "e3", "kind": "corrects", "evidence_ids": ["s4"]})
    audit, view = built(sample, 900)
    packet = books.read_cards(audit, view, card_ids=[view["event_to_card"]["e1"]],
                              current_records=sample["records"], identity=sample["identity"])
    assert {e["event_id"] for c in packet["cards"] for e in c["events"]} == {"e1", "e2", "e3"}


def test_large_event_is_wholly_withheld_and_blocks_its_required_peer(sample):
    sample["draft"]["events"][2]["facts"] *= 20
    audit, view = built(sample)
    assert not view["layout_complete"]
    assert view["blocked_events"][0]["event_id"] == "e3"
    assert view["blocked_events"][0]["fact_count"] == 20
    assert "e3" not in view["event_to_card"]
    old = view["event_to_card"]["e1"]
    selected = books.read_cards(audit, view, card_ids=[old], current_records=sample["records"], identity=sample["identity"])
    assert selected["status"] == "required_context_unavailable" and "cards" not in selected
    with pytest.raises(SummaryError, match="invalid_delivery_budget"):
        books.read_cards(audit, view, card_ids=[old], current_records=sample["records"], identity=sample["identity"], max_bytes=1)
    larger = books.build_booklet(audit, card_bytes=64000)
    assert larger["layout_complete"] and larger["book_id"] != view["book_id"]
    assert audit["draft"] == sample["draft"]


def test_exact_card_boundary_uses_utf8_serialization_not_character_count(sample):
    audit, roomy = built(sample, 64000)
    size = len(reading.compact(roomy["cards"][0]))
    exact = books.build_booklet(audit, card_bytes=size)
    assert len(exact["cards"]) == 1
    smaller = books.build_booklet(audit, card_bytes=size - 1)
    assert len(smaller["cards"]) > 1
    assert [e["event_id"] for c in smaller["cards"] for e in c["events"]] == ["e1", "e2", "e3"]


@pytest.mark.parametrize("change", ["source_text", "new_correction", "room", "branch", "card", "author", "approval", "group"])
def test_changed_sources_and_forged_booklets_cannot_be_read_or_expanded(sample, change):
    audit, view = built(sample)
    rows, identity = copy.deepcopy(sample["records"]), dict(sample["identity"])
    if change == "source_text": rows[0]["content"] += "后来有更正。"
    if change == "new_correction": rows.append({**rows[0], "record_id": "new", "content": "后续取消已撤回。"})
    if change == "room": identity["room_id"] = "other-room"
    if change == "branch": identity["branch_id"] = "other-branch"
    if change == "card": view["cards"][0]["events"][0]["body"] += "伪造一行"
    if change == "author": audit["author"]["model"] = "different"
    if change == "approval": audit["production_accepted"] = True
    if change == "group": audit["draft"]["events"].reverse()
    for action in (
        lambda: books.catalogue(audit, view, current_records=rows, identity=identity),
        lambda: books.read_cards(audit, view, card_ids=[view["cards"][0]["card_id"]], current_records=rows, identity=identity),
        lambda: books.expand(audit, view, item_id="e2.f1", current_records=rows, identity=identity)):
        with pytest.raises(SummaryError): action()


def test_current_and_historical_quotes_global_unknown_and_relation_expand_exactly(sample):
    audit, view = built(sample)
    item = books.expand(audit, view, item_id="e2.f1", current_records=sample["records"], identity=sample["identity"])
    assert item["current_refs"] == ["s2", "s4"] and item["history_refs"] == ["s2"]
    assert len(item["evidence"]) == 2
    assert "🎐 e\u0301\r\n" in item["evidence"]["s2"]["quote"]
    for pointer in ("e2.f1", "r1", "u1"):
        expanded = books.expand(audit, view, item_id=pointer, current_records=sample["records"], identity=sample["identity"])
        for span in expanded["evidence"].values():
            text = next(r["content"] for r in sample["records"] if r["record_id"] == span["record_id"])
            assert text[span["start"]:span["end"]] == span["quote"]
    cap = len(reading.compact(item)) - 1
    assert books.expand(audit, view, item_id="e2.f1", current_records=sample["records"], identity=sample["identity"], max_bytes=cap)["status"] == "budget_exceeded"


def test_event_unknown_and_catalogue_budget_have_real_source_and_no_silent_crop():
    sample = trial.capacity_fixture()
    audit, view = built(sample, 1800)
    expanded = books.expand(audit, view, item_id="e28.u1", current_records=sample["records"], identity=sample["identity"])
    assert expanded["current_refs"] == ["s28"]
    full = books.catalogue(audit, view, current_records=sample["records"], identity=sample["identity"], max_bytes=64000)
    withheld = books.catalogue(audit, view, current_records=sample["records"], identity=sample["identity"], max_bytes=len(reading.compact(full)) - 1)
    assert withheld["status"] == "budget_exceeded" and "events" not in withheld


@pytest.mark.parametrize("ids", [[], ["c99"], ["c1", "c1"], [None]])
def test_invalid_selection_is_rejected(sample, ids):
    audit, view = built(sample)
    with pytest.raises(SummaryError, match="unknown_card"):
        books.read_cards(audit, view, card_ids=ids, current_records=sample["records"], identity=sample["identity"])


@pytest.mark.parametrize("pointer", ["e2.f0", "e99.f1", "e2.f99", "r99", "u99", "e2.u99", "../secret", None])
def test_unknown_pointer_is_rejected(sample, pointer):
    audit, view = built(sample)
    with pytest.raises(SummaryError, match="unknown_item"):
        books.expand(audit, view, item_id=pointer, current_records=sample["records"], identity=sample["identity"])


@pytest.mark.parametrize("issue", ["duplicate", "missing_relation", "self", "cycle", "bad_ref", "false_source", "gap", "ok_with_gap"])
def test_ambiguous_relations_sources_and_false_complete_claims_are_rejected(sample, issue):
    draft = sample["draft"]
    if issue == "duplicate": draft["events"][1]["event_id"] = "e1"
    if issue == "missing_relation": draft["relations"][0]["to_event"] = "e99"
    if issue == "self": draft["relations"][0]["to_event"] = "e3"
    if issue == "cycle": draft["relations"].append({**draft["relations"][0], "from_event": "e1", "to_event": "e3"})
    if issue == "bad_ref": draft["relations"][0]["evidence_ids"] = ["s999"]
    if issue == "false_source": sample["records"][0]["source_kind"] = "chatgpt"
    if issue == "gap": draft.update(status="partial", gaps=[{"record_ids": ["unknown"], "reason": "未知来源"}])
    if issue == "ok_with_gap": draft["gaps"] = [{"record_ids": ["row-1"], "reason": "尚未处理"}]
    with pytest.raises(SummaryError): books.make_audit(**sample)


def test_declared_partial_is_not_turned_complete_by_successful_layout(sample):
    sample["draft"].update(status="partial", gaps=[{"record_ids": ["row-1"], "reason": "原作者声明仍需核对"}])
    audit, view = built(sample)
    index = books.catalogue(audit, view, current_records=sample["records"], identity=sample["identity"])
    assert index["author_status"] == "partial" and index["layout_complete"]
    assert index["declared_gaps"] == sample["draft"]["gaps"] and index["semantic_passed"] is None


def test_layout_never_invents_an_event_the_author_omitted():
    sample = trial.capacity_fixture()
    sample["draft"]["events"].pop()  # Writer omitted the final backup plan, without declaring it.
    audit, view = built(sample, 1800)
    index = books.catalogue(audit, view, current_records=sample["records"], identity=sample["identity"], max_bytes=64000)
    assert "row-30" in index["uncited_records"]
    assert all(e["event_id"] != "e30" for e in index["events"])
    assert index["layout_complete"] and index["semantic_passed"] is None
    assert not index["production_accepted"]


def test_invalid_event_date_is_preserved_as_diagnostic_not_repaired(sample):
    sample["draft"]["events"][0]["facts"][0]["event_date"] = "2037-01-01"
    audit, view = built(sample, 1800)
    assert audit["review"]["structural_errors"]
    assert "事件日=2037-01-01" in view["cards"][0]["events"][0]["body"]
    assert not audit["production_accepted"]


@pytest.mark.parametrize("status", ["insufficient", "refused"])
def test_empty_or_refused_draft_stays_distinct(sample, status):
    sample["draft"].update(status=status, events=[], relations=[], refusal_reason="无法处理" if status == "refused" else None)
    audit, view = built(sample)
    index = books.catalogue(audit, view, current_records=sample["records"], identity=sample["identity"])
    assert not view["cards"] and index["author_status"] == status and index["uncited_records"]


def test_total_window_cap_is_explicit_rejection_not_partial_success():
    sample = trial.capacity_fixture()
    for e in sample["draft"]["events"]: e["facts"] = e["facts"] * 5
    with pytest.raises(SummaryError, match="total_capacity_exceeded"): books.make_audit(**sample)


def test_whole_draft_byte_cap_is_explicit_and_keeps_the_input():
    sample = trial.capacity_fixture()
    for e in sample["draft"]["events"]:
        e["uncertainties"] = [{"text": "未" * 240, "evidence_ids": ["s1"]} for _ in range(12)]
    before = copy.deepcopy(sample["draft"])
    with pytest.raises(SummaryError, match="draft_budget_exceeded"): books.make_audit(**sample)
    assert sample["draft"] == before


def test_offline_demo_does_not_invoke_a_provider_and_never_overwrites(tmp_path):
    report = trial.save(tmp_path / "demo")
    assert report["model_calls"] == 0 and report["evaluation"] == "injected_structural_tests_not_model_quality"
    assert all(r["supplied_facts"] == r["retained_facts"] for r in report["rows"])
    assert report["rows"][0]["supplied_facts"] > 24
    with pytest.raises(FileExistsError): trial.save(tmp_path / "demo")
