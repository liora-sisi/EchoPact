"""Reading previews preserve qualifications and bind every expansion to exact sources."""
import copy
import json

import pytest

from backend.memory.caretaker_summary import SummaryError
from backend.memory import summary_reading_card as cards
from scripts import prepare_summary_comprehensive_comparison as original
from scripts import prepare_summary_round2 as exam
from scripts import summary_reading_card_trial as replay


def fact(text="原话中的安排", **changes):
    return {"text": text, "event_status": "planned", "statement_role": "direct",
            "event_date": None, "planned_date": None, "evidence_ids": ["s1"],
            "supersedes": None, **changes}


@pytest.fixture
def sample():
    content = "重复原话。🛶\n重复原话。原定2035年6月15日寄书。后来取消，取消日期没记。"
    start = content.index("重复原话", 1)
    records = [{"record_id": "r1", "source_kind": "synthetic",
                "recorded_at": "2035-06-20T20:00:00+08:00", "content": content}]
    catalog = {"s1": {"record_id": "r1", "start": start, "end": len(content), "quote": content[start:]},
               "s2": {"record_id": "r1", "start": 0, "end": start, "quote": content[:start]}}
    summary = {"status": "ok", "facts": [fact("据更正，寄书安排已取消。", event_status="cancelled",
        statement_role="correction", planned_date="2035-06-15",
        supersedes={"text": "原先说仍按计划寄书。", "evidence_ids": ["s1"]}),
        fact("她说喜欢这个称呼。", event_status="not_applicable", statement_role="retelling")],
        "uncertainties": ["取消决定日未知；是否退款没记。"], "refusal_reason": None}
    return dict(summary=summary, records=records, catalog=catalog,
        identity={"source_namespace": "synthetic", "room_id": "room-a", "branch_id": "branch-a"},
        author={"provider": "injected", "model": "synthetic", "recipe": "test", "prompt_sha256": "a" * 64})


def test_all_facts_negation_history_uncertainty_and_status_survive(sample):
    before = copy.deepcopy(sample)
    audit = cards.make_audit(**sample)
    packet = cards.render_preview(audit)
    assert sample == before
    assert audit["model_summary"] == sample["summary"]
    for f in sample["summary"]["facts"]:
        assert f["text"] in packet["body"]
        assert cards.STATES[f["event_status"]] in packet["body"]
        assert cards.ROLES[f["statement_role"]] in packet["body"]
    assert "已被纠正的旧说法：原先说仍按计划寄书。" in packet["body"]
    assert "原定日=2035-06-15" in packet["body"]
    assert sample["summary"]["uncertainties"][0] in packet["body"]
    assert "2035-06-20" not in packet["body"]  # Message time is not an event date.
    assert packet["uncited_spans"] == ["s2"]
    assert "引用也不保证" in packet["body"]
    assert "适用日期未列=未知" in packet["body"]
    assert packet["status"] == "diagnostic_preview"
    assert audit["production_accepted"] is False
    assert audit["review"]["semantic_passed"] is None


def test_exact_unicode_position_and_old_evidence_expand_without_find(sample):
    audit = cards.make_audit(**sample)
    packet = cards.render_preview(audit)
    result = cards.expand_preview(audit, card_id=packet["card_id"], fact_id="f1",
                                  current_records=sample["records"], identity=sample["identity"])
    span = result["evidence"][result["current_refs"][0]]
    assert span["start"] == sample["catalog"]["s1"]["start"] > 0
    assert sample["records"][0]["content"][span["start"]:span["end"]] == span["quote"]
    assert result["evidence"][result["history_refs"][0]]["quote"] == span["quote"]
    assert len(result["evidence"]) == 1  # Shared current/history evidence is sent once.
    assert result["source"] == sample["identity"]
    assert len(result["records"][span["record_id"]]["record_sha256"]) == 64


@pytest.mark.parametrize("change", ["text", "new_record", "room", "branch", "author", "summary"])
def test_stale_or_wrong_room_pointer_cannot_open_source(sample, change):
    audit = cards.make_audit(**sample)
    packet = cards.render_preview(audit)
    records, identity = copy.deepcopy(sample["records"]), dict(sample["identity"])
    if change == "text": records[0]["content"] += "后来又纠正了。"
    if change == "new_record": records.append({**records[0], "record_id": "r2", "content": "新的纠正。"})
    if change == "room": identity["room_id"] = "room-b"
    if change == "branch": identity["branch_id"] = "branch-b"
    if change == "author": audit["author"]["model"] = "different-model"
    if change == "summary": audit["model_summary"]["facts"][0]["text"] = "改过的答案。"
    with pytest.raises(SummaryError, match="stale_or_wrong_source"):
        cards.expand_preview(audit, card_id=packet["card_id"], fact_id="f1",
                             current_records=records, identity=identity)


@pytest.mark.parametrize("change", ["offset", "quote", "unknown", "duplicate", "real_source", "approval"])
def test_invalid_sources_and_fake_approval_fail_closed(sample, change):
    if change == "offset": sample["catalog"]["s1"]["start"] = 0
    if change == "quote": sample["catalog"]["s1"]["quote"] = "不存在的原话"
    if change == "unknown": sample["summary"]["facts"][0]["evidence_ids"] = ["s99"]
    if change == "duplicate": sample["summary"]["facts"][0]["evidence_ids"] = ["s1", "s1"]
    if change == "real_source": sample["records"][0]["source_kind"] = "chatgpt"
    with pytest.raises(SummaryError):
        audit = cards.make_audit(**sample)
        if change == "approval":
            audit["production_accepted"] = True
            cards.render_preview(audit)


def test_budget_accounts_for_whole_serialized_card_and_never_chops_correction(sample):
    audit = cards.make_audit(**sample)
    full = cards.render_preview(audit)
    exact = len(cards.compact(full))
    assert cards.render_preview(audit, max_bytes=exact) == full
    withheld = cards.render_preview(audit, max_bytes=exact - 1)
    assert withheld["status"] == "budget_exceeded" and "body" not in withheld
    assert withheld["required_bytes"] == exact and withheld["omitted_facts"] == 2
    assert len(cards.compact(withheld)) <= exact - 1
    assert audit["model_summary"] == sample["summary"]


def test_known_invalid_date_remains_visible_and_is_not_repaired_by_packaging(sample):
    sample["summary"]["facts"][1]["event_date"] = "2035-06-15"
    audit = cards.make_audit(**sample, structural_errors=["event_date_requires_happened"])
    packet = cards.render_preview(audit)
    assert packet["structural_errors"] == ["event_date_requires_happened"]
    assert "事件日=2035-06-15" in packet["body"]
    assert audit["model_summary"] == sample["summary"]
    assert "未获准使用" in packet["body"]


@pytest.mark.parametrize("status", ["insufficient", "refused"])
def test_empty_or_refused_response_stays_distinct_from_a_usable_summary(sample, status):
    sample["summary"] = {"status": status, "facts": [], "uncertainties": ["资料不足"],
                         "refusal_reason": "无法处理本片段" if status == "refused" else None}
    packet = cards.render_preview(cards.make_audit(**sample))
    assert packet["summary_status"] == status
    assert "资料不足" in packet["body"]
    assert packet["uncited_spans"] == ["s1", "s2"]


@pytest.fixture
def saved_bundle():
    private_stub = {"schema": "echo-summary-private-excerpts-v1", "captured_at": "2035-01-01T00:00:00+08:00",
        "source_context": copy.deepcopy(original.PRIVATE_CONTEXT), "cases": [
            {"case_id": f"private-stub-{i}", "category": "explicit_adult_excerpt", "focus": "synthetic plumbing",
             "records": [{"record_id": f"local-excerpt-stub-{i}", "recorded_at": None,
                          "source_kind": original.PRIVATE_KIND, "content": "DO_NOT_EXPORT_PRIVATE_STUB"}],
             "review_targets": ["test"], "forbidden_claims": ["test"]} for i in range(3)]}
    bundle = exam.build_bundle(private_stub)
    finals = {}
    for provider in replay.previous.PROVIDERS:
        rows = [{"case_id": c["case_id"], "summary": {"status": "ok", "facts": [
            fact("DO_NOT_EXPORT_PRIVATE_ANSWER" if c["cohort"] == "private_repeat" else "合成测试安排。")],
            "uncertainties": ["日期未定"], "refusal_reason": None}}
            for c in bundle["reviewer_sheet.json"]["cases"]]
        finals[provider] = {"finish": "complete", "model": replay.shared.PROVIDERS[provider]["model"],
                            "final_content": json.dumps({"cases": rows}, ensure_ascii=False)}
    return private_stub, bundle, finals


def test_replay_exports_only_three_public_cases_and_counts_expansions(saved_bundle, monkeypatch):
    _, bundle, finals = saved_bundle
    def forbidden(*args, **kwargs): pytest.fail("offline replay must not call a model or read a key")
    monkeypatch.setattr(replay.shared, "Client", forbidden)
    monkeypatch.setattr(replay.shared, "read_dpapi_key", forbidden)
    before = copy.deepcopy((bundle, finals))
    report, artifacts = replay.replay(bundle, finals)
    assert (bundle, finals) == before
    assert report["model_calls"] == 0 and not report["production_accepted"]
    assert len(artifacts) == len(report["rows"]) == 6
    assert "DO_NOT_EXPORT" not in json.dumps(artifacts)
    for row in report["rows"]:
        artifact = artifacts[f"{row['provider']}-{row['case_id']}"]
        assert row["reading_card_bytes"] == len(cards.compact(artifact["reading_card"]))
        assert row["card_plus_first_expansion_bytes"] > row["reading_card_bytes"]
        assert row["card_plus_all_expansions_bytes"] >= row["card_plus_first_expansion_bytes"]
    withheld, _ = replay.replay(bundle, finals, max_bytes=512)
    assert all(not row["card_delivered"] and row["reading_to_source_ratio"] is None
               for row in withheld["rows"])


def test_saved_run_preserves_inputs_refuses_overwrite_and_rejects_wrong_request(saved_bundle, tmp_path, monkeypatch):
    private_stub, bundle, finals = saved_bundle
    monkeypatch.setattr(original, "PRIVATE_ROOT", tmp_path)
    private_path = tmp_path / "stub.json"
    private_path.write_text(json.dumps(private_stub), encoding="utf-8")
    monkeypatch.setattr(original, "PRIVATE_FIXTURE", private_path)
    suite, trial, out = tmp_path / "suite", tmp_path / "trial", tmp_path / "output"
    original.prepare(suite, bundle)
    bundle = replay.previous.load_frozen(suite)
    trial.mkdir()
    reviews = []
    for provider, final in finals.items():
        folder = trial / provider
        folder.mkdir()
        replay.shared.write_new(folder / "request.json", replay.previous.payload_for(bundle, provider))
        replay.shared.write_new(folder / "final-response.json", final)
        reviews.extend({"case_id": cid, "provider": provider, "production_accepted": False,
                        "reviewer_findings": ["synthetic review only"]} for cid in replay.CASE_IDS)
    replay.shared.write_new(trial / "semantic-review-v1.private.json", {"per_case": reviews})
    files = {p: p.read_bytes() for root in (suite, trial) for p in root.rglob("*.json")}
    report = replay.run(suite, trial, out)
    assert len(report["rows"]) == 6 and (out / "comparison.md").is_file()
    assert all(p.read_bytes() == data for p, data in files.items())
    assert "DO_NOT_EXPORT" not in "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
    with pytest.raises(FileExistsError): replay.run(suite, trial, out)
    request = trial / "openai" / "request.json"
    data = json.loads(request.read_text(encoding="utf-8"))
    data["model"] = "wrong-model"
    request.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SummaryError, match="saved_request_mismatch"):
        replay.run(suite, trial, tmp_path / "wrong")
    assert not (tmp_path / "wrong").exists()
