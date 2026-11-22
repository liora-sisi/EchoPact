import copy
import json

import pytest

from backend.memory import semantic_shortlist as core
from backend.memory import semantic_shortlist_trial as api
from backend.memory import semantic_evidence as windows
from backend.memory import evidence_packet as codec
from backend.memory.identity import set_agent_enabled
from test_semantic_ordered_evidence import setup, all_items, QUERY, OLD, NEW


def bridge_for(db, ordered):
    return core.ShortlistBridge(db, "owner", source_sha256=ordered.base.source_sha256)


def shortlist(request, inspect=("c1",), defer=()):
    return {"inspect": list(inspect), "defer": list(defer), "discard": [row["candidate_id"]
            for row in request["candidates"] if row["candidate_id"] not in {*inspect, *defer}]}


def reviewer(request):
    return {"judgments": [{"candidate_id": card["candidate_id"], "relation": "answers" if NEW in card["text"] else "context",
                           "quote": NEW if NEW in card["text"] else ""} for card in request["candidates"]],
            "sufficiency": "sufficient", "gap": "none", "gaps": []}


def test_eighth_candidate_can_receive_context_without_eager_expansion_losing_it(tmp_path):
    specs = [{"content": OLD if i == 14 else NEW if i == 15 else "别的房间记录" + str(i),
              "conversation_id": "room" + str(i // 2),
              "branch_memberships": [{"branch_id": "main", "position": i % 2}]} for i in range(20)]
    db, ordered, _, raw = setup(tmp_path, specs, selected=tuple("r" + str(i) for i in range(0, 20, 2)))
    bridge = bridge_for(db, ordered)
    seed = bridge.prepare(raw, query=QUERY)
    before = copy.deepcopy(seed)
    assert ordered.prepare(raw, query=QUERY)["adjacency"]["deferred"]
    seen = []
    def inspect(request):
        seen.append(request)
        assert len(request["candidates"]) == 10 and request["candidates"][7]["text"] == OLD
        return shortlist(request, ("c8",))
    def review(request):
        seen.append(request)
        assert {c["text"] for c in request["candidates"]} == {OLD, NEW}
        assert request["source_order_windows"] == [{"candidate_ids": ["c1", "c2"]}]
        return reviewer(request)
    result = bridge.run(seed, shortlister=inspect, reviewer=review)
    assert len(seen) == 2 and result["calls"] == {"shortlist": 1, "review": 1}
    assert result["status"] == "reviewed_requires_acceptance" and not result["production_accepted"]
    assert seed == before == result["audit"]["seed"]
    assert codec.expand(result["brief"])["items"][0]["text"] == NEW
    assert len(core.selection.encoded(result["brief"])) <= 12000
    assert sum(len(core.selection.encoded(request)) for request in seen) <= 48000
    assert windows.file_digest(db) == bridge.base.source_sha256


@pytest.mark.parametrize("mutation", [
    lambda d: d["inspect"].append("unknown"),
    lambda d: d["defer"].append("c1"),
    lambda d: d.update(answer="must not generate facts"),
    lambda d: d.update(inspect=[]),
    lambda d: d.update(inspect=["c1", "c1", "c1"]),
])
def test_bad_shortlist_never_calls_reviewer_or_replaces_original(tmp_path, mutation):
    db, ordered, _, raw = setup(tmp_path)
    bridge = bridge_for(db, ordered)
    seed = bridge.prepare(raw, query=QUERY)
    draft = shortlist(seed["request"])
    mutation(draft)
    result = bridge.run(seed, shortlister=lambda _: draft, reviewer=lambda _: pytest.fail("bad shortlist"))
    assert result["calls"] == {"shortlist": 1, "review": 0}
    assert result["status"] == "shortlist_failed_original_preserved"
    assert result["brief"] == bridge.base.preview(seed)


def test_no_shortlist_is_not_a_claim_that_history_has_no_answer(tmp_path):
    db, ordered, _, raw = setup(tmp_path)
    bridge = bridge_for(db, ordered)
    seed = bridge.prepare(raw, query=QUERY)
    result = bridge.run(seed, shortlister=lambda r: shortlist(r, ()), reviewer=lambda _: pytest.fail("none selected"))
    assert result["status"] == "no_shortlist_original_preserved"
    assert result["brief"]["evidence_status"] == "not_assessed" and result["brief"]["needs_more_evidence"]


def test_disabled_and_missing_reviewer_have_honest_status(tmp_path):
    db, ordered, _, raw = setup(tmp_path)
    bridge = bridge_for(db, ordered)
    seed = bridge.prepare(raw, query=QUERY)
    assert bridge.run(seed)["calls"] == {"shortlist": 0, "review": 0}
    result = bridge.run(seed, shortlister=shortlist)
    assert result["status"] == "shortlisted_context_unreviewed" and result["calls"]["review"] == 0
    assert codec.expand(result["brief"])["evidence_status"] == "not_assessed"
    assert {item["text"] for item in all_items(codec.expand(result["brief"]))} == {OLD, NEW}


def test_all_windows_of_inspected_record_are_kept_including_unselected_correction(tmp_path):
    text = "天气。" * 1500 + OLD + "杂记。" * 200 + NEW
    db, ordered, _, raw = setup(tmp_path, [{"content": text}])
    bridge = bridge_for(db, ordered)
    raw["semantic_shadow"] = {"version": windows.INDEX_VERSION, "status": "hybrid_candidates_only", "selected": {
        "r0": {"content_sha256": core.selection.digest(text), "spans": [
            {"start": text.index(OLD), "end": text.index(OLD) + len(OLD)},
            {"start": text.index(NEW), "end": len(text)}]}}}
    seed = bridge.prepare(raw, query=QUERY)
    old_cid = next(cid for cid,row in seed["local"].items() if OLD in row["text"])
    result = bridge.run(seed, shortlister=lambda r: shortlist(r,(old_cid,)), reviewer=reviewer)
    assert result["status"] == "reviewed_requires_acceptance"
    focused = result["audit"]["focused"]
    assert len(focused["seed"]["local"]) == len(seed["local"]) == 3
    assert any(NEW in row["text"] for row in focused["local"].values())


def test_known_conflict_closure_cannot_be_removed_by_first_model(tmp_path):
    db, ordered, _, raw = setup(tmp_path, [{"content": str(i)} for i in range(3)], selected=("r0","r1","r2"))
    seed = bridge_for(db,ordered).prepare(raw,query=QUERY)
    for row in seed["local"].values(): row["conflict_group_id"] = "known"
    with pytest.raises(ValueError, match="conflict_group_exceeds"):
        core.focused_seed(seed,shortlist(seed["request"]))
    seed["local"]["c3"]["conflict_group_id"] = None
    focused=core.focused_seed(seed,shortlist(seed["request"]))
    assert {row["record_id"] for row in focused["local"].values()} == {"r0","r1"}


def test_unresolved_candidates_remain_reported_after_second_model(tmp_path):
    db, ordered, _, raw = setup(tmp_path,[{"content":OLD},{"content":NEW},{"content":"别的相关线索","conversation_id":"other"}],selected=("r0","r2"))
    bridge=bridge_for(db,ordered)
    seed=bridge.prepare(raw,query=QUERY)
    result=bridge.run(seed,shortlister=lambda r:shortlist(r,("c1",),("c2",)),reviewer=reviewer)
    brief=codec.expand(result["brief"])
    assert brief["shortlist"]["deferred_cards"] == 1 and brief["needs_more_evidence"]
    assert brief["shortlist"]["is_answer"] is False


def test_context_still_over_budget_preserves_entire_original_seed(tmp_path):
    db, ordered, _, raw=setup(tmp_path,[{"content":OLD,"branch_memberships":[{"branch_id":str(i),"position":0} for i in range(5)]},
                                       {"content":"other","conversation_id":"elsewhere"}],selected=("r0","r1"))
    bridge=bridge_for(db,ordered)
    seed=bridge.prepare(raw,query=QUERY)
    result=bridge.run(seed,shortlister=shortlist,reviewer=lambda _:pytest.fail("context deferred"))
    assert result["status"] == "context_budget_deferred_original_preserved"
    assert result["brief"] == bridge.base.preview(seed) and len(result["audit"]["seed"]["local"]) == 2


@pytest.mark.parametrize("stage", ["shortlist","review"])
def test_revocation_during_either_stage_fails_closed(tmp_path,stage):
    db,ordered,_,raw=setup(tmp_path)
    bridge=bridge_for(db,ordered)
    seed=bridge.prepare(raw,query=QUERY)
    def fail(_):
        set_agent_enabled("owner",False,actor="test",db_path=str(db))
        raise RuntimeError("network failure")
    with pytest.raises(ValueError):
        bridge.run(seed,shortlister=fail if stage=="shortlist" else shortlist,
                   reviewer=fail if stage=="review" else reviewer)


def test_second_model_error_returns_original_not_unchecked_shortlist(tmp_path):
    db,ordered,_,raw=setup(tmp_path)
    bridge=bridge_for(db,ordered)
    seed=bridge.prepare(raw,query=QUERY)
    result=bridge.run(seed,shortlister=shortlist,reviewer=lambda _:{"invented":"answer"})
    assert result["status"] == "review_failed_original_preserved"
    assert result["brief"] == bridge.base.preview(seed) and result["calls"]["review"] == 1


def test_shortlist_transport_has_no_answer_schema_or_extra_private_fields(tmp_path):
    db,ordered,_,raw=setup(tmp_path)
    seed=bridge_for(db,ordered).prepare(raw,query=QUERY)
    payload=api.request_payload(seed["request"],"openai")
    assert payload["store"] is False and payload["max_output_tokens"] == 768
    assert set(payload["text"]["format"]["schema"]["properties"]) == {"inspect","defer","discard"}
    seed["request"]["candidates"][0]["private_debug"]="must not be sent"
    with pytest.raises(ValueError):api.request_payload(seed["request"],"openai")


def test_two_stage_exam_freezes_fiction_and_reserves_both_calls(tmp_path):
    from scripts import semantic_shortlist_exam as exam
    output=tmp_path/"exam"
    plan=exam.prepare(output)
    assert len(plan["cases"]) == 5 and plan["cases"][0]["eager_context_deferred"]
    assert all(row["second_max_usd"] == "0.20" for row in plan["requests"].values())
    fixture=json.loads(exam.FIXTURE.read_text(encoding="utf-8"))
    case=fixture["cases"][0]
    seed=json.loads((output/(case["id"]+".seed.json")).read_text(encoding="utf-8"))
    exam.verify_fiction(seed,case,fixture,first=True)
    seed["local"]["c8"]["text"] = seed["request"]["candidates"][7]["text"] = "Unapproved real conversation."
    with pytest.raises(ValueError,match="frozen_fiction"):
        exam.verify_fiction(seed,case,fixture,first=True)
