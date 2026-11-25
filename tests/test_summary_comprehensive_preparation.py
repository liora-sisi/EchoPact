"""Coverage, provenance and input integrity; no real excerpts or model calls here."""
import copy
import hashlib
import json

import pytest

from backend.memory.caretaker_summary import SummaryError
from scripts import prepare_summary_comprehensive_comparison as exam
from scripts.prepare_summary_fidelity_comparison import build_bundle as old_build, encoded, load_cases as old_cases


def stub_packet():
    # A plainly synthetic plumbing stub, not an actual adult-content quality test.
    return {"schema": "echo-summary-private-excerpts-v1", "captured_at": "2034-01-01T00:00:00+08:00",
            "source_context": copy.deepcopy(exam.PRIVATE_CONTEXT), "cases": [
                {"case_id": f"private-stub-{i}", "category": "explicit_adult_excerpt", "focus": "stub only",
                 "records": [{"record_id": f"local-excerpt-stub-{i}", "recorded_at": None,
                              "source_kind": exam.PRIVATE_KIND,
                              "content": "[Synthetic plumbing stub] 甲问：花开了吗？\n  " + "乙" * 450}],
                 "review_targets": ["stub question is not a confirmed event"],
                 "forbidden_claims": ["inferred date"]} for i in range(3)]}


def test_default_public_suite_reports_the_missing_group_not_full_coverage():
    bundle = exam.build_bundle(exam.load_cases())
    manifest = bundle["manifest.json"]
    assert manifest["case_count"] == 9 and manifest["record_count"] == 18
    assert manifest["prepared_comparison_cells"] == 27 and manifest["planned_comparison_cells"] == 36
    assert not manifest["coverage_complete"] and not manifest["contains_private_excerpts"]
    assert [c["prepared_cases"] for c in manifest["categories"]] == [3, 3, 3, 0]


def test_complete_preparation_never_means_a_model_passed():
    bundle = exam.build_bundle(exam.load_cases(), stub_packet())
    manifest = bundle["manifest.json"]
    assert manifest["case_count"] == 12 and manifest["record_count"] == 21
    assert manifest["coverage_complete"] and manifest["model_calls"] == 0
    assert not manifest["live_runner_implemented"] and not manifest["production_accepted"]
    assert manifest["external_send_authorization"] == "not_granted_by_this_preparation"
    assert all(c["prepared_cases"] == 3 and c["semantic_passed"] is None for c in manifest["categories"])
    assert all(g["status"] == "not_run" and g["semantic_passed"] is None
               for c in bundle["reviewer_sheet.json"]["cases"] for g in c["grades"])


def test_no_text_loss_unknown_times_or_gold_leakage_and_one_common_prompt():
    cases, private = exam.load_cases(), stub_packet()
    before = copy.deepcopy((cases, private))
    bundle = exam.build_bundle(cases, private)
    requests = bundle["writer_inputs.json"]["cases"]
    assert len(requests) == len(cases) + len(private["cases"])
    assert len({r["messages"][0]["content"] for r in requests}) == 1
    for source, request, review in zip(cases + private["cases"], requests, bundle["reviewer_sheet.json"]["cases"]):
        payload = json.loads(request["messages"][1]["content"])
        assert set(payload) == {"source_context", "records"}
        assert len(payload["records"]) == len(source["records"])
        for original, row in zip(source["records"], payload["records"]):
            assert "".join(s["text"] for s in row["spans"]) == original["content"]
            assert row["recorded_at"] == original["recorded_at"]
            assert row["source_kind"] == original["source_kind"]
            for span in review["evidence_catalog"].values():
                if span["record_id"] == row["record_id"]:
                    assert original["content"][span["start"]:span["end"]] == span["quote"]
        assert "captured_at" not in payload
    assert (cases, private) == before
    cases[0]["review_targets"] = ["REVIEW_ONLY_GOLD"]
    private["cases"][0]["focus"] = "REVIEW_ONLY_FOCUS"
    private["cases"][1]["forbidden_claims"] = ["REVIEW_ONLY_FORBIDDEN"]
    changed = exam.build_bundle(cases, private)
    assert changed["writer_inputs.json"] == bundle["writer_inputs.json"]
    assert b"REVIEW_ONLY" not in encoded(changed["writer_inputs.json"])
    assert changed["manifest.json"]["private_fixture_sha256"] != bundle["manifest.json"]["private_fixture_sha256"]


@pytest.mark.parametrize("field,value", [("source_kind", "synthetic"), ("recorded_at", "2034-01-01T00:00:00+08:00"),
                                         ("record_id", "purported-original-message"), ("content", "a" * 801)])
def test_private_source_cannot_be_relabelled_or_given_an_invented_time(field, value):
    packet = stub_packet()
    packet["cases"][0]["records"][0][field] = value
    with pytest.raises(SummaryError, match="private_excerpt_provenance_or_bounds"):
        exam.build_bundle(exam.load_cases(), packet)


@pytest.mark.parametrize("change", ["missing_case", "wrong_category", "duplicate_id", "wrong_provenance", "no_timezone"])
def test_bad_coverage_or_provenance_is_rejected(change):
    cases, packet = exam.load_cases(), stub_packet()
    if change == "missing_case":
        packet["cases"].pop()
    elif change == "wrong_category":
        cases[0]["category"] = "emotional_expression"
    elif change == "duplicate_id":
        packet["cases"][0]["records"][0]["record_id"] = packet["cases"][1]["records"][0]["record_id"]
    elif change == "wrong_provenance":
        packet["source_context"]["original_message_ids_known"] = True
    else:
        packet["captured_at"] = "2034-01-01T00:00:00"
    with pytest.raises(SummaryError):
        exam.build_bundle(cases, packet)


def test_private_packet_cannot_accidentally_write_to_repo_or_escape_root(tmp_path, monkeypatch):
    protected = tmp_path / "protected"
    protected.mkdir()
    monkeypatch.setattr(exam, "PRIVATE_ROOT", protected)
    bundle = exam.build_bundle(exam.load_cases(), stub_packet())
    for destination in (tmp_path / "repo" / "exam", protected, protected / ".." / "escaped"):
        with pytest.raises(SummaryError, match="protected_directory"):
            exam.prepare(destination, bundle)
    assert list(protected.iterdir()) == []
    destination = protected / "exam"
    exam.prepare(destination, bundle)
    first = {p.name: p.read_bytes() for p in destination.iterdir()}
    for name, expected in bundle["manifest.json"]["files_sha256"].items():
        assert hashlib.sha256(first[name]).hexdigest() == expected
    with pytest.raises(FileExistsError):
        exam.prepare(destination, bundle)
    assert first == {p.name: p.read_bytes() for p in destination.iterdir()}


def test_original_eight_case_exam_is_unchanged_and_real_sources_still_rejected():
    original = old_build(old_cases())
    assert original["manifest.json"]["files_sha256"]["writer_inputs.json"] == (
        "1301e6fb4c3f7a8658b16db1e986eaaa035315e66f796edf52bccf302a6675b6")
    rows = old_cases()
    rows[0]["records"][0]["source_kind"] = exam.PRIVATE_KIND
    with pytest.raises(SummaryError, match="trial_accepts_synthetic_only"):
        old_build(rows)


def test_cli_is_offline_reports_only_counts_and_does_not_write_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert exam.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "dry_run" and report["case_count"] == 9 and report["model_calls"] == 0
    assert not report["coverage_complete"] and list(tmp_path.iterdir()) == []
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(exam, "PRIVATE_FIXTURE", missing)
    assert exam.main(["--include-private-excerpts"]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "comprehensive_preparation_failed"
