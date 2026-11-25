"""Preparation integrity, not a substitute for actual model quality evaluation."""
import copy
import hashlib
import json

import pytest

from backend.memory.caretaker_summary import SummaryError
from scripts.prepare_summary_fidelity_comparison import build_bundle, encoded, load_cases, main, prepare


def test_all_source_text_reaches_messages_without_chinese_loss_or_normalization():
    cases = load_cases()
    before = copy.deepcopy(cases)
    bundle = build_bundle(cases)
    assert len(bundle["writer_inputs.json"]["cases"]) == len(cases)
    for source, request in zip(cases, bundle["writer_inputs.json"]["cases"]):
        prepared = json.loads(request["messages"][1]["content"])["synthetic_records"]
        assert len(prepared) == len(source["records"])
        for original, row in zip(source["records"], prepared):
            assert row["record_id"] == original["record_id"]
            assert "".join(span["text"] for span in row["spans"]) == original["content"]
        review = next(r for r in bundle["reviewer_sheet.json"]["cases"] if r["case_id"] == source["case_id"])
        originals = {r["record_id"]: r["content"] for r in source["records"]}
        for span in review["evidence_catalog"].values():
            assert originals[span["record_id"]][span["start"]:span["end"]] == span["quote"]
    assert cases == before


def test_grading_answers_and_focus_are_separate_from_writer_input():
    cases = load_cases()
    original = build_bundle(cases)
    cases[0]["focus"] = "REVIEW_ONLY_FOCUS"
    cases[0]["review_targets"].append("REVIEW_ONLY_EXPECTED_FACT")
    cases[0]["forbidden_claims"].append("REVIEW_ONLY_FORBIDDEN_CLAIM")
    changed = build_bundle(cases)
    assert original["writer_inputs.json"] == changed["writer_inputs.json"]
    assert b"REVIEW_ONLY" not in encoded(changed["writer_inputs.json"])
    assert original["manifest.json"]["fixture_sha256"] != changed["manifest.json"]["fixture_sha256"]
    assert original["reviewer_sheet.json"] != changed["reviewer_sheet.json"]


def test_preparation_never_awards_a_model_quality_pass():
    bundle = build_bundle(load_cases())
    manifest = bundle["manifest.json"]
    assert manifest["status"] == "prepared_not_run"
    assert manifest["model_calls"] == 0 and not manifest["production_accepted"]
    assert not manifest["live_runner_implemented"]
    for case in bundle["reviewer_sheet.json"]["cases"]:
        assert {g["provider"] for g in case["grades"]} == {"deepseek", "venice", "openai"}
        assert all(g["status"] == "not_run" and g["semantic_passed"] is None for g in case["grades"])


@pytest.mark.parametrize("source_kind", ["chatgpt-official-export-v1", "codex-session-v1", None])
def test_real_source_kinds_are_not_accepted(source_kind):
    cases = load_cases()
    cases[0]["records"][0]["source_kind"] = source_kind
    with pytest.raises(SummaryError, match="trial_accepts_synthetic_only"):
        build_bundle(cases)


def test_duplicate_case_ids_cannot_mix_review_receipts():
    cases = load_cases()
    cases[1]["case_id"] = cases[0]["case_id"]
    with pytest.raises(SummaryError, match="invalid_fidelity_case_id"):
        build_bundle(cases)


def test_prepared_hashes_match_files_and_a_repeat_preserves_first_bundle(tmp_path):
    bundle = build_bundle(load_cases())
    directory = tmp_path / "exam"
    prepare(directory, bundle)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    manifest = json.loads(before["manifest.json"])
    for name, expected in manifest["files_sha256"].items():
        assert hashlib.sha256(before[name]).hexdigest() == expected
    with pytest.raises(FileExistsError):
        prepare(directory, build_bundle(load_cases()))
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


def test_cli_dry_run_and_prepare_report_zero_calls(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "dry_run" and report["model_calls"] == 0
    assert list(tmp_path.iterdir()) == []
    destination = tmp_path / "exam"
    assert main(["--prepare", "--output-dir", str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "prepared_not_run"
    before = (destination / "manifest.json").read_bytes()
    assert main(["--prepare", "--output-dir", str(destination)]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "output_exists_not_overwritten"
    assert (destination / "manifest.json").read_bytes() == before
