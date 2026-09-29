"""Structured sidecars and deterministic replay for private Echo bad cases.

Raw ``bad-case-*.json`` packets remain immutable.  This module writes only to
an explicit registry directory outside the repository, where review cards and
replay summaries live beside (but never replace) the captured evidence.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence


RAW_SCHEMA_VERSION = "echo-pact-bad-case-v1"
CARD_SCHEMA_VERSION = "echo-pact-bad-case-card-v1"
REPORT_SCHEMA_VERSION = "echo-pact-bad-case-report-v1"
REPLAY_SCHEMA_VERSION = "echo-pact-bad-case-replay-v1"

STATUS_VALUES = frozenset({"open", "triaged", "fixed", "monitoring", "wont_fix"})
ISSUE_FAMILIES = frozenset(
    {
        "answer.synthesis",
        "coverage.archive_gap",
        "entity.distinctive_term",
        "entity.relation_disambiguation",
        "query.speech_rescue",
        "retrieval.candidate_miss",
        "retrieval.ranking",
        "temporal.explicit_date",
        "temporal.event_timeline",
        "temporal.first_occurrence",
        "temporal.named_calendar_event",
        "temporal.relative_date",
        "other.unclassified",
    }
)
LAYER_VALUES = frozenset(
    {"coverage", "query_planning", "candidate_generation", "ranking", "answer"}
)

_RAW_GLOB = "bad-case-*.json"
_EXPLICIT_DATE_RE = re.compile(
    r"(?:20\d{2}[-/.年])?\s*(?:0?[1-9]|1[0-2])\s*(?:月|[-/.])\s*"
    r"(?:0?[1-9]|[12]\d|3[01])\s*(?:日|号)?"
)
_DISTINCTIVE_TOKEN_RE = re.compile(r"[A-Za-z\u3400-\u9fff]+")


class BadCaseRegistryError(ValueError):
    """Raised when private registry invariants are violated."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("xb") as handle:
            handle.write(_canonical_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BadCaseRegistryError(f"invalid JSON: {path.name}") from exc
    if not isinstance(payload, dict):
        raise BadCaseRegistryError(f"JSON root must be an object: {path.name}")
    return payload


def _user_query(packet: Mapping[str, Any]) -> str:
    dialogue = packet.get("dialogue")
    if not isinstance(dialogue, list):
        raise BadCaseRegistryError("bad-case dialogue must be a list")
    for item in dialogue:
        if isinstance(item, Mapping) and item.get("role") == "user":
            query = str(item.get("content") or "").strip()
            if query:
                return query
    raise BadCaseRegistryError("bad-case packet has no user query")


def _original_top_ids(packet: Mapping[str, Any]) -> list[str]:
    recall = packet.get("recall")
    if not isinstance(recall, Mapping):
        return []
    debug = recall.get("recall_debug")
    if not isinstance(debug, Mapping):
        return []
    result = []
    for item in debug.get("top_k") or []:
        if isinstance(item, Mapping) and item.get("record_id"):
            result.append(str(item["record_id"]))
    return result


def _coverage_gap(packet: Mapping[str, Any]) -> bool:
    recall = packet.get("recall")
    coverage = recall.get("coverage") if isinstance(recall, Mapping) else None
    if not isinstance(coverage, Mapping) or not coverage.get("coverage_gap"):
        return False
    # Older real-life packets reported a gap whenever their verified cutoff was
    # unknown.  That is a coverage caveat, not evidence that coverage caused the
    # bad case, so do not flood every card with the same false diagnosis.
    return coverage.get("coverage_status") not in {None, "verified_cutoff_unknown"}


def _contains_mixed_distinctive_token(query: str) -> bool:
    for token in _DISTINCTIVE_TOKEN_RE.findall(query):
        if re.search(r"[A-Za-z]", token) and re.search(r"[\u3400-\u9fff]", token):
            return True
    return False


def suggest_issue_families(packet: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Return conservative, explainable suggestions; never a final diagnosis."""

    query = _user_query(packet)
    note = str(packet.get("note") or "")
    text = f"{query}\n{note}"
    families: set[str] = set()
    layers: set[str] = set()

    if _coverage_gap(packet):
        families.add("coverage.archive_gap")
        layers.add("coverage")
    named_calendar_event = any(
        term in query for term in ("七夕", "春节", "元宵", "端午", "中秋")
    )
    if not named_calendar_event and any(
        term in query for term in ("上周", "下周", "昨天", "前天", "那天", "最近")
    ):
        families.add("temporal.relative_date")
        layers.add("query_planning")
    if _EXPLICIT_DATE_RE.search(query):
        families.add("temporal.explicit_date")
        layers.add("query_planning")
    if re.search(r"(?:两|三|几)次|第[一二三]次|具体哪些日子|哪些日子|分别哪天", note):
        families.add("temporal.event_timeline")
        layers.update(("query_planning", "answer"))
    if any(term in query for term in ("第一次", "最早", "开始做", "开始写", "哪天取")):
        families.add("temporal.first_occurrence")
        layers.update(("query_planning", "ranking"))
    if named_calendar_event:
        families.add("temporal.named_calendar_event")
        layers.add("query_planning")
    if _contains_mixed_distinctive_token(query):
        families.add("entity.distinctive_term")
        layers.update(("candidate_generation", "ranking"))
    if any(term in note for term in ("顺序不对", "召回顺序", "没有给脑袋看到", "排在")):
        families.add("retrieval.ranking")
        layers.add("ranking")
    if any(term in note for term in ("没有召回", "没召回", "召不回来", "召回不准")):
        families.add("retrieval.candidate_miss")
        layers.add("candidate_generation")
    if any(
        term.lower() in text.lower()
        for term in ("模型", "脑袋", "脑子", "嘴笨", "ds")
    ):
        families.add("answer.synthesis")
        layers.add("answer")
    if any(term in note for term in ("对应", "混入", "串线", "不是我", "不是你的")):
        families.add("entity.relation_disambiguation")
        layers.update(("query_planning", "answer"))

    if not families:
        families.add("other.unclassified")
    return sorted(families), sorted(layers)


def _validate_values(values: Iterable[str], allowed: frozenset[str], label: str) -> list[str]:
    normalized = sorted({str(value).strip() for value in values if str(value).strip()})
    invalid = [value for value in normalized if value not in allowed]
    if invalid:
        raise BadCaseRegistryError(f"unknown {label}: {', '.join(invalid)}")
    return normalized


class BadCaseRegistry:
    """Manage private cards and replay summaries without editing raw captures."""

    def __init__(
        self,
        bad_case_root: str | Path,
        registry_root: str | Path,
        *,
        forbidden_root: str | Path | None = None,
    ):
        self.bad_case_root = Path(bad_case_root).expanduser().resolve()
        self.registry_root = Path(registry_root).expanduser().resolve()
        if self.bad_case_root == self.registry_root:
            raise BadCaseRegistryError("registry directory must not replace the raw directory")
        if forbidden_root is not None:
            forbidden = Path(forbidden_root).resolve()
            for target in (self.bad_case_root, self.registry_root):
                if target == forbidden or forbidden in target.parents:
                    raise BadCaseRegistryError("private bad-case data must stay outside repository")

    @property
    def cards_root(self) -> Path:
        return self.registry_root / "cards"

    @property
    def replays_root(self) -> Path:
        return self.registry_root / "replays"

    def raw_paths(self) -> list[Path]:
        if not self.bad_case_root.is_dir():
            raise BadCaseRegistryError("bad-case directory does not exist")
        return sorted(self.bad_case_root.glob(_RAW_GLOB))

    def load_raw(self, path: Path) -> tuple[Dict[str, Any], str]:
        resolved = path.resolve()
        if resolved.parent != self.bad_case_root:
            raise BadCaseRegistryError("bad-case path escaped configured directory")
        raw_bytes = resolved.read_bytes()
        packet = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(packet, dict) or packet.get("schema_version") != RAW_SCHEMA_VERSION:
            raise BadCaseRegistryError(f"unsupported bad-case schema: {path.name}")
        _user_query(packet)
        return packet, _sha256_bytes(raw_bytes)

    def card_path(self, case_id: str) -> Path:
        if not re.fullmatch(r"bad-case-[A-Za-z0-9._-]+", case_id):
            raise BadCaseRegistryError("invalid case id")
        return self.cards_root / f"{case_id}.json"

    def initialize(self, *, now: Callable[[], str] = _utc_now) -> Dict[str, int]:
        created = 0
        existing = 0
        for raw_path in self.raw_paths():
            packet, raw_hash = self.load_raw(raw_path)
            case_id = raw_path.stem
            card_path = self.card_path(case_id)
            suggestions, layers = suggest_issue_families(packet)
            if card_path.exists():
                card = _read_json(card_path)
                if card.get("source_sha256") != raw_hash:
                    raise BadCaseRegistryError(
                        f"raw packet changed after registration: {raw_path.name}"
                    )
                existing += 1
                continue
            registered_at = now()
            card = {
                "schema_version": CARD_SCHEMA_VERSION,
                "case_id": case_id,
                "source_file": raw_path.name,
                "source_sha256": raw_hash,
                "captured_at": packet.get("captured_at"),
                "registered_at": registered_at,
                "status": "open",
                "suggested_issue_families": suggestions,
                "reviewed_issue_families": [],
                "suspected_layers": layers,
                "expectations": {
                    "must_recall_any": [],
                    "must_recall_all": [],
                    "must_not_recall": [],
                },
                "resolution": {"fix_ref": None, "summary": None},
                "history": [
                    {
                        "at": registered_at,
                        "event": "registered",
                        "status": "open",
                    }
                ],
            }
            _write_json_atomic(card_path, card)
            created += 1
        return {"raw_cases": len(self.raw_paths()), "created": created, "existing": existing}

    def load_card(self, case_id: str) -> Dict[str, Any]:
        card = _read_json(self.card_path(case_id))
        if card.get("schema_version") != CARD_SCHEMA_VERSION:
            raise BadCaseRegistryError("unsupported card schema")
        source_path = self.bad_case_root / str(card.get("source_file") or "")
        _, raw_hash = self.load_raw(source_path)
        if card.get("source_sha256") != raw_hash:
            raise BadCaseRegistryError("raw packet changed after registration")
        return card

    def review(
        self,
        case_id: str,
        *,
        status: Optional[str] = None,
        issue_families: Optional[Sequence[str]] = None,
        layers: Optional[Sequence[str]] = None,
        must_recall_any: Optional[Sequence[str]] = None,
        must_recall_all: Optional[Sequence[str]] = None,
        must_not_recall: Optional[Sequence[str]] = None,
        fix_ref: Optional[str] = None,
        summary: Optional[str] = None,
        note: str = "",
        now: Callable[[], str] = _utc_now,
    ) -> Dict[str, Any]:
        card = self.load_card(case_id)
        changes: Dict[str, Any] = {}
        if status is not None:
            if status not in STATUS_VALUES:
                raise BadCaseRegistryError(f"unknown status: {status}")
            changes["status"] = status
            card["status"] = status
        if issue_families is not None:
            reviewed = _validate_values(issue_families, ISSUE_FAMILIES, "issue family")
            changes["reviewed_issue_families"] = reviewed
            card["reviewed_issue_families"] = reviewed
        if layers is not None:
            reviewed_layers = _validate_values(layers, LAYER_VALUES, "layer")
            changes["suspected_layers"] = reviewed_layers
            card["suspected_layers"] = reviewed_layers

        expectations = dict(card.get("expectations") or {})
        for key, values in (
            ("must_recall_any", must_recall_any),
            ("must_recall_all", must_recall_all),
            ("must_not_recall", must_not_recall),
        ):
            if values is not None:
                normalized = sorted({str(value).strip() for value in values if str(value).strip()})
                expectations[key] = normalized
                changes.setdefault("expectations", {})[key] = normalized
        card["expectations"] = expectations

        resolution = dict(card.get("resolution") or {})
        if fix_ref is not None:
            resolution["fix_ref"] = fix_ref.strip() or None
            changes.setdefault("resolution", {})["fix_ref"] = resolution["fix_ref"]
        if summary is not None:
            resolution["summary"] = summary.strip() or None
            changes.setdefault("resolution", {})["summary"] = resolution["summary"]
        card["resolution"] = resolution

        if card.get("status") == "fixed" and not resolution.get("fix_ref"):
            raise BadCaseRegistryError("fixed status requires a fix reference")
        if not changes and not note.strip():
            raise BadCaseRegistryError("review must change a field or add a note")
        event = {
            "at": now(),
            "event": "reviewed",
            "changes": changes,
            "note": note.strip(),
        }
        history = list(card.get("history") or [])
        history.append(event)
        card["history"] = history
        _write_json_atomic(self.card_path(case_id), card)
        return card

    def cards(self) -> list[Dict[str, Any]]:
        if not self.cards_root.exists():
            return []
        return [self.load_card(path.stem) for path in sorted(self.cards_root.glob("*.json"))]

    def report(self, *, include_queries: bool = False) -> Dict[str, Any]:
        status_counts: Counter[str] = Counter()
        family_counts: Counter[str] = Counter()
        layer_counts: Counter[str] = Counter()
        cases = []
        for card in self.cards():
            status = str(card.get("status") or "open")
            reviewed = list(card.get("reviewed_issue_families") or [])
            families = reviewed or list(card.get("suggested_issue_families") or [])
            layers = list(card.get("suspected_layers") or [])
            status_counts[status] += 1
            family_counts.update(families)
            layer_counts.update(layers)
            entry: Dict[str, Any] = {
                "case_id": card["case_id"],
                "status": status,
                "issue_families": families,
                "family_source": "reviewed" if reviewed else "suggested",
                "suspected_layers": layers,
                "fix_ref": (card.get("resolution") or {}).get("fix_ref"),
            }
            latest_replay = self._latest_replay(card["case_id"])
            entry["latest_replay_verdict"] = (
                latest_replay.get("verdict") if latest_replay else None
            )
            if include_queries:
                raw, _ = self.load_raw(self.bad_case_root / card["source_file"])
                entry["query"] = _user_query(raw)
            cases.append(entry)
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": _utc_now(),
            "case_count": len(cases),
            "status_counts": dict(sorted(status_counts.items())),
            "issue_family_counts": dict(sorted(family_counts.items())),
            "suspected_layer_counts": dict(sorted(layer_counts.items())),
            "cases": cases,
        }

    def _latest_replay(self, case_id: str) -> Optional[Dict[str, Any]]:
        replay_dir = self.replays_root / case_id
        paths = sorted(replay_dir.glob("*.json")) if replay_dir.exists() else []
        return _read_json(paths[-1]) if paths else None

    def replay(
        self,
        case_id: str,
        *,
        runner: Callable[[str], Mapping[str, Any]],
        now: Callable[[], str] = _utc_now,
    ) -> Dict[str, Any]:
        card = self.load_card(case_id)
        raw, _ = self.load_raw(self.bad_case_root / card["source_file"])
        query = _user_query(raw)
        response = runner(query)
        if not isinstance(response, Mapping):
            raise BadCaseRegistryError("replay runner must return a mapping")
        debug = response.get("recall_debug") if isinstance(response, Mapping) else None
        top_k = debug.get("top_k") if isinstance(debug, Mapping) else []
        current = [item for item in (top_k or []) if isinstance(item, Mapping)]
        current_ids = [str(item["record_id"]) for item in current if item.get("record_id")]
        baseline_ids = _original_top_ids(raw)
        expectations = card.get("expectations") or {}
        expected_any = set(expectations.get("must_recall_any") or [])
        expected_all = set(expectations.get("must_recall_all") or [])
        forbidden = set(expectations.get("must_not_recall") or [])
        current_set = set(current_ids)
        scored = bool(expected_any or expected_all or forbidden)
        passed = (
            (not expected_any or bool(expected_any & current_set))
            and expected_all.issubset(current_set)
            and not (forbidden & current_set)
        )
        verdict = "passed" if scored and passed else "failed" if scored else "unscored"
        replayed_at = now()
        overlap = [record_id for record_id in current_ids if record_id in set(baseline_ids)]
        result = {
            "schema_version": REPLAY_SCHEMA_VERSION,
            "case_id": case_id,
            "replayed_at": replayed_at,
            "status_at_replay": card.get("status"),
            "verdict": verdict,
            "expectations": expectations,
            "baseline_top_record_ids": baseline_ids,
            "current_top_record_ids": current_ids,
            "baseline_overlap_record_ids": overlap,
            "current_top_k": [
                {
                    "position": item.get("position"),
                    "record_id": item.get("record_id"),
                    "source_channel": item.get("source_channel"),
                    "source_kind": item.get("source_kind"),
                    "content_sha256": _sha256_bytes(
                        str(item.get("content_preview") or "").encode("utf-8")
                    ),
                }
                for item in current
            ],
            "recall_mode": response.get("recall_mode"),
            "coverage": response.get("coverage"),
        }
        stamp = re.sub(r"[^0-9]", "", replayed_at)[:20]
        digest = _sha256_bytes(
            f"{case_id}\n{replayed_at}\n{'|'.join(current_ids)}".encode("utf-8")
        )[:10]
        path = self.replays_root / case_id / f"replay-{stamp}-{digest}.json"
        if path.exists():
            raise BadCaseRegistryError("replay event already exists; refusing overwrite")
        _write_json_atomic(path, result)
        result["path"] = str(path)
        return result
