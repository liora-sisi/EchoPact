import hashlib
import json
from pathlib import Path

import pytest

from backend.adapters import room_ferry_acceptance as acceptance
from backend.adapters import room_ferry_v1
from scripts import preflight_room_ferry


STATIC_FIXTURE = Path(__file__).parent / "fixtures" / "room_ferry_backup_v1.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _message_texts(payload):
    return [
        part.get("text")
        for message in payload["data"]["messages"]
        for part in message.get("contentParts", [])
        if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]
    ]


def test_preflight_creates_only_redacted_report_and_preserves_input(tmp_path):
    source = tmp_path / "private-archive-do-not-disclose.json"
    source.write_bytes(STATIC_FIXTURE.read_bytes())
    report_path = tmp_path / "preflight.json"
    before = _sha256(source)

    report = acceptance.run_room_ferry_preflight(str(source), str(report_path))

    assert _sha256(source) == before
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "preflight.json",
        "private-archive-do-not-disclose.json",
    ]
    assert report["decision"]["can_proceed_to_conversion"] is True
    assert report["source"] == {
        "sha256": before,
        "size_bytes": source.stat().st_size,
        "input_unchanged": True,
        "path_disclosed": False,
    }
    assert report["safety"] == {
        "formal_record_package_created": False,
        "database_written": False,
        "network_used": False,
        "message_content_included": False,
        "source_ids_included": False,
        "report_overwrote_existing_file": False,
    }
    on_disk = json.loads(report_path.read_text(encoding="utf-8"))
    assert on_disk == report


def test_report_excludes_path_content_ids_and_free_form_issue_messages(tmp_path):
    source = tmp_path / "secret-filename-2042.json"
    source.write_bytes(STATIC_FIXTURE.read_bytes())
    payload = json.loads(source.read_text(encoding="utf-8"))
    report_path = tmp_path / "report.json"

    acceptance.run_room_ferry_preflight(str(source), str(report_path))
    rendered = report_path.read_text(encoding="utf-8")

    assert str(source) not in rendered
    assert source.name not in rendered
    for conversation in payload["data"]["conversations"]:
        assert conversation["id"] not in rendered
    for message in payload["data"]["messages"]:
        assert message["id"] not in rendered
        assert message["sourceMessageId"] not in rendered
    for text in _message_texts(payload):
        assert text not in rendered
    assert '"message"' not in rendered


def test_tampered_checksum_writes_redacted_failure_report(tmp_path):
    payload = json.loads(STATIC_FIXTURE.read_text(encoding="utf-8"))
    payload["data"]["messages"][0]["plainText"] += " tampered"
    source = tmp_path / "tampered.json"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report_path = tmp_path / "report.json"

    report = acceptance.run_room_ferry_preflight(str(source), str(report_path))

    assert report["decision"]["can_proceed_to_conversion"] is False
    assert report["protocol"]["checksum_valid"] is False
    assert {item["code"] for item in report["issues"]["fatal"]} == {
        "checksum-mismatch"
    }
    assert all(set(item) == {"code", "count"} for item in report["issues"]["fatal"])


def test_source_controlled_protocol_strings_cannot_leak_into_report(tmp_path):
    payload = json.loads(STATIC_FIXTURE.read_text(encoding="utf-8"))
    private_phrase = "PRIVATE-TITLE-SHOULD-NOT-LEAK"
    payload["format"] = private_phrase
    payload["appVersion"] = private_phrase
    payload["checksumAlgorithm"] = private_phrase
    source = tmp_path / "hostile-header.json"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report_path = tmp_path / "report.json"

    report = acceptance.run_room_ferry_preflight(str(source), str(report_path))

    assert report["decision"]["can_proceed_to_conversion"] is False
    assert report["protocol"]["format"] is None
    assert report["protocol"]["app_version"] is None
    assert report["protocol"]["checksum_algorithm"] is None
    assert private_phrase not in report_path.read_text(encoding="utf-8")


def test_malformed_json_writes_safe_failure_report(tmp_path):
    source = tmp_path / "malformed.json"
    source.write_text('{"private": "do not quote"', encoding="utf-8")
    report_path = tmp_path / "report.json"

    report = acceptance.run_room_ferry_preflight(str(source), str(report_path))

    assert report["source"]["input_unchanged"] is True
    assert report["decision"]["can_proceed_to_conversion"] is False
    assert report["issues"]["fatal"] == [{"code": "malformed-json", "count": 1}]
    assert "do not quote" not in report_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("limit, payload", [(None, b""), (1, b"{}")])
def test_stable_empty_or_oversized_input_is_not_mislabeled_as_changed(
    tmp_path, monkeypatch, limit, payload
):
    source = tmp_path / "ineligible.json"
    source.write_bytes(payload)
    report_path = tmp_path / "report.json"
    if limit is not None:
        monkeypatch.setattr(room_ferry_v1, "MAX_INPUT_BYTES", limit)

    report = acceptance.run_room_ferry_preflight(str(source), str(report_path))

    assert report["source"]["input_unchanged"] is True
    assert report["decision"]["can_proceed_to_conversion"] is False
    assert report["issues"]["fatal"] == [{"code": "input-unreadable", "count": 1}]


def test_preflight_refuses_to_overwrite_existing_report(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text("keep-me", encoding="utf-8")

    with pytest.raises(FileExistsError):
        acceptance.run_room_ferry_preflight(str(STATIC_FIXTURE), str(report_path))

    assert report_path.read_text(encoding="utf-8") == "keep-me"


def test_preflight_refuses_missing_report_directory(tmp_path):
    report_path = tmp_path / "missing" / "report.json"

    with pytest.raises(FileNotFoundError):
        acceptance.run_room_ferry_preflight(str(STATIC_FIXTURE), str(report_path))

    assert not report_path.exists()


def test_preflight_refuses_to_use_input_as_report(tmp_path):
    source = tmp_path / "source.json"
    source.write_bytes(STATIC_FIXTURE.read_bytes())
    before = _sha256(source)

    with pytest.raises(ValueError):
        acceptance.run_room_ferry_preflight(str(source), str(source))

    assert _sha256(source) == before


def test_preflight_refuses_lexical_alias_of_input_as_report(tmp_path):
    source = tmp_path / "source.json"
    source.write_bytes(STATIC_FIXTURE.read_bytes())
    alias = tmp_path / "unused" / ".." / source.name

    with pytest.raises(ValueError):
        acceptance.run_room_ferry_preflight(str(source), str(alias))

    assert source.read_bytes() == STATIC_FIXTURE.read_bytes()


def test_missing_input_produces_only_redacted_issue_codes(tmp_path):
    source = tmp_path / "missing-private-archive.json"
    report_path = tmp_path / "report.json"

    report = acceptance.run_room_ferry_preflight(str(source), str(report_path))

    assert report["decision"]["can_proceed_to_conversion"] is False
    assert report["source"]["sha256"] is None
    assert report["issues"]["fatal"] == [{"code": "input-unreadable", "count": 1}]
    rendered = report_path.read_text(encoding="utf-8")
    assert source.name not in rendered
    assert '"message"' not in rendered


def test_source_change_during_preflight_fails_closed(tmp_path, monkeypatch):
    source = tmp_path / "changing.json"
    source.write_bytes(STATIC_FIXTURE.read_bytes())
    report_path = tmp_path / "report.json"
    original_dry_run = acceptance.dry_run_ferry_backup

    def mutate_after_read(path):
        report = original_dry_run(path)
        Path(path).write_bytes(Path(path).read_bytes() + b"\n")
        return report

    monkeypatch.setattr(acceptance, "dry_run_ferry_backup", mutate_after_read)
    report = acceptance.run_room_ferry_preflight(str(source), str(report_path))

    assert report["source"]["input_unchanged"] is False
    assert report["decision"]["can_proceed_to_conversion"] is False
    assert {item["code"] for item in report["issues"]["fatal"]} == {
        "source-changed-during-preflight"
    }


def test_snapshot_swallows_native_path_bearing_file_error(tmp_path, monkeypatch):
    source = tmp_path / "private-directory-name" / "archive.json"
    source.parent.mkdir()
    source.write_bytes(STATIC_FIXTURE.read_bytes())

    def disappear_during_hash(path):
        raise FileNotFoundError(2, "file disappeared", str(path))

    monkeypatch.setattr(acceptance, "_sha256_file", disappear_during_hash)
    assert acceptance._snapshot(source) is None


def test_cli_native_file_race_does_not_leak_input_path(tmp_path, monkeypatch, capsys):
    source = tmp_path / "private-directory-name" / "archive.json"
    source.parent.mkdir()
    source.write_bytes(STATIC_FIXTURE.read_bytes())
    report_path = tmp_path / "report.json"

    def disappear_during_adapter(path):
        raise FileNotFoundError(2, "file disappeared", str(source.resolve()))

    monkeypatch.setattr(acceptance, "dry_run_ferry_backup", disappear_during_adapter)
    assert preflight_room_ferry.main(
        [str(source), "--report", str(report_path)]
    ) == 2
    captured = capsys.readouterr()
    assert str(source) not in captured.err
    assert str(source.resolve()) not in captured.err
    assert source.name not in captured.err
    assert "file disappeared" not in captured.err
    assert "became unavailable" in captured.err
    assert not report_path.exists()


def test_future_adapter_fields_and_messages_are_dropped(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    source.write_bytes(STATIC_FIXTURE.read_bytes())
    report_path = tmp_path / "report.json"
    private_phrase = "FUTURE-PRIVATE-FIELD-MUST-NOT-LEAK"
    original_dry_run = acceptance.dry_run_ferry_backup

    def inject_future_fields(path):
        report = original_dry_run(path)
        report["future_private_section"] = {"body": private_phrase}
        report["counts"]["future_private_count"] = 99
        report["warnings"].append(
            {"code": "future-warning", "message": private_phrase, "count": 1}
        )
        return report

    monkeypatch.setattr(acceptance, "dry_run_ferry_backup", inject_future_fields)
    report = acceptance.run_room_ferry_preflight(str(source), str(report_path))
    rendered = report_path.read_text(encoding="utf-8")

    assert private_phrase not in rendered
    assert "future_private_section" not in rendered
    assert "future_private_count" not in rendered
    assert report["issues"]["warnings"] == [
        {"code": "future-warning", "count": 1}
    ]


def test_adapter_true_with_fatal_issue_still_fails_closed():
    snapshot = acceptance._SourceSnapshot("a" * 64, 1, 1)
    adapter_report = {
        "input_sha256": snapshot.sha256,
        "can_convert": True,
        "warnings": [],
        "fatal": [{"code": "forced-fatal", "message": "private", "count": 1}],
    }

    report = acceptance._build_redacted_report(
        adapter_report, snapshot, snapshot
    )

    assert report["source"]["input_unchanged"] is True
    assert report["decision"]["adapter_can_convert"] is True
    assert report["decision"]["can_proceed_to_conversion"] is False


def test_partial_report_write_is_removed(tmp_path, monkeypatch):
    report_path = tmp_path / "report.json"
    original_open = Path.open

    class BrokenWriter:
        def __enter__(self):
            self.handle = original_open(
                report_path, "x", encoding="utf-8", newline="\n"
            )
            return self

        def write(self, value):
            self.handle.write(value[:4])
            self.handle.flush()
            raise OSError("synthetic write failure")

        def __exit__(self, exc_type, exc, traceback):
            self.handle.close()

    def broken_open(path, mode="r", *args, **kwargs):
        if path == report_path and mode == "x":
            return BrokenWriter()
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", broken_open)
    with pytest.raises(OSError, match="synthetic write failure"):
        acceptance._write_exclusive(report_path, "redacted payload")

    assert not report_path.exists()


def test_report_creation_race_preserves_other_writer_file(tmp_path, monkeypatch):
    report_path = tmp_path / "report.json"
    original_open = Path.open
    injected = False

    def racing_open(path, mode="r", *args, **kwargs):
        nonlocal injected
        if path == report_path and mode == "x" and not injected:
            injected = True
            with original_open(report_path, "w", encoding="utf-8") as handle:
                handle.write("other-writer")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    with pytest.raises(FileExistsError):
        acceptance._write_exclusive(report_path, "our-report")

    assert report_path.read_text(encoding="utf-8") == "other-writer"


def test_cli_exit_codes_and_stdout_are_redacted(tmp_path, capsys):
    source = tmp_path / "cli-private.json"
    source.write_bytes(STATIC_FIXTURE.read_bytes())
    report_path = tmp_path / "report.json"

    assert preflight_room_ferry.main(
        [str(source), "--report", str(report_path)]
    ) == 0
    output = capsys.readouterr().out
    assert source.name not in output
    assert json.loads(output)["can_proceed_to_conversion"] is True

    assert preflight_room_ferry.main(
        [str(source), "--report", str(report_path)]
    ) == 2
    captured = capsys.readouterr()
    assert "already exists" in captured.err
    assert report_path.is_file()
