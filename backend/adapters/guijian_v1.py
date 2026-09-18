"""GuiJian browser-capture export v1 adapter for Echo Pact.

The adapter is deliberately source-specific.  It validates one offline
``web-memory-ferry/export-v1`` JSON file, preserves the visible message order,
and emits the existing compact records-v2 protocol without opening an Echo
database or using the network.

GuiJian is observation evidence, not an official ChatGPT export.  Formal
records therefore remain unverified and carry an explicit time basis in their
``source_ref``.  A strict self-written Chengdu timestamp at the beginning of an
assistant message is usable minute-level evidence.  Unstamped messages use a
following direct timestamp (or their observation time) only as a conservative
upper bound; that bound is never relabelled as an official platform time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote, urlencode


EXPORT_SCHEMA_VERSION = "web-memory-ferry/export-v1"
ADAPTER_ID = "guijian-browser-capture-v1"
RECORD_SCHEMA_VERSION = "echo-pact-records-v2"
SUPPORTED_PLATFORM = "chatgpt"
SUPPORTED_AUTHORITY = "provisional-pending-official"
SUPPORTED_CAPTURE_STATUSES = {"done-top", "live-observation"}
MAX_INPUT_BYTES = 500 * 1024 * 1024
MIN_TOP_WITNESS_PREFIX_MESSAGES = 6
TOP_WITNESS_IGNORABLE_FATAL_CODES = {
    "capture-has-gaps",
    "non-monotonic-direct-time",
}
# GuiJian's strict self-stamp literally says 成都 and the supported captures
# are modern records.  Using the declared UTC+08:00 offset avoids introducing
# a platform ``tzdata`` dependency on Windows while preserving that evidence.
CHENGDU = timezone(timedelta(hours=8), name="Asia/Shanghai")

TOP_LEVEL_FIELDS = {
    "exportSchemaVersion",
    "exportedAt",
    "manifest",
    "raw",
    "normalized",
}
MANIFEST_REQUIRED_FIELDS = {
    "snapshotId",
    "roomKey",
    "platform",
    "authorityStatus",
    "startedAt",
    "completedAt",
    "status",
    "messageCount",
    "sequenceHash",
    "evidence",
    "rawIncluded",
    "normalizedIncluded",
}
NORMALIZED_REQUIRED_FIELDS = {
    "index",
    "role",
    "content",
    "thinking",
    "timeEvidence",
    "provenanceStatus",
    "observedAt",
    "contentHash",
    "segmentIndex",
}
VALID_ROLES = {"system", "user", "assistant", "tool", "developer"}
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
SELF_STAMP = re.compile(
    r"^\ufeff?\s*(?P<date>20\d{2}-\d{2}-\d{2})[ \t]+"
    r"(?P<time>\d{2}:\d{2})(?:\s*[（(]成都[）)])\s*(?:\n|$)"
)


class GuijianAdapterError(ValueError):
    """The GuiJian export is ineligible for safe formal conversion."""

    def __init__(self, message: str, *, report: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.report = report


class _Issues:
    def __init__(self) -> None:
        self.warnings: List[Dict[str, Any]] = []
        self.fatal: List[Dict[str, Any]] = []

    @staticmethod
    def _add(target: List[Dict[str, Any]], code: str, message: str, count: int) -> None:
        for issue in target:
            if issue["code"] == code:
                issue["count"] += count
                return
        target.append({"code": code, "message": message, "count": count})

    def warning(self, code: str, message: str, count: int = 1) -> None:
        self._add(self.warnings, code, message, count)

    def fail(self, code: str, message: str, count: int = 1) -> None:
        self._add(self.fatal, code, message, count)


@dataclass(frozen=True)
class GuijianMessage:
    index: int
    role: str
    content: str
    content_hash: str
    segment_index: int
    created_at: str
    time_basis: str
    time_precision: str


@dataclass
class GuijianInspection:
    report: Dict[str, Any]
    payload: Optional[Dict[str, Any]]
    messages: List[GuijianMessage]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _normalize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip("\t ") for line in text.split("\n"))
    return text.strip("\n")


def guijian_content_hash(role: str, content: str, thinking: str = "") -> str:
    """Reproduce GuiJian's ``fingerprintMessage`` contract."""

    return _sha256_text(
        f"{str(role or 'unknown').lower()}\0"
        f"{_normalize_text(content)}\0{_normalize_text(thinking)}"
    )


def guijian_sequence_hash(content_hashes: Sequence[str]) -> str:
    """Reproduce GuiJian's sequence hash over normalized fingerprints."""

    return _sha256_text("\n".join(content_hashes))


def _normalized_timestamp(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _strict_content_timestamp(role: str, content: str) -> Optional[str]:
    if role != "assistant":
        return None
    match = SELF_STAMP.match(content)
    if match is None:
        return None
    try:
        local = datetime.strptime(
            f"{match.group('date')} {match.group('time')}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=CHENGDU)
    except ValueError:
        return None
    return local.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _load_input(path: str) -> tuple[bytes, str]:
    input_path = Path(path)
    if not input_path.is_file():
        raise GuijianAdapterError(f"GuiJian export does not exist: {input_path}")
    size = input_path.stat().st_size
    if size <= 0:
        raise GuijianAdapterError("GuiJian export is empty")
    if size > MAX_INPUT_BYTES:
        raise GuijianAdapterError("GuiJian export exceeds the 500 MB safety limit")
    raw = input_path.read_bytes()
    return raw, _sha256_bytes(raw)


def _parse_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GuijianAdapterError("GuiJian export must be strict UTF-8") from exc

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON number: {value}")

    try:
        return json.loads(text, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise GuijianAdapterError("GuiJian export is malformed JSON") from exc


def _blank_report(input_sha256: Optional[str] = None) -> Dict[str, Any]:
    return {
        "adapter": ADAPTER_ID,
        "dry_run": True,
        "input_sha256": input_sha256,
        "export_schema_version": None,
        "platform": None,
        "authority_status": None,
        "status": None,
        "exported_at": None,
        "counts": {
            "normalized_messages": 0,
            "raw_segments": 0,
            "raw_observations": 0,
            "repeated_content_hash_groups": 0,
            "repeated_content_hash_observations": 0,
        },
        "capture_evidence": {
            "top_confirmed": None,
            "effective_top_confirmed": None,
            "top_confirmation_basis": None,
            "top_witness": None,
            "dom_quiet_confirmed": None,
            "absolute_completeness_proven": None,
            "gap_count": 0,
            "role_warnings": None,
        },
        "time_evidence": {
            "platform_time": 0,
            "content_self_stamp": 0,
            "next_content_stamp_upper_bound": 0,
            "capture_observed_upper_bound": 0,
            "unresolved": 0,
        },
        "estimated": {"output_records": 0, "branch_memberships": 0},
        "warnings": [],
        "fatal": [],
        "can_convert": False,
    }


def _finish_report(report: Dict[str, Any], issues: _Issues) -> None:
    report["warnings"] = issues.warnings
    report["fatal"] = issues.fatal
    report["can_convert"] = not issues.fatal


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _hex64(value: Any) -> bool:
    return isinstance(value, str) and bool(HEX_64.fullmatch(value))


def _render_content(content: str, thinking: str) -> str:
    if not thinking:
        return content
    if not content:
        return f"[归笺可见思考区]\n{thinking}"
    return f"{content}\n\n[归笺可见思考区]\n{thinking}"


def inspect_guijian_export(
    path: str, *, top_witness_path: Optional[str] = None
) -> GuijianInspection:
    """Validate one single-room GuiJian export without writing anything."""

    raw, input_sha256 = _load_input(path)
    report = _blank_report(input_sha256)
    issues = _Issues()
    try:
        payload = _parse_json(raw)
    except GuijianAdapterError as exc:
        issues.fail("invalid-json", str(exc))
        _finish_report(report, issues)
        return GuijianInspection(report, None, [])
    if not isinstance(payload, dict):
        issues.fail("invalid-root", "GuiJian export root must be an object")
        _finish_report(report, issues)
        return GuijianInspection(report, None, [])

    unknown_top = sorted(set(payload) - TOP_LEVEL_FIELDS)
    if unknown_top:
        issues.warning(
            "unknown-top-level-fields",
            "Unrecognized top-level fields were preserved in the source but not converted",
            len(unknown_top),
        )
    export_schema = payload.get("exportSchemaVersion")
    report["export_schema_version"] = export_schema
    if export_schema != EXPORT_SCHEMA_VERSION:
        issues.fail(
            "unsupported-export-schema",
            f"GuiJian export schema must be {EXPORT_SCHEMA_VERSION}",
        )

    exported_at = _normalized_timestamp(payload.get("exportedAt"))
    report["exported_at"] = exported_at
    if exported_at is None:
        issues.fail("invalid-exported-at", "exportedAt must be timezone-aware ISO-8601")

    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        issues.fail("invalid-manifest", "manifest must be an object")
        _finish_report(report, issues)
        return GuijianInspection(report, payload, [])
    missing_manifest = sorted(MANIFEST_REQUIRED_FIELDS - set(manifest))
    if missing_manifest:
        issues.fail(
            "missing-manifest-fields",
            "GuiJian manifest is missing reviewed required fields",
            len(missing_manifest),
        )

    platform = manifest.get("platform")
    authority = manifest.get("authorityStatus")
    status = manifest.get("status")
    report["platform"] = platform
    report["authority_status"] = authority
    report["status"] = status
    if platform != SUPPORTED_PLATFORM:
        issues.fail(
            "unsupported-platform",
            "This adapter accepts only ChatGPT GuiJian captures",
        )
    if authority != SUPPORTED_AUTHORITY:
        issues.fail(
            "unsupported-authority-status",
            "GuiJian capture must remain provisional pending an official export",
        )
    if status not in SUPPORTED_CAPTURE_STATUSES:
        issues.fail(
            "unsupported-capture-status",
            "GuiJian capture status is not eligible for formal conversion",
        )
    started_at = _normalized_timestamp(manifest.get("startedAt"))
    completed_at = _normalized_timestamp(manifest.get("completedAt"))
    if started_at is None or completed_at is None:
        issues.fail(
            "invalid-capture-window",
            "startedAt and completedAt must be timezone-aware ISO-8601",
        )
    elif completed_at < started_at:
        issues.fail(
            "backward-capture-window",
            "completedAt must not precede startedAt",
        )
    if not _hex64(manifest.get("snapshotId")):
        issues.fail("invalid-snapshot-id", "snapshotId must be lowercase SHA-256")
    predecessor = manifest.get("predecessorSnapshotId")
    if predecessor is not None and not _hex64(predecessor):
        issues.fail(
            "invalid-predecessor-snapshot-id",
            "predecessorSnapshotId must be null or lowercase SHA-256",
        )
    room_key = manifest.get("roomKey")
    if not isinstance(room_key, str) or not room_key.strip():
        issues.fail("invalid-room-key", "roomKey must be a non-empty string")

    normalized = payload.get("normalized")
    raw_layer = payload.get("raw")
    if not isinstance(normalized, list):
        issues.fail("invalid-normalized", "normalized must be an array")
        normalized = []
    if not isinstance(raw_layer, dict) or not isinstance(raw_layer.get("segments"), list):
        issues.fail("invalid-raw-layer", "raw.segments must be an array")
        raw_segments: List[Any] = []
    else:
        raw_segments = raw_layer["segments"]
    if any(not isinstance(segment, list) for segment in raw_segments):
        issues.fail("invalid-raw-segment", "every raw segment must be an array")
    raw_fingerprints: List[str] = []
    for segment in raw_segments:
        if not isinstance(segment, list):
            continue
        for observation in segment:
            if not isinstance(observation, dict) or not _hex64(
                observation.get("fingerprint")
            ):
                issues.fail(
                    "invalid-raw-observation",
                    "Every raw observation must carry a lowercase SHA-256 fingerprint",
                )
                continue
            raw_fingerprints.append(observation["fingerprint"])
    raw_observation_count = sum(
        len(segment) for segment in raw_segments if isinstance(segment, list)
    )
    report["counts"]["normalized_messages"] = len(normalized)
    report["counts"]["raw_segments"] = len(raw_segments)
    report["counts"]["raw_observations"] = raw_observation_count
    if raw_observation_count != len(normalized):
        issues.fail(
            "raw-normalized-count-mismatch",
            "raw observations and normalized messages must have equal counts",
        )
    if manifest.get("messageCount") != len(normalized):
        issues.fail(
            "message-count-mismatch",
            "manifest messageCount must equal normalized message count",
        )
    if manifest.get("rawIncluded") is not True or manifest.get("normalizedIncluded") is not True:
        issues.fail(
            "incomplete-export-layers",
            "Formal conversion requires both raw and normalized GuiJian layers",
        )

    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict):
        issues.fail("invalid-capture-evidence", "manifest.evidence must be an object")
        evidence = {}
    capture = report["capture_evidence"]
    capture["top_confirmed"] = evidence.get("topConfirmed")
    capture["dom_quiet_confirmed"] = evidence.get("domQuietConfirmed")
    capture["absolute_completeness_proven"] = evidence.get(
        "absoluteCompletenessProven"
    )
    gaps = evidence.get("gaps")
    if not isinstance(gaps, list):
        issues.fail("invalid-gap-evidence", "capture gaps must be an array")
        gaps = []
    capture["gap_count"] = len(gaps)
    capture["role_warnings"] = evidence.get("roleWarnings")
    needs_top_witness = evidence.get("topConfirmed") is not True
    if not needs_top_witness:
        capture["effective_top_confirmed"] = True
        capture["top_confirmation_basis"] = "current-capture"
    elif top_witness_path is None:
        issues.fail("top-not-confirmed", "Capture did not prove it reached the visible top")
    if evidence.get("domQuietConfirmed") is not True:
        issues.fail("dom-not-quiet", "Capture did not reach a stable visible DOM")
    if gaps:
        issues.fail(
            "capture-has-gaps",
            "Disjoint capture segments must be repaired before formal conversion",
            len(gaps),
        )
    if evidence.get("roleWarnings") != 0:
        issues.fail("role-warnings", "Capture contains unresolved role warnings")
    if evidence.get("segmentCount") != len(raw_segments):
        issues.fail(
            "segment-count-mismatch",
            "evidence segmentCount must match raw.segments",
        )

    candidates: List[Dict[str, Any]] = []
    content_hashes: List[str] = []
    for expected_index, item in enumerate(normalized):
        if not isinstance(item, dict):
            issues.fail("invalid-normalized-message", "normalized items must be objects")
            continue
        missing = NORMALIZED_REQUIRED_FIELDS - set(item)
        if missing:
            issues.fail(
                "missing-normalized-fields",
                "A normalized message is missing reviewed required fields",
                len(missing),
            )
            continue
        index = item.get("index")
        if index != expected_index:
            issues.fail(
                "non-sequential-index",
                "normalized message indexes must be zero-based and sequential",
            )
            continue
        role = item.get("role")
        if role not in VALID_ROLES:
            issues.fail("unsupported-role", "GuiJian message role is not supported")
            continue
        content = _normalize_text(item.get("content"))
        thinking = _normalize_text(item.get("thinking"))
        rendered = _render_content(content, thinking)
        if not rendered.strip():
            issues.fail("blank-message", "GuiJian message content must not be blank")
            continue
        segment_index = item.get("segmentIndex")
        if (
            not _is_nonnegative_int(segment_index)
            or segment_index >= len(raw_segments)
        ):
            issues.fail(
                "invalid-segment-index",
                "segmentIndex must identify one raw segment",
            )
            continue
        declared_hash = item.get("contentHash")
        actual_hash = guijian_content_hash(role, content, thinking)
        if declared_hash != actual_hash:
            issues.fail(
                "content-hash-mismatch",
                "A normalized contentHash does not match role, content, and thinking",
            )
            continue
        if item.get("provenanceStatus") != authority:
            issues.fail(
                "provenance-status-mismatch",
                "Message provenanceStatus must match manifest authorityStatus",
            )
            continue
        observed_at = _normalized_timestamp(item.get("observedAt"))
        if observed_at is None:
            issues.fail(
                "invalid-observed-at",
                "Every GuiJian message requires a timezone-aware observedAt",
            )
            continue
        time_evidence = item.get("timeEvidence")
        if not isinstance(time_evidence, dict):
            issues.fail("invalid-time-evidence", "timeEvidence must be an object")
            continue
        platform_time = None
        if time_evidence.get("kind") == "platform-time-candidate":
            platform_time = _normalized_timestamp(time_evidence.get("eventTime"))
            if platform_time is None:
                issues.fail(
                    "unresolved-platform-time",
                    "A platform time candidate must contain a timezone-aware eventTime",
                )
                continue
        content_stamp = _strict_content_timestamp(role, content)
        direct_time = platform_time or content_stamp
        direct_basis = (
            "platform-time-candidate"
            if platform_time
            else "content-self-stamp-minute"
            if content_stamp
            else None
        )
        candidates.append(
            {
                "index": index,
                "role": role,
                "content": rendered,
                "content_hash": declared_hash,
                "segment_index": segment_index,
                "observed_at": observed_at,
                "direct_time": direct_time,
                "direct_basis": direct_basis,
            }
        )
        content_hashes.append(declared_hash)

    if len(content_hashes) == len(normalized):
        actual_sequence_hash = guijian_sequence_hash(content_hashes)
        if manifest.get("sequenceHash") != actual_sequence_hash:
            issues.fail(
                "sequence-hash-mismatch",
                "manifest sequenceHash does not match normalized message order",
            )
        if raw_fingerprints != content_hashes:
            issues.fail(
                "raw-normalized-sequence-mismatch",
                "Raw observation fingerprints must match normalized message order",
            )

    if needs_top_witness and top_witness_path is not None:
        try:
            witness = inspect_guijian_export(top_witness_path)
        except (GuijianAdapterError, OSError):
            witness = None
        if witness is None or witness.payload is None:
            issues.fail(
                "invalid-top-witness",
                "The prior top witness could not be validated",
            )
        else:
            witness_fatal = {
                str(item.get("code")) for item in witness.report.get("fatal", [])
            }
            disallowed_fatal = witness_fatal - TOP_WITNESS_IGNORABLE_FATAL_CODES
            witness_manifest = witness.payload.get("manifest")
            witness_normalized = witness.payload.get("normalized")
            if disallowed_fatal:
                issues.fail(
                    "invalid-top-witness",
                    "The prior top witness failed a structural or integrity gate",
                )
            elif not isinstance(witness_manifest, dict) or not isinstance(
                witness_normalized, list
            ):
                issues.fail(
                    "invalid-top-witness",
                    "The prior top witness lacks reviewed manifest or message data",
                )
            elif witness_manifest.get("roomKey") != manifest.get("roomKey"):
                issues.fail(
                    "top-witness-room-mismatch",
                    "The prior top witness belongs to a different room",
                )
            elif (witness_manifest.get("evidence") or {}).get("topConfirmed") is not True:
                issues.fail(
                    "top-witness-not-confirmed",
                    "The prior capture did not itself confirm the visible top",
                )
            elif (
                witness.report.get("exported_at") is None
                or exported_at is None
                or witness.report["exported_at"] > exported_at
            ):
                issues.fail(
                    "top-witness-time-order",
                    "The top witness must not be newer than the current capture",
                )
            else:
                witness_hashes = [
                    item.get("contentHash")
                    for item in witness_normalized
                    if isinstance(item, dict)
                ]
                matching_prefix = 0
                prefix_limit = min(len(witness_hashes), len(content_hashes))
                while (
                    matching_prefix < prefix_limit
                    and witness_hashes[matching_prefix]
                    == content_hashes[matching_prefix]
                ):
                    matching_prefix += 1
                if matching_prefix < MIN_TOP_WITNESS_PREFIX_MESSAGES:
                    issues.fail(
                        "top-witness-prefix-too-short",
                        "The prior top witness does not match enough leading messages",
                    )
                else:
                    capture["effective_top_confirmed"] = True
                    capture["top_confirmation_basis"] = "prior-top-prefix-witness"
                    capture["top_witness"] = {
                        "input_sha256": witness.report.get("input_sha256"),
                        "matching_prefix_messages": matching_prefix,
                    }

    hash_counts: Dict[str, int] = {}
    for value in content_hashes:
        hash_counts[value] = hash_counts.get(value, 0) + 1
    repeated = [count for count in hash_counts.values() if count > 1]
    report["counts"]["repeated_content_hash_groups"] = len(repeated)
    report["counts"]["repeated_content_hash_observations"] = sum(repeated)
    if repeated:
        issues.warning(
            "repeated-content-hashes-preserved",
            "Repeated bodies were preserved because content equality is not message identity",
            len(repeated),
        )

    direct = [
        (item["index"], item["direct_time"])
        for item in candidates
        if item["direct_time"] is not None
    ]
    for (_, prior), (_, current) in zip(direct, direct[1:]):
        if current < prior:
            issues.fail(
                "non-monotonic-direct-time",
                "Direct message-time evidence moves backward in visible order",
            )
            break
    direct_indexes = [item[0] for item in direct]
    direct_by_index = {item[0]: item[1] for item in direct}
    messages: List[GuijianMessage] = []
    for item in candidates:
        created_at = item["direct_time"]
        basis = item["direct_basis"]
        precision = "minute" if basis == "content-self-stamp-minute" else "instant"
        if basis is None:
            next_offset = bisect_right(direct_indexes, item["index"])
            if next_offset < len(direct_indexes):
                created_at = direct_by_index[direct_indexes[next_offset]]
                basis = "next-content-stamp-upper-bound"
                precision = "upper-bound-only"
            else:
                created_at = item["observed_at"]
                basis = "capture-observed-upper-bound"
                precision = "upper-bound-only"
        if created_at is None:
            report["time_evidence"]["unresolved"] += 1
            issues.fail("unresolved-message-time", "A message has no defensible time bound")
            continue
        report["time_evidence"][
            {
                "platform-time-candidate": "platform_time",
                "content-self-stamp-minute": "content_self_stamp",
                "next-content-stamp-upper-bound": "next_content_stamp_upper_bound",
                "capture-observed-upper-bound": "capture_observed_upper_bound",
            }[basis]
        ] += 1
        messages.append(
            GuijianMessage(
                index=item["index"],
                role=item["role"],
                content=item["content"],
                content_hash=item["content_hash"],
                segment_index=item["segment_index"],
                created_at=created_at,
                time_basis=basis,
                time_precision=precision,
            )
        )

    report["estimated"]["output_records"] = len(messages)
    report["estimated"]["branch_memberships"] = len(messages)
    _finish_report(report, issues)
    return GuijianInspection(report, payload, messages)


def dry_run_guijian_export(
    path: str, *, top_witness_path: Optional[str] = None
) -> Dict[str, Any]:
    return inspect_guijian_export(path, top_witness_path=top_witness_path).report


def _source_ref(
    input_sha256: str,
    room_key: str,
    snapshot_id: str,
    message: GuijianMessage,
) -> str:
    query = urlencode(
        {
            "timeBasis": message.time_basis,
            "timePrecision": message.time_precision,
            "segment": message.segment_index,
        }
    )
    return (
        f"guijian-browser-capture-v1://sha256/{input_sha256}"
        f"/room/{quote(room_key, safe='')}"
        f"/snapshot/{snapshot_id}/index/{message.index}?{query}"
    )


def _authority_for_time_basis(time_basis: str) -> str:
    if time_basis == "platform-time-candidate":
        return "guijian-browser-platform-time-candidate"
    if time_basis == "content-self-stamp-minute":
        return "guijian-browser-content-self-stamp"
    return "guijian-browser-time-upper-bound"


def convert_guijian_export(
    path: str, *, top_witness_path: Optional[str] = None
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Return a deterministic compact record package and redacted dry-run."""

    inspection = inspect_guijian_export(path, top_witness_path=top_witness_path)
    report = inspection.report
    payload = inspection.payload
    if not report["can_convert"] or payload is None:
        raise GuijianAdapterError("GuiJian export failed dry-run", report=report)
    manifest = payload["manifest"]
    room_key = manifest["roomKey"]
    snapshot_id = manifest["snapshotId"]
    input_sha256 = report["input_sha256"]
    source_cutoff_at = report["exported_at"]
    room_hash = _sha256_text(f"{SUPPORTED_PLATFORM}\n{room_key}")
    conversation_id = f"guijian-v1:{room_hash}"
    branch_id = f"guijian-v1:{room_hash[:24]}:visible"

    records: List[Dict[str, Any]] = []
    for message in inspection.messages:
        record_seed = "\n".join(
            (SUPPORTED_PLATFORM, room_key, str(message.index), message.content_hash)
        )
        record_hash = _sha256_text(record_seed)
        records.append(
            {
                "record_id": f"gjv1_{record_hash}",
                "source_kind": ADAPTER_ID,
                "source_ref": _source_ref(
                    input_sha256, room_key, snapshot_id, message
                ),
                "conversation_id": conversation_id,
                "branch_memberships": [
                    {"branch_id": branch_id, "position": message.index}
                ],
                "message_id": f"guijian-v1:{record_hash}",
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at,
                "verified": False,
                "authority": _authority_for_time_basis(message.time_basis),
                "source_cutoff_at": source_cutoff_at,
                "conflict_group_id": None,
            }
        )

    package = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "source": {
            "adapter": ADAPTER_ID,
            "input_sha256": input_sha256,
            "format": EXPORT_SCHEMA_VERSION,
            "platform": SUPPORTED_PLATFORM,
            "authority_status": SUPPORTED_AUTHORITY,
            "snapshot_id": snapshot_id,
            "room_key_sha256": room_hash,
            "exported_at": source_cutoff_at,
            "verification": "unverified-browser-observation",
            "time_evidence": dict(report["time_evidence"]),
            "top_confirmation": dict(report["capture_evidence"]),
        },
        "records": records,
    }
    if len(records) != report["estimated"]["output_records"]:
        raise GuijianAdapterError(
            "conversion record count differed from dry-run", report=report
        )
    return package, report


def serialize_record_package(package: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            package,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def write_converted_package(
    path: str,
    output_path: str,
    *,
    top_witness_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically create one formal compact package without overwriting."""

    destination = Path(output_path)
    if destination.exists():
        raise FileExistsError(f"conversion output already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(
            f"conversion output parent does not exist: {destination.parent}"
        )
    package, report = convert_guijian_export(
        path, top_witness_path=top_witness_path
    )
    output_bytes = serialize_record_package(package)
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary.write(output_bytes)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return {
        "adapter": ADAPTER_ID,
        "output_path": str(destination),
        "output_records": len(package["records"]),
        "output_bytes": len(output_bytes),
        "output_sha256": _sha256_bytes(output_bytes),
        "dry_run": report,
    }
