import copy
import json
from decimal import Decimal

import pytest

from backend.memory import evidence_selector as core
from backend.memory import evidence_selector_trial as api
from backend.memory import query_caretaker as planner
from backend.memory.caretaker_summary import SummaryError
from scripts import evidence_selection_trial as trial

QUESTION = "陶杯怎么上釉？"
AS_OF = "2031-05-07T00:00:00Z"


def record(rid, content, **kw):
    return {"record_id": rid, "content": content, "source_kind": "synthetic",
            "conversation_id": "room", "role": "assistant", "created_at": AS_OF, **kw}


def shadow(rows, passes=()):
    return {"original": {"memories": rows, "coverage": {"coverage_gap": False}},
            "passes": [{"memories": x} for x in passes], "brief": {"old": "untouched"},
            "plan": {"status": "planned", "intent": "procedure", "topic_terms": ["陶杯"]}}


def answer(prepared, relations=None, *, status="sufficient", gap="none"):
    return {"judgments": [{"candidate_id": cid,
        "relation": (relations or {}).get(cid, "answers"),
        "quote": row["text"] if (relations or {}).get(cid, "answers") in ("answers", "uncertain") else ""}
        for cid, row in prepared["local"].items()], "sufficiency": status, "gap": gap}


def test_only_short_source_cards_reach_selector_original_and_audit_are_preserved():
    raw = shadow([record("s1", "陶杯用透明釉。", private_debug="never send")])
    raw["original"]["giant_audit"] = "not sent" * 10000
    before = copy.deepcopy(raw)
    def selector(request):
        assert set(request) == {"question", "candidates"}
        assert "giant_audit" not in json.dumps(request) and "record_id" not in json.dumps(request)
        assert "never send" not in json.dumps(request)
        return answer(core.prepare(raw, query=QUESTION))
    result = core.refine_shadow(raw, query=QUESTION, as_of=AS_OF, selector=selector)
    assert raw == before and result["selector_attempts"] == 1
    assert result["status"] == "selected_requires_review"
    assert result["brief"]["items"][0]["text"] == "陶杯用透明釉。"


def test_round_robin_does_not_let_original_route_take_every_slot():
    raw = shadow([record("a" + str(i), "原路线陶杯" + str(i)) for i in range(10)],
                 [[record("b" + str(i), "补查陶杯" + str(i)) for i in range(10)],
                  [record("c" + str(i), "另一补查陶杯" + str(i)) for i in range(10)]])
    prepared = core.prepare(raw, query=QUESTION)
    assert [x["record_id"] for x in prepared["local"].values()][:6] == ["a0", "b0", "c0", "a1", "b1", "c1"]
    assert prepared["candidate_count"] == 30 and prepared["omitted_candidates"] == 18
    assert len(core.encoded(prepared["request"])) <= core.MAX_INPUT_BYTES


def test_same_id_dedup_and_changed_content_or_role_fail_closed():
    first = record("same", "陶杯上釉。")
    raw = shadow([first], [[copy.deepcopy(first)]])
    assert len(core.prepare(raw, query=QUESTION)["local"]) == 1
    for field, value in (("content", "变了"), ("role", "user"), ("content_truncated", True)):
        changed = copy.deepcopy(raw)
        changed["passes"][0]["memories"][0][field] = value
        with pytest.raises(core.SelectionError, match="source_changed"):
            core.prepare(changed, query=QUESTION)


def test_long_source_exact_quote_offsets_and_truncation_hash_are_honest():
    content = "前话。" * 300 + "陶杯约定使用透明釉。" + "后话。" * 300
    raw = shadow([record("s1", content + "\n[Echo Pact: content truncated]", content_truncated=True)])
    prepared = core.prepare(raw, query=QUESTION)
    draft = answer(prepared)
    draft["judgments"][0]["quote"] = "陶杯约定使用透明釉。"
    brief = core.make_brief(draft, prepared)
    ref = brief["items"][0]["references"][0]
    assert content[ref["quote_start"]:ref["quote_end"]] == brief["items"][0]["text"]
    assert ref["observed_content_sha256"] == core.digest(content)
    assert ref["source_truncated"] and "content_sha256" not in ref


def test_no_topic_words_still_reaches_semantic_selector_not_a_literal_gate():
    case = next(x for x in trial.load_cases("all") if x["case_id"] == "short-name-understanding")
    prepared = trial.prepare_case(case)
    assert "松鼠宠物动作" in prepared["request"]["candidates"][0]["text"]
    assert case["terms"][0] not in prepared["request"]["candidates"][0]["text"]


@pytest.mark.parametrize("mutation", [
    lambda d: d["judgments"][0].update(candidate_id="invented"),
    lambda d: d["judgments"][0].update(quote="陶杯用红色釉。"),
    lambda d: d["judgments"].append(copy.deepcopy(d["judgments"][0])),
    lambda d: d["judgments"].clear(),
    lambda d: d.update(answer="invented conclusion"),
    lambda d: d["judgments"][0].update(relation="irrelevant"),
    lambda d: d.update(sufficiency="insufficient", gap="missing_answer"),
])
def test_invented_quotes_ids_missing_judgments_and_inconsistent_status_are_rejected(mutation):
    prepared = core.prepare(shadow([record("s1", "陶杯用透明釉。")]), query=QUESTION)
    draft = answer(prepared)
    mutation(draft)
    with pytest.raises(core.SelectionError):
        core.make_brief(draft, prepared)


def test_repeated_phrase_needs_a_unique_larger_quote():
    prepared = core.prepare(shadow([record("s1", "陶杯：不烧。草稿写要烧，最终确认不烧。")]), query=QUESTION)
    draft = answer(prepared)
    draft["judgments"][0]["quote"] = "不烧"
    with pytest.raises(core.SelectionError, match="unique"):
        core.make_brief(draft, prepared)


def test_paired_emphasis_is_restored_as_an_exact_source_slice_not_model_text():
    content = "前话。\n> **陶杯必须先晾干。**\n然后上釉。\n后话。"
    hint = "陶杯必须先晾干。\n然后上釉。"
    quote, offset, method = core.resolve_quote(content, hint)
    assert quote == "**陶杯必须先晾干。**\n然后上釉。"
    assert content[offset:offset + len(quote)] == quote and method == "paired_emphasis_restored_from_source"
    prepared = core.prepare(shadow([record("s1", content)]), query=QUESTION)
    draft = answer(prepared)
    draft["judgments"][0]["quote"] = hint
    brief = core.make_brief(draft, prepared)
    assert brief["items"][0]["text"] == quote
    assert brief["items"][0]["references"][0]["quote_start"] == offset


@pytest.mark.parametrize("source,hint", [
    ("**陶杯不要烧制。**", "陶杯要烧制。"),
    ("**陶杯晾三天。**", "陶杯晾一天。"),
    ("**陶杯先晾干。**\n然后上釉。", "陶杯先晾干。 然后上釉。"),
    ("**陶杯晾干。**", "陶杯晾干!"),
    ("**陶杯晾干。** 和 **陶杯晾干。**", "陶杯晾干。"),
    ("aaa", "aa"),
    ("`**陶杯**`", "`陶杯`"),
    (r"\**陶杯**", r"\陶杯"),
    (r"**陶杯\**继续", r"陶杯\继续"),
])
def test_quote_recovery_never_changes_words_spacing_code_or_ambiguous_occurrences(source, hint):
    with pytest.raises(core.SelectionError):
        core.resolve_quote(source, hint)


def test_known_conflict_cannot_drop_one_side():
    prepared = core.prepare(shadow([
        record("s1", "陶杯晾一天。", conflict_group_id="g"),
        record("s2", "陶杯晾三天。", conflict_group_id="g")]), query=QUESTION)
    with pytest.raises(core.SelectionError, match="conflict"):
        core.make_brief(answer(prepared, {"c2": "irrelevant"}), prepared)


def test_exact_display_collapse_keeps_both_sources_but_not_other_dates_or_truncated_text():
    rows = [record("s1", "陶杯贴蓝签。", source_kind="chatgpt_official"),
            record("s2", "陶杯贴蓝签。", source_kind="browser_capture"),
            record("s3", "陶杯贴蓝签。", created_at="2031-06-07T00:00:00Z"),
            record("s4", "陶杯贴蓝签。", content_truncated=True)]
    prepared = core.prepare(shadow(rows), query=QUESTION)
    brief = core.make_brief(answer(prepared), prepared)
    assert len(brief["items"]) == 3 and brief["duplicate_display_count"] == 1
    assert len(brief["items"][0]["references"]) == 2
    assert "exact_text_group_is_not_event_or_mirror_identity" in brief["limitations"]


def test_full_brief_size_includes_references_and_omissions_require_more_evidence():
    rows = [record("x" * 240 + str(i), "陶杯：" + "长句" * 120,
                   conversation_id="r" * 256, source_kind="s" * 256, role="a" * 256) for i in range(10)]
    prepared = core.prepare(shadow(rows), query=QUESTION)
    brief = core.make_brief(answer(prepared), prepared)
    assert len(core.encoded(brief)) <= core.MAX_BRIEF_BYTES
    assert brief["omitted_selected"] > 0 and brief["needs_more_evidence"]


def test_unknown_coverage_does_not_turn_into_false_and_offtopic_does_not_pad():
    raw = shadow([record("s1", "陶杯真漂亮。")])
    raw["original"]["coverage"] = {}
    prepared = core.prepare(raw, query=QUESTION)
    brief = core.make_brief(answer(prepared, {"c1": "context"}, status="insufficient", gap="missing_answer"), prepared)
    assert brief["items"] == [] and brief["coverage_gap"] is None and brief["needs_more_evidence"]


@pytest.mark.parametrize("query", ["昨天陶杯怎么做？", "GPT房间的陶杯做法", "别找陶杯", "陶杯第一次上釉？", "我说过陶杯怎么上釉？"])
def test_constraints_and_disabled_model_never_call_selector(query):
    def forbidden(_):
        pytest.fail("model must not run")
    raw = shadow([record("s1", "陶杯用透明釉。")])
    result = core.refine_shadow(raw, query=query, as_of=AS_OF, selector=forbidden)
    assert result["selector_attempts"] == 0 and result["brief"] == raw["brief"]
    assert core.refine_shadow(raw, query=QUESTION, as_of=AS_OF)["status"] == "disabled"


def test_timeout_or_malformed_response_falls_back_once_without_error_body():
    raw = shadow([record("s1", "陶杯用透明釉。")])
    calls = []
    def broken(_):
        calls.append(1)
        raise RuntimeError("PRIVATE BODY")
    result = core.refine_shadow(raw, query=QUESTION, as_of=AS_OF, selector=broken)
    assert calls == [1] and result["brief"] == raw["brief"]
    assert result["status"] == "failed_original_brief_preserved" and "PRIVATE BODY" not in json.dumps(result)


def test_reference_fixture_exercises_contract_not_model_quality():
    for case in trial.load_cases("all"):
        prepared = trial.prepare_case(case)
        gold = {r["id"]: r for r in case["records"]}
        relations = {cid: gold[r["record_id"]]["relation"] for cid, r in prepared["local"].items()}
        draft = answer(prepared, relations, status=case["sufficiency"], gap=case["gap"])
        brief = core.make_brief(draft, prepared)
        assert trial.score(case, prepared, draft, brief)["passed"]
        serialized_request = json.dumps(prepared["request"])
        assert '"required"' not in serialized_request and '"relation"' not in serialized_request


@pytest.mark.parametrize("provider", ["venice", "openai"])
def test_existing_api_transports_are_bounded_and_cost_reserved_before_call(provider):
    request = trial.prepare_case(trial.load_cases("development")[0])["request"]
    payload = api.request_payload(request, provider)
    assert payload["store"] is False and "tools" not in payload and "previous_response_id" not in payload
    assert api.reservation(request, provider) > Decimal(0)
    calls = []
    class Client:
        def request(self, *args):
            assert calls == ["reserved"]
            calls.append("request")
            raise SummaryError("test failure")
    sha = core.digest(core.encoded(payload).decode())
    with pytest.raises(SummaryError):
        api.run_case(Client(), request, provider=provider, frozen_request_sha256=sha,
                     before_request=lambda _: calls.append("reserved"))
    assert calls == ["reserved", "request"]
    with pytest.raises(SummaryError, match="frozen"):
        api.run_case(Client(), request, provider=provider, frozen_request_sha256="0" * 64,
                     before_request=lambda _: pytest.fail("must not reserve"))


def test_response_retains_only_final_selection_and_usage():
    raw = {"model": api.openai.MODEL, "status": "completed", "output": [
        {"type": "reasoning", "summary": [{"text": "never save me"}]},
        {"type": "message", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": '{"judgments":[],"sufficiency":"insufficient","gap":"missing_answer"}'}]}],
        "usage": {"input_tokens": 100, "output_tokens": 50}}
    result = api.parse_response(raw, "openai")
    assert "never save me" not in json.dumps(result) and result["estimated_usd"] == "0.0014"
    raw["output"][1]["content"][0]["type"] = "refusal"
    with pytest.raises(SummaryError):
        api.parse_response(raw, "openai")
