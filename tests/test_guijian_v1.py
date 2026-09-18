import copy
import hashlib
import json
from pathlib import Path

import pytest

from backend.adapters.guijian_v1 import (
    ADAPTER_ID,
    GuijianAdapterError,
    convert_guijian_export,
    dry_run_guijian_export,
    guijian_content_hash,
    guijian_sequence_hash,
    serialize_record_package,
    write_converted_package,
)
from backend.memory.records_v1 import (
    check_records_index_consistency,
    import_record_package,
    load_record_package,
    recall_records,
)


def _message(index, role, content, *, observed_at, segment_index=0, thinking=""):
    return {
        "index": index,
        "role": role,
        "roleEvidence": {"source": "attribute", "confidence": "high"},
        "content": content,
        "thinking": thinking,
        "sourceTimestamp": None,
        "timeEvidence": {
            "kind": "observed-upper-bound",
            "originalValue": None,
            "eventTime": None,
            "precision": "upper-bound-only",
            "source": "page-observation",
            "confidence": "bounded",
            "contextRequired": False,
            "firstObservedAt": observed_at,
            "lowerBound": None,
            "upperBound": observed_at,
        },
        "provenanceStatus": "provisional-pending-official",
        "observedAt": observed_at,
        "contentHash": guijian_content_hash(role, content, thinking),
        "segmentIndex": segment_index,
    }


def _payload(messages=None):
    messages = messages or [
        _message(
            0,
            "user",
            "合成问题：归笺桥代码是 GUIJIAN-731。",
            observed_at="2026-09-18T02:00:30.000Z",
        ),
        _message(
            1,
            "assistant",
            "2026-09-18 10:01（成都）\n\n合成回答：只走离线适配。",
            observed_at="2026-09-18T02:01:30.000Z",
        ),
    ]
    hashes = [item["contentHash"] for item in messages]
    snapshot_id = hashlib.sha256(b"synthetic-guijian-snapshot").hexdigest()
    return {
        "exportSchemaVersion": "web-memory-ferry/export-v1",
        "exportedAt": "2026-09-18T02:05:00.000Z",
        "manifest": {
            "snapshotId": snapshot_id,
            "predecessorSnapshotId": None,
            "roomKey": "chatgpt:synthetic-room",
            "roomCode": "synthetic",
            "platform": "chatgpt",
            "authorityStatus": "provisional-pending-official",
            "title": "synthetic private title",
            "startedAt": "2026-09-18T02:00:00.000Z",
            "completedAt": "2026-09-18T02:04:00.000Z",
            "status": "done-top",
            "messageCount": len(messages),
            "sequenceHash": guijian_sequence_hash(hashes),
            "evidence": {
                "topConfirmed": True,
                "domQuietConfirmed": True,
                "absoluteCompletenessProven": False,
                "observedWindows": 4,
                "observedMessages": len(messages),
                "duplicateObservations": 0,
                "gaps": [],
                "segmentCount": 1,
                "repairs": None,
                "roleWarnings": 0,
                "timeEvidenceStats": {"observed-upper-bound": len(messages)},
                "planSource": "synthetic-test",
            },
            "rawIncluded": True,
            "normalizedIncluded": True,
        },
        "raw": {"segments": [[{"fingerprint": value} for value in hashes]]},
        "normalized": messages,
    }


def _write(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _issue_codes(report, category):
    return {issue["code"] for issue in report[category]}


def _source_id_payload(*, ambiguous=False):
    source_ids = {
        "a": "00000000-0000-4000-8000-000000000001",
        "b": "00000000-0000-4000-8000-000000000002",
        "c": "00000000-0000-4000-8000-000000000003",
        "d": "00000000-0000-4000-8000-000000000004",
    }
    templates = {
        "a": ("user", "合成甲。"),
        "b": ("assistant", "2026-09-18 10:01（成都）\n\n合成乙。"),
        "c": ("user", "合成丙。"),
        "d": ("assistant", "2026-09-18 10:03（成都）\n\n合成丁。"),
    }
    segment_keys = (
        [["a", "b"], ["c", "d"]]
        if ambiguous
        else [["a", "b", "c", "b", "c", "d"], ["b", "c", "d"]]
    )
    normalized = []
    raw_segments = []
    ordinal = 0
    for segment_index, keys in enumerate(segment_keys):
        raw_segment = []
        for key in keys:
            role, content = templates[key]
            observed_at = f"2026-09-18T02:00:{ordinal:02d}.000Z"
            item = _message(
                ordinal,
                role,
                content,
                observed_at=observed_at,
                segment_index=segment_index,
            )
            normalized.append(item)
            raw_segment.append(
                {
                    "fingerprint": item["contentHash"],
                    "sourceMessageId": source_ids[key],
                    "sourceMessageIdSource": "attr:data-message-id",
                }
            )
            ordinal += 1
        raw_segments.append(raw_segment)
    payload = _payload(normalized)
    payload["raw"]["segments"] = raw_segments
    payload["manifest"]["evidence"].update(
        {
            "duplicateObservations": len(normalized) - 4,
            "gaps": [
                {"beforeSegment": 1, "reason": "synthetic-virtual-window-gap"}
            ],
            "segmentCount": 2,
        }
    )
    return payload, source_ids


def test_dry_run_is_redacted_and_preserves_input(tmp_path):
    input_path = _write(tmp_path / "guijian.json", _payload())
    before = hashlib.sha256(input_path.read_bytes()).hexdigest()

    report = dry_run_guijian_export(str(input_path))
    rendered = json.dumps(report, ensure_ascii=False)

    assert report["can_convert"] is True
    assert report["counts"]["normalized_messages"] == 2
    assert report["time_evidence"] == {
        "platform_time": 0,
        "content_self_stamp": 1,
        "next_content_stamp_upper_bound": 1,
        "capture_observed_upper_bound": 0,
        "unresolved": 0,
    }
    assert "GUIJIAN-731" not in rendered
    assert "synthetic private title" not in rendered
    assert "chatgpt:synthetic-room" not in rendered
    assert hashlib.sha256(input_path.read_bytes()).hexdigest() == before
    assert list(tmp_path.iterdir()) == [input_path]


def test_conversion_keeps_time_basis_and_visible_order(tmp_path):
    input_path = _write(tmp_path / "guijian.json", _payload())

    package, report = convert_guijian_export(str(input_path))

    assert report["can_convert"] is True
    assert package["schema_version"] == "echo-pact-records-v2"
    assert package["source"]["adapter"] == ADAPTER_ID
    assert [item["role"] for item in package["records"]] == ["user", "assistant"]
    assert [
        item["branch_memberships"][0]["position"] for item in package["records"]
    ] == [0, 1]
    assert all(item["verified"] is False for item in package["records"])
    assert package["records"][0]["created_at"] == "2026-09-18T02:01:00Z"
    assert package["records"][1]["created_at"] == "2026-09-18T02:01:00Z"
    assert package["records"][0]["authority"] == "guijian-browser-time-upper-bound"
    assert (
        package["records"][1]["authority"]
        == "guijian-browser-content-self-stamp"
    )
    assert "timeBasis=next-content-stamp-upper-bound" in package["records"][0][
        "source_ref"
    ]
    assert "timeBasis=content-self-stamp-minute" in package["records"][1][
        "source_ref"
    ]


def test_visible_thinking_is_preserved_with_an_explicit_label(tmp_path):
    messages = [
        _message(
            0,
            "assistant",
            "2026-09-18 10:01（成都）\n\n最终回答。",
            thinking="网页上可见的思考摘要。",
            observed_at="2026-09-18T02:01:30.000Z",
        )
    ]
    input_path = _write(tmp_path / "thinking.json", _payload(messages))

    package, _ = convert_guijian_export(str(input_path))

    assert package["records"][0]["content"].endswith(
        "[归笺可见思考区]\n网页上可见的思考摘要。"
    )


def test_gaps_refuse_formal_conversion(tmp_path):
    payload = _payload()
    payload["manifest"]["evidence"]["gaps"] = [
        {"beforeSegment": 1, "reason": "synthetic-gap"}
    ]
    input_path = _write(tmp_path / "gapped.json", payload)
    output_path = tmp_path / "must-not-exist.json"

    report = dry_run_guijian_export(str(input_path))
    with pytest.raises(GuijianAdapterError):
        write_converted_package(str(input_path), str(output_path))

    assert "capture-has-gaps" in _issue_codes(report, "fatal")
    assert not output_path.exists()


def test_prior_same_room_top_witness_can_supply_only_the_top_boundary(tmp_path):
    witness_messages = [
        _message(
            index,
            "user",
            f"合成顶部见证 {index}",
            observed_at=f"2026-09-18T02:00:{index:02d}.000Z",
        )
        for index in range(6)
    ]
    current_messages = copy.deepcopy(witness_messages)
    current_messages.append(
        _message(
            6,
            "assistant",
            "2026-09-18 10:01（成都）\n\n合成新增消息。",
            observed_at="2026-09-18T02:01:30.000Z",
        )
    )
    witness_payload = _payload(witness_messages)
    witness_payload["exportedAt"] = "2026-09-18T02:04:00.000Z"
    current_payload = _payload(current_messages)
    current_payload["manifest"]["evidence"]["topConfirmed"] = False
    witness_path = _write(tmp_path / "witness.json", witness_payload)
    current_path = _write(tmp_path / "current.json", current_payload)

    without_witness = dry_run_guijian_export(str(current_path))
    with_witness = dry_run_guijian_export(
        str(current_path), top_witness_path=str(witness_path)
    )

    assert without_witness["can_convert"] is False
    assert "top-not-confirmed" in _issue_codes(without_witness, "fatal")
    assert with_witness["can_convert"] is True
    assert with_witness["capture_evidence"]["effective_top_confirmed"] is True
    assert with_witness["capture_evidence"]["top_confirmation_basis"] == (
        "prior-top-prefix-witness"
    )
    assert with_witness["capture_evidence"]["top_witness"] == {
        "input_sha256": hashlib.sha256(witness_path.read_bytes()).hexdigest(),
        "matching_prefix_messages": 6,
    }


def test_top_witness_from_another_room_fails_closed(tmp_path):
    messages = [
        _message(
            index,
            "user",
            f"合成顶部见证 {index}",
            observed_at=f"2026-09-18T02:00:{index:02d}.000Z",
        )
        for index in range(6)
    ]
    witness_payload = _payload(messages)
    witness_payload["manifest"]["roomKey"] = "chatgpt:different-room"
    current_payload = _payload(copy.deepcopy(messages))
    current_payload["manifest"]["evidence"]["topConfirmed"] = False
    witness_path = _write(tmp_path / "wrong-room.json", witness_payload)
    current_path = _write(tmp_path / "current.json", current_payload)

    report = dry_run_guijian_export(
        str(current_path), top_witness_path=str(witness_path)
    )

    assert report["can_convert"] is False
    assert "top-witness-room-mismatch" in _issue_codes(report, "fatal")


def test_manual_visible_top_attestation_is_bound_to_input_and_first_hash(tmp_path):
    payload = _payload()
    payload["manifest"]["evidence"]["topConfirmed"] = False
    input_path = _write(tmp_path / "manual-top.json", payload)

    report = dry_run_guijian_export(
        str(input_path), manual_top_attestation=True
    )

    assert report["can_convert"] is True
    capture = report["capture_evidence"]
    assert capture["effective_top_confirmed"] is True
    assert capture["top_confirmation_basis"] == "manual-visible-top-attestation"
    assert capture["manual_top_attestation"] == {
        "kind": "human-observed-visible-top-and-recognized-first-message",
        "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "first_message_content_hash": payload["normalized"][0]["contentHash"],
    }
    assert payload["normalized"][0]["content"] not in json.dumps(
        report, ensure_ascii=False
    )


def test_dominant_ordered_superset_segment_can_replace_redundant_capture(tmp_path):
    base_contents = [f"合成顺序消息 {index}" for index in range(6)]
    segment_zero = [
        _message(
            index,
            "user",
            content,
            observed_at=f"2026-09-18T02:00:{index:02d}.000Z",
            segment_index=0,
        )
        for index, content in enumerate(base_contents)
    ]
    superset_contents = base_contents[:3] + ["合成插入消息 A"] + base_contents[3:]
    superset_contents.append("合成插入消息 B")
    segment_one = [
        _message(
            len(segment_zero) + index,
            "user",
            content,
            observed_at=f"2026-09-18T02:01:{index:02d}.000Z",
            segment_index=1,
        )
        for index, content in enumerate(superset_contents)
    ]
    flattened = segment_zero + segment_one
    payload = _payload(flattened)
    payload["raw"]["segments"] = [
        [{"fingerprint": item["contentHash"]} for item in segment_zero],
        [{"fingerprint": item["contentHash"]} for item in segment_one],
    ]
    payload["manifest"]["evidence"]["segmentCount"] = 2
    payload["manifest"]["evidence"]["gaps"] = [
        {"beforeSegment": 1, "reason": "synthetic-no-overlap"}
    ]

    input_path = _write(tmp_path / "redundant-segments.json", payload)
    refused = dry_run_guijian_export(str(input_path))
    package, repaired = convert_guijian_export(
        str(input_path), repair_redundant_segments=True
    )

    assert refused["can_convert"] is False
    assert "capture-has-gaps" in _issue_codes(refused, "fatal")
    assert repaired["can_convert"] is True
    assert repaired["counts"]["normalized_messages"] == 14
    assert repaired["counts"]["selected_messages"] == 8
    assert repaired["capture_repair"] == {
        "requested": True,
        "applied": True,
        "kind": "dominant-ordered-superset-segment",
        "selected_segment_index": 1,
        "input_observations": 14,
        "selected_observations": 8,
        "redundant_observations_ignored": 6,
    }
    assert len(package["records"]) == 8
    assert [item["content"] for item in package["records"]] == superset_contents
    assert all("segment=1" in item["source_ref"] for item in package["records"])
    assert package["source"]["capture_repair"] == repaired["capture_repair"]


def test_source_message_ids_can_prove_one_unique_repaired_order(tmp_path):
    payload, source_ids = _source_id_payload()
    input_path = _write(tmp_path / "source-id-windows.json", payload)

    refused = dry_run_guijian_export(str(input_path))
    package, repaired = convert_guijian_export(
        str(input_path), repair_source_message_ids=True
    )

    assert refused["can_convert"] is False
    assert repaired["can_convert"] is True
    assert repaired["counts"]["normalized_messages"] == 9
    assert repaired["counts"]["selected_messages"] == 4
    assert repaired["capture_repair"] == {
        "requested": True,
        "applied": True,
        "kind": "source-message-id-unique-topological-order",
        "selected_segment_index": None,
        "input_observations": 9,
        "selected_observations": 4,
        "redundant_observations_ignored": 5,
        "source_message_id_observations": 9,
        "unique_source_message_ids": 4,
        "ordering_edges": 3,
        "reciprocal_edge_pairs_resolved": 1,
        "minority_edge_observations_ignored": 1,
    }
    assert [item["role"] for item in package["records"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert len({item["record_id"] for item in package["records"]}) == 4
    redacted = json.dumps(repaired, ensure_ascii=False)
    assert all(source_id not in redacted for source_id in source_ids.values())
    assert all(
        source_id not in json.dumps(package["source"], ensure_ascii=False)
        for source_id in source_ids.values()
    )


def test_source_message_id_repair_fails_when_order_is_not_unique(tmp_path):
    payload, _ = _source_id_payload(ambiguous=True)
    input_path = _write(tmp_path / "ambiguous-source-id-windows.json", payload)

    report = dry_run_guijian_export(
        str(input_path), repair_source_message_ids=True
    )

    assert report["can_convert"] is False
    assert "source-id-order-ambiguous" in _issue_codes(report, "fatal")
    assert report["capture_repair"]["applied"] is False


def test_content_or_sequence_tampering_fails_closed(tmp_path):
    content_payload = _payload()
    content_payload["normalized"][0]["content"] = "被篡改的正文"
    content_path = _write(tmp_path / "content-tampered.json", content_payload)

    sequence_payload = _payload()
    sequence_payload["manifest"]["sequenceHash"] = "0" * 64
    sequence_path = _write(tmp_path / "sequence-tampered.json", sequence_payload)

    content_report = dry_run_guijian_export(str(content_path))
    sequence_report = dry_run_guijian_export(str(sequence_path))

    assert "content-hash-mismatch" in _issue_codes(content_report, "fatal")
    assert "sequence-hash-mismatch" in _issue_codes(sequence_report, "fatal")


def test_raw_and_normalized_layers_must_describe_the_same_sequence(tmp_path):
    payload = _payload()
    payload["raw"]["segments"][0][0]["fingerprint"] = "f" * 64
    input_path = _write(tmp_path / "raw-tampered.json", payload)

    report = dry_run_guijian_export(str(input_path))

    assert report["can_convert"] is False
    assert "raw-normalized-sequence-mismatch" in _issue_codes(report, "fatal")


def test_repeated_bodies_are_not_mistaken_for_message_identity(tmp_path):
    messages = [
        _message(
            0,
            "user",
            "嗯",
            observed_at="2026-09-18T02:00:10.000Z",
        ),
        _message(
            1,
            "user",
            "嗯",
            observed_at="2026-09-18T02:00:20.000Z",
        ),
        _message(
            2,
            "assistant",
            "2026-09-18 10:01（成都）\n\n收到了。",
            observed_at="2026-09-18T02:01:30.000Z",
        ),
    ]
    input_path = _write(tmp_path / "repeated.json", _payload(messages))

    package, report = convert_guijian_export(str(input_path))

    assert len(package["records"]) == 3
    assert len({item["record_id"] for item in package["records"]}) == 3
    assert report["counts"]["repeated_content_hash_groups"] == 1
    assert "repeated-content-hashes-preserved" in _issue_codes(report, "warnings")


def test_backward_direct_timestamps_expose_bad_visible_order(tmp_path):
    messages = [
        _message(
            0,
            "assistant",
            "2026-09-18 10:02（成都）\n\n先出现。",
            observed_at="2026-09-18T02:02:30.000Z",
        ),
        _message(
            1,
            "assistant",
            "2026-09-18 10:01（成都）\n\n却更早。",
            observed_at="2026-09-18T02:03:30.000Z",
        ),
    ]
    input_path = _write(tmp_path / "backward.json", _payload(messages))

    report = dry_run_guijian_export(str(input_path))

    assert report["can_convert"] is False
    assert "non-monotonic-direct-time" in _issue_codes(report, "fatal")


def test_deterministic_conversion_import_recall_and_idempotent_retry(
    tmp_path, monkeypatch
):
    input_path = _write(tmp_path / "guijian.json", _payload())
    first_output = tmp_path / "records-first.json"
    second_output = tmp_path / "records-second.json"

    first = write_converted_package(str(input_path), str(first_output))
    second = write_converted_package(str(input_path), str(second_output))

    assert first["output_sha256"] == second["output_sha256"]
    assert first_output.read_bytes() == second_output.read_bytes()
    assert (
        serialize_record_package(convert_guijian_export(str(input_path))[0])
        == first_output.read_bytes()
    )
    loaded = load_record_package(str(first_output))
    assert len(loaded.records) == 2

    db_path = tmp_path / "echo-pact-guijian.db"
    initial = import_record_package(str(first_output), db_path=str(db_path))
    repeated = import_record_package(str(first_output), db_path=str(db_path))
    assert initial["added"] == 2
    assert initial["branch_memberships_added"] == 2
    assert repeated["idempotent_replay"] is True
    assert repeated["added"] == 0
    assert repeated["skipped"] == 2

    def network_forbidden(*args, **kwargs):
        pytest.fail("GuiJian recall must remain offline")

    monkeypatch.setattr("requests.post", network_forbidden)
    recalled = recall_records("GUIJIAN-731", db_path=str(db_path))
    assert recalled["memories"]
    result = recalled["memories"][0]
    assert result["source_kind"] == ADAPTER_ID
    assert result["source_ref"].startswith(
        f"{ADAPTER_ID}://sha256/{first['dry_run']['input_sha256']}/"
    )
    assert result["conversation_id"].startswith("guijian-v1:")
    assert result["verified"] is False
    assert check_records_index_consistency(str(db_path))["ok"] is True


def test_formal_output_refuses_overwrite(tmp_path):
    input_path = _write(tmp_path / "guijian.json", _payload())
    output_path = tmp_path / "records.json"

    write_converted_package(str(input_path), str(output_path))

    with pytest.raises(FileExistsError):
        write_converted_package(str(input_path), str(output_path))


def test_unknown_top_level_field_is_reported_without_leaking_value(tmp_path):
    payload = copy.deepcopy(_payload())
    payload["futurePrivateField"] = "do-not-print-this-value"
    input_path = _write(tmp_path / "future.json", payload)

    report = dry_run_guijian_export(str(input_path))
    rendered = json.dumps(report, ensure_ascii=False)

    assert report["can_convert"] is True
    assert "unknown-top-level-fields" in _issue_codes(report, "warnings")
    assert "do-not-print-this-value" not in rendered
