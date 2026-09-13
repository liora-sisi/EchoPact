import hashlib
import json
from pathlib import Path

import pytest

from backend.adapters import codex_app_server_capture as capture_module
from backend.adapters.codex_app_server_capture import (
    CodexCaptureError,
    build_codex_rollout_snapshot,
    capture_codex_threads,
    write_codex_rollout_snapshot,
)
from backend.adapters.codex_rollout_v1 import (
    CodexRolloutAdapterError,
    codex_rollout_checksum,
    convert_codex_rollout,
    dry_run_codex_rollout,
    serialize_record_package,
    write_converted_package,
)
from backend.memory.records_v1 import (
    check_records_index_consistency,
    import_record_package,
    load_record_package,
    recall_records,
)


STATIC_FIXTURE = Path(__file__).parent / "fixtures" / "codex_rollout_v1.json"


def _load_fixture():
    return json.loads(STATIC_FIXTURE.read_text(encoding="utf-8"))


def _write_snapshot(path: Path, payload, *, refresh_checksum=True) -> Path:
    if refresh_checksum and isinstance(payload.get("threads"), list):
        payload["checksum"] = codex_rollout_checksum(payload["threads"])
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _issue_codes(report, category):
    return {issue["code"] for issue in report[category]}


def test_committed_synthetic_fixture_remains_convertible():
    report = dry_run_codex_rollout(str(STATIC_FIXTURE))

    assert report["can_convert"] is True
    assert report["checksum"]["valid"] is True
    assert report["counts"] == {
        "threads": 1,
        "turns": 2,
        "items": 7,
        "user_messages": 2,
        "agent_messages": 3,
        "ignored_nonconversation_items": 2,
    }
    assert report["estimated"]["output_records"] == 5


def test_capture_envelope_builder_matches_adapter_checksum():
    payload = _load_fixture()

    snapshot = build_codex_rollout_snapshot(
        payload["threads"],
        codex_cli_version="codex-cli 0.153.4",
        captured_at="2026-09-14T00:00:00+08:00",
    )

    assert snapshot["format"] == "echo-pact-codex-rollout"
    assert snapshot["formatVersion"] == 1
    assert snapshot["sourceMethod"] == "thread/read"
    assert snapshot["acquisitionMethods"] == ["thread/read"]
    assert snapshot["checksum"] == codex_rollout_checksum(snapshot["threads"])


def test_app_server_initialization_opts_into_reviewed_pagination_api():
    client = object.__new__(capture_module.AppServerClient)
    calls = []
    client.request = lambda method, params: calls.append(("request", method, params))
    client.notify = lambda method, params: calls.append(("notify", method, params))

    client.initialize()

    assert calls[0][0:2] == ("request", "initialize")
    assert calls[0][2]["capabilities"]["experimentalApi"] is True
    assert calls[1] == ("notify", "initialized", {})


def test_capture_reads_only_explicit_thread_ids(monkeypatch):
    payload = _load_fixture()
    thread = payload["threads"][0]
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            calls.append(("construct", args, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            calls.append(("close",))

        def initialize(self):
            calls.append(("initialize",))

        def request(self, method, params):
            calls.append(("request", method, params))
            return {"thread": thread}

    monkeypatch.setattr(capture_module, "AppServerClient", FakeClient)
    monkeypatch.setattr(
        capture_module,
        "_read_codex_version",
        lambda executable, timeout: "codex-cli synthetic",
    )

    snapshot = capture_codex_threads([thread["id"]])

    assert snapshot["threads"] == [thread]
    assert (
        "request",
        "thread/read",
        {"threadId": thread["id"], "includeTurns": False},
    ) in calls
    assert (
        "request",
        "thread/read",
        {"threadId": thread["id"], "includeTurns": True},
    ) in calls
    assert not any(call[0] == "request" and call[1] == "thread/list" for call in calls)


def test_capture_hydrates_paginated_threads_without_enumerating(monkeypatch):
    payload = _load_fixture()
    source_thread = payload["threads"][0]
    metadata = dict(source_thread, historyMode="paginated", turns=[])
    pages = {
        None: {"data": [source_thread["turns"][0]], "nextCursor": "next-page"},
        "next-page": {"data": [source_thread["turns"][1]], "nextCursor": None},
    }
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            pass

        def initialize(self):
            pass

        def request(self, method, params):
            calls.append((method, params))
            if method == "thread/read":
                assert params["includeTurns"] is False
                return {"thread": metadata}
            if method == "thread/turns/list":
                return pages[params.get("cursor")]
            raise AssertionError(f"unexpected method: {method}")

    monkeypatch.setattr(capture_module, "AppServerClient", FakeClient)
    monkeypatch.setattr(
        capture_module,
        "_read_codex_version",
        lambda executable, timeout: "codex-cli synthetic",
    )

    snapshot = capture_codex_threads([source_thread["id"]])

    assert snapshot["threads"][0]["turns"] == source_thread["turns"]
    assert snapshot["acquisitionMethods"] == ["thread/read", "thread/turns/list"]
    turn_calls = [params for method, params in calls if method == "thread/turns/list"]
    assert turn_calls == [
        {
            "threadId": source_thread["id"],
            "limit": 100,
            "sortDirection": "asc",
            "itemsView": "full",
        },
        {
            "threadId": source_thread["id"],
            "limit": 100,
            "sortDirection": "asc",
            "itemsView": "full",
            "cursor": "next-page",
        },
    ]
    assert not any(method == "thread/list" for method, _ in calls)


def test_capture_rejects_repeated_paginated_cursor(monkeypatch):
    payload = _load_fixture()
    source_thread = payload["threads"][0]
    metadata = dict(source_thread, historyMode="paginated", turns=[])

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            pass

        def initialize(self):
            pass

        def request(self, method, params):
            if method == "thread/read":
                return {"thread": metadata}
            return {"data": [], "nextCursor": "stuck"}

    monkeypatch.setattr(capture_module, "AppServerClient", FakeClient)
    monkeypatch.setattr(
        capture_module,
        "_read_codex_version",
        lambda executable, timeout: "codex-cli synthetic",
    )

    with pytest.raises(CodexCaptureError, match="repeated a pagination cursor"):
        capture_codex_threads([source_thread["id"]])


def test_capture_refuses_unbounded_or_duplicate_selection():
    with pytest.raises(CodexCaptureError):
        capture_codex_threads([])
    with pytest.raises(CodexCaptureError):
        capture_codex_threads(["same", "same"])
    with pytest.raises(CodexCaptureError):
        capture_codex_threads([f"thread-{index}" for index in range(51)])
    with pytest.raises(CodexCaptureError):
        capture_codex_threads(["thread-one"], timeout_seconds=0)


def test_capture_output_is_atomic_and_refuses_overwrite(tmp_path):
    payload = _load_fixture()
    snapshot = build_codex_rollout_snapshot(
        payload["threads"],
        codex_cli_version="codex-cli synthetic",
        captured_at=payload["capturedAt"],
    )
    output_path = tmp_path / "private-codex-snapshot.json"

    result = write_codex_rollout_snapshot(snapshot, str(output_path))

    assert result["threads"] == 1
    assert result["message_text_reported"] is False
    assert json.loads(output_path.read_text(encoding="utf-8")) == snapshot
    with pytest.raises(FileExistsError):
        write_codex_rollout_snapshot(snapshot, str(output_path))


def test_dry_run_is_redacted_and_writes_nothing(tmp_path):
    payload = _load_fixture()
    input_path = _write_snapshot(tmp_path / "codex.json", payload)
    before = hashlib.sha256(input_path.read_bytes()).hexdigest()

    report = dry_run_codex_rollout(str(input_path))
    rendered = json.dumps(report, ensure_ascii=False)

    assert report["can_convert"] is True
    assert report["privacy"] == {
        "message_text_reported": False,
        "cwd_reported": False,
        "media_locations_preserved": False,
        "tool_and_reasoning_items_imported": False,
    }
    assert "CODEX-ROLL-731" not in rendered
    assert "private-image.png" not in rendered
    assert "Synthetic" not in rendered
    assert hashlib.sha256(input_path.read_bytes()).hexdigest() == before
    assert list(tmp_path.iterdir()) == [input_path]


def test_visible_messages_convert_but_internal_items_and_media_paths_do_not():
    package, report = convert_codex_rollout(str(STATIC_FIXTURE))
    serialized = serialize_record_package(package).decode("utf-8")

    assert report["estimated"]["output_records"] == 5
    assert package["schema_version"] == "echo-pact-records-v2"
    assert {record["role"] for record in package["records"]} == {
        "user",
        "assistant",
    }
    assert all(record["source_kind"] == "codex-app-server-v1" for record in package["records"])
    assert all(record["verified"] is False for record in package["records"])
    assert all(
        record["authority"] == "codex-local-unverified-archive"
        for record in package["records"]
    )
    assert "[Codex local image input]" in serialized
    assert "private-image.png" not in serialized
    assert "synthetic-command" not in serialized
    assert "合成工具输出" not in serialized
    assert "合成推理" not in serialized
    assert any("phase=commentary" in record["source_ref"] for record in package["records"])
    positions = [
        record["branch_memberships"][0]["position"]
        for record in package["records"]
    ]
    assert positions == list(range(5))


def test_checksum_mismatch_is_rejected(tmp_path):
    payload = _load_fixture()
    payload["threads"][0]["turns"][0]["items"][0]["content"][0]["text"] = "tampered"
    input_path = _write_snapshot(
        tmp_path / "tampered.json",
        payload,
        refresh_checksum=False,
    )

    report = dry_run_codex_rollout(str(input_path))

    assert report["can_convert"] is False
    assert "checksum-mismatch" in _issue_codes(report, "fatal")


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("format", "some-other-format", "unsupported-format"),
        ("formatVersion", 2, "unsupported-format-version"),
        ("sourceMethod", "thread/resume", "unsupported-source-method"),
    ],
)
def test_wrong_protocol_identity_is_rejected(tmp_path, field, value, code):
    payload = _load_fixture()
    payload[field] = value
    input_path = _write_snapshot(tmp_path / f"wrong-{field}.json", payload)

    report = dry_run_codex_rollout(str(input_path))

    assert report["can_convert"] is False
    assert code in _issue_codes(report, "fatal")


def test_missing_original_turn_times_are_not_replaced_by_capture_time(tmp_path):
    user_payload = _load_fixture()
    user_payload["threads"][0]["turns"][0]["startedAt"] = None
    user_path = _write_snapshot(tmp_path / "missing-user-time.json", user_payload)

    agent_payload = _load_fixture()
    agent_payload["threads"][0]["turns"][0]["completedAt"] = None
    agent_path = _write_snapshot(tmp_path / "missing-agent-time.json", agent_payload)

    user_report = dry_run_codex_rollout(str(user_path))
    agent_report = dry_run_codex_rollout(str(agent_path))

    assert "missing-user-message-time" in _issue_codes(user_report, "fatal")
    assert "missing-agent-message-time" in _issue_codes(agent_report, "fatal")


def test_interrupted_turn_uses_original_start_as_flagged_assistant_time(tmp_path):
    payload = _load_fixture()
    turn = payload["threads"][0]["turns"][0]
    turn["status"] = "interrupted"
    turn["completedAt"] = None
    input_path = _write_snapshot(tmp_path / "interrupted.json", payload)

    package, report = convert_codex_rollout(str(input_path))
    affected = [
        record
        for record in package["records"]
        if f"/turn/{turn['id']}/" in record["source_ref"]
        and record["role"] == "assistant"
    ]

    assert report["can_convert"] is True
    assert "assistant-time-fallback" in _issue_codes(report, "warnings")
    assert affected
    assert all(
        record["created_at"] == "2023-11-19T13:20:01Z" for record in affected
    )
    assert all(
        "timeBasis=interrupted-turn-start" in record["source_ref"]
        for record in affected
    )


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda turn: turn.update(itemsView="summary"), "incomplete-turn-items"),
        (lambda turn: turn.update(status="inProgress"), "nonterminal-turn"),
    ],
)
def test_partial_or_active_turns_fail_closed(tmp_path, mutation, code):
    payload = _load_fixture()
    mutation(payload["threads"][0]["turns"][0])
    input_path = _write_snapshot(tmp_path / f"{code}.json", payload)

    report = dry_run_codex_rollout(str(input_path))

    assert report["can_convert"] is False
    assert code in _issue_codes(report, "fatal")


def test_new_user_input_type_fails_instead_of_silently_losing_content(tmp_path):
    payload = _load_fixture()
    payload["threads"][0]["turns"][0]["items"][0]["content"].append(
        {"type": "futureHologram", "payload": "synthetic"}
    )
    input_path = _write_snapshot(tmp_path / "future-input.json", payload)

    report = dry_run_codex_rollout(str(input_path))

    assert report["can_convert"] is False
    assert "unrecognized-user-input-type" in _issue_codes(report, "fatal")


def test_duplicate_message_item_ids_fail_closed(tmp_path):
    payload = _load_fixture()
    turns = payload["threads"][0]["turns"]
    turns[1]["items"][0]["id"] = turns[0]["items"][0]["id"]
    input_path = _write_snapshot(tmp_path / "duplicate-item.json", payload)

    report = dry_run_codex_rollout(str(input_path))

    assert report["can_convert"] is False
    assert "duplicate-message-item-id" in _issue_codes(report, "fatal")


def test_unknown_envelope_field_is_reported_not_silently_accepted(tmp_path):
    payload = _load_fixture()
    payload["futureHeader"] = "synthetic"
    input_path = _write_snapshot(tmp_path / "future-field.json", payload)

    report = dry_run_codex_rollout(str(input_path))

    assert report["can_convert"] is True
    assert "unknown-top-level-field" in _issue_codes(report, "warnings")


def test_malformed_json_refuses_formal_output(tmp_path):
    input_path = tmp_path / "malformed.json"
    input_path.write_text('{"format":', encoding="utf-8")
    output_path = tmp_path / "must-not-exist.json"

    report = dry_run_codex_rollout(str(input_path))
    with pytest.raises(CodexRolloutAdapterError):
        write_converted_package(str(input_path), str(output_path))

    assert report["can_convert"] is False
    assert "malformed-json" in _issue_codes(report, "fatal")
    assert not output_path.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_deterministic_conversion_import_recall_and_idempotent_retry(
    tmp_path,
    monkeypatch,
):
    first_output = tmp_path / "records-first.json"
    second_output = tmp_path / "records-second.json"

    first = write_converted_package(str(STATIC_FIXTURE), str(first_output))
    second = write_converted_package(str(STATIC_FIXTURE), str(second_output))

    assert first["output_sha256"] == second["output_sha256"]
    assert first_output.read_bytes() == second_output.read_bytes()
    loaded = load_record_package(str(first_output))
    assert len(loaded.records) == 5
    assert serialize_record_package(convert_codex_rollout(str(STATIC_FIXTURE))[0]) == first_output.read_bytes()

    db_path = tmp_path / "echo-pact-codex.db"
    initial = import_record_package(str(first_output), db_path=str(db_path))
    repeated = import_record_package(str(first_output), db_path=str(db_path))
    assert initial["added"] == 5
    assert initial["branch_memberships_added"] == 5
    assert repeated["idempotent_replay"] is True
    assert repeated["added"] == 0
    assert repeated["skipped"] == 5

    def network_forbidden(*args, **kwargs):
        pytest.fail("Codex rollout recall must remain offline")

    monkeypatch.setattr("requests.post", network_forbidden)
    recalled = recall_records("CODEX-ROLL-731", db_path=str(db_path))
    assert recalled["memories"]
    result = recalled["memories"][0]
    assert result["source_kind"] == "codex-app-server-v1"
    assert result["source_ref"].startswith("codex-app-server-v1://thread/")
    assert result["conversation_id"].startswith("codex-app-server-v1:")
    assert result["authority"] == "codex-local-unverified-archive"
    assert result["verified"] is False
    assert check_records_index_consistency(str(db_path))["ok"] is True
