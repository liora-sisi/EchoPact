import copy
import json

import pytest

from backend.memory import semantic_evidence as core
from backend.memory import evidence_selector as selection
from backend.memory import evidence_packet as codec
from backend.memory.identity import register_agent, set_agent_enabled
from backend.memory.records_v1 import COMPACT_RECORD_SCHEMA_VERSION, import_record_package
from backend.mcp.readonly_server import _bounded_result

QUERY = "盒子怎么收纳东西？"
TARGET = "白昼盒用来收餐具。"
CORRECTION = "更正，白昼盒实际用来收杯垫。"


def setup(tmp_path, texts=None):
    texts = texts or ["天气晴朗。" * 1000 + TARGET + "整理记录。" * 200 + CORRECTION]
    db, package = tmp_path / "source.sqlite3", tmp_path / "source.json"
    register_agent("owner", "owner", actor="test", db_path=str(db))
    register_agent("outsider", "outsider", actor="test", db_path=str(db))
    records = [{"record_id": "r" + str(i), "source_kind": "synthetic", "source_ref": "synthetic://windows",
                "conversation_id": "room", "message_id": "m" + str(i), "role": "assistant", "content": t,
                "created_at": "2035-05-01T12:00:00Z", "verified": False, "authority": "synthetic",
                "source_cutoff_at": "2035-05-01T12:00:00Z",
                "branch_memberships": [{"branch_id": "main", "position": i}]} for i, t in enumerate(texts)]
    package.write_text(json.dumps({"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": records}), encoding="utf-8")
    import_record_package(str(package), db_path=str(db), owner_agent_id="owner", actor="test")
    bridge = core.WindowBridge(db, "owner", source_sha256=core.file_digest(db))
    packet = _bounded_result({"memories": copy.deepcopy(records), "coverage": {"coverage_gap": False}})
    return db, bridge, records, packet


def add_hits(packet, records, *, spans=None):
    packet = copy.deepcopy(packet)
    packet["semantic_shadow"] = {"version": core.INDEX_VERSION, "status": "hybrid_candidates_only", "selected": {}}
    for row in records:
        text = row["content"]
        positions = spans or [(text.index(TARGET), text.index(TARGET) + len(TARGET)),
                              (text.index(CORRECTION), len(text))]
        packet["semantic_shadow"]["selected"][row["record_id"]] = {
            "content_sha256": selection.digest(text), "spans": [{"start": a, "end": b} for a, b in positions]}
    return packet


def draft(request):
    judgments = []
    for c in request["candidates"]:
        quote = TARGET if TARGET in c["text"] else CORRECTION if CORRECTION in c["text"] else ""
        judgments.append({"candidate_id": c["candidate_id"], "relation": "uncertain" if quote else "context", "quote": quote})
    return {"judgments": judgments, "sufficiency": "ambiguous", "gap": "conflicting_evidence"}


def test_tail_and_separate_same_record_correction_reach_existing_selector_and_pack(tmp_path):
    db, bridge, records, raw = setup(tmp_path)
    before = copy.deepcopy(raw)
    prepared = bridge.prepare(add_hits(raw, records), query=QUERY)
    assert raw == before and all(TARGET not in r["content"] for r in raw["memories"])
    assert any(TARGET in c["text"] for c in prepared["request"]["candidates"])
    assert any(CORRECTION in c["text"] for c in prepared["request"]["candidates"])
    seen = []
    def select(request):
        seen.append(copy.deepcopy(request))
        assert set(request) == {"question", "candidates"}
        assert "record_id" not in json.dumps(request) and "semantic_score" not in json.dumps(request)
        return draft(request)
    result = bridge.review(prepared, selector=select)
    assert len(seen) == result["selector_attempts"] == 1
    assert result["status"] == "selected_requires_review"
    expanded = codec.expand(result["brief"])
    assert {r["text"] for r in expanded["items"]} == {TARGET, CORRECTION}
    for item in expanded["items"]:
        ref = item["references"][0]
        assert ref["observed_content_sha256"] == selection.digest(records[0]["content"])
        assert not ref["source_truncated"]
        assert ref["quote_start"] > 4000
        assert records[0]["content"][ref["quote_start"]:ref["quote_end"]] == item["text"]
    assert len(selection.encoded(result["brief"])) <= selection.MAX_BRIEF_BYTES
    assert core.file_digest(db) == bridge.source_sha256


def test_lexical_arm_reuses_existing_literal_term_window(tmp_path):
    text = "天气晴朗。" * 100 + "盒子用来收餐具。" + "后记。" * 100
    _, bridge, _, raw = setup(tmp_path, [text])
    prepared = bridge.prepare(raw, query=QUERY, topic_terms=["盒子"])
    legacy = selection.prepare({"original": raw, "plan": {"status": "planned", "intent": "topic", "topic_terms": ["盒子"]}}, query=QUERY)
    assert prepared["request"] == legacy["request"]
    assert prepared["local"]["c1"]["excerpt_start"] == legacy["local"]["c1"]["excerpt_start"]


def test_same_count_byte_limits_and_one_window_per_record_before_extra_windows(tmp_path):
    _, bridge, records, raw = setup(tmp_path, ["天气。" * 1400 + TARGET + "分隔。" * 200 + CORRECTION for _ in range(10)])
    a, b = bridge.prepare(raw, query=QUERY), bridge.prepare(add_hits(raw, records), query=QUERY)
    assert a["limits"] == b["limits"]
    assert len(b["local"]) == 12 and b["omitted_candidates"] > 0
    assert [r["record_id"] for r in list(b["local"].values())[:10]] == ["r" + str(i) for i in range(10)]
    for prepared in (a, b):
        assert len(selection.encoded(prepared["request"])) <= selection.MAX_INPUT_BYTES
        preview = bridge.preview(prepared)
        assert len(selection.encoded(preview)) <= selection.MAX_BRIEF_BYTES
        assert len(preview["items"]) <= 6 and preview["evidence_status"] == "not_assessed"
        assert preview["needs_more_evidence"] and preview["generation"] == "unreviewed_verbatim_windows"


@pytest.mark.parametrize("mutation", [
    lambda p: p["semantic_shadow"]["selected"]["r0"].update(content_sha256="0" * 64),
    lambda p: p["semantic_shadow"]["selected"]["r0"]["spans"][0].update(start=-1),
    lambda p: p["semantic_shadow"]["selected"]["r0"]["spans"][0].update(start=True),
    lambda p: p["semantic_shadow"]["selected"]["r0"]["spans"][0].update(end=999999),
    lambda p: p["memories"][0].update(role="user"),
    lambda p: p["memories"][0].update(content="forged prefix"),
])
def test_forged_observations_and_bad_offsets_fail_closed(tmp_path, mutation):
    _, bridge, records, raw = setup(tmp_path)
    raw = add_hits(raw, records)
    mutation(raw)
    with pytest.raises(ValueError):
        bridge.prepare(raw, query=QUERY)


def test_source_or_permissions_change_after_preparation_refuses_old_preview(tmp_path):
    db, bridge, _, raw = setup(tmp_path)
    prepared = bridge.prepare(raw, query=QUERY)
    set_agent_enabled("owner", False, actor="test", db_path=str(db))
    with pytest.raises(ValueError):
        bridge.review(prepared)


def test_revocation_during_selector_never_returns_cached_fallback(tmp_path):
    db, bridge, records, raw = setup(tmp_path)
    prepared = bridge.prepare(add_hits(raw, records), query=QUERY)
    def select(request):
        set_agent_enabled("owner", False, actor="test", db_path=str(db))
        return draft(request)
    with pytest.raises(ValueError):
        bridge.review(prepared, selector=select)


def test_other_identity_cannot_replay_prepared_windows(tmp_path):
    db, bridge, _, raw = setup(tmp_path)
    prepared = bridge.prepare(raw, query=QUERY)
    other = core.WindowBridge(db, "outsider", source_sha256=bridge.source_sha256)
    with pytest.raises(ValueError):
        other.review(prepared)


def test_model_invents_quote_preserves_marked_preview_and_makes_one_attempt(tmp_path):
    _, bridge, records, raw = setup(tmp_path)
    prepared = bridge.prepare(add_hits(raw, records), query=QUERY)
    def select(request):
        response = draft(request)
        response["judgments"][0]["quote"] = "made up quotation"
        return response
    result = bridge.review(prepared, selector=select)
    assert result["selector_attempts"] == 1 and result["status"] == "selector_failed_unreviewed_windows"
    assert result["brief"]["evidence_status"] == "not_assessed" and "judgments" not in result


def test_known_constraints_never_expand_or_call_selector(tmp_path, monkeypatch):
    _, bridge, _, raw = setup(tmp_path)
    monkeypatch.setattr(core.ReadonlyGateway, "expand", lambda *a, **k: pytest.fail("constrained query must bypass"))
    with pytest.raises(core.SemanticError, match="constraints_original_only"):
        bridge.prepare(raw, query="昨天整理了什么？")


def test_modifying_prepared_request_detected_before_model_call(tmp_path):
    _, bridge, _, raw = setup(tmp_path)
    prepared = bridge.prepare(raw, query=QUERY)
    prepared["request"]["candidates"][0]["text"] = "changed"
    with pytest.raises(core.SemanticError, match="preparation_changed"):
        bridge.review(prepared, selector=lambda _: pytest.fail("changed source request must not reach model"))


@pytest.mark.parametrize("change", [
    lambda c: c.update(private_debug="must never leave this machine"),
    lambda c: c.update(role="user"),
    lambda c: c.update(excerpt_incomplete="private text"),
])
def test_rehashed_card_cannot_smuggle_metadata_or_unapproved_fields(tmp_path, change):
    _, bridge, _, raw = setup(tmp_path)
    prepared = bridge.prepare(raw, query=QUERY)
    change(prepared["request"]["candidates"][0])
    prepared.pop("prepared_sha256")
    prepared["prepared_sha256"] = core.canonical_hash(prepared)
    with pytest.raises(ValueError):
        bridge.review(prepared, selector=lambda _: pytest.fail("invalid card must not reach model"))


def test_api_exam_rejects_private_text_even_if_frozen_record_ids_are_reused():
    from scripts import semantic_window_exam as exam
    fixture = json.loads(exam.FIXTURE.read_text(encoding="utf-8"))
    source = exam.source_records(fixture)[0]
    row = {**selection._metadata(source), "text": source["content"][:50], "excerpt_start": 0,
           "observed_content_sha256": selection.digest(source["content"])}
    card = {"candidate_id": "c1", "source_kind": "synthetic", "role": "assistant", "text": row["text"], "excerpt_incomplete": True}
    prepared = {"request": {"question": fixture["cases"][0]["query"], "candidates": [card]}, "local": {"c1": row}}
    exam.verify_fictional_request(prepared, fixture["cases"][0], fixture)
    row["text"] = card["text"] = "Private source accidentally pasted into the synthetic exam."
    with pytest.raises(ValueError, match="frozen_fiction"):
        exam.verify_fictional_request(prepared, fixture["cases"][0], fixture)
