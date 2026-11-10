import copy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import urllib.error

import pytest

from backend.memory.caretaker_summary import SummaryError
from backend.memory import openai_summary_trial as api
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory.venice_summary_trial import read_dpapi_key
from scripts import openai_summary_comparison as cli
from scripts.summary_cabinet_trial import load_fixture

CASES, CORPUS = load_fixture()


def response(case=CASES[0]):
    return {"model":api.MODEL,"status":"completed","output":[
        {"type":"reasoning","summary":[{"text":"synthetic internal material must not be saved"}]},
        {"type":"message","role":"assistant","status":"completed",
         "content":[{"type":"output_text","text":json.dumps(case["reference_summary"],ensure_ascii=False)}]}],
        "usage":{"input_tokens":1000,"output_tokens":500}}


def ledger_file(path, *, limit="0.50", provider="openai"):
    value = {"schema":"echo-summary-budget-v1","provider":provider,
             "trial_id":"openai-summary-comparison-20261110","limit_usd":limit,
             "prior_reserved_usd":"0","reservations":[]}
    path.write_text(json.dumps(value),encoding="utf-8")


def test_same_source_and_evidence_contract_with_no_tools_or_gold_answers():
    total = Decimal(0)
    for case in CASES:
        payload = api.request_payload(case["records"])
        assert payload["input"] == api.contract.build_messages(case["records"])
        assert payload["text"]["format"]["schema"] == api.contract.SUMMARY_SCHEMA
        assert payload["store"] is False and payload["service_tier"] == "default"
        assert "tools" not in payload and "review_targets" not in json.dumps(payload)
        assert "reference_summary" not in json.dumps(payload)
        total += api.reservation(case["records"])
    assert Decimal("0.1") < total < api.MAX_BUDGET_USD
    private = copy.deepcopy(CASES[0]["records"])
    private[0]["source_kind"] = "chatgpt_official"
    with pytest.raises(SummaryError):
        api.request_payload(private)


def test_response_retains_final_evidence_only_and_requires_later_review():
    result = api.parse_response(response(),CASES[0]["records"])
    assert result["model_summary"] == CASES[0]["reference_summary"]
    assert result["semantic_validation"] == "requires_human_review"
    assert "synthetic internal material" not in json.dumps(result)
    assert result["estimated_usd"] == "0.014"


@pytest.mark.parametrize("change,code",[
    (lambda r:r.update(model="other-model"),"response_model_changed"),
    (lambda r:r.update(status="incomplete"),"summary_incomplete"),
    (lambda r:r["output"][1].update(type="function_call"),"unexpected_response_item"),
    (lambda r:r["output"][1]["content"][0].update(type="refusal"),"summary_provider_refused"),
    (lambda r:r["output"][1]["content"][0].update(text="not-json"),"summary_response_invalid"),
])
def test_failed_response_is_never_a_memory(change,code):
    raw=response()
    change(raw)
    with pytest.raises(SummaryError,match=code):
        api.parse_response(raw,CASES[0]["records"])


def test_bad_citation_retains_only_quarantined_final_draft():
    raw=response()
    draft=copy.deepcopy(CASES[0]["reference_summary"])
    draft["facts"][0]["evidence_ids"]=["made-up"]
    raw["output"][1]["content"][0]["text"]=json.dumps(draft)
    with pytest.raises(SummaryError,match="invalid_evidence_ids") as rejected:
        api.parse_response(raw,CASES[0]["records"])
    assert rejected.value.rejected_draft["final_content"] == draft


def test_budget_reservation_precedes_network_and_failure_never_retries():
    calls=[]
    class Client:
        def request(self,endpoint,payload):
            assert calls == ["reserved"]
            calls.append("request")
            raise SummaryError("openai_network_failure_no_retry")
    with pytest.raises(SummaryError,match="no_retry"):
        api.run_case(Client(),CASES[0]["records"],before_request=lambda item:calls.append("reserved"))
    assert calls == ["reserved","request"]


def test_old_venice_ceiling_stays_default_and_new_ceiling_is_explicit(tmp_path):
    path=tmp_path/"ledger.json"
    ledger_file(path)
    with pytest.raises(SummaryError,match="invalid_existing"):
        with BudgetLedger(path):
            pass
    with BudgetLedger(path,maximum_limit=api.MAX_BUDGET_USD) as ledger:
        assert ledger.limit == Decimal("0.50")
    with pytest.raises(SummaryError,match="unsupported_key_provider"):
        read_dpapi_key(tmp_path/"never-read",provider="unknown")


def test_price_expiry_stops_before_account_request():
    class Never:
        def request(self,*args):
            pytest.fail("must not read account after price expires")
    with pytest.raises(SummaryError,match="pricing_requires"):
        api.preflight(Never(),today=date(2026,12,1))


def test_transport_uses_fixed_origin_and_suppresses_key_echo(monkeypatch):
    secret="sk-"+"x"*40
    requests=[]
    class Reply:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,size): return json.dumps({"echo":secret}).encode()
    class Opener:
        def open(self,request,timeout):
            requests.append(request.full_url)
            return Reply()
    monkeypatch.setattr(api.urllib.request,"build_opener",lambda *args:Opener())
    client=api.OpenAIClient(secret)
    assert secret not in repr(client)
    with pytest.raises(SummaryError,match="endpoint_not_allowed"):
        client.request("https://unapproved.example")
    with pytest.raises(SummaryError,match="echoed_secret") as hidden:
        client.request("/models/"+api.MODEL)
    assert secret not in str(hidden.value)
    assert requests == [api.BASE_URL+"/models/"+api.MODEL]


def test_http_failure_is_not_retried_or_dumped(monkeypatch):
    calls=[]
    class Opener:
        def open(self,request,timeout):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url,401,"sensitive provider message",{},None)
    monkeypatch.setattr(api.urllib.request,"build_opener",lambda *args:Opener())
    with pytest.raises(SummaryError) as error:
        api.OpenAIClient("sk-"+"x"*40).request("/models/"+api.MODEL)
    assert str(error.value) == "openai_http_401" and len(calls) == 1


def test_cli_preparation_and_mock_live_resume_are_bounded(tmp_path,monkeypatch,capsys):
    output=tmp_path/"run"
    assert cli.main([]) == 0
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0
    assert cli.main(["--prepare","--output-dir",str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0
    key=tmp_path/"synthetic.dpapi"
    key.write_text("mocked",encoding="utf-8")
    ledger=tmp_path/"budget.json"
    ledger_file(ledger)
    monkeypatch.setattr(cli,"read_dpapi_key",lambda p,**kw:"sk-"+"x"*40)
    calls=[]
    class MockClient:
        def __init__(self,key): pass
        def request(self,endpoint,payload=None):
            if endpoint.startswith("/models/"):
                return {"id":api.MODEL}
            calls.append(endpoint)
            persisted=json.loads(ledger.read_text())
            assert len(persisted["reservations"]) == len(calls)
            case=next(c for c in CASES if api.request_payload(c["records"])["input"] == payload["input"])
            return response(case)
    monkeypatch.setattr(api,"OpenAIClient",MockClient)
    monkeypatch.setattr(api,"PRICE_RECHECK_AFTER",date(9999,1,1))
    arguments=["--allow-live-api","--resume","--output-dir",str(output),"--key-file",str(key),"--budget-ledger",str(ledger)]
    assert cli.main(arguments) == 0
    capsys.readouterr()
    assert len(calls) == 4
    before=ledger.read_bytes()
    assert cli.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["model_calls_reserved"] == 0
    assert len(calls) == 4 and ledger.read_bytes() == before
    report=json.loads((output/"report.json").read_text())
    assert all(o["state"] == "draft" for o in report["outcomes"])
    assert report["production_accepted"] is False
