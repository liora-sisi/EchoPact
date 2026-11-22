import copy
import json

import pytest

from backend.memory import semantic_ordered_evidence as core
from backend.memory import semantic_evidence as windows
from backend.memory import evidence_selector as selection
from backend.memory import evidence_packet as codec
from backend.memory.identity import register_agent, set_agent_enabled
from backend.memory.records_v1 import COMPACT_RECORD_SCHEMA_VERSION, import_record_package
from backend.mcp.readonly_server import _bounded_result

QUERY = "白昼盒现在用来收什么？"
OLD = "白昼盒用来收餐具。"
NEW = "不是餐具，刚刚说错了，实际收杯垫。"


def setup(tmp_path, specs=None, selected=("r0",)):
    specs = specs or [{"content": OLD}, {"content": NEW}]
    db = tmp_path / "source.sqlite3"
    for owner in ("owner", "other"):
        register_agent(owner, owner, actor="test", db_path=str(db))
    records = []
    for i, spec in enumerate(specs):
        row = {"record_id": "r" + str(i), "source_kind": "synthetic", "source_ref": "synthetic://adjacency",
               "conversation_id": "room", "message_id": "m" + str(i), "role": "assistant",
               "created_at": "2035-05-01T12:00:00Z", "verified": False, "authority": "synthetic",
               "source_cutoff_at": "2035-05-01T12:00:00Z",
               "branch_memberships": [{"branch_id": "main", "position": i}], **spec}
        records.append(row)
    for owner in ("owner", "other"):
        rows = [{k: v for k, v in row.items() if k != "owner"} for row in records if row.get("owner", "owner") == owner]
        if rows:
            path = tmp_path / (owner + ".json")
            path.write_text(json.dumps({"schema_version": COMPACT_RECORD_SCHEMA_VERSION, "records": rows}), encoding="utf-8")
            import_record_package(str(path), db_path=str(db), owner_agent_id=owner, actor="test")
    bridge = core.OrderedWindowBridge(db, "owner", source_sha256=windows.file_digest(db))
    packet = _bounded_result({"memories": [{k: v for k, v in row.items() if k != "owner"}
                             for row in records if row["record_id"] in selected], "coverage": {"coverage_gap": False}})
    return db, bridge, records, packet


def response(prepared, selected_ids):
    return {"judgments": [{"candidate_id": cid, "relation": "answers" if cid in selected_ids else "context",
                          "quote": row["text"] if cid in selected_ids else ""}
                         for cid, row in prepared["local"].items()],
            "sufficiency": "sufficient", "gap": "none", "gaps": []}


def all_items(brief):
    return brief["items"] + [item for b in brief.get("source_order_context", []) for item in b["items"]]


def test_later_keyword_free_correction_survives_even_if_model_discards_it(tmp_path):
    db, bridge, _, raw = setup(tmp_path)
    raw["memories"][0]["conversation_context"] = [{"record_id": "forged", "content": "forged"}]
    prepared = bridge.prepare(raw, query=QUERY)
    assert [row["text"] for row in prepared["local"].values()] == [OLD, NEW]
    assert prepared["request"]["source_order_windows"] == [{"candidate_ids": ["c1", "c2"]}]
    assert "record_id" not in json.dumps(prepared["request"])
    result = bridge.review(prepared, selector=lambda _: response(prepared, {"c1"}))
    assert result["status"] == "selected_requires_review"
    brief = codec.expand(result["brief"])
    assert brief["items"][0]["text"] == OLD  # A contract pass is NOT truth acceptance.
    assert brief["source_order_context"][0]["items"][0]["text"] == NEW
    assert brief["source_order_context"][0]["items"][0]["relation"] == "context"
    assert brief["needs_more_evidence"]
    assert brief["evidence_status"] == "model_judgment_with_local_order_context_review_required"
    assert windows.file_digest(db) == bridge.base.source_sha256


def test_neighbors_require_actual_source_room_branch_position_and_visibility(tmp_path):
    specs = [{"content": OLD, "branch_memberships": [{"branch_id": "main", "position": 2}]},
             {"content": NEW, "branch_memberships": [{"branch_id": "main", "position": 3}]},
             {"content": "wrong room", "conversation_id": "elsewhere"},
             {"content": "wrong source", "source_kind": "other-source"},
             {"content": "other branch", "branch_memberships": [{"branch_id": "fork", "position": 3}]},
             {"content": "hidden", "owner": "other", "branch_memberships": [{"branch_id": "main", "position": 1}]},
             {"content": "too far"}]
    _, bridge, _, raw = setup(tmp_path, specs)
    prepared = bridge.prepare(raw, query=QUERY)
    assert {row["record_id"] for row in prepared["local"].values()} == {"r0", "r1"}
    assert "hidden" not in json.dumps(prepared, ensure_ascii=False)


def test_shared_parent_on_two_branches_does_not_order_forks_against_each_other(tmp_path):
    specs = [{"content": OLD, "branch_memberships": [{"branch_id": b, "position": 0} for b in ("a", "b")]},
             {"content": NEW, "branch_memberships": [{"branch_id": "a", "position": 1}]},
             {"content": "另一分支：收钥匙。", "branch_memberships": [{"branch_id": "b", "position": 1}]}]
    _, bridge, _, raw = setup(tmp_path, specs)
    prepared = bridge.prepare(raw, query=QUERY)
    assert prepared["request"]["source_order_windows"] == [
        {"candidate_ids": ["c1", "c2"]}, {"candidate_ids": ["c1", "c3"]}]
    result = bridge.review(prepared, selector=lambda _: response(prepared, {"c1"}))
    orders = codec.expand(result["brief"])["source_order_context"][0]["source_order_windows"]
    assert orders == [{"record_ids": ["r0", "r1"]}, {"record_ids": ["r0", "r2"]}]


def test_same_record_windows_keep_offsets_and_selected_accounting(tmp_path):
    text = "天气。" * 1500 + OLD + "杂记。" * 200 + NEW
    _, bridge, records, raw = setup(tmp_path, [{"content": text}, {"content": "补充，杯垫只在洗净晾干后入盒。"}])
    raw["semantic_shadow"] = {"version": windows.INDEX_VERSION, "status": "hybrid_candidates_only", "selected": {
        "r0": {"content_sha256": selection.digest(text), "spans": [
            {"start": text.index(OLD), "end": text.index(OLD) + len(OLD)},
            {"start": text.index(NEW), "end": len(text)}]}}}
    prepared = bridge.prepare(raw, query=QUERY)
    selected = {cid: OLD if OLD in row["text"] else NEW for cid, row in prepared["local"].items()
                if OLD in row["text"] or NEW in row["text"]}
    draft = response(prepared, set())
    for judgment in draft["judgments"]:
        if judgment["candidate_id"] in selected:
            judgment.update(relation="answers", quote=selected[judgment["candidate_id"]])
    result = bridge.review(prepared, selector=lambda _: draft)
    brief = codec.expand(result["brief"])
    assert {item["text"] for item in brief["items"]} == {OLD, NEW}
    assert brief["omitted_selected"] == 0
    for item in all_items(brief):
        for ref in item["references"]:
            assert records[int(ref["record_id"][1:])]["content"][ref["quote_start"]:ref["quote_end"]] == item["text"]
    for bundle in brief["source_order_context"]:
        assert len(bundle["for_record_ids"]) == len(set(bundle["for_record_ids"]))
        assert all(len(w["record_ids"]) == len(set(w["record_ids"])) for w in bundle["source_order_windows"])


def test_order_links_survive_when_both_messages_are_model_selected(tmp_path):
    _, bridge, _, raw = setup(tmp_path)
    prepared = bridge.prepare(raw, query=QUERY)
    brief = codec.expand(bridge.review(prepared, selector=lambda _: response(prepared, {"c1", "c2"}))["brief"])
    assert brief["source_order_context"][0]["source_order_windows"] == [{"record_ids": ["r0", "r1"]}]
    assert brief["source_order_context"][0]["items"] == []


@pytest.mark.parametrize("mutation", ["reverse_order", "add_private_field", "remove_neighbor"])
def test_rehashing_cannot_forge_canonical_order_or_drop_correction(tmp_path, mutation):
    _, bridge, _, raw = setup(tmp_path)
    prepared = bridge.prepare(raw, query=QUERY)
    if mutation == "reverse_order":
        prepared["request"]["source_order_windows"][0]["candidate_ids"].reverse()
    elif mutation == "add_private_field":
        prepared["request"]["candidates"][0]["private"] = "do not transmit"
    else:
        prepared["request"]["candidates"].pop()
        del prepared["local"]["c2"]
        prepared["request"]["source_order_windows"] = []
    prepared.pop("prepared_sha256")
    prepared["prepared_sha256"] = core.canonical_hash(prepared)
    with pytest.raises(ValueError, match="preparation_changed"):
        bridge.review(prepared, selector=lambda _: pytest.fail("forged packet must not be sent"))


@pytest.mark.parametrize("kind", ["branch_limit", "ambiguous_position"])
def test_ambiguous_or_unbounded_groups_are_withheld_before_model(tmp_path, kind):
    specs = [{"content": OLD}, {"content": NEW}]
    if kind == "branch_limit":
        specs[0]["branch_memberships"] = [{"branch_id": str(i), "position": 0} for i in range(5)]
    else:
        specs[1]["branch_memberships"] = [{"branch_id": "main", "position": 0}]
    _, bridge, _, raw = setup(tmp_path, specs)
    prepared = bridge.prepare(raw, query=QUERY)
    assert {row["record_id"] for row in prepared["local"].values()} == {"r0"}
    assert prepared["adjacency"][kind + "_groups"] == 1 and prepared["adjacency"]["deferred"]
    result = bridge.review(prepared, selector=lambda _: pytest.fail("withheld group"))
    assert result["selector_attempts"] == 0 and result["brief"]["evidence_status"] == "not_assessed"


def test_input_card_limit_defers_instead_of_publishing_a_partial_subset(tmp_path):
    specs = [{"content": str(i), "conversation_id": "room" + str(i // 5),
              "branch_memberships": [{"branch_id": "main", "position": i % 5}]} for i in range(15)]
    _, bridge, _, raw = setup(tmp_path, specs, selected=("r2", "r7", "r12"))
    prepared = bridge.prepare(raw, query=QUERY)
    assert prepared["adjacency"]["deferred"] and prepared["adjacency"]["discarded_partial_groups"] == 2
    assert prepared["local"] == prepared["seed"]["local"]
    assert {row["record_id"] for row in prepared["local"].values()} == {"r2", "r7", "r12"}
    assert prepared["omitted_candidates"] == 12
    assert len(selection.encoded(prepared["request"])) <= selection.MAX_INPUT_BYTES
    assert bridge.review(prepared, selector=lambda _: pytest.fail("context not ready"))["selector_attempts"] == 0


def test_context_pressure_cannot_evict_the_eighth_seed(tmp_path):
    specs = [{"content": "原始候选" + str(i), "conversation_id": "room" + str(i // 2),
              "branch_memberships": [{"branch_id": "main", "position": i % 2}]} for i in range(20)]
    _, bridge, _, raw = setup(tmp_path, specs, selected=tuple("r" + str(i) for i in range(0, 20, 2)))
    prepared = bridge.prepare(raw, query=QUERY)
    assert prepared["adjacency"]["deferred"]
    assert prepared["local"] == prepared["seed"]["local"]
    assert list(prepared["local"].values())[7]["record_id"] == "r14"
    result = bridge.review(prepared, selector=lambda _: pytest.fail("incomplete adjacency must not certify an answer"))
    assert result["status"] == "adjacency_deferred_unreviewed_windows" and result["production_accepted"] is False


def test_output_context_limit_withholds_connected_group_instead_of_stale_parent(tmp_path):
    specs = [{"content": "消息" + str(i)} for i in range(9)]
    _, bridge, _, raw = setup(tmp_path, specs, selected=("r2", "r4", "r6"))
    prepared = bridge.prepare(raw, query=QUERY)
    result = bridge.review(prepared, selector=lambda _: response(prepared, {"c1"}))
    brief = codec.expand(result["brief"])
    assert not brief["items"] and brief["omitted_selected"] == 1
    assert brief["order_context_groups_withheld"] == 1 and brief["needs_more_evidence"]
    preview = codec.expand(bridge.preview(prepared))
    assert len(all_items(preview)) == 9 and all(item["relation"] == "context" for item in all_items(preview))
    assert len(selection.encoded(result["brief"])) <= selection.MAX_BRIEF_BYTES


def test_long_neighbor_is_marked_incomplete_not_claimed_fully_read(tmp_path):
    _, bridge, _, raw = setup(tmp_path, [{"content": OLD}, {"content": "后记。" * 500 + NEW}])
    prepared = bridge.prepare(raw, query=QUERY)
    assert prepared["adjacency"]["incomplete_cards"] == 1
    assert NEW not in prepared["request"]["candidates"][1]["text"]
    assert prepared["object_scout_needs_more"]


def test_source_revoked_during_model_call_never_falls_back_to_cached_preview(tmp_path):
    db, bridge, _, raw = setup(tmp_path)
    prepared = bridge.prepare(raw, query=QUERY)
    def select(_):
        set_agent_enabled("owner", False, actor="test", db_path=str(db))
        raise RuntimeError("model failed")
    with pytest.raises(ValueError):
        bridge.review(prepared, selector=select)


def test_failed_model_retains_labeled_whole_group_preview(tmp_path):
    _, bridge, _, raw = setup(tmp_path)
    prepared = bridge.prepare(raw, query=QUERY)
    result = bridge.review(prepared, selector=lambda _: {"invented": "answer"})
    brief = codec.expand(result["brief"])
    assert result["selector_attempts"] == 1 and result["status"] == "selector_failed_unreviewed_windows"
    assert brief["evidence_status"] == "not_assessed"
    assert {item["text"] for item in all_items(brief)} == {OLD, NEW}


def test_date_constraints_bypass_all_adjacency(tmp_path, monkeypatch):
    _, bridge, _, raw = setup(tmp_path)
    monkeypatch.setattr(core, "_connect_readonly", lambda _: pytest.fail("date route must bypass"))
    with pytest.raises(ValueError, match="constraints_original_only"):
        bridge.prepare(raw, query="昨天整理了什么？")


def test_fictional_exam_prepares_offline_and_checks_saved_requests(tmp_path):
    from scripts import semantic_ordered_exam as exam
    output = tmp_path / "exam"
    plan = exam.prepare(output)
    assert plan["scope"] == "fixed_seed_component_exam_not_retrieval_accuracy"
    assert len(plan["cases"]) == 7 and all(row["adjacency"]["withheld_groups"] == 0 for row in plan["cases"])
    fixture = json.loads(exam.FIXTURE.read_text(encoding="utf-8"))
    case = fixture["cases"][0]
    prepared = json.loads((output / (case["id"] + ".prepared.json")).read_text(encoding="utf-8"))
    exam.verify_fictional_request(prepared, case, fixture)
    cid = prepared["request"]["candidates"][0]["candidate_id"]
    prepared["local"][cid]["text"] = prepared["request"]["candidates"][0]["text"] = "Unapproved real conversation."
    with pytest.raises(ValueError, match="frozen_fiction"):
        exam.verify_fictional_request(prepared, case, fixture)


def test_retracted_claim_without_replacement_keeps_non_answer_evidence(tmp_path):
    _, bridge, _, raw = setup(tmp_path, [{"content": OLD}, {"content": "用途说错了，实际用途还没核实。"}])
    prepared = bridge.prepare(raw, query=QUERY)
    draft = response(prepared, {"c2"})
    draft["judgments"][1]["relation"] = "uncertain"
    draft.update(sufficiency="insufficient", gap="missing_answer", gaps=["missing_answer"])
    brief = codec.expand(bridge.review(prepared, selector=lambda _: draft)["brief"])
    assert brief["model_sufficiency"] == "insufficient" and brief["needs_more_evidence"]
    assert brief["items"][0]["relation"] == "uncertain"
    assert {item["text"] for item in all_items(brief)} == {OLD, "用途说错了，实际用途还没核实。"}
    # No status/quote is rewritten, and no ambiguous fact gets promoted.
    for gap in ("none", "object_identity", "conflicting_evidence", "excerpt_incomplete"):
        invalid = copy.deepcopy(draft)
        invalid.update(gap=gap, gaps=[] if gap == "none" else [gap])
        with pytest.raises(selection.SelectionError):
            selection.validate_selection(invalid, prepared, multiple_gaps=True)
    invalid = copy.deepcopy(draft)
    invalid["judgments"][1]["relation"] = "answers"
    with pytest.raises(selection.SelectionError):
        selection.validate_selection(invalid, prepared, multiple_gaps=True)
    legacy = {k: v for k, v in draft.items() if k != "gaps"}
    with pytest.raises(selection.SelectionError):
        selection.validate_selection(legacy, prepared)
