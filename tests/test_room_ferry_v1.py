import hashlib
import json
from pathlib import Path

import pytest

from backend.adapters.room_ferry_v1 import (
    FerryAdapterError,
    convert_ferry_backup,
    dry_run_ferry_backup,
    ferry_checksum,
    serialize_record_package,
    write_converted_package,
)
from backend.memory.records_v1 import (
    check_records_index_consistency,
    import_record_package,
    load_record_package,
    recall_records,
)


EXPORTED_AT = 1_786_147_200_000
FIRST_IMPORTED_AT = 1_799_999_999_999
STATIC_FIXTURE = Path(__file__).parent / "fixtures" / "room_ferry_backup_v1.json"


def _independent_checksum(data):
    compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def _conversation(conversation_id, message_count, branch_count=0):
    return {
        "id": conversation_id,
        "sourceType": "chatgpt-conversations-json",
        "sourceConversationId": f"official-{conversation_id}",
        "title": f"synthetic-{conversation_id}",
        "createdAt": 1_700_000_000_000,
        "updatedAt": 1_700_000_060_000,
        "messageCount": message_count,
        "branchCount": branch_count,
        "contentFingerprint": f"fingerprint-{conversation_id}",
        "tags": [],
        "favorite": False,
        "archived": False,
        "userNotes": "",
        "firstImportedAt": FIRST_IMPORTED_AT,
        "lastImportedAt": FIRST_IMPORTED_AT,
        "lastImportBatchId": "batch-synthetic",
    }


def _message(
    message_id,
    conversation_id,
    source_id,
    parent_source_id,
    role,
    created_at,
    text,
    sort_key,
    *,
    parts=None,
):
    return {
        "id": message_id,
        "conversationId": conversation_id,
        "sourceType": "chatgpt-conversations-json",
        "sourceMessageId": source_id,
        "parentSourceMessageId": parent_source_id,
        "role": role,
        "createdAt": created_at,
        "contentParts": parts if parts is not None else [{"type": "text", "text": text}],
        "plainText": text,
        "contentFingerprint": f"fingerprint-{message_id}",
        "sortKey": sort_key,
        "firstImportedAt": FIRST_IMPORTED_AT,
        "lastImportedAt": FIRST_IMPORTED_AT,
        "lastImportBatchId": "batch-synthetic",
    }


def _single_branch_data():
    messages = [
        _message(
            "ferry-message-root",
            "ferry-conversation-one",
            "source-root",
            None,
            "system",
            1_700_000_000_000,
            "合成系统锚点。",
            "00000000:0001700000000000:root",
        ),
        _message(
            "ferry-message-user",
            "ferry-conversation-one",
            "source-user",
            "source-root",
            "user",
            1_700_000_001_000,
            "合成事实：星港代码是 FERRY-M2-731。\n第二行保留。",
            "00000001:0001700000001000:user",
        ),
        _message(
            "ferry-message-assistant",
            "ferry-conversation-one",
            "source-assistant",
            "source-user",
            "assistant",
            1_700_000_002_000,
            "合成确认：只进行离线测试。",
            "00000002:0001700000002000:assistant",
        ),
    ]
    return {
        "conversations": [_conversation("ferry-conversation-one", len(messages))],
        "messages": messages,
        "importBatches": [
            {
                "id": "batch-synthetic",
                "sourceType": "chatgpt-conversations-json",
                "fileName": "synthetic.json",
                "fileSize": 1,
                "startedAt": FIRST_IMPORTED_AT,
                "status": "completed",
                "addedConversations": 1,
                "updatedConversations": 0,
                "unchangedConversations": 0,
                "addedMessages": len(messages),
                "updatedMessages": 0,
                "skippedItems": 0,
                "conflicts": 0,
                "errors": [],
            }
        ],
        "handoffDrafts": [
            {
                "id": "draft-synthetic",
                "title": "synthetic draft",
                "conversationIds": ["ferry-conversation-one"],
                "templateType": "complete",
                "sections": [],
                "createdAt": FIRST_IMPORTED_AT,
                "updatedAt": FIRST_IMPORTED_AT,
            }
        ],
        "appMeta": [{"key": "schemaVersion", "value": 1}],
    }


def _backup(data=None, **overrides):
    data = data if data is not None else _single_branch_data()
    result = {
        "format": "liora-elion-room-ferry-backup",
        "formatVersion": 1,
        "appVersion": "0.1.9",
        "schemaVersion": 1,
        "exportedAt": EXPORTED_AT,
        "checksumAlgorithm": "SHA-256",
        "checksum": _independent_checksum(data),
        "data": data,
    }
    result.update(overrides)
    return result


def _write_backup(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _issue_codes(report, category):
    return {issue["code"] for issue in report[category]}


def _branched_data():
    conversation_id = "ferry-conversation-branches"
    messages = [
        _message(
            "branch-root",
            conversation_id,
            "source-branch-root",
            None,
            "system",
            1_700_100_000_000,
            "分支根节点",
            "00000000:0001700100000000:root",
        ),
        _message(
            "branch-user",
            conversation_id,
            "source-branch-user",
            "source-branch-root",
            "user",
            1_700_100_001_000,
            "请选择合成航线",
            "00000001:0001700100001000:user",
        ),
        _message(
            "branch-a",
            conversation_id,
            "source-branch-a",
            "source-branch-user",
            "assistant",
            1_700_100_002_000,
            "合成航线 A",
            "00000002:0001700100002000:a",
        ),
        _message(
            "branch-b",
            conversation_id,
            "source-branch-b",
            "source-branch-user",
            "assistant",
            1_700_100_003_000,
            "合成航线 B",
            "00000002:0001700100003000:b",
        ),
        _message(
            "branch-c",
            conversation_id,
            "source-branch-c",
            "source-branch-user",
            "assistant",
            1_700_100_004_000,
            "合成航线 C",
            "00000002:0001700100004000:c",
        ),
    ]
    return {
        "conversations": [_conversation(conversation_id, len(messages), branch_count=2)],
        "messages": messages,
        "importBatches": [],
        "handoffDrafts": [],
        "appMeta": [{"key": "schemaVersion", "value": 1}],
    }


def test_checksum_matches_json_stringify_rule_for_synthetic_data():
    data = _single_branch_data()
    assert ferry_checksum(data) == _independent_checksum(data)


def test_committed_synthetic_fixture_remains_convertible():
    report = dry_run_ferry_backup(str(STATIC_FIXTURE))

    assert report["can_convert"] is True
    assert report["checksum"]["valid"] is True
    assert report["counts"]["conversations"] == 1
    assert report["counts"]["messages"] == 2
    assert report["estimated"]["output_records"] == 2


def test_dry_run_valid_backup_reports_counts_and_writes_nothing(tmp_path):
    input_path = _write_backup(tmp_path / "ferry.json", _backup())
    before = hashlib.sha256(input_path.read_bytes()).hexdigest()

    report = dry_run_ferry_backup(str(input_path))

    assert report["can_convert"] is True
    assert report["checksum"]["valid"] is True
    assert report["counts"] == {
        "conversations": 1,
        "messages": 3,
        "import_batches": 1,
        "handoff_drafts": 1,
        "app_meta": 1,
    }
    assert report["branching"]["single_branch_conversations"] == 1
    assert report["estimated"]["output_records"] == 3
    assert report["estimated"]["fatal"] == 0
    assert hashlib.sha256(input_path.read_bytes()).hexdigest() == before
    assert list(tmp_path.iterdir()) == [input_path]


def test_tampered_checksum_is_rejected(tmp_path):
    payload = _backup()
    payload["data"]["messages"][1]["plainText"] = "tampered"
    input_path = _write_backup(tmp_path / "tampered.json", payload)

    report = dry_run_ferry_backup(str(input_path))

    assert report["can_convert"] is False
    assert "checksum-mismatch" in _issue_codes(report, "fatal")


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("format", "some-other-backup", "unsupported-format"),
        ("formatVersion", 2, "unsupported-format-version"),
        ("schemaVersion", 2, "unsupported-schema-version"),
    ],
)
def test_wrong_protocol_identity_is_rejected(tmp_path, field, value, code):
    payload = _backup()
    payload[field] = value
    input_path = _write_backup(tmp_path / f"wrong-{field}.json", payload)

    report = dry_run_ferry_backup(str(input_path))

    assert report["can_convert"] is False
    assert code in _issue_codes(report, "fatal")


def test_malformed_json_is_rejected_without_output(tmp_path):
    input_path = tmp_path / "malformed.json"
    input_path.write_text('{"format":', encoding="utf-8")
    output_path = tmp_path / "must-not-exist.json"

    report = dry_run_ferry_backup(str(input_path))
    with pytest.raises(FerryAdapterError):
        write_converted_package(str(input_path), str(output_path))

    assert report["can_convert"] is False
    assert report["input_sha256"] == hashlib.sha256(input_path.read_bytes()).hexdigest()
    assert "malformed-json" in _issue_codes(report, "fatal")
    assert not output_path.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_missing_created_at_does_not_fall_back_to_import_time(tmp_path):
    data = _single_branch_data()
    message = data["messages"][1]
    del message["createdAt"]
    assert message["firstImportedAt"] == FIRST_IMPORTED_AT
    input_path = _write_backup(tmp_path / "missing-time.json", _backup(data))

    report = dry_run_ferry_backup(str(input_path))

    assert report["can_convert"] is False
    assert report["missing"]["original_message_time"] == 1
    assert "missing-original-message-time" in _issue_codes(report, "fatal")


def test_recoverable_multi_branch_tree_materializes_complete_paths(tmp_path):
    input_path = _write_backup(tmp_path / "branches.json", _backup(_branched_data()))

    package, report = convert_ferry_backup(str(input_path))

    assert report["branching"]["multi_branch_conversations"] == 1
    assert report["branching"]["recoverable_multi_branch_conversations"] == 1
    assert report["branching"]["derived_branch_paths"] == 3
    assert len(package["records"]) == 9
    branch_ids = {record["branch_id"] for record in package["records"]}
    assert len(branch_ids) == 3
    for branch_id in branch_ids:
        branch_contents = [
            record["content"] for record in package["records"]
            if record["branch_id"] == branch_id
        ]
        assert branch_contents[:2] == ["分支根节点", "请选择合成航线"]
        assert len(branch_contents) == 3


def test_one_declared_ferry_branch_means_a_real_two_path_fork(tmp_path):
    data = _branched_data()
    data["messages"] = data["messages"][:-1]
    data["conversations"][0]["messageCount"] = len(data["messages"])
    data["conversations"][0]["branchCount"] = 1
    input_path = _write_backup(tmp_path / "one-fork.json", _backup(data))

    package, report = convert_ferry_backup(str(input_path))

    assert report["branching"]["single_branch_conversations"] == 0
    assert report["branching"]["multi_branch_conversations"] == 1
    assert report["branching"]["derived_branch_paths"] == 2
    assert len(package["records"]) == 6


def test_linear_conversation_with_complete_ids_must_still_have_a_valid_tree(tmp_path):
    data = _single_branch_data()
    data["messages"][1]["parentSourceMessageId"] = "missing-parent"
    input_path = _write_backup(tmp_path / "broken-linear-tree.json", _backup(data))

    report = dry_run_ferry_backup(str(input_path))

    assert report["can_convert"] is False
    assert "branch-tree-missing-parent" in _issue_codes(report, "fatal")


def test_multi_branch_without_message_mapping_fails_closed(tmp_path):
    data = _branched_data()
    del data["messages"][2]["sourceMessageId"]
    input_path = _write_backup(tmp_path / "unmapped-branches.json", _backup(data))
    output_path = tmp_path / "forbidden-output.json"

    report = dry_run_ferry_backup(str(input_path))
    with pytest.raises(FerryAdapterError):
        write_converted_package(str(input_path), str(output_path))

    assert report["can_convert"] is False
    assert report["branching"]["unrecoverable_multi_branch_conversations"] == 1
    assert "multi-branch-missing-source-id" in _issue_codes(report, "fatal")
    assert not output_path.exists()


def test_same_source_message_id_in_different_conversations_stays_separate(tmp_path):
    first = _message(
        "ferry-one-message",
        "ferry-one",
        "same-official-source-id",
        None,
        "user",
        1_700_200_000_000,
        "第一间合成房",
        "00000000:0001700200000000:first",
    )
    second = _message(
        "ferry-two-message",
        "ferry-two",
        "same-official-source-id",
        None,
        "user",
        1_700_200_001_000,
        "第二间合成房",
        "00000000:0001700200001000:second",
    )
    data = {
        "conversations": [_conversation("ferry-one", 1), _conversation("ferry-two", 1)],
        "messages": [first, second],
        "importBatches": [],
        "handoffDrafts": [],
        "appMeta": [{"key": "schemaVersion", "value": 1}],
    }
    input_path = _write_backup(tmp_path / "cross-conversation.json", _backup(data))

    package, _ = convert_ferry_backup(str(input_path))

    assert len(package["records"]) == 2
    assert len({record["record_id"] for record in package["records"]}) == 2
    assert len({record["conversation_id"] for record in package["records"]}) == 2
    assert len({record["message_id"] for record in package["records"]}) == 2


def test_unicode_multiple_parts_and_source_unknown_are_preserved_with_warning(tmp_path):
    data = _single_branch_data()
    message = data["messages"][1]
    message["contentParts"] = [
        {"type": "text", "text": "第一段🌙\n第二行"},
        {"type": "audio-transcription", "text": "合成语音转写"},
        {"type": "attachment-placeholder", "name": "虚构附件.txt"},
        {"type": "unknown", "summary": "虚构未知部分"},
    ]
    message["plainText"] = (
        "第一段🌙\n第二行\n[语音转写]\n合成语音转写\n"
        "[附件：虚构附件.txt]\n[虚构未知部分]"
    )
    input_path = _write_backup(tmp_path / "parts.json", _backup(data))

    package, report = convert_ferry_backup(str(input_path))

    converted = next(
        record for record in package["records"]
        if record["message_id"] == "room-ferry-v1:ferry-message-user"
    )
    assert converted["content"] == message["plainText"]
    assert "source-unknown-content-part" in _issue_codes(report, "warnings")


def test_empty_message_is_skipped_and_new_content_type_is_fatal(tmp_path):
    empty_data = _single_branch_data()
    empty_data["messages"][1]["contentParts"] = []
    empty_data["messages"][1]["plainText"] = ""
    empty_path = _write_backup(tmp_path / "empty.json", _backup(empty_data))

    package, report = convert_ferry_backup(str(empty_path))

    assert len(package["records"]) == 2
    assert report["estimated"]["skipped_messages"] == 1
    assert "empty-message-skipped" in _issue_codes(report, "warnings")

    unknown_data = _single_branch_data()
    unknown_data["messages"][1]["contentParts"] = [{"type": "future-hologram"}]
    unknown_data["messages"][1]["plainText"] = "[future]"
    unknown_path = _write_backup(tmp_path / "unknown.json", _backup(unknown_data))

    unknown_report = dry_run_ferry_backup(str(unknown_path))
    assert unknown_report["can_convert"] is False
    assert "unrecognized-content-part-type" in _issue_codes(unknown_report, "fatal")


def test_unknown_envelope_fields_are_reported_not_silently_dropped(tmp_path):
    payload = _backup()
    payload["futureHeader"] = "synthetic"
    payload["data"]["futureData"] = []
    payload["checksum"] = _independent_checksum(payload["data"])
    input_path = _write_backup(tmp_path / "future-fields.json", payload)

    report = dry_run_ferry_backup(str(input_path))

    assert report["can_convert"] is True
    assert "unknown-top-level-field" in _issue_codes(report, "warnings")
    assert "unknown-data-field" in _issue_codes(report, "warnings")


def test_deterministic_conversion_then_m1_import_recall_and_index(
    tmp_path, monkeypatch
):
    input_path = _write_backup(tmp_path / "end-to-end.json", _backup())
    first_output = tmp_path / "records-first.json"
    second_output = tmp_path / "records-second.json"

    first = write_converted_package(str(input_path), str(first_output))
    second = write_converted_package(str(input_path), str(second_output))

    assert first["output_sha256"] == second["output_sha256"]
    assert first_output.read_bytes() == second_output.read_bytes()
    loaded = load_record_package(str(first_output))
    assert len(loaded.records) == 3
    assert serialize_record_package(convert_ferry_backup(str(input_path))[0]) == first_output.read_bytes()

    db_path = tmp_path / "echo-pact-v1.db"
    initial = import_record_package(str(first_output), db_path=str(db_path))
    repeated = import_record_package(str(first_output), db_path=str(db_path))
    assert initial["added"] == 3
    assert repeated["added"] == 0
    assert repeated["skipped"] == 3

    def network_forbidden(*args, **kwargs):
        pytest.fail("Room Ferry end-to-end recall must remain offline")

    monkeypatch.setattr("requests.post", network_forbidden)
    recalled = recall_records("FERRY-M2-731", db_path=str(db_path))
    assert recalled["memories"]
    result = recalled["memories"][0]
    assert result["source_kind"] == "room-ferry-backup-v1"
    assert result["source_ref"].startswith(
        f"room-ferry-backup-v1://sha256/{first['dry_run']['input_sha256']}/"
    )
    assert result["conversation_id"] == "room-ferry-v1:ferry-conversation-one"
    assert result["branch_id"].startswith("rfv1_branch_")
    assert result["authority"] == "room-ferry-unverified-archive"
    assert result["verified"] is False
    assert result["source_cutoff_at"].endswith("Z")
    assert recalled["coverage"]["verified_knowledge_cutoff_at"] is None
    assert recalled["coverage"]["coverage_status"] == "verified_cutoff_unknown"
    assert check_records_index_consistency(str(db_path))["ok"] is True
