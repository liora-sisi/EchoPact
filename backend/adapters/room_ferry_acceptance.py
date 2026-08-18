"""Redacted, read-only acceptance preflight for Room Ferry backups.

This module deliberately stops before conversion or database import.  It wraps
the source adapter's dry-run with three additional guarantees needed before a
private archive is handled in a real acceptance run:

* the source is fingerprinted before and after validation;
* only an allow-listed aggregate report can leave the adapter boundary; and
* the report is created exclusively and never overwrites an existing file.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from backend.adapters.room_ferry_v1 import (
    ADAPTER_ID,
    FERRY_FORMAT,
    dry_run_ferry_backup,
)


REPORT_SCHEMA = "echo-pact-room-ferry-acceptance-preflight-v1"
_HASH_CHUNK_BYTES = 1024 * 1024
_SAFE_VERSION = re.compile(
    r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]{1,48})?$"
)
_SAFE_ISSUE_CODE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_COUNT_FIELDS = (
    "conversations",
    "messages",
    "import_batches",
    "handoff_drafts",
    "app_meta",
)
_BRANCH_FIELDS = (
    "single_branch_conversations",
    "multi_branch_conversations",
    "recoverable_multi_branch_conversations",
    "unrecoverable_multi_branch_conversations",
    "derived_branch_paths",
)
_MISSING_FIELDS = ("original_message_time", "source_message_id", "role")
_CONTENT_FIELDS = (
    "empty_messages",
    "source_unknown_parts",
    "unrecognized_part_types",
)
_ESTIMATE_FIELDS = (
    "output_records",
    "branch_memberships",
    "skipped_messages",
    "warnings",
    "fatal",
)


@dataclass(frozen=True)
class _SourceSnapshot:
    sha256: str
    size_bytes: int
    mtime_ns: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(path: Path) -> Optional[_SourceSnapshot]:
    """Return a stable source snapshot, or ``None`` when no regular file exists."""

    if not path.is_file():
        return None
    before = path.stat()
    sha256 = _sha256_file(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None
    return _SourceSnapshot(
        sha256=sha256,
        size_bytes=after.st_size,
        mtime_ns=after.st_mtime_ns,
    )


def _copy_integer_map(value: Any, fields: tuple[str, ...]) -> Dict[str, int]:
    if not isinstance(value, Mapping):
        value = {}
    return {
        key: item if isinstance(item, int) and not isinstance(item, bool) else 0
        for key in fields
        for item in (value.get(key),)
    }


def _issue_counts(value: Any) -> list[Dict[str, Any]]:
    """Drop free-form messages and retain only stable code/count pairs."""

    if not isinstance(value, list):
        return []
    result = []
    for issue in value:
        if not isinstance(issue, Mapping):
            continue
        raw_code = issue.get("code")
        count = issue.get("count")
        if (
            isinstance(raw_code, str)
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count > 0
        ):
            code = (
                raw_code
                if _SAFE_ISSUE_CODE.fullmatch(raw_code)
                else "unclassified-adapter-issue"
            )
            result.append({"code": code, "count": count})
    return result


def _build_redacted_report(
    adapter_report: Mapping[str, Any],
    before: Optional[_SourceSnapshot],
    after: Optional[_SourceSnapshot],
) -> Dict[str, Any]:
    adapter_sha = adapter_report.get("input_sha256")
    input_unchanged = (
        before is not None
        and after is not None
        and before == after
        and adapter_sha == before.sha256
    )

    warnings = _issue_counts(adapter_report.get("warnings"))
    fatal = _issue_counts(adapter_report.get("fatal"))
    if not input_unchanged:
        source_issue = (
            "source-unavailable-for-fingerprint"
            if before is None and after is None
            else "source-changed-during-preflight"
        )
        if not any(issue["code"] == source_issue for issue in fatal):
            fatal.append({"code": source_issue, "count": 1})

    adapter_can_convert = adapter_report.get("can_convert") is True
    can_proceed = adapter_can_convert and input_unchanged and not fatal
    checksum = adapter_report.get("checksum")
    if not isinstance(checksum, Mapping):
        checksum = {}
    raw_app_version = adapter_report.get("app_version")
    app_version = (
        raw_app_version
        if isinstance(raw_app_version, str)
        and _SAFE_VERSION.fullmatch(raw_app_version)
        else None
    )
    raw_format_version = adapter_report.get("format_version")
    format_version = (
        raw_format_version
        if isinstance(raw_format_version, int)
        and not isinstance(raw_format_version, bool)
        else None
    )
    raw_schema_version = adapter_report.get("schema_version")
    schema_version = (
        raw_schema_version
        if isinstance(raw_schema_version, int)
        and not isinstance(raw_schema_version, bool)
        else None
    )

    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read-only-preflight",
        "source": {
            "sha256": before.sha256 if before is not None else None,
            "size_bytes": before.size_bytes if before is not None else None,
            "input_unchanged": input_unchanged,
            "path_disclosed": False,
        },
        "protocol": {
            "adapter": ADAPTER_ID,
            "format": FERRY_FORMAT if adapter_report.get("format") == FERRY_FORMAT else None,
            "format_version": format_version,
            "app_version": app_version,
            "schema_version": schema_version,
            "checksum_algorithm": (
                "SHA-256" if checksum.get("algorithm") == "SHA-256" else None
            ),
            "checksum_valid": checksum.get("valid") is True,
        },
        "counts": _copy_integer_map(adapter_report.get("counts"), _COUNT_FIELDS),
        "branching": _copy_integer_map(adapter_report.get("branching"), _BRANCH_FIELDS),
        "missing": _copy_integer_map(adapter_report.get("missing"), _MISSING_FIELDS),
        "content_summary": _copy_integer_map(
            adapter_report.get("content"), _CONTENT_FIELDS
        ),
        "estimated": _copy_integer_map(
            adapter_report.get("estimated"), _ESTIMATE_FIELDS
        ),
        "issues": {"warnings": warnings, "fatal": fatal},
        "decision": {
            "adapter_can_convert": adapter_can_convert,
            "can_proceed_to_conversion": can_proceed,
        },
        "safety": {
            "formal_record_package_created": False,
            "database_written": False,
            "network_used": False,
            "message_content_included": False,
            "source_ids_included": False,
            "report_overwrote_existing_file": False,
        },
    }


def _serialize_redacted(report: Mapping[str, Any], source_path: Path) -> str:
    source_spellings = {str(source_path), str(source_path.resolve())}

    def walk_strings(value: Any):
        if isinstance(value, str):
            yield value
        elif isinstance(value, Mapping):
            for key, item in value.items():
                yield from walk_strings(key)
                yield from walk_strings(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from walk_strings(item)

    for value in walk_strings(report):
        for spelling in source_spellings:
            if spelling and spelling in value:
                raise AssertionError("source path leaked into the redacted report")
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)


def _write_exclusive(path: Path, payload: str) -> None:
    if path.exists():
        raise FileExistsError(f"acceptance report already exists: {path}")
    if not path.parent.is_dir():
        raise FileNotFoundError(f"acceptance report directory does not exist: {path.parent}")
    created = False
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            created = True
            handle.write(payload)
            handle.write("\n")
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise


def run_room_ferry_preflight(input_path: str, report_path: str) -> Dict[str, Any]:
    """Validate one archive and create a redacted, no-overwrite JSON report."""

    if not isinstance(input_path, str) or not input_path.strip():
        raise ValueError("input path must not be empty")
    if not isinstance(report_path, str) or not report_path.strip():
        raise ValueError("report path must not be empty")

    source = Path(input_path)
    destination = Path(report_path)
    if source.resolve() == destination.resolve():
        raise ValueError("input and acceptance report must be different files")
    if destination.exists():
        raise FileExistsError(f"acceptance report already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(
            f"acceptance report directory does not exist: {destination.parent}"
        )

    before = _snapshot(source)
    adapter_report = dry_run_ferry_backup(str(source))
    after = _snapshot(source)
    report = _build_redacted_report(adapter_report, before, after)
    payload = _serialize_redacted(report, source)
    _write_exclusive(destination, payload)
    return report
