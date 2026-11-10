import copy
import json

import pytest

from backend.memory import query_caretaker as core
from backend.memory import openai_query_trial as api
from backend.memory.caretaker_summary import SummaryError

QUESTION = "以前我们说过陶艺要怎么做？"
PLAN = {"status": "planned", "intent": "procedure", "topic_terms": ["陶艺"]}


def packet(text="陶艺约定：烧制前晾干。", rid="s1", date_status="not_requested"):
    return {"memories": [{"record_id": rid, "source_kind": "synthetic", "conversation_id": "room",
        "content": text, "created_at": "2031-05-06T00:00:00Z", "role": "assistant"}],
        "temporal_scope": {"status": date_status}, "coverage": {"coverage_gap": False},
        "adaptive_recall": {"query_passes_used": 1, "passes": []}}


def test_original_route_is_first_provider_gets_only_question_and_budget_is_bounded():
    calls, original = [], packet()
    def runner(args):
        assert set(args) == {"query", "limit", "as_of"}
        assert args["limit"] == 10 and args["as_of"] == "2031-05-07T00:00:00Z"
        calls.append(args["query"])
        return copy.deepcopy(original)
    def planner(question):
        assert calls == [QUESTION] and question == QUESTION
        return PLAN
    result = core.run_shadow(QUESTION, as_of="2031-05-07T00:00:00Z", limit=10, runner=runner, planner=planner)
    assert calls == [QUESTION, "陶艺 规则", "陶艺 格式"]
    assert result["original"] == original and result["planner_attempts"] == 1
    assert result["retrieval_calls"] == 3
    assert len(result["brief"]["items"]) == 1  # Same ID is not repeated.
    assert result["brief"]["items"][0]["text"] == original["memories"][0]["content"]


@pytest.mark.parametrize("query", ["昨天陶艺做了什么？", "2031年5月6日陶艺怎么做？", "我说过陶艺怎么做？",
    "Codex房间的陶艺规则", "别找陶艺，找木工", "陶艺第一次是在哪天", "陶艺没做成吗？", '陶艺的原话“晾干”'])
def test_constraints_never_disappear_through_rewriting(query):
    calls = []
    def forbidden(_):
        pytest.fail("constrained question must not be sent to model")
    result = core.run_shadow(query, as_of="2031-05-07T00:00:00Z", limit=10,
                             runner=lambda args: calls.append(args) or packet(), planner=forbidden)
    assert len(calls) == 1 and result["planner_attempts"] == 0
    assert result["planner_status"].endswith("original_only")


@pytest.mark.parametrize("plan", [
    {**PLAN, "agent_id": "other"}, {**PLAN, "topic_terms": ["蓝灯陶坊"]},
    {**PLAN, "topic_terms": ["陶艺", "陶艺"]}, {**PLAN, "topic_terms": ["陶艺 OR *"]},
    {**PLAN, "status": "needs_clue"}, {**PLAN, "intent": "comparison"},
    {**PLAN, "topic_terms": ["以前"]}, {**PLAN, "topic_terms": ["陶艺", "说过", "怎么", "我们"]},
])
def test_invented_facts_commands_identity_and_unbounded_plans_are_rejected(plan):
    with pytest.raises(core.PlannerError):
        core.validate_plan(plan, QUESTION)


def test_disabled_or_failed_provider_preserves_original_without_error_body():
    original = packet()
    def broken(_):
        raise RuntimeError("private failure details")
    for planner in (None, broken, lambda _: {"wrong": "schema"}):
        calls = []
        result = core.run_shadow(QUESTION, as_of="2031-05-07T00:00:00Z", limit=10,
                                 runner=lambda args: calls.append(args) or copy.deepcopy(original), planner=planner)
        assert result["original"] == original and len(calls) == 1
        assert "private failure" not in json.dumps(result)


def test_failed_extra_retrieval_is_counted_even_when_original_is_restored():
    calls = []
    def runner(args):
        calls.append(args)
        if len(calls) > 1:
            raise RuntimeError("do not leak")
        return packet()
    result = core.run_shadow(QUESTION, as_of="2031-05-07T00:00:00Z", limit=10,
                             runner=runner, planner=lambda _: PLAN)
    assert result["planner_status"] == "failed_original_preserved"
    assert result["passes"] == [] and result["retrieval_calls"] == 2
    assert len(result["attempted_queries"]) == 2


def test_changed_source_or_new_calendar_scope_fails_back():
    for later in (packet("Different content same ID"), packet(date_status="resolved_calendar_scope")):
        responses = iter([packet(), later, later])
        result = core.run_shadow(QUESTION, as_of="2031-05-07T00:00:00Z", limit=10,
                                 runner=lambda _: next(responses), planner=lambda _: PLAN)
        assert result["planner_status"] == "failed_original_preserved"


def test_excerpts_use_exact_offsets_and_entire_short_packet_is_bounded():
    original = packet("合成闲话。" * 250 + "陶艺规则：烧制前晾干。" + "后文。" * 100)
    passes = [packet("陶艺格式：" + "合成步骤。" * 250, f"s{i}") for i in range(2, 12)]
    result = core.evidence_brief(original, passes, query=QUESTION, plan=PLAN)
    assert core._size(result) <= 8000 and result["content_chars"] <= 2400 and len(result["items"]) <= 6
    assert result["excluded_count"] > 0
    source = {p["memories"][0]["record_id"]: p["memories"][0]["content"] for p in [original, *passes]}
    for item in result["items"]:
        assert item["text"] == source[item["record_id"]][item["excerpt_start"]:item["excerpt_end"]]
        assert "陶艺" in item["text"]


def response(plan=PLAN):
    return {"model": api.transport.MODEL, "status": "completed", "output": [
        {"type": "reasoning", "summary": [{"text": "do not retain this"}]},
        {"type": "message", "role": "assistant", "status": "completed", "content": [
            {"type": "output_text", "text": json.dumps(plan)}]}], "usage": {"input_tokens": 500, "output_tokens": 50}}


def test_api_request_contains_question_only_no_records_history_tools_or_answer():
    request = api.request_payload(QUESTION)
    assert json.loads(request["input"][1]["content"]) == {"question": QUESTION}
    assert len(request["input"]) == 2 and request["store"] is False
    assert "tools" not in request and "previous_response_id" not in request
    result = api.parse_response(response(), QUESTION)
    assert result["plan"] == PLAN and result["estimated_usd"] == "0.003"
    assert "do not retain" not in json.dumps(result)


@pytest.mark.parametrize("change", [
    lambda r: r.update(status="incomplete"), lambda r: r.update(model="other"),
    lambda r: r["output"][1].update(type="function_call"),
    lambda r: r["output"][1]["content"][0].update(type="refusal"),
    lambda r: r["output"][1]["content"][0].update(text="not JSON"),
])
def test_bad_provider_response_is_never_a_plan(change):
    raw = response()
    change(raw)
    with pytest.raises(SummaryError):
        api.parse_response(raw, QUESTION)


def test_reservation_is_durable_before_api_and_no_retry_on_transport_failure():
    calls = []
    class Client:
        def request(self, *args):
            assert calls == ["reserved"]
            calls.append("attempted")
            raise SummaryError("synthetic network failure")
    with pytest.raises(SummaryError):
        api.run_query(Client(), QUESTION, before_request=lambda _: calls.append("reserved"))
    assert calls == ["reserved", "attempted"]


def test_secret_like_or_multiline_input_is_stopped_before_api():
    for query in ("sk-" + "a" * 40, "a question\nwith archive", "x" * 301):
        with pytest.raises(core.PlannerError):
            api.request_payload(query)
