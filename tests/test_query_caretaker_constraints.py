"""Routing acceptance: preserve restrictions, without claiming answer quality."""
import copy
import hashlib
import socket

import pytest

from backend.memory import caretaker_pipeline as pipeline
from backend.memory import query_caretaker as core
from backend.memory import records_v1
from scripts import caretaker_pipeline_challenge as exam

AS_OF = exam.AS_OF
RESTRICTED = [
    ("你送我的两只保温杯怎么区分？", "speaker_or_relationship"),
    ("我送你的两只保温杯怎么区分？", "speaker_or_relationship"),
    ("你当时让我喊你什么称呼？", "speaker_or_relationship"),
    ("我曾经让你喊我什么称呼？", "speaker_or_relationship"),
    ("你把保温杯送给我时说的是什么？", "speaker_or_relationship"),
    ("他帮她整理缝纫工具用的什么规则？", "speaker_or_relationship"),
    ("我对你提过的缝纫工具规则是什么？", "speaker_or_relationship"),
    ("头一回收纳缝纫工具用的是什么规则？", "order_or_count"),
    ("头一次收纳缝纫工具用了哪个盒子？", "order_or_count"),
    ("最初那两只保温杯怎么区分？", "order_or_count"),
    ("上回整理缝纫工具用了什么规则？", "order_or_count"),
    ("缝纫工具规则是什么日子定的？", "event_time"),
    ("缝纫工具规则是何时定的？", "event_time"),
    ("缝纫工具规则是哪一年定的？", "event_time"),
    ("两只保温杯有什么不同？别找邻居的。", "negation_or_revision"),
    ("两只保温杯有什么不同？不看车里的。", "negation_or_revision"),
    ("有什么不同意的缝纫工具规则？", "negation_or_revision"),
    ("两只保温杯有什么不同？只要GPT来源。", "source_scope"),
    ("两只保温杯有什么不同？要逐字原话。", "exact_wording"),
    ("昨天两只保温杯有什么不同？", "calendar_scope"),
]
ORDINARY = [
    "两只保温杯有什么不同？",
    "两只保温杯有何不同？",
    "两只保温杯哪里不同？",
    "两只保温杯有什么不一样？",
    "咱们那两只笔袋怎么区分来着？",
    "缝纫工具平时怎么收？",
    "我想问你一下，缝纫工具平时怎么收？",
    "以前我们说过陶艺要怎么做？",
]


def forbidden(*args, **kwargs):
    pytest.fail("restricted questions cannot enter a provider or object scan")


def packet():
    return {"memories": [{"record_id": "fictional-cup", "source_kind": "synthetic",
        "conversation_id": "fictional-room", "role": "user", "created_at": AS_OF,
        "content": "两只保温杯放在桌上；这里只是候选，不是问题的答案。"}],
        "coverage": {"coverage_gap": True}, "temporal_scope": {"status": "not_requested"}}


@pytest.mark.parametrize("query,reason", RESTRICTED)
def test_restricted_wording_bypasses_all_providers_and_explains_it_in_consumed_brief(query, reason):
    calls, source = [], packet()
    result = pipeline.run_pipeline(query, as_of=AS_OF, limit=10,
        runner=lambda arguments: calls.append(copy.deepcopy(arguments)) or copy.deepcopy(source),
        planner=forbidden, selector=forbidden, object_runner=forbidden, object_reviewer=forbidden)
    assert calls == [{"query": query, "limit": 10, "as_of": AS_OF}]
    assert result["route"] == "original"
    assert result["trace"]["planner_attempts"] == result["trace"]["evidence_review_attempts"] == 0
    assert result["trace"]["planner_status"].endswith("original_only")
    guard = result["brief"]["question_guard"]
    assert reason in guard["reasons"]
    assert guard["assessment"] == "bounded_cues_not_complete_language_understanding"
    assert "question_constraints_preserved_not_resolved_by_caretaker" in result["brief"]["limitations"]
    assert result["audit"]["query_shadow"]["original"] == source
    assert result["brief"]["coverage"]["coverage_gap"] is True
    assert core._size(result["brief"]) <= core.MAX_PAYLOAD_CHARS


@pytest.mark.parametrize("query", ORDINARY)
def test_ordinary_comparisons_and_conversational_prefaces_remain_eligible(query):
    assert core.eligibility(query, AS_OF) == "eligible"
    assert core.assess_question(query, AS_OF)["reasons"] == []


@pytest.mark.parametrize("query", ["两只保温杯有什么不同？", "两只保温杯有什么不一样？"])
def test_clear_comparison_accepts_a_literal_comparison_plan(query):
    plan = {"status": "planned", "intent": "comparison", "topic_terms": ["保温杯"]}
    assert core.validate_plan(plan, query) == plan
    assert core.candidate_queries(plan, query) == ["保温杯 区别", "保温杯"]


def test_multiple_constraints_are_not_cancelled_by_a_comparison_phrase():
    guard = core.assess_question("头一回你送我的保温杯有什么不同？别找GPT房间的。", AS_OF)
    assert set(guard["reasons"]) >= {
        "order_or_count", "speaker_or_relationship", "negation_or_revision", "source_scope"}


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return exam.create_corpus(tmp_path_factory.mktemp("constraint-routing") / "corpus")


def test_actual_readonly_gateway_keeps_question_and_source_intact_on_restricted_route(corpus, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(records_v1, "migrate_records_db", forbidden)
    before = hashlib.sha256(corpus.read_bytes()).hexdigest()
    for query, reason in RESTRICTED:
        result = pipeline.readonly_pipeline(corpus, exam.OWNER)(query, as_of=AS_OF,
            planner=forbidden, selector=forbidden, object_reviewer=forbidden)
        original = result["audit"]["query_shadow"]
        assert original["queries"] == original["attempted_queries"] == [query]
        assert result["trace"]["retrieval_operations"] == 1
        assert reason in result["brief"]["question_guard"]["reasons"]
    assert hashlib.sha256(corpus.read_bytes()).hexdigest() == before


def test_unblocked_comparison_reaches_both_ambiguous_pairs_in_actual_corpus(corpus, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", forbidden)
    case = copy.deepcopy(exam.load_cases()[1])
    case["query"] = "那两只保温杯有什么不同？"
    before = hashlib.sha256(corpus.read_bytes()).hexdigest()
    result = pipeline.readonly_pipeline(corpus, exam.OWNER)(case["query"], as_of=AS_OF,
        planner=lambda _: case["plan"], selector=forbidden,
        object_reviewer=lambda request: exam.reference_draft(case, request))
    assert result["route"] == "object"
    assert exam.score(case, result)["passed"]
    assert result["trace"]["planner_attempts"] == result["trace"]["evidence_review_attempts"] == 1
    assert hashlib.sha256(corpus.read_bytes()).hexdigest() == before
