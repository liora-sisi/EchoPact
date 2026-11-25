"""No-network transport, scope, provenance and first-attempt protection tests."""
import copy
from decimal import Decimal
import json

import pytest

from backend.memory.caretaker_summary import SummaryError
from scripts import prepare_summary_comprehensive_comparison as exam
from scripts import run_summary_comprehensive_comparison as live
from scripts.prepare_summary_fidelity_comparison import encoded


@pytest.fixture
def bundle():
    packet = {"schema": "echo-summary-private-excerpts-v1", "captured_at": "2034-01-01T00:00:00+08:00",
              "source_context": copy.deepcopy(exam.PRIVATE_CONTEXT), "cases": [
        {"case_id": f"private-stub-{i}", "category": "explicit_adult_excerpt", "focus": "synthetic plumbing stub",
         "records": [{"record_id": f"local-excerpt-stub-{i}", "recorded_at": None,
                      "source_kind": exam.PRIVATE_KIND, "content": "[Synthetic plumbing stub] 花开了吗？"}],
         "review_targets": ["question, not confirmation"], "forbidden_claims": ["inferred date"]}
        for i in range(3)]}
    return exam.build_bundle(exam.load_cases(), packet)


def answers(bundle):
    return {"cases": [{"case_id": c["case_id"], "summary": {"status": "insufficient", "facts": [],
            "uncertainties": ["Synthetic test response only."], "refusal_reason": None}}
            for c in bundle["writer_inputs.json"]["cases"]]}


def authorization(bundle):
    return {"scope": live.scope_for(bundle), "user_authorized": True,
            "authorization_id": "liora-comprehensive-20261125"}


def test_same_twelve_cases_prompt_and_schema_without_review_answers(bundle):
    payloads = {p: live.payload_for(bundle, p) for p in live.PROVIDERS}
    shared = payloads["openai"]["input"]
    assert all(payloads[p]["messages"] == shared for p in ("venice", "deepseek"))
    assert len(json.loads(shared[1]["content"])["cases"]) == 12
    assert payloads["openai"]["store"] is False
    assert payloads["venice"]["venice_parameters"]["enable_web_search"] == "off"
    assert payloads["deepseek"]["thinking"] == {"type": "disabled"}
    assert payloads["deepseek"]["response_format"] == {"type": "json_object"}
    changed = copy.deepcopy(bundle)
    changed["reviewer_sheet.json"]["cases"][0]["review_targets"] = ["GOLD_MUST_NOT_SEND"]
    assert live.payload_for(changed, "openai") == payloads["openai"]
    for p in live.PROVIDERS:
        assert Decimal("0") < live.reservation(payloads[p], p) < live.PROVIDERS[p]["ceiling"]


@pytest.mark.parametrize("provider", list(live.PROVIDERS))
def test_only_final_text_and_usage_are_retained_not_reasoning(bundle, provider):
    content = json.dumps(answers(bundle))
    if provider == "openai":
        raw = {"model": live.PROVIDERS[provider]["model"], "status": "completed", "usage": {"input_tokens": 10,"output_tokens":20},
               "output": [{"type":"reasoning", "summary":[{"text":"SECRET_REASONING"}]},
                          {"type":"message","content":[{"type":"output_text","text":content}]}]}
    else:
        raw = {"model": live.PROVIDERS[provider]["model"], "usage":{"prompt_tokens":10,"completion_tokens":20},
               "choices":[{"finish_reason":"stop","message":{"content":content,"reasoning_content":"SECRET_REASONING"}}]}
    final = live.final_only(raw, provider)
    assert "SECRET_REASONING" not in json.dumps(final)
    assert final["finish"] == "complete"
    outcomes = live.inspect_final(final, bundle)
    assert len(outcomes) == 12 and all(o["structural_passed"] for o in outcomes)
    assert all(o["semantic_passed"] is None for o in outcomes)
    raw["model"] = "another-model"
    assert live.final_only(raw, provider)["finish"] == "model_changed"


@pytest.mark.parametrize("alteration,expected", [("blank","blank_output"),("missing","batch_format_or_coverage_invalid"),
    ("duplicate","batch_format_or_coverage_invalid"),("truncated","provider_incomplete")])
def test_omitted_duplicate_blank_and_truncated_answers_are_not_passes(bundle, alteration, expected):
    content, finish = answers(bundle), "complete"
    if alteration == "missing": content["cases"].pop()
    if alteration == "duplicate": content["cases"][-1] = content["cases"][0]
    if alteration == "truncated": finish = "incomplete"
    final = {"finish":finish,"final_content":" " if alteration=="blank" else json.dumps(content)}
    outcomes = live.inspect_final(final, bundle)
    assert all(expected in o["structural_errors"] and not o["structural_passed"] for o in outcomes)


def test_provenance_and_dates_are_checked_without_relabelling_private_records(bundle):
    case = bundle["reviewer_sheet.json"]["cases"][-1]
    summary = {"status":"ok","facts":[{"text":"Synthetic stub question", "event_status":"happened",
       "statement_role":"direct","event_date":"2034-01-01","planned_date":None,
       "evidence_ids":["s1"],"supersedes":None}],"uncertainties":[],"refusal_reason":None}
    assert "date_without_source_support" in live.inspect_summary(summary,case["evidence_catalog"])
    summary["facts"][0].update(event_date=None,evidence_ids=["other-case-reference"])
    assert "invalid_evidence_ids" in live.inspect_summary(summary,case["evidence_catalog"])
    assert case["records"][0]["source_kind"] == exam.PRIVATE_KIND


@pytest.mark.parametrize("value", ["2034-05-06", "2034年5月6日", "2034年05月06日"])
def test_both_prompt_allowed_date_forms_use_the_same_evidence_without_rewriting(value):
    summary = {"status":"ok","facts":[{"text":"Synthetic cancelled plan", "event_status":"cancelled",
       "statement_role":"direct","event_date":None,"planned_date":value,
       "evidence_ids":["s1","s2"],"supersedes":None}],"uncertainties":[],"refusal_reason":None}
    catalog = {"s1":{"quote":"原定2034年5月6日取表。"},"s2":{"quote":"后来取消了。"}}
    before = copy.deepcopy((summary,catalog))
    assert live.inspect_summary(summary,catalog) == []
    assert (summary,catalog) == before
    summary["facts"][0]["evidence_ids"] = ["s2"]
    assert "date_without_source_support" in live.inspect_summary(summary,catalog)


@pytest.mark.parametrize("value", ["5月6日", "2034年2月30日", "2034-05", "明天", "2034/05/06", "2034年5月6日或7日"])
def test_date_comparison_does_not_infer_missing_or_ambiguous_calendar_parts(value):
    assert live.canonical_complete_date(value) is None


def setup_run(tmp_path, monkeypatch, bundle, provider="openai", limit="10"):
    monkeypatch.setattr(exam,"PRIVATE_ROOT",tmp_path)
    ledger=tmp_path/"ledger.json"
    ledger.write_bytes(encoded({"schema":"echo-summary-budget-v1","provider":provider,
                               "limit_usd":limit,"prior_reserved_usd":"0","reservations":[]}))
    return dict(bundle=bundle,provider=provider,directory=tmp_path/"run",key_file=tmp_path/"key.dpapi",
                ledger_path=ledger,authorization=authorization(bundle))


def test_bad_authorization_or_budget_never_reads_key_or_sends(tmp_path,monkeypatch,bundle):
    args=setup_run(tmp_path,monkeypatch,bundle,limit="0.001")
    def key_reader(*a,**k): pytest.fail("key must not be read")
    args["key_reader"]=key_reader
    args["authorization"]["scope"]["case_count"]=11
    with pytest.raises(SummaryError,match="exact_authorized_scope"):
        live.run(**args)
    assert not args["directory"].exists()
    args["authorization"]=authorization(bundle)
    report=live.run(**args)
    assert report["error_code"]=="trial_budget_exhausted" and report["model_calls_reserved"]==0


def test_reservation_precedes_paid_request_and_same_scope_cannot_retry(tmp_path,monkeypatch,bundle):
    args=setup_run(tmp_path,monkeypatch,bundle)
    monkeypatch.setattr(live,"preflight",lambda c:{"ok":True})
    key_reads=[]
    def key_reader(*a,**k): key_reads.append(1); return "synthetic-key-"+"x"*32
    class MockClient:
        def __init__(self,provider,key): self.provider=provider
        def request(self,endpoint,payload):
            data=json.loads(args["ledger_path"].read_text())
            assert len(data["reservations"])==1
            assert (args["directory"]/"attempt.json").is_file()
            raise SummaryError("mock_network_failure_no_retry")
    args.update(key_reader=key_reader,client_factory=MockClient)
    report=live.run(**args)
    assert report["model_calls_reserved"]==1 and report["status"]=="stopped_no_retry"
    before=(args["directory"]/"report.json").read_bytes()
    args["directory"]=tmp_path/"another-run"
    retry=live.run(**args)
    assert retry["error_code"]=="batch_already_attempted_no_automatic_retry" and len(key_reads)==1
    assert (tmp_path/"run"/"report.json").read_bytes()==before


def test_network_client_blocks_wrong_endpoint_key_payload_and_secret_echo(monkeypatch):
    key="synthetic-key-"+"x"*32
    client=live.Client("deepseek",key)
    with pytest.raises(SummaryError,match="endpoint_not_allowed"):
        client.request("https://unexpected.example/")
    with pytest.raises(SummaryError,match="key_in_payload"):
        client.request("/chat/completions",{"x":key})
    class Response:
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def read(self,n): return json.dumps({"echo":key}).encode()
    class Opener:
        def open(self,request,timeout):
            assert request.full_url=="https://api.deepseek.com/models" and timeout==180
            return Response()
    monkeypatch.setattr(live.urllib.request,"build_opener",lambda handler:Opener())
    with pytest.raises(SummaryError,match="echoed_secret_suppressed"):
        client.request("/models")
