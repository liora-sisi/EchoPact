import copy
import hashlib
import json
import socket

import pytest

from backend.memory import caretaker_pipeline as core
from backend.memory import query_caretaker as query_core
from backend.memory import records_v1
from scripts import caretaker_pipeline_exam as exam

AS_OF = exam.AS_OF
QUERY = "陶杯怎么收纳？"
PLAN = {"status": "planned", "intent": "procedure", "topic_terms": ["陶杯"]}


def row(text="陶杯收纳规则：放蓝盒。", *, position=0):
    return {"record_id": "s1", "source_kind": "synthetic", "conversation_id": "room", "role": "user",
            "created_at": "2032-03-04T00:00:00Z", "content": text,
            "branch_memberships": [{"branch_id": "main", "position": position}]}


def packet(rows=None, scope="not_requested"):
    return {"memories": [row()] if rows is None else rows, "coverage": {"coverage_gap": False},
            "temporal_scope": {"status": scope}, "adaptive_recall": {"query_passes_used": 1, "passes": []}}


def select(request):
    return {"judgments": [{"candidate_id": c["candidate_id"], "relation": "answers", "quote": c["text"]}
                          for c in request["candidates"]], "sufficiency": "sufficient", "gap": "none"}


def forbidden(*args, **kwargs):
    pytest.fail("disabled stage must not run")


@pytest.mark.parametrize("query", [QUERY, "昨天陶杯放哪了？", "GPT房间的陶杯收纳", "陶杯第一次放在哪？"])
def test_disabled_route_is_exact_original_shadow_and_only_one_retrieval(query):
    calls = []
    baseline = query_core.run_shadow(query, as_of=AS_OF, limit=10, runner=lambda _: packet())
    result = core.run_pipeline(query, as_of=AS_OF, limit=10,
        runner=lambda a: calls.append(a) or packet(), selector=forbidden, object_runner=forbidden, object_reviewer=forbidden)
    assert result["brief"] == baseline["brief"] and len(calls) == 1
    assert result["trace"]["planner_attempts"] == result["trace"]["evidence_review_attempts"] == 0
    assert result["production_accepted"] is False


@pytest.mark.parametrize("query", ["昨天陶杯放哪了？", "GPT房间的陶杯收纳", "别搜陶杯", "陶杯的原话是什么？"])
def test_constraints_bypass_every_provider_and_object_scan(query):
    result = core.run_pipeline(query, as_of=AS_OF, limit=10, runner=lambda _: packet(),
        planner=forbidden, selector=forbidden, object_runner=forbidden, object_reviewer=forbidden)
    assert result["route"] == "original" and result["trace"]["retrieval_operations"] == 1


@pytest.mark.parametrize("plan", [
    {"status": "needs_clue", "intent": "unspecified", "topic_terms": []},
    {"status": "no_rewrite", "intent": "unspecified", "topic_terms": []},
    {**PLAN, "agent_id": "other"}, {**PLAN, "topic_terms": ["invented"]},
])
def test_no_usable_plan_never_falls_through_to_another_model(plan):
    result = core.run_pipeline(QUERY, as_of=AS_OF, limit=10, runner=lambda _: packet(), planner=lambda _: plan,
        selector=forbidden, object_runner=forbidden, object_reviewer=forbidden)
    assert result["route"] == "original" and result["trace"]["evidence_review_attempts"] == 0


def test_general_pipeline_passes_only_question_then_short_source_cards():
    calls, provider = [], []
    def planner(q):
        assert calls == [QUERY] and q == QUERY
        provider.append("planner")
        return PLAN
    def selector(request):
        assert set(request) == {"question", "candidates"}
        assert all(set(c) == {"candidate_id", "source_kind", "role", "text", "excerpt_incomplete"}
                   for c in request["candidates"])
        provider.append("selector")
        return select(request)
    result = core.run_pipeline(QUERY, as_of=AS_OF, limit=10,
        runner=lambda a: calls.append(a["query"]) or packet(), planner=planner,
        selector=selector, object_runner=forbidden, object_reviewer=forbidden)
    assert provider == ["planner", "selector"] and calls == [QUERY, "陶杯 规则", "陶杯 格式"]
    assert result["status"] == "selected_requires_review" and result["route"] == "general"
    assert result["trace"]["evidence_review_attempts"] == 1
    assert "audit" not in result["brief"] and "original" not in result["brief"]


def object_setup():
    query = "陶杯怎么区分？"
    plan = {"status": "planned", "intent": "comparison", "topic_terms": ["陶杯"]}
    return query, plan, packet([row("陶杯《白叶》是白色，陶杯《蓝叶》是蓝色。")])


def test_object_pipeline_chooses_one_reviewer_not_both_and_counts_all_operations():
    query, plan, source = object_setup()
    events = []
    def reviewer(request):
        assert "source_order_windows" in request
        events.append("review")
        return select(request)
    result = core.run_pipeline(query, as_of=AS_OF, limit=10, runner=lambda _: copy.deepcopy(source),
        planner=lambda _: plan, selector=forbidden,
        object_runner=lambda _: copy.deepcopy(source), object_reviewer=reviewer)
    assert result["status"] == "selected_requires_review" and events == ["review"]
    assert result["trace"] == {"base_operations": 3, "object_operations": 1, "retrieval_operations": 4,
        "planner_attempts": 1, "evidence_review_attempts": 1, "planner_status": "planned", "route": "object"}
    assert result["brief"]["needs_more_evidence"] is True


@pytest.mark.parametrize("missing", ["runner", "reviewer"])
def test_partial_object_configuration_does_not_use_general_selector(missing):
    query, plan, source = object_setup()
    result = core.run_pipeline(query, as_of=AS_OF, limit=10, runner=lambda _: source, planner=lambda _: plan,
        selector=forbidden, object_runner=None if missing=="runner" else forbidden,
        object_reviewer=None if missing=="reviewer" else forbidden)
    assert result["status"] == "object_route_not_enabled" and result["trace"]["object_operations"] == 0


@pytest.mark.parametrize("change", ["content", "role", "created_at"])
def test_changed_sources_across_stages_are_stopped_before_review(change):
    query, plan, source = object_setup()
    changed = copy.deepcopy(source)
    changed["memories"][0][change] = {"content": "陶杯《白叶》是红色，陶杯《蓝叶》是蓝色。",
        "role": "assistant", "created_at": "2032-03-03T00:00:00Z"}[change]
    result = core.run_pipeline(query, as_of=AS_OF, limit=10, runner=lambda _: source, planner=lambda _: plan,
        selector=forbidden, object_runner=lambda _: changed, object_reviewer=forbidden)
    assert result["status"] == "source_validation_fallback" and result["trace"]["evidence_review_attempts"] == 0


def test_consistent_truncated_prefix_is_not_a_changed_source_but_a_divergent_prefix_is():
    short = {**row("陶杯"), "content_truncated": True}
    core._source_coherence([packet([short]), packet()])
    core._source_coherence([packet(), packet([short])])
    with pytest.raises(core.PipelineError):
        core._source_coherence([packet([{**short, "content": "木杯"}]), packet()])


def test_provider_failure_keeps_previous_brief_once_and_no_error_text():
    calls=[]
    def broken(_):
        calls.append(1)
        raise RuntimeError("PRIVATE provider body")
    result = core.run_pipeline(QUERY, as_of=AS_OF, limit=10, runner=lambda _: packet(), planner=lambda _: PLAN,
                               selector=broken)
    assert calls == [1] and result["status"] == "failed_original_brief_preserved"
    assert result["brief"] == result["audit"]["query_shadow"]["brief"]
    assert "PRIVATE" not in json.dumps(result)


def test_initial_failure_does_not_invent_a_fallback_or_expose_error_body():
    with pytest.raises(core.PipelineError, match="^original_retrieval_unavailable$"):
        core.run_pipeline(QUERY, as_of=AS_OF, limit=10, runner=lambda _: (_ for _ in ()).throw(RuntimeError("PRIVATE")))


@pytest.fixture
def synthetic_db(tmp_path):
    return exam.create_corpus(tmp_path/"synthetic")


@pytest.mark.parametrize("index", [0, 1])
def test_fresh_fixed_cases_through_actual_sqlite_readonly_and_both_routes(synthetic_db, monkeypatch, index):
    before = hashlib.sha256(synthetic_db.read_bytes()).hexdigest()
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(records_v1, "migrate_records_db", forbidden)
    case = exam.load_cases()[index]
    run = core.readonly_pipeline(synthetic_db, exam.OWNER)
    result = run(case["query"], as_of=AS_OF, planner=lambda _: case["plan"],
                 selector=lambda q: exam.reference_draft(case,q), object_reviewer=lambda q: exam.reference_draft(case,q))
    assert exam.score(case,result)["passed"]
    assert hashlib.sha256(synthetic_db.read_bytes()).hexdigest() == before
    assert result["trace"]["planner_attempts"] == result["trace"]["evidence_review_attempts"] == 1


@pytest.mark.parametrize("index", [0, 1])
def test_outsider_gets_no_source_on_either_path_and_no_evidence_callback(synthetic_db, index):
    case = exam.load_cases()[index]
    result = core.readonly_pipeline(synthetic_db, exam.OUTSIDER)(case["query"], as_of=AS_OF,
        planner=lambda _: case["plan"], selector=forbidden, object_reviewer=forbidden)
    assert result["brief"]["items"] == [] and result["trace"]["evidence_review_attempts"] == 0


def test_gold_is_excluded_from_imported_source_and_model_packets(synthetic_db):
    package=json.loads((synthetic_db.parent/"synthetic-source.json").read_text(encoding="utf-8"))
    assert all(not any(k in r for k in ("selected","required","plan","expected_route")) for r in package["records"])
