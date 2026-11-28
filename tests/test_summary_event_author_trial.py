"""Frozen author inputs, whole-batch reservations, retained first answers and source readback."""
import copy
from decimal import Decimal
import json

import pytest

from backend.memory.caretaker_summary import SummaryError
from backend.memory import summary_event_booklet as book
from backend.memory import summary_event_author_v2 as author
from backend.memory.summary_trial_budget import BudgetLedger
from scripts import prepare_summary_comprehensive_comparison as sources
from scripts import summary_event_author_trial as trial


def draft():
    return {"status": "ok", "events": [{"event_id": "e1", "topic": "虚构资料的一项陈述",
        "kind": "life", "facts": [{"text": "待复核的虚构陈述，不是测试金标准。",
        "event_status": "not_applicable", "event_date": None, "planned_date": None,
        "statement_role": "direct", "supersedes": None, "evidence_ids": ["s1"]}],
        "uncertainties": []}], "relations": [], "uncertainties": [], "gaps": [], "refusal_reason": None}


def ledger_file(tmp_path, *, prior="11.849804", limit="20.00", provider="openai"):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"schema": "echo-summary-budget-v1", "provider": provider,
        "limit_usd": limit, "prior_reserved_usd": prior, "reservations": []}), encoding="utf-8")
    return path


def final(answer=None, *, finish="complete"):
    return {"model": "gpt-5.6-sol", "finish": finish,
            "final_content": json.dumps(draft() if answer is None else answer)}


def test_same_three_old_windows_are_sent_whole_without_reviewer_answers():
    suite = trial.build_suite()
    assert len(suite["jobs.json"]) == 3
    assert suite["manifest.json"]["source_chars"] == 5327
    assert not suite["manifest.json"]["contains_private_excerpts"]
    assert suite["manifest.json"]["evaluation_kind"] == "known_development_cases_not_blind"
    for job, case in zip(suite["jobs.json"], suite["reviewer.json"]):
        request = job["request"]
        assert request["input"][0]["content"] == author.SYSTEM_PROMPT
        source = json.loads(request["input"][1]["content"])["records"]
        assert len(source) == len(case["records"])
        assert ["".join(span["text"] for span in row["spans"]) for row in source] == [
            row["content"] for row in case["records"]]
        assert all(row["source_kind"] == "synthetic" for row in source)
        assert not any(key in json.dumps(request) for key in (
            "review_targets", "forbidden_claims", "review_evidence_groups", "evidence_catalog"))
        assert request["max_output_tokens"] == 6000 and request["store"] is False
        prepared = author.prepare_author_window(case["records"])
        assert request["text"]["format"]["schema"] == trial.provider_schema(prepared["response_schema"])
    assert suite["reviewer.json"][-1]["records"][-1]["record_id"] == "ldn-24"


def test_provider_schema_only_moves_uniqueness_to_unchanged_local_validation():
    before = copy.deepcopy(book.DRAFT_SCHEMA)
    legacy = trial.build_suite(legacy_schema=True)
    current = trial.build_suite()
    for old, new in zip(legacy["jobs.json"], current["jobs.json"]):
        previous = copy.deepcopy(old["request"])
        previous["text"]["format"]["schema"] = trial.provider_schema(previous["text"]["format"]["schema"])
        assert previous == new["request"]
    assert legacy["reviewer.json"] == current["reviewer.json"]
    assert book.DRAFT_SCHEMA == before and '"uniqueItems"' not in json.dumps(current["jobs.json"][0]["request"])
    answer = draft()
    answer["uncertainties"] = [{"text": "重复引用仍不能通过。", "evidence_ids": ["s1", "s1"]}]
    with pytest.raises(SummaryError, match="invalid_shape"):
        trial.inspect_final(final(answer), current["jobs.json"][0], current["reviewer.json"][0])


def test_fees_cover_all_three_actual_caps_and_old_entry_points_keep_their_limits(tmp_path):
    scope = trial.scope(trial.build_suite())
    assert scope["maximum_calls"] == 3 and not scope["automatic_retry"]
    assert Decimal(scope["reserved_usd"]) == sum(Decimal(n) for n in scope["job_reserved_usd"].values())
    assert Decimal(scope["reserved_usd"]) <= trial.MAX_RESERVATION
    path = ledger_file(tmp_path)
    before = path.read_bytes()
    for old_ceiling in ("0.50", "1.00", "10.00", "12.00"):
        with pytest.raises(SummaryError, match="invalid_existing_budget_ledger"):
            with BudgetLedger(path, maximum_limit=Decimal(old_ceiling)):
                pass
    with BudgetLedger(path, maximum_limit=trial.LEDGER_CEILING) as ledger:
        assert ledger.total == Decimal("11.849804") and ledger.limit == Decimal("20")
    assert path.read_bytes() == before
    with pytest.raises(SummaryError, match="invalid_trial_budget_ceiling"):
        BudgetLedger(path, maximum_limit=Decimal("20.01"))


@pytest.mark.parametrize("part", ["manifest.json", "jobs.json", "reviewer.json", "scope.json"])
def test_frozen_input_review_or_budget_tampering_is_rejected(tmp_path, monkeypatch, part):
    monkeypatch.setattr(sources, "PRIVATE_ROOT", tmp_path)
    suite = trial.build_suite()
    trial.freeze(tmp_path / "frozen", suite)
    assert trial.load_frozen(tmp_path / "frozen") == suite
    with pytest.raises(FileExistsError): trial.freeze(tmp_path / "frozen", suite)
    (tmp_path / "frozen" / part).write_text("{}", encoding="utf-8")
    with pytest.raises(SummaryError, match="frozen_material_changed"):
        trial.load_frozen(tmp_path / "frozen")


@pytest.mark.parametrize("problem", ["authorization", "changed", "provider", "budget", "ceiling"])
def test_rejected_preflight_never_reads_credentials(tmp_path, monkeypatch, problem):
    monkeypatch.setattr(sources, "PRIVATE_ROOT", tmp_path)
    ledger = ledger_file(tmp_path, limit="12" if problem == "budget" else "21" if problem == "ceiling" else "20",
                         provider="deepseek" if problem == "provider" else "openai")
    def no_key(*args, **kwargs): pytest.fail("credentials must remain unopened")
    suite = trial.build_suite()
    if problem == "changed": suite["jobs.json"][0]["request"]["input"][1]["content"] += "changed"
    args = (suite, tmp_path / "run", tmp_path / "key", ledger)
    if problem in {"authorization", "changed"}:
        with pytest.raises(SummaryError):
            trial.run(*args, authorized=problem != "authorization", key_reader=no_key)
        assert not (tmp_path / "run").exists()
    else:
        report = trial.run(*args, authorized=True, key_reader=no_key)
        assert report["model_calls_reserved"] == report["model_calls_attempted"] == 0


def test_every_fact_unknown_and_relation_can_expand_without_becoming_semantically_approved():
    suite = trial.build_suite()
    job, case = suite["jobs.json"][0], suite["reviewer.json"][0]
    answer = draft()
    answer["events"][0]["uncertainties"] = [{"text": "一个待复核未知。", "evidence_ids": ["s1"]}]
    answer["events"].append({**copy.deepcopy(answer["events"][0]), "event_id": "e2"})
    answer["relations"] = [{"from_event": "e2", "to_event": "e1", "kind": "updates", "evidence_ids": ["s1"]}]
    answer["uncertainties"] = [{"text": "全局待复核未知。", "evidence_ids": ["s1"]}]
    before = copy.deepcopy(answer)
    result = trial.inspect_final(final(answer), job, case)
    assert result["metrics"]["exact_expansion_items_checked"] == 6
    assert result["metrics"]["semantic_passed"] is None
    assert result["metrics"]["uncited_records"]
    assert not result["metrics"]["production_accepted"]
    assert result["audit"]["draft"] == before == answer


@pytest.mark.parametrize("problem", ["incomplete", "refused", "model_changed", "wrong_source", "invalid_reference"])
def test_invalid_or_incomplete_first_answers_are_not_passes(problem):
    suite = trial.build_suite()
    job, case = suite["jobs.json"][0], suite["reviewer.json"][0]
    answer = draft()
    if problem == "invalid_reference": answer["events"][0]["facts"][0]["evidence_ids"] = ["s999"]
    if problem == "wrong_source": case["records"][0]["content"] += "wrong source"
    with pytest.raises(SummaryError):
        trial.inspect_final(final(answer, finish=problem if problem in {"incomplete", "refused", "model_changed"} else "complete"), job, case)


def mock_client(ledger_path, calls, *, fail_second=False, malformed_first=False):
    class Client:
        def __init__(self, provider, key): pass
        def request(self, endpoint, payload):
            assert len(json.loads(ledger_path.read_text())["reservations"]) == 1
            calls.append(payload)
            if fail_second and len(calls) == 2: raise SummaryError("synthetic_network_failure")
            value = "invalid" if malformed_first and len(calls) == 1 else json.dumps(draft())
            return {"model": "gpt-5.6-sol", "status": "completed", "usage": {"input_tokens": 10, "output_tokens": 10},
                "output": [{"type": "reasoning", "content": "must not be retained"},
                           {"type": "message", "content": [{"type": "output_text", "text": value}]}]}
    return Client


def test_network_failure_retains_first_answer_full_budget_and_blocks_repeat(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "PRIVATE_ROOT", tmp_path)
    monkeypatch.setattr(trial.transport, "preflight", lambda client: {"synthetic": True})
    ledger = ledger_file(tmp_path)
    calls, keys = [], []
    def key_reader(*args, **kwargs): keys.append(True); return "synthetic-key"
    suite = trial.build_suite()
    client = mock_client(ledger, calls, fail_second=True)
    report = trial.run(suite, tmp_path / "run", tmp_path / "key", ledger, authorized=True,
                       key_reader=key_reader, client_factory=client)
    assert report["model_calls_reserved"] == 3 and report["model_calls_attempted"] == 2
    assert report["status"] == "stopped_no_retry" and len(report["outcomes"]) == 1
    saved = tmp_path / "run" / suite["jobs.json"][0]["job_id"] / "final-response.json"
    before = saved.read_bytes()
    assert b"must not be retained" not in before
    repeat = trial.run(suite, tmp_path / "repeat", tmp_path / "key", ledger, authorized=True,
                       key_reader=key_reader, client_factory=client)
    assert repeat["error_code"] == "event_author_no_automatic_repeat"
    assert len(calls) == 2 and len(keys) == 1 and saved.read_bytes() == before
    assert Decimal(json.loads(ledger.read_text())["reservations"][0]["reserved_usd"]) == Decimal(trial.scope(suite)["reserved_usd"])


def test_invalid_first_draft_is_saved_and_remaining_windows_are_still_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "PRIVATE_ROOT", tmp_path)
    monkeypatch.setattr(trial.transport, "preflight", lambda client: {"synthetic": True})
    ledger = ledger_file(tmp_path)
    calls = []
    suite = trial.build_suite()
    report = trial.run(suite, tmp_path / "run", tmp_path / "key", ledger, authorized=True,
        key_reader=lambda *args, **kwargs: "synthetic-key",
        client_factory=mock_client(ledger, calls, malformed_first=True))
    assert len(calls) == report["model_calls_attempted"] == 3
    assert [row["status"] for row in report["outcomes"]] == [
        "author_answer_rejected", "requires_semantic_review", "requires_semantic_review"]
    assert json.loads((tmp_path / "run" / suite["jobs.json"][0]["job_id"] / "final-response.json").read_text())["final_content"] == "invalid"
    assert all(row["semantic_passed"] is None and not row["production_accepted"] for row in report["outcomes"])


def test_provider_diagnostics_never_retain_private_echoes_or_arbitrary_error_fields():
    diagnostic = trial.transport.http_error_diagnostic
    body = {"error": {"code": "invalid_json_schema", "type": "invalid_request_error",
        "param": "text.format.schema", "message": "Schema: 'uniqueItems' not permitted; PRIVATE_SOURCE_TEXT"}}
    result = diagnostic(json.dumps(body).encode(), "synthetic-secret")
    assert result == {"code": "invalid_json_schema", "type": "invalid_request_error",
        "param": "text.format.schema", "mentioned_schema_keywords": ["uniqueItems"]}
    assert "PRIVATE_SOURCE_TEXT" not in json.dumps(result)
    for raw in (b"invalid", b"x" * 16385, b'[]', b'{"error": {"message": "synthetic-secret"}}'):
        assert diagnostic(raw, "synthetic-secret") == {}
    body["error"].update(code="PRIVATE", type="PRIVATE", param="PRIVATE", message="PRIVATE")
    assert diagnostic(json.dumps(body).encode(), "synthetic-secret") == {}
