"""Fidelity contract regression, long-window preparation and opt-in trial boundaries."""
import copy
from decimal import Decimal
import json

import pytest

from backend.memory import summary_faithfulness as rules
from backend.memory import summary_reading_card as cards
from backend.memory.caretaker_summary import SummaryError
from scripts import summary_faithfulness_trial as trial
from scripts import prepare_summary_comprehensive_comparison as sources


def fact(**changes):
    return {"text": "根据记录，软件还没发布。", "event_status": "not_happened", "statement_role": "direct",
            "event_date": None, "planned_date": None, "evidence_ids": ["s1"], "supersedes": None, **changes}


def summary(*facts):
    return {"status": "ok", "facts": list(facts), "uncertainties": [], "refusal_reason": None}


CATALOG = {"s1": {"quote": "2036年3月2日已买票，原定2036年3月8日出行；软件还没发布。"},
           "s2": {"quote": "后来行程取消，取消日期没有记。"},
           "s3": {"quote": "刚才说错了，是青山把杯子送给秋月。"}}


def test_explicit_non_occurrence_and_narrated_occurrence_are_distinct_without_external_proof():
    draft = summary(fact(), fact(text="记录说买票已完成。", event_status="happened", event_date="2036-03-02"),
                    fact(text="退款有没有到账未知。", event_status="unknown"))
    before = copy.deepcopy(draft)
    result = rules.inspect_draft(draft, CATALOG)
    assert result["mechanical_passed"] and result["semantic_passed"] is None
    assert result["next"] == "semantic_review_required" and not result["production_accepted"]
    assert draft == before
    assert "叙述" in rules.SYSTEM_PROMPT and "不表示经过现实中的独立核实" in rules.SYSTEM_PROMPT


@pytest.mark.parametrize("change,code", [
    ({"event_date": "2036-03-02"}, "event_date_requires_happened"),
    ({"planned_date": "2036-03-08"}, "planned_date_requires_plan"),
    ({"event_status": "happened", "event_date": "2036-03-02", "planned_date": "2036-03-08"}, "different_event_dates_mixed_in_one_fact"),
    ({"event_status": "happened", "event_date": "2036-03-03"}, "date_without_current_evidence"),
    ({"event_status": "happened", "event_date": "2036-02-30"}, "invalid_complete_date"),
    ({"evidence_ids": ["s1", "s1"]}, "invalid_evidence_ids"),
    ({"evidence_ids": ["s999"]}, "invalid_evidence_ids"),
])
def test_invalid_dates_states_and_references_are_located_not_repaired(change, code):
    draft = summary(fact(**change))
    before = copy.deepcopy(draft)
    result = rules.inspect_draft(draft, CATALOG)
    assert {"code": code, "fact_index": 0} in result["errors"]
    assert result["next"] == "repair_draft" and draft == before


def test_correction_requires_combined_context_and_exact_old_claim_is_blocked():
    current = fact(text="据更正，青山送杯子给秋月。", event_status="happened", statement_role="correction",
                   evidence_ids=["s3"], supersedes={"text": "秋月送杯子给青山。", "evidence_ids": ["s1"]})
    result = rules.inspect_draft(summary(current), CATALOG)
    assert result["errors"] == [{"code": "correction_missing_combined_context", "fact_index": 0}]
    current["evidence_ids"] = ["s1", "s3"]
    assert rules.inspect_draft(summary(current), CATALOG)["mechanical_passed"]
    result = rules.inspect_draft(summary(current, fact(text="秋月送杯子给青山。")), CATALOG)
    assert {"code": "exact_old_claim_also_current", "fact_index": 1} in result["errors"]


def test_shared_source_is_only_a_review_hint_and_never_deletes_a_distinct_fact():
    current = fact(text="据更正，青山送杯子给秋月。", event_status="happened", statement_role="correction",
                   evidence_ids=["s1", "s3"], supersedes={"text": "秋月送杯子给青山。", "evidence_ids": ["s1"]})
    other = fact(text="软件还没发布。")
    draft = summary(current, other)
    before = copy.deepcopy(draft)
    result = rules.inspect_draft(draft, CATALOG)
    assert result["mechanical_passed"]
    assert result["review_hints"] == [{"code": "check_old_claim_still_current", "fact_index": 1, "correcting_fact_index": 0}]
    assert draft == before and result["semantic_passed"] is None


@pytest.mark.parametrize("date", ["2036-03-02", "2036年3月2日", "2036年03月02日"])
def test_supported_full_date_formats_are_equivalent_but_not_inferred(date):
    assert rules.inspect_draft(summary(fact(event_status="happened", event_date=date)), CATALOG)["mechanical_passed"]


def test_date_mention_is_not_claimed_to_prove_semantic_attribution():
    # An unrelated date can appear in the same chunk. Only semantic review can
    # establish which event it belongs to; a mechanical pass must never approve it.
    result = rules.inspect_draft(summary(fact(text="自行车已取回。", event_status="happened",
                                            event_date="2036-03-02")), CATALOG)
    assert result["mechanical_passed"] and result["semantic_passed"] is None
    assert not result["production_accepted"]


def test_suite_has_real_turn_boundaries_and_keeps_gold_out_of_writer_input():
    suite = trial.build_suite()
    assert len(suite["reviewer.json"]) == 6
    long_cases = [c for c in suite["reviewer.json"] if c["cohort"] == "new_long_development_not_blind"]
    assert [len(c["records"]) for c in long_cases] == [20, 24]
    for case in long_cases:
        assert sum(len(r["content"]) for r in case["records"]) > 2000
        assert len({r["content"] for r in case["records"]}) == len(case["records"])
        assert all(r["source_kind"] == "synthetic" for r in case["records"])
        ids = {r["record_id"] for r in case["records"]}
        assert all(set(g) <= ids for g in case["review_evidence_groups"])
    payload = json.dumps(suite["request.json"], ensure_ascii=False)
    assert all(field not in payload for field in ("review_targets", "forbidden_claims", "review_evidence_groups", "cohort"))
    assert not suite["manifest.json"]["contains_private_excerpts"]
    assert suite["request.json"]["store"] is False


def test_long_reading_preview_is_versioned_and_preserves_explicit_negative():
    suite = trial.build_suite()
    case = next(c for c in suite["reviewer.json"] if c["case_id"] == "long-dense-01")
    draft = summary(*(fact(text=f"合成第{i}项明确未完成。") for i in range(24)))
    author = {"provider": "injected", "model": "synthetic", "recipe": rules.RECIPE,
              "prompt_sha256": suite["manifest.json"]["prompt_sha256"]}
    identity = {"source_namespace": "synthetic", "room_id": case["case_id"], "branch_id": "fixture"}
    audit = cards.make_audit(draft, records=case["records"], catalog=case["evidence_catalog"],
                             identity=identity, author=author)
    packet = cards.render_preview(audit, max_bytes=8192)
    assert "明确未发生" in packet["body"] and "f24" in packet["body"]
    expanded = cards.expand_preview(audit, card_id=packet["card_id"], fact_id="f24",
                                    current_records=case["records"], identity=identity)
    assert expanded["evidence"]["s1"]["quote"] == case["records"][0]["content"]
    old_author = {**author, "recipe": "echo-summary-round2-v1"}
    with pytest.raises(SummaryError):
        cards.make_audit(draft, records=case["records"], catalog=case["evidence_catalog"],
                         identity=identity, author=old_author)
    with pytest.raises(SummaryError, match="unknown_fact"):
        cards.expand_preview(audit, card_id=packet["card_id"], fact_id="f25",
                             current_records=case["records"], identity=identity)


@pytest.mark.parametrize("change", ["source", "duplicate", "too_many", "too_long"])
def test_long_window_limits_do_not_accept_real_or_unbounded_sources(change):
    case = next(c for c in trial.build_suite()["reviewer.json"] if c["case_id"] == "long-dense-01")
    rows = copy.deepcopy(case["records"])
    if change == "source": rows[0]["source_kind"] = "chatgpt"
    if change == "duplicate": rows[1]["record_id"] = rows[0]["record_id"]
    if change == "too_many": rows = [{**rows[0], "record_id": str(i)} for i in range(65)]
    if change == "too_long": rows = [{**rows[0], "record_id": str(i), "content": "字" * 7000} for i in range(4)]
    with pytest.raises(SummaryError): rules.validate_synthetic_window(rows)


def test_budget_reserves_actual_output_ceiling_instead_of_legacy_eight_thousand():
    suite = trial.build_suite()
    base = Decimal(trial.scope(suite)["reserved_usd"])
    legacy = trial.transport.reservation(suite["request.json"], "openai")
    assert base - legacy == Decimal("0.12")
    changed = copy.deepcopy(suite)
    changed["request.json"]["max_output_tokens"] += 1000
    assert Decimal(trial.scope(changed)["reserved_usd"]) - base >= Decimal("0.02")
    assert base < trial.MAX_RESERVATION


def test_frozen_material_change_fails_before_use(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "PRIVATE_ROOT", tmp_path)
    suite = trial.build_suite()
    folder = tmp_path / "suite"
    trial.freeze(folder, suite)
    assert trial.load_frozen(folder) == suite
    with pytest.raises(FileExistsError): trial.freeze(folder, suite)
    changed = copy.deepcopy(suite["request.json"])
    changed["input"][1]["content"] += "changed"
    (folder / "request.json").write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(SummaryError, match="frozen_material_changed"):
        trial.load_frozen(folder)


@pytest.mark.parametrize("issue", ["no_enable", "changed_scope", "wrong_provider", "old_ceiling", "too_little_budget"])
def test_boundary_failures_never_read_a_key(tmp_path, monkeypatch, issue):
    monkeypatch.setattr(sources, "PRIVATE_ROOT", tmp_path)
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"schema": "echo-summary-budget-v1", "provider": "deepseek" if issue == "wrong_provider" else "openai",
        "limit_usd": "10" if issue == "old_ceiling" else "12", "prior_reserved_usd": "11.9" if issue == "too_little_budget" else "10.200964",
        "reservations": []}), encoding="utf-8")
    def no_key(*args, **kwargs): pytest.fail("must not read any credentials")
    suite = trial.build_suite()
    if issue == "changed_scope": suite["request.json"]["input"][1]["content"] += "changed"
    args = (suite, tmp_path / "run", tmp_path / "key", ledger)
    if issue in ("no_enable", "changed_scope"):
        with pytest.raises(SummaryError): trial.run(*args, authorized=issue != "no_enable", key_reader=no_key)
        assert not (tmp_path / "run").exists()
    else:
        report = trial.run(*args, authorized=True, key_reader=no_key)
        assert report["status"] == "stopped_no_retry" and report["model_calls_reserved"] == 0


def test_first_answer_and_reservation_survive_failure_without_automatic_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "PRIVATE_ROOT", tmp_path)
    monkeypatch.setattr(trial.transport, "preflight", lambda client: {"synthetic": True})
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"schema": "echo-summary-budget-v1", "provider": "openai", "limit_usd": "12",
                                 "prior_reserved_usd": "10.200964", "reservations": []}), encoding="utf-8")
    reads = []
    def key_reader(*args, **kwargs): reads.append(True); return "synthetic-key"
    class MockClient:
        def __init__(self, provider, key): pass
        def request(self, endpoint, payload):
            assert len(json.loads(ledger.read_text())["reservations"]) == 1
            assert (tmp_path / "first" / "attempt.json").is_file()
            raise SummaryError("mock_timeout")
    suite = trial.build_suite()
    first = trial.run(suite, tmp_path / "first", tmp_path / "key", ledger, authorized=True,
                      key_reader=key_reader, client_factory=MockClient)
    assert first["model_calls_reserved"] == 1 and first["error_code"] == "mock_timeout"
    original = (tmp_path / "first" / "report.json").read_bytes()
    second = trial.run(suite, tmp_path / "second", tmp_path / "key", ledger, authorized=True,
                       key_reader=key_reader, client_factory=MockClient)
    assert second["error_code"] == "faithfulness_no_automatic_repeat" and reads == [True]
    assert (tmp_path / "first" / "report.json").read_bytes() == original


@pytest.mark.parametrize("problem", ["missing", "duplicate", "truncated", "blank"])
def test_incomplete_answer_is_never_reported_as_a_fidelity_pass(problem):
    suite = trial.build_suite()
    rows = [{"case_id": c["case_id"], "summary": summary(fact())} for c in suite["reviewer.json"]]
    if problem == "missing": rows.pop()
    if problem == "duplicate": rows[-1] = rows[0]
    final = {"finish": "incomplete" if problem == "truncated" else "complete",
             "final_content": "" if problem == "blank" else json.dumps({"cases": rows})}
    with pytest.raises(SummaryError): trial.inspect_final(final, suite)
