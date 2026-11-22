import copy
import hashlib
import json
import socket
from decimal import Decimal

import pytest

from backend.memory import caretaker_pipeline as core, records_v1
from backend.memory import caretaker_evaluation as evaluation, evidence_packet
from scripts import caretaker_pipeline_novel as exam
from scripts import caretaker_packet_reader_exam as reader
from backend.memory.summary_trial_budget import BudgetLedger
from backend.memory.caretaker_summary import SummaryError


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return exam.create_corpus(tmp_path_factory.mktemp("novel") / "corpus")


def offline(case, db):
    return core.readonly_pipeline(db, exam.OWNER)(case["query"], as_of=exam.AS_OF,
        planner=lambda _: copy.deepcopy(case["plan"]),
        selector=lambda request: exam.reference_draft(case, request),
        object_reviewer=lambda request: exam.reference_draft(case, request))


@pytest.mark.parametrize("index", range(6))
def test_frozen_questions_have_reachable_sources_without_gold_or_network(corpus, monkeypatch, index):
    def forbidden(*args, **kwargs):
        pytest.fail("offline wiring cannot migrate or call a model")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(records_v1, "migrate_records_db", forbidden)
    before = hashlib.sha256(corpus.read_bytes()).hexdigest()
    case = exam.load_cases()[index]
    result = offline(case, corpus)
    assert exam.score(case, result)["passed"]
    assert result["trace"]["planner_attempts"] == 1 and result["trace"]["evidence_review_attempts"] == 1
    assert result["trace"]["retrieval_operations"] <= 5
    assert hashlib.sha256(corpus.read_bytes()).hexdigest() == before
    package = json.loads((corpus.parent / "synthetic-source.json").read_text(encoding="utf-8"))
    sources = {r["record_id"]: r for r in package["records"]}
    assert evaluation.verify_brief(result["brief"], sources.get)["all_references_valid"]
    cards = result["audit"]["selection"]["prepared"]["request"]["candidates"]
    assert all(set(c) == {"candidate_id", "source_kind", "role", "text", "excerpt_incomplete"} for c in cards)


def test_separate_branches_do_not_create_a_fictitious_order(corpus):
    case = exam.load_cases()[2]
    result = offline(case, corpus)
    request = result["audit"]["selection"]["prepared"]["request"]
    assert request["source_order_windows"] == []
    assert evidence_packet.expand(result["brief"])["gaps"] == ["conflicting_evidence"]


def test_scorer_rejects_a_short_one_sided_or_overconfident_packet(corpus):
    case = exam.load_cases()[2]
    result = offline(case, corpus)
    result["brief"] = evidence_packet.expand(result["brief"])
    result["brief"]["items"].pop()
    assert not exam.score(case, result)["passed"]
    result = offline(case, corpus)
    result["audit"]["selection"]["judgments"]["gaps"] = []
    assert not exam.score(case, result)["passed"]


def test_import_has_no_gold_and_cannot_reuse_existing_directory(corpus):
    package = json.loads((corpus.parent / "synthetic-source.json").read_text(encoding="utf-8"))
    assert len(package["records"]) == 17
    assert all(not set(r) & {"selected", "relation", "required", "plan", "expected_gaps"} for r in package["records"])
    with pytest.raises(FileExistsError):
        exam.create_corpus(corpus.parent)


def reader_fixture(corpus):
    case = exam.load_cases()[2]
    packet = offline(case, corpus)["brief"]
    plain = evidence_packet.expand(packet)
    draft = {"answer": "两条记录相互矛盾，目前不能确定。", "can_answer_completely": False,
        "unresolved": ["conflicting_evidence"], "citations": [
            {"record_id": item["references"][0]["record_id"], "role": item["references"][0]["role"],
             "quote": item["text"]} for item in plain["items"]]}
    return case, packet, draft


def test_receiver_uses_only_the_compact_packet_and_no_evaluator_labels(corpus):
    case, packet, draft = reader_fixture(corpus)
    payload = reader.request_payload(case["query"], packet)
    content = json.loads(payload["input"][1]["content"])
    assert content == {"question": case["query"], "packet": packet}
    assert not set(content) & {"plan", "expected_gaps", "records", "required"}
    assert payload["store"] is False and reader.reservation(case["query"], packet) < Decimal("0.14")
    assert reader.score(case, packet, draft)["mechanical_passed"]


@pytest.mark.parametrize("mutation", ["alias_as_id", "wrong_role", "one_side", "certainty", "gaps"])
def test_receiver_cannot_get_credit_for_wrong_sources_or_erased_uncertainty(corpus, mutation):
    case, packet, draft = reader_fixture(corpus)
    if mutation == "alias_as_id":
        draft["citations"][0]["record_id"] = "s1"
    elif mutation == "wrong_role":
        draft["citations"][0]["role"] = "not-the-speaker"
    elif mutation == "one_side":
        draft["citations"].pop()
    elif mutation == "certainty":
        draft["can_answer_completely"] = True
    else:
        draft["unresolved"] = []
    assert not reader.score(case, packet, draft)["mechanical_passed"]


def test_receiver_rejects_non_synthetic_packet_before_transport(corpus):
    case, packet, _ = reader_fixture(corpus)
    plain = evidence_packet.expand(packet)
    for item in plain["items"]:
        item["references"][0]["source_kind"] = "chatgpt_export"
    with pytest.raises(SummaryError, match="synthetic"):
        reader.request_payload(case["query"], evidence_packet.pack(plain))


def test_larger_delegated_ledger_never_relaxes_older_caller_limits(tmp_path):
    path = tmp_path / "budget.json"
    data = {"schema": "echo-summary-budget-v1", "provider": "openai", "limit_usd": "10.00",
        "prior_reserved_usd": "0", "reservations": [{"reserved_usd": "5.70", "run_id": "prior"}]}
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    for ceiling in ("0.50", "2.00", "3.00", "4.00", "5.00", "6.00"):
        with pytest.raises(SummaryError):
            with BudgetLedger(path, maximum_limit=Decimal(ceiling)):
                pass
        assert path.read_bytes() == before
    with BudgetLedger(path, maximum_limit=Decimal("10.00")) as ledger:
        ledger.reserve(amount=Decimal("0.20"), run_id="novel", model="synthetic", case_id="first")
        assert ledger.total == Decimal("5.90") and ledger.data["reservations"][0] == data["reservations"][0]
