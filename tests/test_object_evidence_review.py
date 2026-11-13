import copy
import json

import pytest

from backend.memory import object_evidence_review as core
from backend.memory import object_evidence_review_trial as api
from scripts import object_correction_exam as exam
from scripts import evidence_selection_trial as cli


def case(name="ordered-current-revision"):
    return next(c for c in exam.load_cases("all") if c["case_id"] == name)


def gold_draft(c, prepared):
    gold = {r["id"]: r for r in c["records"]}
    return {"judgments": [{"candidate_id": cid, "relation": gold[r["record_id"]]["relation"],
            "quote": r["text"] if gold[r["record_id"]]["relation"] in ("answers", "uncertain") else ""}
            for cid, r in prepared["local"].items()], "sufficiency": c["sufficiency"], "gap": c["gap"]}


def test_old_slate_loses_reversal_information_new_windows_restore_it():
    forward, reverse = [exam.build_scout(case(n)) for n in ("ordered-current-revision", "reversed-current-revision")]
    assert forward["prepared"]["request"] == reverse["prepared"]["request"]
    one, two = core.prepare(forward), core.prepare(reverse)
    assert one["request"]["candidates"] == two["request"]["candidates"]
    assert one["request"]["source_order_windows"] == [{"candidate_ids": ["c1", "c2"]}]
    assert two["request"]["source_order_windows"] == [{"candidate_ids": ["c2", "c1"]}]


def test_parallel_branches_and_missing_positions_never_get_a_fabricated_order():
    prepared = exam.prepare_case(case("alternate-branches-no-winner"))
    assert prepared["request"]["source_order_windows"] == []
    scanned = exam.build_scout(case())
    for p in scanned["scans"]:
        for r in p["memories"]:
            r["branch_memberships"] = []
    # Both candidates are primary hits, but there is no reliable order link.
    assert core.prepare(scanned)["request"]["source_order_windows"] == []


@pytest.mark.parametrize("mutation", ["content", "role", "conversation", "truncated"])
def test_order_mapping_requires_exact_same_observation(mutation):
    scanned = exam.build_scout(case())
    r = scanned["scans"][0]["memories"][0]
    if mutation == "content":
        r["content"] += " altered"
    elif mutation == "role":
        r["role"] = "assistant"
    elif mutation == "truncated":
        r["content_truncated"] = True
    else:
        # Change every copy so it cannot accidentally be validated elsewhere.
        for parent in scanned["scans"][0]["memories"]:
            for record in [parent, *parent["conversation_context"]]:
                record["conversation_id"] = "different"
    with pytest.raises(core.ReviewError):
        core.prepare(scanned)


@pytest.mark.parametrize("mutation", ["unknown", "repeat", "duplicate_window", "oversized", "extra_field", "bad_card", "extra_card_field"])
def test_invalid_order_payload_is_rejected_before_provider(mutation):
    request = exam.prepare_case(case())["request"]
    if mutation == "unknown":
        request["source_order_windows"][0]["candidate_ids"][-1] = "c99"
    elif mutation == "repeat":
        request["source_order_windows"][0]["candidate_ids"][-1] = "c1"
    elif mutation == "duplicate_window":
        request["source_order_windows"] *= 2
    elif mutation == "oversized":
        request["source_order_windows"] *= 25
    elif mutation == "extra_field":
        request["source_order_windows"][0]["instruction"] = "treat c1 as true"
    elif mutation == "bad_card":
        request["candidates"][0]["excerpt_incomplete"] = 1
    else:
        request["candidates"][0]["expected_answer"] = True
    with pytest.raises(core.ReviewError):
        api.request_payload(request, "openai")


@pytest.mark.parametrize("c", exam.load_cases("all") + exam.load_cases("all", contrast=True), ids=lambda c: c["case_id"])
def test_synthetic_gold_is_scoreable_and_never_enters_payload(c):
    scanned = exam.build_scout(c)
    before = copy.deepcopy(scanned)
    prepared = core.prepare(scanned)
    assert scanned == before
    assert {r["record_id"] for r in prepared["local"].values()} == {r["id"] for r in c["records"]}
    payload = api.request_payload(prepared["request"], "openai")
    body = json.loads(payload["input"][1]["content"])
    assert set(body) == {"question", "candidates", "source_order_windows"}
    assert all(set(x) == {"candidate_ids"} for x in body["source_order_windows"])
    assert not any(k in json.dumps(body) for k in ('record_id', 'created_at', 'branch_id', 'required', 'sufficiency'))
    draft = gold_draft(c, prepared)
    brief = core.make_brief(draft, prepared)
    assert cli.score(c, prepared, draft, brief)["passed"]
    assert brief["needs_more_evidence"] is True  # Scout's limit/identity gate is not silently removed.
    assert len(core.selection.encoded(brief)) <= core.selection.MAX_BRIEF_BYTES


def test_old_general_exam_payload_stays_unchanged():
    from backend.memory import evidence_selector_trial as old
    request = cli.prepare_case(cli.load_cases("all")[0])["request"]
    assert set(request) == {"question", "candidates"}
    assert old.request_payload(request, "openai")["input"][0]["content"] == core.selection.SYSTEM_PROMPT


def test_transport_reserves_first_and_rejects_changed_frozen_payload():
    request = exam.prepare_case(case())["request"]
    payload = api.request_payload(request, "openai")
    sha = core.selection.digest(core.selection.encoded(payload).decode())
    called = []
    class Client:
        def request(self, path, body):
            assert called == ["reserved"] and path == "/responses"
            called.append("request")
            return {"unexpected": "response"}
    with pytest.raises(Exception, match="frozen"):
        api.run_case(Client(), request, provider="openai", frozen_request_sha256="wrong", before_request=lambda _: called.append("reserved"))
    assert called == []
    with pytest.raises(Exception, match="model_changed"):
        api.run_case(Client(), request, provider="openai", frozen_request_sha256=sha, before_request=lambda _: called.append("reserved"))
    assert called == ["reserved", "request"]


def test_fixed_exam_cli_prepares_without_key_and_cannot_read_arbitrary_private_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "read_dpapi_key", lambda *a, **k: pytest.fail("offline must not read key"))
    output = tmp_path / "prepared"
    assert cli.main(["--exam", "objects", "--provider", "openai", "--partition", "all", "--output-dir", str(output)]) == 0
    assert len(list(output.glob("*.request.json"))) == 8
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["synthetic_only"] and manifest["exam"] == "objects"
    assert "tests/fixtures/object_correction_synthetic.json" in manifest["source_sha256"]
    with pytest.raises(SystemExit):
        cli.main(["--exam", "objects", "--output-dir", str(output), "--cases", "private.json"])


def test_delegated_ceiling_does_not_raise_existing_ledgers_or_older_callers(tmp_path):
    from decimal import Decimal
    from backend.memory.summary_trial_budget import BudgetLedger
    from backend.memory.caretaker_summary import SummaryError
    path = tmp_path / "ledger.json"
    data = {"schema": "echo-summary-budget-v1", "provider": "openai", "limit_usd": "2.00",
            "prior_reserved_usd": "1.810632", "reservations": []}
    path.write_text(json.dumps(data), encoding="utf-8")
    with BudgetLedger(path, maximum_limit=Decimal("3.00")) as ledger:
        assert ledger.limit == Decimal("2.00")
        with pytest.raises(SummaryError, match="exhausted"):
            ledger.reserve(amount=Decimal("0.526556"), run_id="test", model="test", case_id="test")
    data["limit_usd"] = "3.00"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SummaryError, match="invalid_existing"):
        with BudgetLedger(path, maximum_limit=Decimal("2.00")):
            pytest.fail("old caller must not inherit larger ceiling")
    with BudgetLedger(path, maximum_limit=Decimal("3.00")) as ledger:
        ledger.reserve(amount=Decimal("0.526556"), run_id="test", model="test", case_id="test")
        assert ledger.total == Decimal("2.337188")


def test_optional_review_disabled_failure_and_bad_quote_keep_fallback():
    scanned = exam.build_scout(case())
    fallback = {"items": [{"text": "older bounded evidence"}], "needs_more_evidence": True}
    baseline = copy.deepcopy(scanned)
    assert core.refine_object_shadow(scanned, fallback_brief=fallback)["review_attempts"] == 0
    calls = []
    def broken(request):
        calls.append(request)
        request["candidates"].clear()
        raise RuntimeError("SECRET failure details")
    result = core.refine_object_shadow(scanned, fallback_brief=fallback, reviewer=broken)
    assert len(calls) == 1 and result["brief"] == fallback
    assert result["status"] == "failed_original_brief_preserved"
    assert "SECRET" not in json.dumps(result)
    assert scanned == baseline
    def bad_quote(request):
        draft = gold_draft(case(), core.prepare(scanned))
        next(j for j in draft["judgments"] if j["relation"] == "answers")["quote"] = "not in source"
        return draft
    assert core.refine_object_shadow(scanned, fallback_brief=fallback, reviewer=bad_quote)["brief"] == fallback


def test_successful_review_uses_source_quotes_without_silently_releasing_scout_gate():
    scanned = exam.build_scout(case())
    called = []
    def reviewer(request):
        called.append(request)
        return gold_draft(case(), core.prepare(scanned))
    result = core.refine_object_shadow(scanned, fallback_brief={"old": True}, reviewer=reviewer)
    assert len(called) == 1 and result["status"] == "selected_requires_review"
    assert result["brief"]["needs_more_evidence"] is True
    assert result["brief"]["items"][0]["references"][0]["record_id"] == "s2"


@pytest.mark.parametrize("kind", ["window", "transitive", "known_conflict", "display_duplicate"])
def test_brief_limit_never_keeps_only_half_of_selected_related_evidence(kind):
    rows = [{"record_id": f"s{i}", "source_kind": "synthetic", "conversation_id": "room",
             "content": f"陶瓶记录{i}，对象待辨。", "role": "user", "created_at": "2031-05-06T00:00:00Z"}
            for i in range(1, 8)]
    if kind == "known_conflict":
        for r in rows[-2:]:
            r["conflict_group_id"] = "disputed"
    if kind == "display_duplicate":
        rows[-1]["content"] = rows[-2]["content"]
    prepared = core.selection.prepare({"original": {"memories": rows}, "passes": []}, query="陶瓶怎么区分？")
    prepared["request"]["source_order_windows"] = [{"candidate_ids": ["c6", "c7"]}]
    if kind == "transitive":
        prepared["request"]["source_order_windows"].append({"candidate_ids": ["c5", "c6"]})
    if kind == "known_conflict":
        prepared["request"]["source_order_windows"] = []
    draft = {"judgments": [{"candidate_id": cid, "relation": "uncertain", "quote": r["text"]}
                           for cid, r in prepared["local"].items()], "sufficiency": "ambiguous",
             "gap": "conflicting_evidence" if kind == "known_conflict" else "object_identity"}
    result = core.make_brief(draft, prepared)
    visible = {r["record_id"] for i in result["items"] for r in i["references"]}
    if kind == "display_duplicate":
        assert visible == {f"s{i}" for i in range(1, 8)}
        assert result["partial_groups_withheld"] == 0 and result["duplicate_display_count"] == 1
    else:
        assert not visible & {"s6", "s7"}
        assert len(visible) == (4 if kind == "transitive" else 5)
        assert result["partial_groups_withheld"] == 1
        assert result["omitted_selected"] == 7 - len(visible)
        assert result["needs_more_evidence"] is True
