import copy
import hashlib
import json
import socket

import pytest

from backend.memory import caretaker_pipeline as core
from backend.memory import records_v1
from scripts import caretaker_pipeline_challenge as exam


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return exam.create_corpus(tmp_path_factory.mktemp("challenge") / "corpus")


def offline(case, corpus):
    return core.readonly_pipeline(corpus, exam.OWNER)(case["query"], as_of=exam.AS_OF,
        planner=lambda _: copy.deepcopy(case["plan"]),
        selector=lambda request: exam.reference_draft(case, request),
        object_reviewer=lambda request: exam.reference_draft(case, request))


@pytest.mark.parametrize("index", range(4))
def test_challenges_have_reachable_targets_and_real_distractors_readonly(corpus, monkeypatch, index):
    def forbidden(*args, **kwargs):
        pytest.fail("offline corpus check must not migrate or use the network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(records_v1, "migrate_records_db", forbidden)
    before = hashlib.sha256(corpus.read_bytes()).hexdigest()
    case = exam.load_cases()[index]
    result = offline(case, corpus)
    assert exam.score(case, result)["passed"]
    assert hashlib.sha256(corpus.read_bytes()).hexdigest() == before


def test_scorer_rejects_a_confident_answer_to_an_ambiguous_pair(corpus):
    case = exam.load_cases()[1]
    result = offline(case, corpus)
    result["brief"]["items"][0]["relation"] = "answers"
    assert not exam.score(case, result)["passed"]


def test_scorer_rejects_a_short_answer_that_has_lost_a_required_step(corpus):
    case = exam.load_cases()[2]
    result = offline(case, corpus)
    result["brief"]["items"][0]["text"] = "针放带盖盒。"
    assert not exam.score(case, result)["passed"]


def test_scorer_does_not_credit_an_easy_single_candidate_as_a_challenge(corpus):
    case = exam.load_cases()[2]
    result = offline(case, corpus)
    result["audit"]["selection"]["prepared"]["request"]["candidates"] = [
        result["audit"]["selection"]["prepared"]["request"]["candidates"][0]]
    assert not exam.score(case, result)["passed"]


def test_import_excludes_gold_and_cannot_overwrite_an_existing_corpus(corpus):
    package = json.loads((corpus.parent / "synthetic-source.json").read_text(encoding="utf-8"))
    assert len(package["records"]) == 11
    assert all(not set(r) & {"selected", "relation", "required", "expected_gap", "plan"}
               for r in package["records"])
    with pytest.raises(FileExistsError):
        exam.create_corpus(corpus.parent)
