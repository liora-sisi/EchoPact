import copy
import json
from pathlib import Path

import pytest

from backend.memory.identity import register_agent
from backend.memory.records_v1 import import_record_package
from backend.memory.assistant_baseline import SUITE_SCHEMA, text_sha
from scripts import query_caretaker_trial as cli
from backend.memory.caretaker_summary import SummaryError


@pytest.fixture
def trial(tmp_path):
    db = tmp_path / "private.sqlite3"
    row = {"record_id": "synthetic-1", "content": "陶艺格式：晾干后再烧制。", "source_kind": "synthetic_conversation",
        "source_ref": "synthetic://planner", "conversation_id": "synthetic-room", "branch_id": "main", "message_id": "s1",
        "role": "user", "created_at": "2031-05-06T00:00:00Z", "source_cutoff_at": "2031-05-06T00:00:00Z",
        "verified": False, "authority": "synthetic-unverified", "conflict_group_id": None}
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"schema_version": "echo-pact-records-v1", "records": [row]}), encoding="utf-8")
    register_agent("owner", "test", actor="test", db_path=str(db))
    import_record_package(str(source), db_path=str(db), owner_agent_id="owner", actor="test")
    target = {k: row[k] for k in ("record_id", "source_kind", "conversation_id")}
    target.update(content_sha256=text_sha(row["content"]), quote="晾干后再烧制。")
    suite = {"schema_version": SUITE_SCHEMA, "corpus_sha256": cli.file_sha(db), "reference_at": "2031-05-07T00:00:00Z",
        "cases": [{"case_id": "clay", "query_origin": "synthetic", "reviewed_by": "test", "targets": [target],
                   "steps": [{"query": "以前我们说过陶艺要怎么做？", "origin": "natural"}]}]}
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(suite), encoding="utf-8")
    key = tmp_path / "synthetic.dpapi"
    key.write_text("not a real credential", encoding="utf-8")
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"schema": "echo-summary-budget-v1", "provider": "openai",
        "trial_id": "openai-summary-comparison-20261110", "limit_usd": "0.50", "prior_reserved_usd": "0",
        "reservations": []}), encoding="utf-8")
    args = ["--cases", str(cases), "--db", str(db), "--agent-id", "owner", "--output-dir", str(tmp_path / "run"),
            "--key-file", str(key), "--budget-ledger", str(ledger)]
    return args, db, tmp_path / "run", ledger


def fake_model(monkeypatch, ledger, *, fail=False):
    requests = []
    class Client:
        def __init__(self, ignored):
            pass
        def request(self, endpoint, payload=None):
            if endpoint.startswith("/models/"):
                return {"id": cli.api.transport.MODEL}
            assert len(json.loads(ledger.read_text())["reservations"]) == 1
            requests.append(copy.deepcopy(payload))
            if fail:
                raise SummaryError("synthetic failure")
            return {"model": cli.api.transport.MODEL, "status": "completed", "output": [{"type": "message",
                "role": "assistant", "status": "completed", "content": [{"type": "output_text",
                    "text": json.dumps({"status": "planned", "intent": "procedure", "topic_terms": ["陶艺"]})}]}],
                "usage": {"input_tokens": 500, "output_tokens": 50}}
    monkeypatch.setattr(cli.api.transport, "OpenAIClient", Client)
    monkeypatch.setattr(cli, "read_dpapi_key", lambda *a, **k: "synthetic never sent")
    return requests


def test_prepare_mock_live_and_resume_are_immutable_question_only(trial, monkeypatch):
    args, db, output, ledger = trial
    requests = fake_model(monkeypatch, ledger)
    assert cli.main(args) == 0 and requests == []
    assert cli.main(args + ["--allow-query-only-api", "--resume"]) == 0
    assert len(requests) == 1
    assert "晾干" not in json.dumps(requests, ensure_ascii=False)
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    budget = ledger.read_bytes()
    assert cli.main(args + ["--allow-query-only-api", "--resume"]) == 0
    assert len(requests) == 1 and ledger.read_bytes() == budget
    assert before == {p.name: p.read_bytes() for p in output.iterdir()}
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["database_unchanged"] and report["archive_records_sent"] == 0
    assert report["cases"][0]["retrieval_calls"] == 3
    assert report["cases"][0]["brief_chars"] <= 8000


def test_failure_is_saved_and_never_retried(trial, monkeypatch):
    args, _, output, ledger = trial
    requests = fake_model(monkeypatch, ledger, fail=True)
    cli.main(args + ["--allow-query-only-api"])
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["cases"][0]["planner_status"] == "failed_original_preserved"
    assert report["cases"][0]["retrieval_calls"] == 1
    assert (output / "clay.response.json").exists()
    cli.main(args + ["--allow-query-only-api", "--resume"])
    assert len(requests) == 1


@pytest.mark.parametrize("issue", ["missing_key", "bad_ledger", "exhausted"])
def test_missing_key_bad_budget_or_budget_exhaustion_keeps_original(trial, monkeypatch, issue):
    args, _, output, ledger = trial
    requests = fake_model(monkeypatch, ledger)
    if issue == "missing_key":
        args = args[:10]  # no credentials or budget arguments
    elif issue == "bad_ledger":
        ledger.write_text("invalid json", encoding="utf-8")
    else:
        value = json.loads(ledger.read_text())
        value["prior_reserved_usd"] = "0.50"
        ledger.write_text(json.dumps(value), encoding="utf-8")
    cli.main(args + ["--allow-query-only-api"])
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["model_calls_reserved"] == 0 and requests == []
    assert report["cases"][0]["retrieval_calls"] == 1


def test_interrupted_attempt_does_not_send_another_request(trial, monkeypatch):
    args, _, output, ledger = trial
    requests = fake_model(monkeypatch, ledger)
    cli.main(args)
    (output / "clay.started.json").write_text('{}', encoding="utf-8")
    cli.main(args + ["--allow-query-only-api", "--resume"])
    assert requests == []
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["cases"][0]["planner_status"] == "failed_original_preserved"


def test_mutated_frozen_request_is_rejected_before_api(trial, monkeypatch):
    args, _, output, ledger = trial
    requests = fake_model(monkeypatch, ledger)
    cli.main(args)
    (output / "clay.request.json").write_text('{}', encoding="utf-8")
    with pytest.raises(cli.core.PlannerError, match="frozen_request_changed"):
        cli.main(args + ["--allow-query-only-api", "--resume"])
    assert requests == []
