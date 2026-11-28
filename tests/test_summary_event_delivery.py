"""Event delivery drops card neighbours, not evidence, corrections or uncertainty."""
import copy
import json

import pytest

from backend.memory import summary_event_booklet as books
from backend.memory import summary_reading_card as reading
from backend.memory.caretaker_summary import SummaryError
from scripts import summary_event_booklet_trial as trial
from scripts import summary_event_delivery_trial as delivery_trial


def build(sample=None, budget=4096):
    sample = sample or trial.relation_fixture()
    audit = books.make_audit(**sample)
    return sample, audit, books.build_booklet(audit, card_bytes=budget)


def read(sample, audit, view, ids, **kwargs):
    return books.read_events(audit, view, event_ids=ids,
        current_records=sample["records"], identity=sample["identity"], **kwargs)


def test_selecting_an_event_keeps_its_cancellation_but_not_its_unrelated_card_neighbour():
    sample, audit, view = build()
    assert len(view["cards"]) == 1  # One physical card mixes three separate topics.
    old = books.read_cards(audit, view, card_ids=["c1"],
        current_records=sample["records"], identity=sample["identity"])
    selected = read(sample, audit, view, ["e1"])
    assert selected["included_event_ids"] == ["e1", "e3"]
    assert "两人没有去" in selected["events"][1]["body"]
    assert all(e["event_id"] != "e2" for e in selected["events"])
    old_events = {e["event_id"]: e for c in old["cards"] for e in c["events"]}
    assert selected["events"] == [old_events["e1"], old_events["e3"]]
    assert selected["global_uncertainties"] == old["global_uncertainties"]
    assert selected["relations"][0]["relation_id"] == "r1"
    assert selected["semantic_passed"] is None and not selected["production_accepted"]


@pytest.mark.parametrize("kind", ["corrects", "updates", "conflicts"])
def test_required_peers_are_bidirectional_and_transitive_independent_of_card_boundaries(kind):
    sample = trial.relation_fixture()
    sample["draft"]["relations"][0]["kind"] = kind
    sample["draft"]["relations"].append({"from_event": "e2", "to_event": "e3",
        "kind": "corrects", "evidence_ids": ["s4"]})
    sample, audit, view = build(sample, 900)
    for ids in (["e1"], ["e2"], ["e3"]):
        selected = read(sample, audit, view, ids)
        assert selected["included_event_ids"] == ["e1", "e2", "e3"]
        assert [r["relation_id"] for r in selected["relations"]] == ["r1", "r2"]


def test_related_pointer_is_visible_without_forcing_its_other_event():
    sample = trial.relation_fixture()
    sample["draft"]["relations"][0]["kind"] = "related"
    sample, audit, view = build(sample)
    selected = read(sample, audit, view, ["e1"])
    assert selected["included_event_ids"] == ["e1"]
    assert selected["relations"][0]["from_event"] == "e3"
    index = books.catalogue(audit, view, current_records=sample["records"], identity=sample["identity"])
    assert [e["event_id"] for e in index["events"]] == ["e1", "e2", "e3"]


def test_exact_utf8_budget_applies_to_whole_packet_including_context_and_metadata():
    sample, audit, view = build()
    packet = read(sample, audit, view, ["e2"])
    size = len(reading.compact(packet))
    assert read(sample, audit, view, ["e2"], max_bytes=size) == packet
    withheld = read(sample, audit, view, ["e2"], max_bytes=size - 1)
    assert withheld["status"] == "budget_exceeded" and withheld["required_bytes"] == size
    assert "events" not in withheld


def test_oversized_required_event_is_not_bypassed_by_reading_directly():
    sample = trial.relation_fixture()
    sample["draft"]["events"][2]["facts"] *= 20
    sample, audit, view = build(sample, 800)
    assert any(e["event_id"] == "e3" for e in view["blocked_events"])
    for ids in (["e1"], ["e3"]):
        selected = read(sample, audit, view, ids, max_bytes=64000)
        assert selected["status"] == "required_context_unavailable" and "events" not in selected


@pytest.mark.parametrize("ids", [None, [], "e1", ["e99"], ["e1", "e1"], [None], [["e1"]]])
def test_invalid_event_selection_is_rejected(ids):
    sample, audit, view = build()
    with pytest.raises(SummaryError, match="unknown_event"):
        read(sample, audit, view, ids)


@pytest.mark.parametrize("change", ["source", "identity", "audit", "booklet"])
def test_delivery_rechecks_source_identity_audit_and_booklet(change):
    sample, audit, view = build()
    if change == "source": sample["records"][0]["content"] += "来源已改。"
    if change == "identity": sample["identity"]["branch_id"] = "different"
    if change == "audit": audit["draft"]["events"][0]["facts"][0]["text"] += "changed"
    if change == "booklet": view["cards"][0]["events"][0]["body"] += "changed"
    with pytest.raises(SummaryError):
        read(sample, audit, view, ["e1"])


def test_partial_gaps_unknowns_old_claim_and_diagnostics_remain_visible_and_read_only():
    sample = trial.relation_fixture()
    sample["draft"].update(status="partial", gaps=[{"record_ids": ["row-5"], "reason": "尚待核对。"}])
    sample["draft"]["events"][1]["facts"][0]["event_date"] = "2038-03-09"
    sample, audit, view = build(sample)
    before = copy.deepcopy((sample, audit, view))
    packet = read(sample, audit, view, ["e2"])
    assert packet["author_status"] == "partial" and packet["declared_gaps"] == sample["draft"]["gaps"]
    assert packet["structural_errors"] and packet["global_uncertainties"]
    assert "已被纠正的旧说法：祁宁给晏舟起名。" in packet["events"][0]["body"]
    evidence = books.expand(audit, view, item_id="e2.f1", current_records=sample["records"], identity=sample["identity"])
    assert evidence["current_refs"] == ["s2", "s4"] and evidence["history_refs"] == ["s2"]
    assert "🎐 e\u0301\r\n" in evidence["evidence"]["s2"]["quote"]
    packet["source"]["room_id"] = "consumer change"
    packet["declared_gaps"][0]["reason"] = "consumer change"
    assert (sample, audit, view) == before


def test_many_events_keep_late_feelings_and_plans_when_they_are_selected():
    sample, audit, view = build(trial.capacity_fixture(), 1800)
    selected = read(sample, audit, view, ["e30", "e29", "e28"])
    assert selected["included_event_ids"] == ["e28", "e29", "e30"]
    text = "\n".join(e["body"] for e in selected["events"])
    assert all(s in text for s in ("小河灯", "互相表达爱意", "仍疲惫", "尚未检查备份盘", "具体检查日期未定"))


def test_comparison_counts_whole_envelopes_and_catalogue_even_when_small_windows_do_not_shrink():
    sample, audit, view = build()
    before = copy.deepcopy((sample, audit, view))
    result, packets = delivery_trial.compare(audit, view, sample["records"])
    assert result["events"] == 3 and len(result["selections"]) == 4
    assert result["exact_expansions_checked"] == 5
    for row, packet in zip(result["selections"], packets):
        assert row["legacy_read_bytes"] == len(reading.compact(packet["legacy"]))
        assert row["event_read_bytes"] == len(reading.compact(packet["event"]))
        assert row["catalogue_plus_event_read_bytes"] == result["catalogue_bytes"] + row["event_read_bytes"]
        assert row["saved_bytes"] == row["legacy_read_bytes"] - row["event_read_bytes"]
    assert result["selections"][-1]["selection"] == "all"
    assert (sample, audit, view) == before and result["semantic_passed"] is None


def test_comparison_does_not_report_shrinkage_by_silently_discarding_blocked_events():
    sample, audit, view = build(trial.capacity_fixture(), 512)
    assert not view["layout_complete"]
    with pytest.raises(SummaryError, match="requires_complete_layout"):
        delivery_trial.compare(audit, view, sample["records"])


def test_offline_replay_preserves_inputs_rejects_changed_request_and_never_overwrites(tmp_path, monkeypatch):
    from scripts import prepare_summary_comprehensive_comparison as sources
    monkeypatch.setattr(sources, "PRIVATE_ROOT", tmp_path)
    sample, audit, view = build()
    job = {"job_id": "fixture", "case_id": "fixture", "request": {"synthetic": True}}
    case = {"case_id": "fixture", "records": sample["records"]}
    suite = {"manifest.json": {"schema": "offline-test"}, "jobs.json": [job], "reviewer.json": [case]}
    inspected = {"audit": audit, "booklet": view, "candidate-raw": sample["draft"]}
    prior = tmp_path / "prior"
    folder = prior / job["job_id"]
    folder.mkdir(parents=True)
    (prior / "manifest.json").write_text(json.dumps(suite["manifest.json"]), encoding="utf-8")
    for name, data in {**inspected, "request": job["request"], "attempt": {"job_id": "fixture", "case_id": "fixture"},
                       "final-response": {"synthetic_saved_response": True}}.items():
        (folder / (name + ".json")).write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(delivery_trial.review_trial, "load_frozen", lambda *a, **k: suite)
    monkeypatch.setattr(delivery_trial.review_trial, "inspect_final", lambda *a: inspected)
    def no_api(*a, **k):
        pytest.fail("offline delivery must not access credentials or a model")
    monkeypatch.setattr(delivery_trial.review_trial.transport, "read_dpapi_key", no_api)
    monkeypatch.setattr(delivery_trial.review_trial.transport, "Client", no_api)
    before = {p: p.read_bytes() for p in prior.rglob("*.json")}
    result = delivery_trial.replay(tmp_path / "suite", tmp_path / "first", prior, tmp_path / "output")
    assert result["model_calls"] == 0 and len(result["cases"]) == 1
    assert all(p.read_bytes() == blob for p, blob in before.items())
    saved = (tmp_path / "output" / "report.json").read_bytes()
    with pytest.raises(FileExistsError):
        delivery_trial.replay(tmp_path / "suite", tmp_path / "first", prior, tmp_path / "output")
    assert (tmp_path / "output" / "report.json").read_bytes() == saved
    (folder / "request.json").write_text('{"synthetic":false}', encoding="utf-8")
    with pytest.raises(SummaryError, match="changed_request"):
        delivery_trial.replay(tmp_path / "suite", tmp_path / "first", prior, tmp_path / "blocked")
    assert not (tmp_path / "blocked").exists()
