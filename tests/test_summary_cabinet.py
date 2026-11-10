import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from backend.memory.caretaker_summary import SummaryError
from backend.memory.summary_cabinet import Cabinet, digest, encoded, plan, SOURCES
from scripts.summary_cabinet_trial import load_fixture, FIXTURE_MODEL, SCOPE

CASES, CORPUS = load_fixture()


def generator(records, *, job_id):
    reference = next(c["reference_summary"] for c in CASES if c["records"] == records)
    return {"model": FIXTURE_MODEL, "model_summary": copy.deepcopy(reference)}


def lookup(cabinet, corpus=CORPUS, **kwargs):
    options = {"summary_model": FIXTURE_MODEL, "scope_id": SCOPE,
               "source_namespaces": sorted(SOURCES), "terms": ["橘灯"]}
    return cabinet.lookup(corpus, **(options | kwargs))


def prepare(path):
    jobs = plan(CORPUS, model=FIXTURE_MODEL)
    cabinet = Cabinet(path)
    cabinet.ingest(jobs, generator)
    for job in jobs:
        cabinet.review(job, accepted=True, reviewer="synthetic-test", note="Synthetic reference evidence reviewed.")
    return cabinet, jobs


def test_precompute_once_review_retrieve_and_expand_do_not_regenerate(tmp_path):
    corpus = copy.deepcopy(CORPUS)
    original = copy.deepcopy(corpus)
    jobs = plan(corpus, model=FIXTURE_MODEL)
    calls = []
    with Cabinet(tmp_path / "summaries.db") as cabinet:
        def generate(records, *, job_id):
            assert cabinet.state(job_id) == "started"  # Persisted before any paid call.
            calls.append(job_id)
            return generator(records, job_id=job_id)
        first = cabinet.ingest(jobs, generate)
        assert len(calls) == 4 and all(r["state"] == "draft" for r in first)
        before_review = lookup(cabinet, corpus)
        assert before_review["route"] == "original_fallback"
        assert before_review["items"][0]["record_id"] == "m1"
        for job in jobs:
            cabinet.review(job, accepted=True, reviewer="reference-fixture", note="Test only, not model quality.")
        assert all(r["reused"] for r in cabinet.ingest(jobs, generate))
        result = lookup(cabinet, corpus)
        assert result["route"] == "summary" and len(result["items"]) == 1
        assert "model_summary" not in encoded(result) and "quote" not in encoded(result)
        expanded = cabinet.expand(corpus, summary_model=FIXTURE_MODEL, scope_id=SCOPE,
            source_namespaces=sorted(SOURCES), job_id=result["items"][0]["job_id"])
        assert expanded["records"] == corpus[0]["records"]
        assert result["model_calls"] == expanded["model_calls"] == 0 and len(calls) == 4
        assert corpus == original


def test_same_record_id_different_sources_and_scope_never_cross(tmp_path):
    cabinet, jobs = prepare(tmp_path / "summaries.db")
    with cabinet:
        result = lookup(cabinet, terms=["红茶"], source_namespaces=["synthetic-codex"])
        assert result["items"][0]["source_namespace"] == "synthetic-codex"
        assert result["items"][0]["original_record_ids"] == ["m1"]
        assert lookup(cabinet, terms=["红茶"], source_namespaces=["synthetic-gpt"])["items"] == []
        assert lookup(cabinet, scope_id="different-family")["items"] == []
        with pytest.raises(SummaryError, match="not_visible"):
            cabinet.expand(CORPUS, summary_model=FIXTURE_MODEL, scope_id="different-family",
                           source_namespaces=sorted(SOURCES), job_id=jobs[0]["job_id"])


def test_changed_room_invalidates_old_summary_and_expansion(tmp_path):
    cabinet, jobs = prepare(tmp_path / "summaries.db")
    changed = copy.deepcopy(CORPUS)
    changed[0]["records"][0]["content"] += "后来的更正：实际只换了灯泡。"
    with cabinet:
        assert lookup(cabinet, changed)["route"] == "original_fallback"
        with pytest.raises(SummaryError, match="stale"):
            cabinet.expand(changed, summary_model=FIXTURE_MODEL, scope_id=SCOPE,
                           source_namespaces=sorted(SOURCES), job_id=jobs[0]["job_id"])
        assert cabinet.state(jobs[0]["job_id"]) == "draft"  # Old artifact retained, never overwritten.


def test_writer_provider_and_model_are_separate_provenance_not_query_dependencies(tmp_path):
    cabinet, jobs = prepare(tmp_path / "summaries.db")
    with cabinet:
        other_provider = plan(CORPUS, model=FIXTURE_MODEL, provider="other-provider")
        assert all(a["job_id"] != b["job_id"] for a, b in zip(jobs, other_provider))
        assert lookup(cabinet, summary_provider="other-provider")["route"] == "original_fallback"
        assert lookup(cabinet, summary_model="other-writer")["route"] == "original_fallback"
        # A query reader needs only the persisted writer provenance; no model client.
        assert lookup(cabinet)["route"] == "summary"


def test_failed_and_interrupted_jobs_never_retry_on_resume(tmp_path):
    jobs = plan(CORPUS, model=FIXTURE_MODEL)
    calls = []
    with Cabinet(tmp_path / "summaries.db") as cabinet:
        def timeout(records, *, job_id):
            calls.append(job_id)
            raise SummaryError("provider_network_failure_no_retry")
        result = cabinet.ingest(jobs, timeout)
        assert len(result) == len(calls) == 1 and result[0]["state"] == "failed"
        resumed = cabinet.ingest(jobs, generator)
        assert resumed[0]["reused"] and len(resumed) == 4
        assert cabinet.state(jobs[0]["job_id"]) == "failed"
    with Cabinet(tmp_path / "crashed.db") as cabinet:
        def crash(records, *, job_id):
            raise KeyboardInterrupt
        with pytest.raises(KeyboardInterrupt):
            cabinet.ingest(jobs, crash)
        assert cabinet.state(jobs[0]["job_id"]) == "started"
        resumed = cabinet.ingest(jobs, generator)
        assert resumed[0] == {"job_id": jobs[0]["job_id"], "state": "started", "reused": True}


def test_rejected_draft_is_retained_but_never_retrieved_or_approved(tmp_path):
    job = plan(CORPUS[:1], model=FIXTURE_MODEL)[0]
    def invalid(records, *, job_id):
        result = generator(records, job_id=job_id)
        result["model_summary"]["facts"][0]["evidence_ids"] = ["invented"]
        return result
    with Cabinet(tmp_path / "summaries.db") as cabinet:
        outcome = cabinet.ingest([job], invalid)[0]
        assert outcome["state"] == "rejected"
        raw = cabinet.db.execute("SELECT result_json FROM jobs").fetchone()[0]
        assert "invented" in raw
        with pytest.raises(SummaryError, match="unavailable"):
            cabinet.review(job, accepted=True, reviewer="test", note="Cannot approve invalid evidence.")
        assert lookup(cabinet)["route"] == "original_fallback"


def test_review_withdrawal_is_append_only_and_does_not_delete_draft(tmp_path):
    cabinet, jobs = prepare(tmp_path / "summaries.db")
    with cabinet:
        cabinet.review(jobs[0], accepted=False, reviewer="test", note="Withdrawn pending review.")
        assert lookup(cabinet)["route"] == "original_fallback"
        assert cabinet.state(jobs[0]["job_id"]) == "draft"
        assert cabinet.db.execute("SELECT COUNT(*) FROM reviews WHERE job_id=?", (jobs[0]["job_id"],)).fetchone()[0] == 2


def test_changing_stored_output_does_not_inherit_old_approval(tmp_path):
    cabinet, jobs = prepare(tmp_path / "summaries.db")
    with cabinet:
        raw = json.loads(cabinet.db.execute("SELECT result_json FROM jobs WHERE job_id=?", (jobs[0]["job_id"],)).fetchone()[0])
        raw["model_summary"]["facts"][0]["text"] = "橘灯修理次数被伪造。"
        with cabinet.db:
            cabinet.db.execute("UPDATE jobs SET result_json=? WHERE job_id=?", (encoded(raw), jobs[0]["job_id"]))
        assert lookup(cabinet)["route"] == "original_fallback"


def test_unrelated_database_is_never_initialized_or_modified(tmp_path):
    path = tmp_path / "original.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE originals(content TEXT)")
        db.execute("INSERT INTO originals VALUES('keep this exactly')")
    before = path.read_bytes()
    with pytest.raises(SummaryError, match="not_a_summary_cabinet"):
        Cabinet(path)
    assert path.read_bytes() == before


def test_queries_use_read_only_connection_and_cannot_write(tmp_path):
    path = tmp_path / "summaries.db"
    cabinet, jobs = prepare(path)
    cabinet.db.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with Cabinet(path, read_only=True) as cabinet:
        assert lookup(cabinet)["route"] == "summary"
        with pytest.raises(SummaryError, match="read_only"):
            cabinet.ingest(jobs, generator)
        with pytest.raises(SummaryError, match="read_only"):
            cabinet.review(jobs[0], accepted=False, reviewer="test", note="No writes from query route.")
        with pytest.raises(sqlite3.OperationalError):
            cabinet.db.execute("DELETE FROM jobs")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_windows_are_bounded_reproducible_and_keep_room_boundary():
    corpus = copy.deepcopy(CORPUS[:2])
    original = corpus[0]["records"][0]
    corpus[0]["records"] = [{**original, "record_id": f"id-{i}"} for i in range(14)]
    jobs = plan(corpus, model=FIXTURE_MODEL)
    assert jobs == plan(corpus, model=FIXTURE_MODEL)
    first_room = [j for j in jobs if j["spec"]["room_id"] == "finished-lamp"]
    assert [len(j["records"]) for j in first_room] == [6, 6, 4]
    assert first_room[0]["records"][-1] == first_room[1]["records"][0]
    assert {r["record_id"] for j in first_room for r in j["records"]} == {f"id-{i}" for i in range(14)}
    assert jobs[-1]["records"] == corpus[1]["records"]


@pytest.mark.parametrize("change", [
    lambda c: c[0]["records"][0].update(source_kind="chatgpt_official"),
    lambda c: c[0].update(source_namespace="real-gpt"),
    lambda c: c.append(copy.deepcopy(c[0])),
    lambda c: c[0]["records"].append(copy.deepcopy(c[0]["records"][0])),
])
def test_non_synthetic_and_ambiguous_identities_fail_before_generation(change):
    corpus = copy.deepcopy(CORPUS)
    change(corpus)
    with pytest.raises(SummaryError):
        plan(corpus, model=FIXTURE_MODEL)


def test_query_budget_is_full_serialized_packet_and_field_names_do_not_match(tmp_path):
    cabinet, _ = prepare(tmp_path / "summaries.db")
    with cabinet:
        result = lookup(cabinet, terms=["橘灯", "红茶", "植物园", "小灯塔"], max_chars=800)
        assert len(encoded(result)) <= 800
        assert lookup(cabinet, terms=["planned_date"])["items"] == []


def test_cli_defaults_offline_and_fixture_exercise_is_clearly_not_a_model(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/summary_cabinet_trial.py"
    def run(*args):
        done = subprocess.run([sys.executable, str(script), *map(str, args)], check=True,
                              capture_output=True, text=True, encoding="utf-8")
        return json.loads(done.stdout)
    assert run()["model_calls"] == 0
    output = tmp_path / "run"
    first = run("--exercise-fixture", "--output-dir", output)
    assert first["status"] == "fixture_mechanism_exercised_no_model" and first["model_calls"] == 0
    second = run("--exercise-fixture", "--output-dir", output, "--resume")
    assert second["model_calls"] == 0
    query = run("--output-dir", output, "--query", "橘灯")
    assert query["route"] == "summary" and query["model_calls"] == 0
    expansion = run("--output-dir", output, "--expand-job", query["items"][0]["job_id"])
    assert expansion["records"] == CORPUS[0]["records"]
    # Gaps in report numbering must never cause an old report to be overwritten.
    (output / "resume-3.json").write_text("original third report", encoding="utf-8")
    assert run("--exercise-fixture", "--output-dir", output, "--resume")["model_calls"] == 0
    assert (output / "resume-2.json").is_file()
    assert (output / "resume-3.json").read_text(encoding="utf-8") == "original third report"
    # Source tampering is visible even if the independent manifest is unchanged.
    (output / "synthetic-source.json").write_text("[]", encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError) as rejected:
        run("--output-dir", output, "--query", "橘灯")
    assert "frozen source missing or changed" in rejected.value.stderr
