"""Local bridge from ranked records/spans to the existing bounded selector.

No provider, planner, background job or production registration. Whole retrieval
packets remain local. The selector receives temporary IDs and verbatim windows;
the assistant receives either a marked unreviewed preview or checked quotations.
"""
from __future__ import annotations

import copy
from itertools import zip_longest
from pathlib import Path

from ..mcp.readonly_server import ReadonlyGateway
from . import evidence_selector as selection
from . import evidence_packet as codec
from .object_evidence_review import validate_request as validate_ordered_request
from .semantic_shadow import SemanticError, VERSION as INDEX_VERSION, file_digest, canonical_hash

VERSION = "echo-semantic-evidence-shadow-v1"
MAX_RECORDS = 10
MAX_WINDOWS_PER_RECORD = 3


def _clean_content(row):
    text = row.get("content")
    if not isinstance(text, str):
        raise SemanticError("invalid_observed_content")
    return text.removesuffix("\n[Echo Pact: content truncated]") if row.get("content_truncated") is True else text


class WindowBridge:
    def __init__(self, db_path, agent_id, *, source_sha256):
        self.path, self.agent_id, self.source_sha256 = Path(db_path).resolve(strict=True), agent_id, source_sha256
        self.gateway = ReadonlyGateway(str(self.path), agent_id)
        self.gateway.validate()
        self.check_snapshot()

    def check_snapshot(self):
        if (Path(str(self.path) + "-wal").exists() or file_digest(self.path) != self.source_sha256):
            raise SemanticError("window_source_snapshot_changed")
        self.gateway.validate()

    def prepare(self, raw, *, query, topic_terms=()):
        """Same selector limits for both arms; known constraints bypass this path.

        Preserve the existing selector's lexical window (optionally with a
        frozen planner's literal terms). Semantic hits add windows, never rewrite
        raw content or pretend a middle-of-record slice starts at character zero.
        """
        if selection.planner.assess_question(query, raw.get("as_of"))["decision"] != "eligible":
            raise SemanticError("constraints_original_only")
        self.check_snapshot()
        rows = raw.get("memories")
        if not isinstance(rows, list) or len(rows) > MAX_RECORDS:
            raise SemanticError("unbounded_window_records")
        shadow = {"original": raw, "passes": []}
        if topic_terms:
            shadow["plan"] = {"status": "planned", "intent": "topic", "topic_terms": list(topic_terms)}
        lexical = selection.prepare(shadow, query=query)
        lexical_by_id = {r["record_id"]: r for r in lexical["local"].values()}
        if len({selection._metadata(r)["record_id"] for r in rows}) != len(rows):
            raise SemanticError("duplicate_window_record")
        trace = raw.get("semantic_shadow", {})
        semantic = trace.get("selected", {}) if trace.get("status") == "hybrid_candidates_only" else {}
        if semantic and (trace.get("version") != INDEX_VERSION or not isinstance(semantic, dict)
                         or set(semantic) - {r["record_id"] for r in rows}):
            raise SemanticError("invalid_semantic_window_trace")
        groups, anchors = [], {}
        for rank, row in enumerate(rows, 1):
            rid = row["record_id"]
            observed = _clean_content(row)
            head = self.gateway.expand({"record_id": rid, "length": 4000})
            if (selection._metadata(row) != selection._metadata(head["record"])
                    or not head["content"].startswith(observed)
                    or row.get("content_truncated") is not True and head["content_chars"] != len(observed)):
                raise SemanticError("recall_observation_changed")
            anchors[rid] = head
            if rid not in lexical_by_id:
                groups.append([])
                continue
            lex_start = lexical_by_id[rid]["excerpt_start"]
            hit = semantic.get(rid, {})
            spans = hit.get("spans", [])
            if (not isinstance(spans, list) or len(spans) > 2
                    or spans and hit.get("content_sha256") != head["content_sha256"]):
                raise SemanticError("invalid_semantic_window_fingerprint")
            windows = []
            for span in spans:
                if (not isinstance(span, dict) or set(span) != {"start", "end"}
                        or type(span["start"]) is not int or type(span["end"]) is not int
                        or not 0 <= span["start"] < span["end"] <= head["content_chars"]
                        or span["end"] - span["start"] > selection.MAX_EXCERPT_CHARS):
                    raise SemanticError("invalid_semantic_window_offsets")
                spare = selection.MAX_EXCERPT_CHARS - (span["end"] - span["start"])
                start = max(0, span["start"] - min(20, spare))
                windows.append((start, min(start + selection.MAX_EXCERPT_CHARS, head["content_chars"]), "semantic"))
            lexical_window = (lex_start, min(lex_start + selection.MAX_EXCERPT_CHARS, head["content_chars"]), "lexical")
            # One window per ranked record before second/third windows consume slots.
            windows.insert(1 if windows else 0, lexical_window)
            unique = []
            for start, end, origin in windows:
                if start == end or any(a <= start and end <= b for a, b, _ in unique):
                    continue
                unique.append((start, end, origin))
            groups.append([(row, rank, span) for span in unique[:MAX_WINDOWS_PER_RECORD]])
        pool = [item for turn in zip_longest(*groups) for item in turn if item is not None]
        request, local = {"question": query, "candidates": []}, {}
        for row, rank, (start, end, origin) in pool:
            if len(local) == selection.MAX_CANDIDATES:
                break
            head = anchors[row["record_id"]]
            page = self.gateway.expand({"record_id": row["record_id"], "offset": start, "length": end - start,
                                        "expected_content_sha256": head["content_sha256"]})
            if page["end_offset"] != end:
                raise SemanticError("window_would_be_truncated")
            cid = "c" + str(len(local) + 1)
            card = {"candidate_id": cid, "source_kind": row["source_kind"], "role": row["role"],
                    "text": page["content"], "excerpt_incomplete": start > 0 or end < head["content_chars"]}
            if len(selection.encoded({**request, "candidates": [*request["candidates"], card]})) > selection.MAX_INPUT_BYTES:
                break
            request["candidates"].append(card)
            local[cid] = {**selection._metadata(row), "text": page["content"], "excerpt_start": start,
                          "observed_content_sha256": head["content_sha256"], "source_truncated": False,
                          "observed_content_chars": head["content_chars"], "retrieval_rank": rank,
                          "window_origin": origin, "fingerprint_basis": "authoritative_full_record"}
        self.check_snapshot()
        coverage = (raw.get("coverage") or {}).get("coverage_gap")
        prepared = {"request": request, "local": local, "candidate_count": len(pool),
                    "omitted_candidates": len(pool) - len(local),
                    "coverage_gap": coverage if type(coverage) is bool else None,
                    "binding": {"version": VERSION, "agent_id": self.agent_id, "source_sha256": self.source_sha256},
                    "limits": {"candidate_count": selection.MAX_CANDIDATES, "candidate_chars": selection.MAX_EXCERPT_CHARS,
                               "request_bytes": selection.MAX_INPUT_BYTES, "brief_bytes": selection.MAX_BRIEF_BYTES}}
        prepared["prepared_sha256"] = canonical_hash(prepared)
        return prepared

    def verify(self, prepared):
        self.check_snapshot()
        value = copy.deepcopy(prepared)
        fingerprint = value.pop("prepared_sha256", None)
        if (fingerprint != canonical_hash(value) or value.get("binding") != {
                "version": VERSION, "agent_id": self.agent_id, "source_sha256": self.source_sha256}):
            raise SemanticError("window_preparation_changed")
        request, local = prepared["request"], prepared["local"]
        if set(request) != {"question", "candidates"}:
            raise SemanticError("invalid_window_request_fields")
        if local:
            validate_ordered_request({**request, "source_order_windows": []})
        elif request != {"question": request.get("question"), "candidates": []}:
            raise SemanticError("invalid_empty_window_request")
        if (len(local) > selection.MAX_CANDIDATES or len(selection.encoded(request)) > selection.MAX_INPUT_BYTES
                or [r["candidate_id"] for r in request["candidates"]] != list(local)):
            raise SemanticError("invalid_prepared_windows")
        for card in request["candidates"]:
            row = local[card["candidate_id"]]
            if not 1 <= len(row["text"]) <= selection.MAX_EXCERPT_CHARS or row["source_truncated"] is not False:
                raise SemanticError("invalid_prepared_window")
            page = self.gateway.expand({"record_id": row["record_id"], "offset": row["excerpt_start"],
                "length": len(row["text"]), "expected_content_sha256": row["observed_content_sha256"]})
            if (page["content"] != row["text"] or card["text"] != row["text"]
                    or card["source_kind"] != row["source_kind"] or card["role"] != row["role"]
                    or card["excerpt_incomplete"] != (row["excerpt_start"] > 0
                        or row["excerpt_start"] + len(row["text"]) < row["observed_content_chars"])
                    or page["content_chars"] != row["observed_content_chars"]
                    or selection._metadata(page["record"]) != selection._metadata(row)):
                raise SemanticError("window_source_changed")
        self.check_snapshot()

    def preview(self, prepared):
        """No model means explicitly unreviewed source windows, never an answer."""
        self.verify(prepared)
        brief = {"schema_version": VERSION + "-preview", "generation": "unreviewed_verbatim_windows",
                 "evidence_status": "not_assessed", "needs_more_evidence": True,
                 "coverage_gap": prepared["coverage_gap"], "items": [],
                 "candidate_count": prepared["candidate_count"], "omitted_from_preview": 0,
                 "limitations": ["not_an_answer", "adjacent_messages_not_included", "record_time_is_not_event_time"]}
        for row in prepared["local"].values():
            if len(brief["items"]) == selection.MAX_ITEMS:
                break
            ref = {k: row[k] for k in codec.SOURCE_FIELDS}
            ref.update(quote_start=row["excerpt_start"], quote_end=row["excerpt_start"] + len(row["text"]),
                       quote_resolution="verbatim_unreviewed_window")
            candidate = copy.deepcopy(brief)
            candidate["items"].append({"text": row["text"], "relation": "context", "references": [ref]})
            if len(selection.encoded(codec.pack(candidate))) > selection.MAX_BRIEF_BYTES - 128:
                break
            brief = candidate
        brief["omitted_from_preview"] = prepared["candidate_count"] - len(brief["items"])
        return codec.pack(brief)

    def review(self, prepared, *, selector=None):
        """One injected selector call; no network client or retries in this bridge."""
        fallback = self.preview(prepared)
        result = {"schema_version": VERSION, "status": "unreviewed_windows", "selector_attempts": 0,
                  "production_accepted": False, "brief": fallback}
        if selector is None or not prepared["local"]:
            return result
        result["selector_attempts"] = 1
        try:
            draft = selector(copy.deepcopy(prepared["request"]))
            brief = selection.make_brief(draft, prepared)
            brief["limitations"].append("adjacent_messages_not_included")
            packed = codec.pack(brief)
            if len(selection.encoded(packed)) > selection.MAX_BRIEF_BYTES or codec.expand(packed) != brief:
                raise SemanticError("invalid_packed_selection")
            result.update(status="selected_requires_review", brief=packed, judgments=copy.deepcopy(draft))
        except Exception:
            result["status"] = "selector_failed_unreviewed_windows"
        # A model failure may fall back; revoked/changed sources must fail closed.
        self.verify(prepared)
        return result
