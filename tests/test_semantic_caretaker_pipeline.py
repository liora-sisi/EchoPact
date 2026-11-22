import pytest

from backend.memory import semantic_caretaker_pipeline as core
from backend.memory import semantic_shadow as vectors
from backend.memory import semantic_shortlist as short
from backend.memory import evidence_packet as codec
from backend.memory.identity import set_agent_enabled
from test_semantic_shadow import FakeEncoder
from test_semantic_ordered_evidence import setup, QUERY, OLD, NEW
from test_semantic_shortlist import shortlist, reviewer

AS_OF = "2035-05-02T00:00:00Z"
PLAN = {"status": "planned", "intent": "topic", "topic_terms": ["白昼盒"]}


def forbidden(*a, **k):
    pytest.fail("disabled stage called")


def pipeline(tmp_path, specs=None, **kwargs):
    db, _, _, _ = setup(tmp_path, specs)
    encoder, index = FakeEncoder(), tmp_path / "index.sqlite3"
    vectors.build_index(db, "owner", index, encoder)
    return core.SemanticCaretaker(db, "owner", index, encoder, **kwargs)


def test_complete_pipeline_uses_real_sqlite_and_vector_index_with_three_callbacks(tmp_path):
    run = pipeline(tmp_path)
    seen = []
    def plan(q):
        seen.append("plan")
        assert q == QUERY
        return PLAN
    def first(request):
        seen.append("shortlist")
        assert set(request) == {"question", "candidates"}
        return shortlist(request)
    def last(request):
        seen.append("review")
        assert {c["text"] for c in request["candidates"]} == {OLD, NEW}
        assert "source_order_windows" in request
        return reviewer(request)
    before = vectors.file_digest(run.bridge.base.path)
    result = run.run(QUERY, as_of=AS_OF, planner=plan, shortlister=first, reviewer=last)
    assert result["status"] == "reviewed_requires_acceptance", result["status"]
    assert seen == ["plan", "shortlist", "review"]
    assert result["trace"]["model_calls"] == 3 and result["trace"]["hybrid_operations"] == 1
    assert result["trace"]["retrieval_operations"] <= 6
    assert result["audit"]["hybrid"]["semantic_shadow"]["status"] == "hybrid_candidates_only"
    assert codec.expand(result["brief"])["items"][0]["text"] == NEW
    assert len(core.selection.encoded(result["brief"])) <= 12000
    assert "audit" not in result["brief"] and not result["production_accepted"]
    assert vectors.file_digest(run.bridge.base.path) == before


@pytest.mark.parametrize("query", ["昨天白昼盒用来收什么？", "GPT房间的白昼盒", "白昼盒第一次收什么？", "白昼盒原话"])
def test_constrained_queries_bypass_models_vectors_and_scout(tmp_path, monkeypatch, query):
    run = pipeline(tmp_path)
    monkeypatch.setattr(run.hybrid, "recall", forbidden)
    monkeypatch.setattr(run, "object_runner", forbidden)
    result = run.run(query, as_of=AS_OF, planner=forbidden, shortlister=forbidden, reviewer=forbidden)
    assert result["status"] == "original_only"
    assert result["trace"]["model_calls"] == 0 and result["trace"]["retrieval_operations"] == 1


@pytest.mark.parametrize("plan", [None, {"status": "planned", "intent": "topic", "topic_terms": ["invented"]},
                                  {"status": "needs_clue", "intent": "unspecified", "topic_terms": []}])
def test_disabled_or_unusable_planner_preserves_exact_original(tmp_path, plan):
    run = pipeline(tmp_path)
    result = run.run(QUERY, as_of=AS_OF, planner=None if plan is None else lambda _: plan,
                     shortlister=forbidden, reviewer=forbidden)
    assert result["status"] == "original_only" and result["trace"]["hybrid_operations"] == 0
    assert result["brief"] == result["audit"]["query_shadow"]["brief"]


def test_object_comparison_scans_rebound_to_authoritative_windows(tmp_path):
    run = pipeline(tmp_path, [{"content": "陶杯《白叶》是绿色，陶杯《蓝叶》是橙色。"}])
    result = run.run("陶杯怎么区分？", as_of=AS_OF,
        planner=lambda _: {"status": "planned", "intent": "comparison", "topic_terms": ["陶杯"]},
        shortlister=shortlist)
    assert result["status"] == "shortlisted_context_unreviewed", result["status"]
    assert 1 <= result["trace"]["object_operations"] <= 2
    assert result["trace"]["retrieval_operations"] <= 6
    seed = result["audit"]["route_seeds"][1]
    assert seed["local"]
    assert all(row["fingerprint_basis"] == "authoritative_full_record" and row["source_truncated"] is False
               for row in seed["local"].values())
    run.bridge.base.verify(seed)


def test_object_scan_omissions_survive_rebinding_and_final_review(tmp_path, monkeypatch):
    run = pipeline(tmp_path, [{"content": "陶杯《白叶》是绿色，陶杯《蓝叶》是橙色。"}])
    original = core.scout.run_object_shadow
    def limited(*args, **kwargs):
        scanned = original(*args, **kwargs)
        scanned["prepared"]["candidate_count"] += 1
        scanned["prepared"]["omitted_candidates"] += 1
        return scanned
    monkeypatch.setattr(core.scout, "run_object_shadow", limited)
    result = run.run("陶杯怎么区分？", as_of=AS_OF,
        planner=lambda _: {"status": "planned", "intent": "comparison", "topic_terms": ["陶杯"]},
        shortlister=shortlist, reviewer=lambda req: {"judgments": [{"candidate_id": c["candidate_id"],
            "relation": "answers", "quote": c["text"]} for c in req["candidates"]],
            "sufficiency": "sufficient", "gap": "none", "gaps": []})
    assert result["status"] == "reviewed_requires_acceptance"
    brief = codec.expand(result["brief"])
    assert brief["needs_more_evidence"] and "earlier_retrieval_route_was_bounded" in brief["limitations"]


def test_hidden_catalog_window_of_selected_record_is_restored(tmp_path):
    run = pipeline(tmp_path, [{"content": "旧片段。新片段。"}, {"content": "另外的记录。"}])
    raw = run.bridge.base.gateway.recall({"query": "旧片段", "limit": 10, "as_of": AS_OF})
    template = run.bridge.prepare(raw, query="旧片段的用途？")
    row = next(r for r in template["local"].values() if r["record_id"] == "r0")
    first = {**row, "text": "旧片段。", "excerpt_start": 0}
    later = {**row, "text": "新片段。", "excerpt_start": 4}
    seed = core._seed(template, [first], total=2, coverage=None)
    run.bridge.base.verify(seed)
    result = run.bridge.run(seed, shortlister=shortlist,
        context_seed_builder=lambda focused: core.restore_selected_windows(focused, [first, later]))
    assert result["status"] == "shortlisted_context_unreviewed"
    focused = result["audit"]["focused"]["seed"]
    assert {r["text"] for r in focused["local"].values()} == {"旧片段。", "新片段。"}
    assert focused["coverage_gap"] is None


@pytest.mark.parametrize("change", ["drop", "other_record", "question"])
def test_context_builder_cannot_drop_original_or_add_unselected_unrelated_root(tmp_path, change):
    run = pipeline(tmp_path, [{"content": OLD}, {"content": NEW, "conversation_id": "other"}])
    raw = run.hybrid.recall({"query": QUERY, "limit": 10, "as_of": AS_OF})
    seed = run.bridge.prepare(raw, query=QUERY)
    assert len(seed["local"]) == 2
    def change_seed(focused):
        rows = list(focused["local"].values())
        if change == "drop": rows = []
        if change == "other_record": rows = list(seed["local"].values())
        out = core._seed(focused, rows, total=2, coverage=None)
        if change == "question":
            out["request"]["question"] = "别的问题？"
            out.pop("prepared_sha256")
            out["prepared_sha256"] = core.canonical_hash(out)
        return out
    result = run.bridge.run(seed, shortlister=shortlist, reviewer=forbidden, context_seed_builder=change_seed)
    assert result["status"] == "context_preparation_failed_original_preserved"
    assert result["brief"] == run.bridge.base.preview(seed)


def test_catalog_round_robin_dedup_and_complete_conflict_closure():
    # Pure catalog accounting; no assertion about retrieval/model quality.
    def row(i, start=0):
        return {"record_id": str(i), "excerpt_start": start, "text": "x", "observed_content_sha256": str(i),
                "source_kind": "synthetic", "role": "user", "observed_content_chars": 100,
                "conflict_group_id": None}
    base = {"binding": {}, "request": {"question": "question", "candidates": []}, "local": {}, "prepared_sha256": "old"}
    a = core._seed(base, [row(i) for i in range(12)], total=12, coverage=None)
    b = core._seed(base, [row(0, 90), *[row(i) for i in range(12, 23)]], total=12, coverage=False)
    seed, catalog = core.combine_seeds([a, b, a])
    assert len(seed["local"]) == 12 and len(catalog) == 24
    assert seed["candidate_count"] == 24 and seed["omitted_candidates"] == 12 and seed["coverage_gap"] is None
    focused = short.focused_seed(seed, shortlist(seed["request"]))
    restored = core.restore_selected_windows(focused, catalog)
    assert len(restored["local"]) == 2
    for r in catalog:
        if r["record_id"] in ("0", "1", "2"): r["conflict_group_id"] = "conflict"
    with pytest.raises(ValueError, match="closure_exceeds"):
        core.restore_selected_windows(focused, catalog)


@pytest.mark.parametrize("stage", ["planner", "shortlist", "review"])
def test_source_revocation_during_any_model_stage_fails_closed(tmp_path, stage):
    run = pipeline(tmp_path)
    def revoke(_):
        set_agent_enabled("owner", False, actor="test", db_path=str(run.bridge.base.path))
        raise RuntimeError("PRIVATE error must not leak")
    with pytest.raises(ValueError):
        run.run(QUERY, as_of=AS_OF, planner=revoke if stage == "planner" else lambda _: PLAN,
                shortlister=revoke if stage == "shortlist" else shortlist,
                reviewer=revoke if stage == "review" else reviewer)


def test_elapsed_boundary_stops_next_operation_after_blocking_callback(tmp_path):
    now = [0]
    run = pipeline(tmp_path, elapsed_limit=1, clock=lambda: now[0])
    def slow(_):
        now[0] = 2
        return PLAN
    result = run.run(QUERY, as_of=AS_OF, planner=slow, shortlister=forbidden, reviewer=forbidden)
    assert result["status"] == "time_budget_exhausted_original_preserved"
    assert result["trace"]["model_calls"] == result["trace"]["retrieval_operations"] == 1
    assert result["brief"] == result["audit"]["query_shadow"]["brief"]


def test_source_incoherence_falls_back_before_shortlisting(tmp_path, monkeypatch):
    run = pipeline(tmp_path)
    original = run.hybrid.recall
    def forged(args):
        raw = original(args)
        raw["memories"][0]["role"] = "tool"
        return raw
    monkeypatch.setattr(run.hybrid, "recall", forged)
    result = run.run(QUERY, as_of=AS_OF, planner=lambda _: PLAN, shortlister=forbidden)
    assert result["status"] == "source_validation_original_preserved"
    assert result["trace"]["shortlist"] == 0


def test_initial_failure_has_no_false_fallback_or_provider_error_body(tmp_path, monkeypatch):
    run = pipeline(tmp_path)
    def fail(*args): raise RuntimeError("PRIVATE")
    monkeypatch.setattr(type(run.bridge.base.gateway), "recall", fail)
    with pytest.raises(ValueError, match="^original_retrieval_unavailable$"):
        run.run(QUERY, as_of=AS_OF)


def test_exam_freezes_fiction_and_controls_without_seeding_targets(tmp_path):
    import json
    from scripts import semantic_pipeline_exam as exam
    output = tmp_path / "exam"
    plan = exam.prepare(output, None, encoder=FakeEncoder())
    assert len(plan["cases"]) == 3 and plan["max_reserved_usd"] == "1.05"
    records = json.loads((output / "synthetic.json").read_text(encoding="utf-8"))["records"]
    assert len(records) == 15 and all("expected" not in row and "selected" not in row for row in records)
    control = json.loads((output / "control-0.json").read_text(encoding="utf-8"))
    assert control["trace"]["model_calls"] == 0
    assert plan["implementation"]["source_files_sha256"]["scripts/semantic_pipeline_exam.py"]


def test_exam_rejects_nonfiction_and_extra_payload_fields():
    import json
    from scripts import semantic_pipeline_exam as exam
    fixture = json.loads(exam.FIXTURE.read_text(encoding="utf-8"))
    case = fixture["cases"][0]
    card = {"candidate_id": "c1", "source_kind": "synthetic", "role": "assistant",
            "excerpt_incomplete": False, "text": case["records"][0]["text"]}
    request = {"question": case["query"], "candidates": [card]}
    exam.verify_request(request, case, fixture, "shortlist")
    card["text"] = "Some unrelated private material."
    with pytest.raises(ValueError, match="not_frozen_fiction"):
        exam.verify_request(request, case, fixture, "shortlist")
    card["text"] = case["records"][0]["text"]
    card["private_debug"] = "forbidden"
    with pytest.raises(ValueError):
        exam.verify_request(request, case, fixture, "shortlist")
