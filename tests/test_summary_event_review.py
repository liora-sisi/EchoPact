"""Editing proposals preserve originals, bind sources and cannot self-approve."""
import copy
from decimal import Decimal
import hashlib
import json

import pytest

from backend.memory.caretaker_summary import SummaryError
from backend.memory import summary_event_review as review
from backend.memory import summary_event_author_v5 as author
from backend.memory import summary_reading_card as cards
from scripts import summary_event_review_trial as trial
from scripts import prepare_summary_comprehensive_comparison as sources


def retained(raw):
    return {"status": "ok", "decisions": [{"item_id": item, "verdict": "retain",
        "reason": "保留已有的时间与状态。", "evidence_ids": fact["evidence_ids"], "revision": None}
        for item, fact in review._items(raw).items()], "refusal_reason": None}


def revised_fact(raw, index, **changes):
    fact = raw["events"][0]["facts"][index]
    revision = {k: copy.deepcopy(fact[k]) for k in ("text", "event", "time_evidence")}
    revision.update(changes)
    return revision


def apply(case, raw, response):
    return review.apply_review(case["records"], raw, response,
        expected_binding=review.prepare_review(case["records"], raw)["binding"])


def test_correct_positive_controls_remain_unchanged_without_semantic_approval():
    case, raw = trial.control_case()
    before = copy.deepcopy(raw)
    result = apply(case, raw, retained(raw))
    assert raw == before == result["candidate"]
    assert result["receipt"]["review_complete"] and not result["receipt"]["changes"]
    assert result["receipt"]["semantic_passed"] is None and not result["receipt"]["production_accepted"]
    # Same-day cancellation is valid; known non-occurrence must not be weakened to unknown.
    projected, dates = author.inspect_and_project(raw, case["records"], author.prepare_author_window(case["records"])["catalog"])
    assert not dates["errors"]
    facts = projected["events"][0]["facts"]
    assert facts[0]["time_context"]["cancelled_at"] == facts[0]["planned_date"]
    assert [f["event_status"] for f in facts[1:6]] == ["unknown", "not_happened", "happened", "not_happened", "unknown"]


def test_targeted_correction_preserves_every_other_fact_and_first_answer():
    case, raw = trial.control_case()
    raw["events"][0]["facts"][1]["event"] = {"status": "not_happened"}
    before = copy.deepcopy(raw)
    response = retained(raw)
    response["decisions"][1].update(verdict="revise", revision=revised_fact(raw, 1, event={"status": "unknown"}))
    result = apply(case, raw, response)
    assert raw == before
    assert result["candidate"]["events"][0]["facts"][1]["event"]["status"] == "unknown"
    expected = copy.deepcopy(raw)
    expected["events"][0]["facts"][1]["event"] = {"status": "unknown"}
    assert result["candidate"] == expected
    assert [c["item_id"] for c in result["receipt"]["changes"]] == ["e1.f2"]
    assert result["receipt"]["candidate_sha256"] != result["receipt"]["first_draft_sha256"]


def test_frozen_json_key_order_is_not_mistaken_for_changed_material():
    case, raw = trial.control_case()
    before = review.prepare_review(case["records"], raw)
    saved_case, saved_raw = json.loads(json.dumps(case, sort_keys=True)), json.loads(json.dumps(raw, sort_keys=True))
    after = review.prepare_review(saved_case["records"], saved_raw)
    assert before["binding"] == after["binding"]
    result = review.apply_review(saved_case["records"], saved_raw, retained(raw), expected_binding=before["binding"])
    assert result["candidate"] == raw


def test_legacy_replay_revalidates_exact_request_and_semantic_material():
    case, raw = trial.control_case()
    prepared = review.prepare_review(case["records"], raw)
    old = {k: v for k, v in prepared["binding"].items() if k != "binding_version"}
    old["request_material_sha256"] = cards.fingerprint({"messages": prepared["messages"], "schema": prepared["response_schema"]})
    rebound = review.rebind_saved_request(case["records"], json.loads(json.dumps(raw, sort_keys=True)), prepared["messages"], old)
    assert rebound["binding"] == prepared["binding"] and rebound["legacy_binding"] == old
    changed = copy.deepcopy(prepared["messages"])
    payload = json.loads(changed[1]["content"])
    payload["first_draft"]["events"][0]["facts"][0]["text"] += "changed"
    changed[1]["content"] = json.dumps(payload)
    # Even if a caller recomputes the old request hash, semantic content must match the pinned originals.
    old["request_material_sha256"] = cards.fingerprint({"messages": changed, "schema": prepared["response_schema"]})
    with pytest.raises(SummaryError, match="saved_request_changed"):
        review.rebind_saved_request(case["records"], raw, changed, old)


@pytest.mark.parametrize("problem", ["missing", "duplicate", "retain_edit", "revise_none", "empty_edit", "foreign_ref", "rewrite_citation"])
def test_incomplete_or_out_of_scope_editing_cannot_produce_a_candidate(problem):
    case, raw = trial.control_case()
    response = retained(raw)
    if problem == "missing": response["decisions"].pop()
    if problem == "duplicate": response["decisions"][1] = copy.deepcopy(response["decisions"][0])
    if problem == "retain_edit": response["decisions"][0]["revision"] = revised_fact(raw, 0)
    if problem == "revise_none": response["decisions"][0]["verdict"] = "revise"
    if problem == "empty_edit": response["decisions"][0].update(verdict="revise", revision=revised_fact(raw, 0))
    if problem == "foreign_ref": response["decisions"][0]["evidence_ids"] = ["s999"]
    if problem == "rewrite_citation":
        revision = revised_fact(raw, 0)
        revision["evidence_ids"] = ["s2"]
        response["decisions"][0].update(verdict="revise", revision=revision)
    with pytest.raises(SummaryError): apply(case, raw, response)


@pytest.mark.parametrize("kind", ["partial", "refused", "needs_context"])
def test_unfinished_review_is_retained_without_applying_partial_edits(kind):
    case, raw = trial.control_case()
    response = retained(raw)
    if kind == "needs_context":
        response["decisions"][0]["verdict"] = kind
    else:
        response.update(status=kind, refusal_reason="未完成本范围核对。", decisions=[])
    result = apply(case, raw, response)
    assert result["candidate"] is None and not result["receipt"]["review_complete"]
    assert result["receipt"]["unreviewed_items"] or result["receipt"]["unresolved_items"]


@pytest.mark.parametrize("kind", ["source", "draft", "binding"])
def test_saved_review_cannot_apply_to_changed_material(kind):
    case, raw = trial.control_case()
    binding = review.prepare_review(case["records"], raw)["binding"]
    response = retained(raw)
    if kind == "source": case["records"][0]["content"] += "来源已变。"
    if kind == "draft": raw["events"][0]["facts"][0]["text"] += "草稿已变。"
    if kind == "binding": binding["recipe"] = "other"
    with pytest.raises(SummaryError, match="changed_material"):
        review.apply_review(case["records"], raw, response, expected_binding=binding)


def test_editor_cannot_expand_bounded_repair_into_whole_window_rewrite():
    case, raw = trial.control_case()
    seed = copy.deepcopy(raw["events"][0]["facts"][-1])
    raw["events"][0]["facts"] = [copy.deepcopy(seed) for _ in range(17)]
    response = retained(raw)
    for i, decision in enumerate(response["decisions"]):
        decision.update(verdict="revise", revision=revised_fact(raw, i, text="林清看到相册会高兴。"))
    with pytest.raises(SummaryError, match="revision_limit"):
        apply(case, raw, response)


def first_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "PRIVATE_ROOT", tmp_path)
    directory = tmp_path / "first"
    hashes = {}
    for cid in trial.base.CASE_IDS:
        folder = directory / (cid + "-event-v5")
        folder.mkdir(parents=True)
        raw = {"status": "ok", "events": [{"event_id": "e1", "topic": "合成管道测试", "kind": "mixed",
            "facts": [{"text": "待核对的合成管道陈述。", "event": {"status": "not_applicable"},
                "evidence_ids": ["s1"], "statement_role": "direct", "supersedes": None,
                "time_evidence": [], "history_time_evidence": []}], "uncertainties": []}],
            "uncertainties": [], "relations": [], "gaps": [], "refusal_reason": None}
        path = folder / "final-response.json"
        path.write_text(json.dumps({"finish": "complete", "model": "gpt-5.6-sol", "final_content": json.dumps(raw)}), encoding="utf-8")
        hashes[cid] = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(trial, "FIRST_HASHES", hashes)
    return directory


def test_frozen_jobs_pin_first_responses_and_never_send_gold(tmp_path, monkeypatch):
    first = first_inputs(tmp_path, monkeypatch)
    suite = trial.build_suite(first)
    assert len(suite["jobs.json"]) == 4
    for job in suite["jobs.json"]:
        request = json.dumps(job["request"])
        assert "review_targets" not in request and "forbidden_claims" not in request
        assert "independent_review" not in request
    trial.freeze(tmp_path / "frozen", suite)
    assert trial.load_frozen(tmp_path / "frozen", first) == suite
    old = first / (trial.base.CASE_IDS[0] + "-event-v5") / "final-response.json"
    old.write_bytes(old.read_bytes() + b" ")
    with pytest.raises(SummaryError, match="unrecognized_first_answer"): trial.build_suite(first)


def ledger_file(tmp_path, *, prior="0", provider="openai"):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"schema": "echo-summary-budget-v1", "provider": provider,
        "limit_usd": "20", "prior_reserved_usd": prior, "reservations": []}), encoding="utf-8")
    return path


@pytest.mark.parametrize("problem", ["authorization", "changed", "budget", "provider"])
def test_preflight_failures_do_not_read_credentials(tmp_path, monkeypatch, problem):
    first = first_inputs(tmp_path, monkeypatch)
    suite = trial.build_suite(first)
    if problem == "changed": suite["jobs.json"][0]["request"]["input"][0]["content"] += "changed"
    ledger = ledger_file(tmp_path, prior="19.99" if problem == "budget" else "0", provider="deepseek" if problem == "provider" else "openai")
    def no_key(*args, **kwargs): pytest.fail("credential must stay unopened")
    args = (suite, first, tmp_path / "run", tmp_path / "key", ledger)
    if problem in {"authorization", "changed"}:
        with pytest.raises(SummaryError): trial.run(*args, authorized=problem != "authorization", key_reader=no_key)
    else:
        result = trial.run(*args, authorized=True, key_reader=no_key)
        assert result["status"] == "stopped_no_retry" and result["model_calls_attempted"] == 0


def test_failed_call_keeps_first_response_reservation_and_blocks_repeat(tmp_path, monkeypatch):
    first = first_inputs(tmp_path, monkeypatch)
    suite = trial.build_suite(first)
    ledger = ledger_file(tmp_path)
    monkeypatch.setattr(trial.transport, "preflight", lambda c: {"synthetic": True})
    calls = []
    class Client:
        def __init__(self, *args): pass
        def request(self, endpoint, payload):
            assert len(json.loads(ledger.read_text())["reservations"]) == 1
            calls.append(payload)
            if len(calls) == 2: raise SummaryError("synthetic_network_failure")
            raw = json.loads(payload["input"][1]["content"])["first_draft"]
            return {"model": "gpt-5.6-sol", "status": "completed", "usage": {"input_tokens": 10, "output_tokens": 10},
                "output": [{"type": "reasoning", "content": "must not persist"},
                    {"type": "message", "content": [{"type": "output_text", "text": json.dumps(retained(raw))}]}]}
    kwargs = {"authorized": True, "key_reader": lambda *a, **kw: "synthetic-key", "client_factory": Client}
    result = trial.run(suite, first, tmp_path / "run", tmp_path / "key", ledger, **kwargs)
    assert result["model_calls_attempted"] == 2 and len(result["outcomes"]) == 1
    saved = tmp_path / "run" / suite["jobs.json"][0]["job_id"] / "final-response.json"
    assert b"must not persist" not in saved.read_bytes()
    repeated = trial.run(suite, first, tmp_path / "repeat", tmp_path / "key", ledger, **kwargs)
    assert repeated["error_code"] == "summary_review_no_automatic_repeat" and len(calls) == 2
    assert Decimal(json.loads(ledger.read_text())["reservations"][0]["reserved_usd"]) == Decimal(trial.scope(suite)["reserved_usd"])


@pytest.mark.parametrize("version,legacy", [("v1", True), ("v2", False), ("v3", False)])
def test_frozen_round_trip_and_offline_replay_never_recall_model_or_modify_first_results(tmp_path, monkeypatch, version, legacy):
    first = first_inputs(tmp_path, monkeypatch)
    suite = trial.build_suite(first, review_version=version)
    trial.freeze(tmp_path / "frozen", suite)
    suite = trial.load_frozen(tmp_path / "frozen", first, review_version=version)
    ledger = ledger_file(tmp_path)
    monkeypatch.setattr(trial.transport, "preflight", lambda c: {"synthetic": True})
    calls = []
    class Client:
        def __init__(self, *args): pass
        def request(self, endpoint, payload):
            calls.append(payload)
            raw = json.loads(payload["input"][1]["content"])["first_draft"]
            return {"model": "gpt-5.6-sol", "status": "completed", "usage": {"input_tokens": 10, "output_tokens": 10},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(retained(raw))}]}]}
    report = trial.run(suite, first, tmp_path / "run", tmp_path / "key", ledger, authorized=True,
        key_reader=lambda *a, **kw: "synthetic-key", client_factory=Client)
    assert report["status"] == "answered_requires_semantic_review" and len(calls) == 4
    assert all(o["status"] == "candidate_requires_semantic_review" for o in report["outcomes"])
    frozen = tmp_path / "frozen"
    if legacy:
        # Reconstruct the old hash format in test-only artifacts; the saved prompt and answer stay exact.
        for job, case in zip(suite["jobs.json"], suite["reviewer.json"]):
            binding = job["review_binding"]
            binding.pop("binding_version")
            schema = review.prepare_review(case["records"], case["first_draft"], version="v1")["response_schema"]
            binding["request_material_sha256"] = cards.fingerprint({"messages": job["request"]["input"], "schema": schema})
            path = tmp_path / "run" / job["job_id"] / "attempt.json"
            path.write_text(json.dumps({k: v for k, v in job.items() if k != "request"}), encoding="utf-8")
        suite["manifest.json"]["jobs_sha256"] = trial.digest(suite["jobs.json"])
        (tmp_path / "run" / "manifest.json").write_text(json.dumps(suite["manifest.json"]), encoding="utf-8")
        frozen = tmp_path / "legacy-frozen"
        trial.freeze(frozen, suite)
    before = {p: p.read_bytes() for p in (tmp_path / "run").rglob('*.json')}
    ledger_before = ledger.read_bytes()
    replayed = trial.replay_saved(frozen, first, tmp_path / "run", tmp_path / "replay")
    assert replayed["model_calls_attempted"] == 0 and len(calls) == 4
    assert ledger.read_bytes() == ledger_before
    assert all(p.read_bytes() == content for p, content in before.items())
    assert all(o["status"] == "candidate_requires_semantic_review" for o in replayed["outcomes"])
    request_path = tmp_path / "run" / suite["jobs.json"][0]["job_id"] / "request.json"
    changed = json.loads(request_path.read_text(encoding="utf-8"))
    changed["input"][1]["content"] += " changed"
    request_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(SummaryError, match="replay_material_changed"):
        trial.replay_saved(frozen, first, tmp_path / "run", tmp_path / "must-not-create")
    assert not (tmp_path / "must-not-create").exists()
