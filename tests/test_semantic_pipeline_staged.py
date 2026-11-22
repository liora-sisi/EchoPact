import copy
import json

import pytest

from backend.memory import semantic_shadow as vectors
from scripts import semantic_pipeline_staged as exam
from test_semantic_shadow import FakeEncoder
from test_semantic_ordered_evidence import setup, OLD, NEW, QUERY
from test_semantic_shortlist import shortlist, reviewer


def stage_fixture(tmp_path):
    db, _, records, _ = setup(tmp_path)
    index = tmp_path / "index.sqlite3"
    vectors.build_index(db, "owner", index, FakeEncoder())
    scope = {"db_path": str(db), "source_sha256": vectors.file_digest(db), "agent_id": "owner",
             "index_path": str(index), "index_sha256": vectors.file_digest(index), "reference_at": "2035-05-02T00:00:00Z",
             "source_scope": "synthetic_wiring_not_model_quality", "controls": ["昨天白昼盒用来做什么？"],
             "cases": [{"case_id": "first", "query": QUERY, "targets": [{"record_id": "r1", "source_kind": "synthetic",
                 "conversation_id": "room", "quote": NEW, "content_sha256": exam.core.selection.digest(NEW)}]}]}
    file = tmp_path / "scope.json"
    file.write_text(json.dumps(scope), encoding="utf-8")
    output = tmp_path / "run"
    exam.prepare(file, output)
    return scope, output


def save_response(output, stage, response):
    exam.write(output / ("first." + stage + ".response.private.json"), {"accepted": True, "response": response})
    exam.write(output / (stage + ".report.json"), {"new_model_calls": 0, "reserved_usd": "0"})


def test_staged_source_freeze_and_exact_replay_have_no_new_calls(tmp_path):
    scope, output = stage_fixture(tmp_path)
    save_response(output, "plan", {"plan": {"status": "planned", "intent": "topic", "topic_terms": ["白昼盒"]}})
    exam.prepare_next(output, None, "shortlist", encoder=FakeEncoder())
    first = exam.requests(output, "shortlist")["first"]["request"]
    save_response(output, "shortlist", {"draft": shortlist(first)})
    exam.prepare_next(output, None, "review")
    last = exam.requests(output, "review")["first"]["request"]
    assert {c["text"] for c in last["candidates"]} == {OLD, NEW}
    save_response(output, "review", {"draft": reviewer(last)})
    report = exam.evaluate(output, None, encoder=FakeEncoder())
    assert report["actual_api_calls"] == report["new_model_calls"] == 0
    assert report["cases"][0]["status"] == "reviewed_requires_acceptance"
    assert report["cases"][0]["targets"][0]["quote_visible"]
    assert report["controls"] == [{"model_calls": 0, "original_unchanged": True}]
    assert vectors.file_digest(scope["db_path"]) == scope["source_sha256"]


def test_frozen_question_cannot_change_during_response_replay(tmp_path):
    _, output = stage_fixture(tmp_path)
    save_response(output, "plan", {"plan": {"status": "planned", "intent": "topic", "topic_terms": ["白昼盒"]}})
    with pytest.raises(ValueError, match="replay_request_changed"):
        exam.saved_callback(output, "plan", "first")("别的问题？")


def test_invalid_case_ids_and_scope_changes_fail_before_api(tmp_path):
    scope, output = stage_fixture(tmp_path)
    bad = copy.deepcopy(scope)
    bad["cases"][0]["case_id"] = "../elsewhere"
    with pytest.raises(ValueError, match="invalid_case_id"):
        exam.validate_scope(bad)
    bad = copy.deepcopy(scope)
    bad["cases"] *= 4
    with pytest.raises(ValueError, match="invalid_scope"):
        exam.validate_scope(bad)
    changed = copy.deepcopy(scope)
    changed["reference_at"] = "2036-05-02T00:00:00Z"
    (output / "scope.private.json").write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen_acceptance_changed"):
        exam.checked(output)


def test_altered_frozen_request_is_not_accepted(tmp_path):
    _, output = stage_fixture(tmp_path)
    path = output / "plan.requests.private.json"
    value = exam.read(path)
    value["entries"][0]["request"] = "Different question"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen_stage_changed"):
        exam.requests(output, "plan")
