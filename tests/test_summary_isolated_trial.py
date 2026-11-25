"""No cross-window inputs, bounded paid runs, retained first answers and honest coverage."""
import copy
from decimal import Decimal
import json

import pytest

from backend.memory.caretaker_summary import SummaryError
from backend.memory import summary_faithfulness as original
from backend.memory import summary_faithfulness_v2 as revised
from backend.memory import summary_reading_card as cards
from scripts import summary_isolated_trial as trial
from scripts import prepare_summary_comprehensive_comparison as sources


def draft():
    return {"status": "ok", "facts": [{"text": "根据资料保留一项陈述。", "event_status": "not_applicable",
        "event_date": None, "planned_date": None, "statement_role": "direct", "supersedes": None,
        "evidence_ids": ["s1"]}], "uncertainties": [], "refusal_reason": None}


def make_ledger(tmp_path, *, total="10.636916", provider="openai", limit="12"):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"schema": "echo-summary-budget-v1", "provider": provider,
        "limit_usd": limit, "prior_reserved_usd": total, "reservations": []}), encoding="utf-8")
    return path


def test_each_request_has_exactly_one_identical_window_and_no_grading_targets():
    suite = trial.build_suite()
    assert len(suite["jobs.json"]) == 6
    assert suite["manifest.json"]["unique_source_chars"] == 5327
    assert suite["manifest.json"]["transmitted_source_chars"] == 10654
    for cid in trial.CASE_IDS:
        jobs = [j for j in suite["jobs.json"] if j["case_id"] == cid]
        assert {j["arm"] for j in jobs} == {"original", "revised"}
        assert jobs[0]["request"]["input"][1] == jobs[1]["request"]["input"][1]
        assert jobs[0]["request"]["text"] == jobs[1]["request"]["text"]
        for job in jobs:
            payload = job["request"]
            prepared = json.loads(payload["input"][1]["content"])["cases"]
            assert len(prepared) == 1 and prepared[0]["case_id"] == cid
            assert all(r["source_kind"] == "synthetic" for r in prepared[0]["records"])
            assert all(other not in json.dumps(payload, ensure_ascii=False)
                       for other in trial.CASE_IDS if other != cid)
            assert not any(key in json.dumps(payload) for key in ("review_targets", "forbidden_claims", "review_evidence_groups"))
            assert payload["store"] is False and payload["max_output_tokens"] == 6000
    assert revised.SUMMARY_SCHEMA == original.SUMMARY_SCHEMA
    assert revised.SYSTEM_PROMPT.startswith(original.SYSTEM_PROMPT)
    assert suite["manifest.json"]["repetitions_per_arm_case"] == 1


def test_amount_reserves_every_request_actual_output_cap():
    suite = trial.build_suite()
    scoped = trial.scope(suite)
    assert scoped["maximum_calls"] == 6
    amount = Decimal(scoped["reserved_usd"])
    assert amount == sum(Decimal(x) for x in scoped["job_reserved_usd"].values())
    assert Decimal("0.72") < amount <= Decimal("1.25")
    assert Decimal("10.636916") + amount <= Decimal("12")


@pytest.mark.parametrize("part", ["jobs.json", "reviewer.json", "manifest.json", "scope.json"])
def test_frozen_changes_are_not_reused(tmp_path, monkeypatch, part):
    monkeypatch.setattr(sources, "PRIVATE_ROOT", tmp_path)
    suite = trial.build_suite()
    folder = tmp_path / "suite"
    trial.freeze(folder, suite)
    assert trial.load_frozen(folder) == suite
    with pytest.raises(FileExistsError): trial.freeze(folder, suite)
    (folder / part).write_text("{}", encoding="utf-8")
    with pytest.raises(SummaryError, match="frozen_material_changed"):
        trial.load_frozen(folder)


@pytest.mark.parametrize("issue", ["unauthorized", "changed", "provider", "budget", "ceiling"])
def test_rejected_scope_or_budget_never_opens_credentials(tmp_path, monkeypatch, issue):
    monkeypatch.setattr(sources, "PRIVATE_ROOT", tmp_path)
    ledger = make_ledger(tmp_path, total="11.5" if issue == "budget" else "10.636916",
                         provider="deepseek" if issue == "provider" else "openai", limit="13" if issue == "ceiling" else "12")
    def no_key(*args, **kwargs): pytest.fail("credentials must stay unopened")
    suite = trial.build_suite()
    if issue == "changed": suite["jobs.json"][0]["request"]["input"][1]["content"] += "changed"
    args = (suite, tmp_path / "live", tmp_path / "key", ledger)
    if issue in ("unauthorized", "changed"):
        with pytest.raises(SummaryError):
            trial.run(*args, authorized=issue != "unauthorized", key_reader=no_key)
        assert not (tmp_path / "live").exists()
    else:
        report = trial.run(*args, authorized=True, key_reader=no_key)
        assert report["model_calls_attempted"] == report["model_calls_reserved"] == 0


def test_mid_run_failure_keeps_first_answer_full_reservation_and_blocks_repeat(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "PRIVATE_ROOT", tmp_path)
    monkeypatch.setattr(trial.transport, "preflight", lambda client: {"synthetic": True})
    ledger = make_ledger(tmp_path)
    reads, calls = [], []
    def key_reader(*args, **kwargs): reads.append(True); return "synthetic-key"
    class Client:
        def __init__(self, provider, key): pass
        def request(self, endpoint, payload):
            calls.append(payload)
            assert len(json.loads(ledger.read_text())["reservations"]) == 1
            if len(calls) == 2: raise SummaryError("mock_timeout")
            cid = json.loads(payload["input"][1]["content"])["cases"][0]["case_id"]
            return {"model": "gpt-5.6-sol", "status": "completed", "usage": {"input_tokens": 10, "output_tokens": 10},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({"cases": [{"case_id": cid, "summary": draft()}]})}]}]}
    suite = trial.build_suite()
    first = trial.run(suite, tmp_path / "live", tmp_path / "key", ledger, authorized=True,
                      key_reader=key_reader, client_factory=Client)
    assert first["status"] == "stopped_no_retry" and first["model_calls_attempted"] == 2
    assert first["model_calls_reserved"] == 6 and len(first["outcomes"]) == 1
    saved = tmp_path / "live" / suite["jobs.json"][0]["job_id"] / "final-response.json"
    original_bytes = saved.read_bytes()
    second = trial.run(suite, tmp_path / "repeat", tmp_path / "key", ledger, authorized=True,
                       key_reader=key_reader, client_factory=Client)
    assert second["error_code"] == "isolated_no_automatic_repeat" and len(calls) == 2 and reads == [True]
    assert saved.read_bytes() == original_bytes
    assert Decimal(json.loads(ledger.read_text())["reservations"][0]["reserved_usd"]) == Decimal(trial.scope(suite)["reserved_usd"])


@pytest.mark.parametrize("issue", ["wrong_case", "extra_case", "truncated", "model_changed"])
def test_wrong_scope_or_incomplete_first_answer_is_never_a_pass(issue):
    suite = trial.build_suite()
    job, case = suite["jobs.json"][0], suite["reviewer.json"][0]
    answers = [{"case_id": "another-window" if issue == "wrong_case" else job["case_id"], "summary": draft()}]
    if issue == "extra_case": answers *= 2
    final = {"finish": issue if issue in ("truncated", "model_changed") else "complete", "final_content": json.dumps({"cases": answers})}
    with pytest.raises(SummaryError): trial.inspect_final(final, job, case)


def test_citing_a_record_is_not_reported_as_semantic_coverage():
    case = trial.build_suite()["reviewer.json"][0]
    answer = draft()
    before = copy.deepcopy(answer)
    result = trial.reference_coverage(answer, case)
    assert result["semantic_coverage"] is None and not result["production_accepted"]
    assert result["records"][0]["fact_ids"] == ["f1"]
    assert any(not r["fact_ids"] for r in result["records"])
    assert result["uncited_record_ids"] and not result["fact_capacity_reached"]
    assert answer == before
    answer["facts"][0]["evidence_ids"] = ["s999"]
    with pytest.raises(SummaryError, match="invalid_reference"): trial.reference_coverage(answer, case)


def test_full_fact_capacity_and_unreferenced_records_are_visible_without_rewriting():
    case = trial.build_suite()["reviewer.json"][2]
    answer = draft()
    answer["facts"] *= 24
    before = copy.deepcopy(answer)
    result = trial.reference_coverage(answer, case)
    assert result["fact_capacity_reached"]
    assert "ldn-20" in result["uncited_record_ids"]
    assert "fact_capacity_reached_check_grouping_or_partition" in result["review_hints"]
    assert result["semantic_coverage"] is None and answer == before


def test_new_recipe_preserves_extended_reading_profile_and_originals():
    suite = trial.build_suite()
    case = suite["reviewer.json"][2]
    job = next(j for j in suite["jobs.json"] if j["case_id"] == case["case_id"] and j["arm"] == "revised")
    identity = {"source_namespace": "synthetic", "room_id": case["case_id"], "branch_id": "fixture"}
    answer = draft()
    audit = cards.make_audit(answer, records=case["records"], catalog=case["evidence_catalog"], identity=identity,
       author={"provider": "injected", "model": "synthetic", "recipe": revised.RECIPE, "prompt_sha256": job["prompt_sha256"]})
    reading = cards.render_preview(audit)
    expanded = cards.expand_preview(audit, card_id=reading["card_id"], fact_id="f1", current_records=case["records"], identity=identity)
    assert expanded["evidence"]["s1"]["quote"] == case["records"][0]["content"]
    assert audit["model_summary"] == answer and audit["production_accepted"] is False
