"""The evaluator must not turn shorter output or known-answer probes into wins."""
import copy
import json
from pathlib import Path
import socket

import pytest

from backend.memory.assistant_baseline import (
    BaselineError, SUITE_SCHEMA, measure_packet, run_suite, text_sha, validate_suite,
)
from backend.memory.identity import register_agent
from backend.memory.records_v1 import import_record_package
from scripts import assistant_recall_baseline as cli


def record():
    return {"record_id": "synthetic-1", "content": "灯塔约定。" * 80 + "铜铃放在第二个抽屉。",
            "source_kind": "synthetic_conversation", "conversation_id": "synthetic-room",
            "created_at": "2031-03-04T02:00:00Z", "role": "user"}


def suite():
    row = record()
    target = {k: row[k] for k in ("record_id", "source_kind", "conversation_id")}
    target.update(content_sha256=text_sha(row["content"]), quote="铜铃放在第二个抽屉。")
    return {"schema_version": SUITE_SCHEMA, "corpus_sha256": "a" * 64,
            "reference_at": "2031-03-05T04:00:00Z", "cases": [{
                "case_id": "storage", "query_origin": "synthetic", "reviewed_by": "synthetic-reviewer",
                "steps": [{"query": "那只铃收在哪里了？", "origin": "natural"},
                          {"query": "灯塔约定", "origin": "reviewer_diagnostic_clue"}],
                "targets": [target]}]}


def packet(rows=None):
    return {"memories": [record()] if rows is None else rows,
            "adaptive_recall": {"query_passes_used": 1, "passes": []},
            "temporal_scope": {"status": "not_requested"}, "coverage": {"coverage_gap": False}}


def test_record_hit_is_not_visible_evidence_after_prefix_truncation():
    source = packet()
    before = copy.deepcopy(source)
    measured = measure_packet(source, query="铃在哪里", targets=suite()["cases"][0]["targets"], limit=10)
    assert source == before
    assert measured["primary_evidence"]["status"] == "all_targets_visible"
    assert measured["brief_evidence"]["status"] == "target_evidence_missing"
    assert measured["brief_evidence"]["targets"][0]["record_present"] is True
    assert "铜铃" not in json.dumps(measured, ensure_ascii=False)


def test_rounds_are_real_calls_known_clue_is_labelled_and_gold_never_reaches_runner():
    calls = []
    def runner(args):
        calls.append(args)
        assert set(args) == {"query", "limit", "as_of"}
        assert args["as_of"] == suite()["reference_at"]
        return packet([] if len(calls) == 1 else None)
    report = run_suite(suite(), runner)
    case = report["cases"][0]
    assert calls[0]["query"] == "那只铃收在哪里了？"
    assert report["retrieval_calls"] == 2 and report["model_calls"] == 0
    assert case["first_packet_hit_round"] == 2 and case["first_brief_hit_round"] is None
    assert case["returned_chars_to_first_hit"] == sum(s["returned_payload_chars"] for s in case["steps"])
    assert case["steps"][1]["origin"] == "reviewer_diagnostic_clue"
    assert case["steps"][0]["packet_evidence"]["status"] == "target_evidence_missing"


def test_unreviewed_cases_are_unscored_and_unlabelled_does_not_mean_wrong():
    value = suite()
    value["cases"][0].update(targets=[], reviewed_by=None)
    case = run_suite(value, lambda args: packet())["cases"][0]
    assert case["scoring"] == "unscored_no_reviewed_targets"
    assert case["first_packet_hit_round"] is None
    assert case["steps"][0]["packet_evidence"]["status"] == "unscored"
    assert case["steps"][0]["unlabelled_primary_count"] == 1


def test_targets_accumulate_across_rounds_without_requiring_same_packet():
    value = suite()
    first = record()
    second = {**first, "record_id": "synthetic-2", "content": "盒盖标着蓝星。"}
    target = {k: second[k] for k in ("record_id", "source_kind", "conversation_id")}
    target.update(quote=second["content"], content_sha256=text_sha(second["content"]))
    value["cases"][0]["targets"].append(target)
    responses = iter([packet([first]), packet([second])])
    case = run_suite(value, lambda args: next(responses))["cases"][0]
    assert all(s["packet_evidence"]["status"] == "target_evidence_missing" for s in case["steps"])
    assert case["first_packet_hit_round"] == 2
    assert case["steps"][1]["cumulative_packet_target_count"] == 2
    assert case["first_brief_hit_round"] is None  # The first target is still clipped.


def test_context_evidence_is_measured_separately_and_not_invented_in_brief():
    other = {**record(), "record_id": "different", "content": "邻近原文", "conversation_context": [record()]}
    value = measure_packet(packet([other]), query="铃", targets=suite()["cases"][0]["targets"], limit=10)
    assert value["primary_evidence"]["status"] == "target_evidence_missing"
    assert value["packet_evidence"]["status"] == "all_targets_visible"
    assert value["packet_evidence"]["targets"][0]["channels"] == ["conversation_context"]
    assert value["brief_evidence"]["status"] == "target_evidence_missing"


def test_wrong_source_or_later_retelling_cannot_supply_target():
    rows = [{**record(), "source_kind": "different-source"}]
    value = packet(rows)
    value["temporal_scope"]["outside_scope_retellings"] = [record()]
    result = measure_packet(value, query="铃", targets=suite()["cases"][0]["targets"], limit=10)
    assert result["packet_evidence"]["status"] == "target_evidence_missing"


@pytest.mark.parametrize("change", [
    lambda s: s.update(reference_at="2031-03-05"),
    lambda s: s.update(corpus_sha256="unknown"),
    lambda s: s["cases"][0]["steps"][0].update(origin="reviewer_diagnostic_clue"),
    lambda s: s["cases"][0].update(reviewed_by=None),
    lambda s: s["cases"][0].update(steps=s["cases"][0]["steps"] * 3),
    lambda s: s["cases"][0].update(agent_id="different-agent"),
])
def test_contract_rejects_unbounded_or_unlabelled_replays(change):
    value = suite()
    change(value)
    with pytest.raises(BaselineError):
        validate_suite(value)


@pytest.fixture
def local_case(tmp_path):
    db = tmp_path / "private corpus #.sqlite3"
    source = {**record(), "source_ref": "synthetic://baseline", "branch_id": "main", "message_id": "s1",
              "verified": False, "authority": "synthetic-unverified", "source_cutoff_at": "2031-03-05T00:00:00Z",
              "conflict_group_id": None}
    package = tmp_path / "source.json"
    package.write_text(json.dumps({"schema_version": "echo-pact-records-v1", "records": [source]}), encoding="utf-8")
    for agent in ("owner", "outsider"):
        register_agent(agent, agent, actor="test", db_path=str(db))
    import_record_package(str(package), db_path=str(db), owner_agent_id="owner", actor="test")
    value = suite()
    value["corpus_sha256"] = cli.file_sha(db)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    args = ["--cases", str(cases), "--db", str(db), "--agent-id", "owner", "--output-dir", str(tmp_path / "run")]
    return db, value, cases, args


def test_cli_real_readonly_pipeline_freezes_private_run_and_never_calls_network(local_case, monkeypatch, capsys):
    db, value, cases, args = local_case
    def deny(*args, **kwargs):
        raise AssertionError("network must not be used")
    monkeypatch.setattr(socket, "create_connection", deny)
    before = cli.file_sha(db)
    assert cli.main(args) == 0
    report_path = Path(args[-1]) / "report.json"
    frozen = report_path.read_bytes()
    report = json.loads(frozen)
    assert before == cli.file_sha(db)
    assert report["database_unchanged"] and report["model_calls"] == 0
    assert "backend/memory/adaptive_recall.py" in report["implementation"]["source_files_sha256"]
    assert report["cases"][0]["first_packet_hit_round"] is not None
    assert "铜铃" not in frozen.decode("utf-8")
    assert "那只铃" not in capsys.readouterr().out
    with pytest.raises(BaselineError, match="new_output"):
        cli.main(args)
    assert frozen == report_path.read_bytes()


def test_cli_refuses_invisible_or_changed_gold_before_any_recall(local_case):
    db, value, _, _ = local_case
    with pytest.raises(BaselineError, match="not_visible"):
        cli.validate_targets(db, value, "outsider")
    value["cases"][0]["targets"][0]["quote"] = "invented"
    with pytest.raises(BaselineError, match="source_changed"):
        cli.validate_targets(db, value, "owner")


def test_cli_refuses_changed_corpus_or_live_wal_before_creating_output(local_case):
    db, value, cases, args = local_case
    value["corpus_sha256"] = "b" * 64
    cases.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(BaselineError, match="does_not_match"):
        cli.main(args)
    Path(str(db) + "-wal").touch()
    with pytest.raises(BaselineError, match="without_wal"):
        cli.main(args)
    assert not Path(args[-1]).exists()


def test_private_output_is_forbidden_inside_repository():
    with pytest.raises(BaselineError, match="outside_repository"):
        cli._private(cli.ROOT / ".tmp" / "private-run")


def test_code_drift_leaves_failure_instead_of_comparing_different_implementations(local_case, monkeypatch):
    _, _, _, args = local_case
    versions = iter([{"source": "before"}, {"source": "after"}])
    monkeypatch.setattr(cli, "implementation", lambda: next(versions))
    with pytest.raises(BaselineError, match="incomplete"):
        cli.main(args)
    receipt = json.loads((Path(args[-1]) / "failed.json").read_text())
    assert receipt["error_code"] == "implementation_or_policy_changed_during_evaluation"


def test_failed_runner_leaves_safe_receipt_never_a_success_report(local_case, monkeypatch):
    _, _, _, args = local_case
    def broken(*args, **kwargs):
        raise RuntimeError("secret body of a private failure")
    monkeypatch.setattr(cli.ReadonlyGateway, "recall", broken)
    with pytest.raises(BaselineError, match="incomplete"):
        cli.main(args)
    output = Path(args[-1])
    assert not (output / "report.json").exists()
    failure = (output / "failed.json").read_text()
    assert "secret" not in failure and "incomplete_not_a_pass" in failure
