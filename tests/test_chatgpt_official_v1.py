from __future__ import annotations

import copy
import json
import zipfile

import pytest

from backend.adapters.chatgpt_official_v1 import (
    ADAPTER_ID,
    ChatGPTOfficialAdapterError,
    convert_chatgpt_official_export,
    dry_run_chatgpt_official_export,
    write_converted_package,
)


def _message(message_id, role, content_type, payload, created_at):
    content = {"content_type": content_type}
    content.update(payload)
    return {
        "id": message_id,
        "author": {"role": role, "name": None},
        "create_time": created_at,
        "content": content,
        "metadata": {},
    }


def _conversation():
    nodes = {
        "root": {"id": "root", "parent": None, "message": None},
        "u1": {
            "id": "u1",
            "parent": "root",
            "message": _message(
                "u1",
                "user",
                "multimodal_text",
                {
                    "parts": [
                        "请看看",
                        {"content_type": "image_asset_pointer"},
                        {
                            "content_type": "audio_transcription",
                            "text": "这是语音正文",
                        },
                    ]
                },
                1000.0,
            ),
        },
        "thought": {
            "id": "thought",
            "parent": "u1",
            "message": _message(
                "thought",
                "assistant",
                "thoughts",
                {
                    "source_analysis_msg_id": "analysis-1",
                    "thoughts": [
                        {
                            "summary": "先核对证据",
                            "content": "再比较两个来源",
                            "chunks": ["保留过程细节"],
                            "finished": True,
                        }
                    ],
                },
                1001.0,
            ),
        },
        "recap": {
            "id": "recap",
            "parent": "thought",
            "message": _message(
                "recap",
                "assistant",
                "reasoning_recap",
                {"content": "推理摘要也保留"},
                1002.0,
            ),
        },
        "final": {
            "id": "final",
            "parent": "recap",
            "message": _message(
                "final",
                "assistant",
                "text",
                {"parts": ["最终正文"]},
                1003.0,
            ),
        },
        "alternate": {
            "id": "alternate",
            "parent": "u1",
            "message": _message(
                "alternate",
                "assistant",
                "text",
                {"parts": ["另一个分支"]},
                1004.0,
            ),
        },
    }
    return {
        "id": "conversation-1",
        "conversation_id": "conversation-1",
        "create_time": 999.0,
        "update_time": 1005.0,
        "current_node": "final",
        "mapping": nodes,
    }


def _write_export(path, conversations=None, *, manifest_mutator=None):
    conversations = conversations or [_conversation()]
    shard_name = "conversations-000.json"
    shard_bytes = json.dumps(
        conversations, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    manifest = {
        "version": 1,
        "manifest_file": "export_manifest.json",
        "export_files": [{"path": shard_name, "size_bytes": len(shard_bytes)}],
        "logical_files": {
            "conversations.json": {
                "files": [shard_name],
                "shard_count": 1,
                "sharded": True,
            }
        },
    }
    if manifest_mutator is not None:
        manifest_mutator(manifest)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(shard_name, shard_bytes)
        archive.writestr(
            "export_manifest.json",
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        )


def test_official_export_retains_visible_and_folded_reasoning(tmp_path):
    archive = tmp_path / "export.zip"
    _write_export(archive)

    package, report = convert_chatgpt_official_export(str(archive))

    assert report["can_convert"] is True
    assert report["manifest"]["zip_crc_ok"] is True
    assert report["counts"]["conversations"] == 1
    assert report["counts"]["thought_messages_retained"] == 1
    assert report["counts"]["reasoning_recap_messages_retained"] == 1
    assert report["counts"]["media_placeholders"] == 1
    assert report["counts"]["audio_transcriptions"] == 1
    assert report["estimated_output_records"] == 5
    assert package["source"]["folded_reasoning_policy"] == (
        "retained-and-phase-labelled"
    )
    records = {record["content"]: record for record in package["records"]}
    assert "请看看\n[ChatGPT image]\n这是语音正文" in records
    thought = next(
        record for record in package["records"] if "phase=thoughts" in record["source_ref"]
    )
    assert thought["content"] == "先核对证据\n再比较两个来源\n保留过程细节"
    assert any(
        "phase=reasoning_recap" in record["source_ref"]
        for record in package["records"]
    )
    final = records["最终正文"]
    assert "phase=final_answer" in final["source_ref"]
    assert final["verified"] is True
    assert final["authority"] == "official"
    user = records["请看看\n[ChatGPT image]\n这是语音正文"]
    assert len(user["branch_memberships"]) == 2


def test_official_export_conversion_is_deterministic(tmp_path):
    archive = tmp_path / "export.zip"
    _write_export(archive)

    first, first_report = convert_chatgpt_official_export(str(archive))
    second, second_report = convert_chatgpt_official_export(str(archive))

    assert first == second
    assert first_report == second_report


def test_official_export_rejects_manifest_size_mismatch(tmp_path):
    archive = tmp_path / "export.zip"

    def corrupt(manifest):
        manifest["export_files"][0]["size_bytes"] += 1

    _write_export(archive, manifest_mutator=corrupt)

    report = dry_run_chatgpt_official_export(str(archive))

    assert report["can_convert"] is False
    assert {issue["code"] for issue in report["fatal_issues"]} >= {
        "manifest-size-mismatch"
    }
    with pytest.raises(ChatGPTOfficialAdapterError):
        convert_chatgpt_official_export(str(archive))


def test_official_export_rejects_unknown_structured_part(tmp_path):
    archive = tmp_path / "export.zip"
    conversation = copy.deepcopy(_conversation())
    conversation["mapping"]["u1"]["message"]["content"]["parts"].append(
        {"content_type": "future_private_media"}
    )
    _write_export(archive, [conversation])

    report = dry_run_chatgpt_official_export(str(archive))

    assert report["can_convert"] is False
    assert {issue["code"] for issue in report["fatal_issues"]} >= {
        "unsupported-content-part"
    }


def test_official_export_rejects_broken_graph(tmp_path):
    archive = tmp_path / "export.zip"
    conversation = copy.deepcopy(_conversation())
    conversation["mapping"]["u1"]["parent"] = "missing"
    _write_export(archive, [conversation])

    report = dry_run_chatgpt_official_export(str(archive))

    assert report["can_convert"] is False
    codes = {issue["code"] for issue in report["fatal_issues"]}
    assert "missing-parent" in codes
    assert "disconnected-mapping" in codes


def test_official_export_writer_refuses_overwrite(tmp_path):
    archive = tmp_path / "export.zip"
    output = tmp_path / "records.json"
    _write_export(archive)

    result = write_converted_package(str(archive), str(output))

    assert result["adapter"] == ADAPTER_ID
    assert result["output_records"] == 5
    with pytest.raises(FileExistsError):
        write_converted_package(str(archive), str(output))


def test_official_export_accepts_a_deep_nonrecursive_conversation(tmp_path):
    archive = tmp_path / "export.zip"
    mapping = {"root": {"id": "root", "parent": None, "message": None}}
    parent = "root"
    for index in range(1200):
        node_id = f"m-{index:04d}"
        mapping[node_id] = {
            "id": node_id,
            "parent": parent,
            "message": _message(
                node_id,
                "user" if index % 2 == 0 else "assistant",
                "text",
                {"parts": [f"message {index}"]},
                1000.0 + index,
            ),
        }
        parent = node_id
    conversation = {
        "id": "deep-conversation",
        "conversation_id": "deep-conversation",
        "create_time": 999.0,
        "update_time": 2300.0,
        "current_node": parent,
        "mapping": mapping,
    }
    _write_export(archive, [conversation])

    report = dry_run_chatgpt_official_export(str(archive))

    assert report["can_convert"] is True
    assert report["estimated_output_records"] == 1200
