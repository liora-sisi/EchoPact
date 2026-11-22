"""Isolated fixed-principal composition of planning, retrieval and two-step review.

Only ``brief`` is receiver-facing. Full source packets and the bounded window
catalog remain local. No provider, writes, retries or production registration.
"""
from __future__ import annotations

import copy
from itertools import zip_longest
import time

from . import query_caretaker as planning
from . import evidence_selector as selection
from . import evidence_packet as codec
from . import object_recall_shadow as scout
from .caretaker_pipeline import _source_coherence
from .semantic_shortlist import ShortlistBridge, MAX_ROOTS
from .semantic_shadow import HybridGateway, SemanticError, canonical_hash, file_digest

VERSION = "echo-semantic-caretaker-pipeline-shadow-v2"
MAX_CATALOG = 60  # 3 lexical packets + 1 hybrid + 1 object slate, at most 12 each.
LIMITS = {"lexical_operations": 3, "hybrid_operations": 1, "object_operations": 2,
          "planner": 1, "shortlist": 1, "review": 1}


class BudgetExceeded(ValueError):
    pass


def _key(row):
    return row["record_id"], row["excerpt_start"], len(row["text"]), row["observed_content_sha256"]


def _seed(template, rows, *, total, coverage, complete=False):
    result = copy.deepcopy(template)
    result["request"]["candidates"], result["local"] = [], {}
    for row in rows:
        if len(result["local"]) == selection.MAX_CANDIDATES:
            break
        cid = "c" + str(len(result["local"]) + 1)
        card = {"candidate_id": cid, "source_kind": row["source_kind"], "role": row["role"], "text": row["text"],
                "excerpt_incomplete": row["excerpt_start"] > 0
                or row["excerpt_start"] + len(row["text"]) < row["observed_content_chars"]}
        proposed = {**result["request"], "candidates": [*result["request"]["candidates"], card]}
        if len(selection.encoded(proposed)) > selection.MAX_INPUT_BYTES:
            break
        result["request"] = proposed
        result["local"][cid] = copy.deepcopy(row)
    if complete and len(result["local"]) != len(rows):
        raise SemanticError("selected_catalog_windows_exceed_budget")
    result.update(candidate_count=total, omitted_candidates=total - len(result["local"]), coverage_gap=coverage)
    result.pop("prepared_sha256")
    result["prepared_sha256"] = canonical_hash(result)
    return result


def combine_seeds(seeds):
    if not seeds or len(seeds) > 5:
        raise SemanticError("invalid_seed_routes")
    first = seeds[0]
    if any(seed["binding"] != first["binding"] or seed["request"]["question"] != first["request"]["question"]
           or len(seed["local"]) > selection.MAX_CANDIDATES for seed in seeds):
        raise SemanticError("seed_routes_not_same_scope")
    catalog, seen = [], set()
    groups = [list(seed["local"].values()) for seed in seeds]
    for turn in zip_longest(*groups):
        for row in turn:
            if row is not None and _key(row) not in seen:
                seen.add(_key(row))
                catalog.append(copy.deepcopy(row))
    if len(catalog) > MAX_CATALOG:
        raise SemanticError("catalog_budget_exceeded")
    gaps = [seed["coverage_gap"] for seed in seeds]
    coverage = True if any(g is True for g in gaps) else False if all(g is False for g in gaps) else None
    return _seed(first, catalog, total=len(catalog), coverage=coverage), catalog


def restore_selected_windows(focused, catalog):
    records = {row["record_id"] for row in focused["local"].values()}
    while True:
        conflicts = {row.get("conflict_group_id") for row in catalog if row["record_id"] in records} - {None, ""}
        expanded = records | {row["record_id"] for row in catalog if row.get("conflict_group_id") in conflicts}
        if expanded == records:
            break
        records = expanded
    if len(records) > MAX_ROOTS:
        raise SemanticError("catalog_conflict_closure_exceeds_budget")
    rows = [row for row in catalog if row["record_id"] in records]
    return _seed(focused, rows, total=len(catalog), coverage=focused["coverage_gap"], complete=True)


class SemanticCaretaker:
    def __init__(self, db_path, agent_id, index_path, encoder, *, elapsed_limit=120, clock=time.monotonic):
        if type(elapsed_limit) not in (int, float) or not 1 <= elapsed_limit <= 120:
            raise SemanticError("invalid_elapsed_limit")
        self.bridge = ShortlistBridge(db_path, agent_id, source_sha256=file_digest(db_path))
        self.hybrid = HybridGateway(db_path, agent_id, index_path, encoder)
        self.object_runner = scout.readonly_runner(str(db_path), agent_id)
        self.elapsed_limit, self.clock = elapsed_limit, clock

    def _object_seed(self, scanned, template):
        rows = []
        prepared = scanned["prepared"]
        if len(prepared["local"]) > selection.MAX_CANDIDATES:
            raise SemanticError("object_slate_exceeds_budget")
        for row in prepared["local"].values():
            if (type(row["excerpt_start"]) is not int or row["excerpt_start"] < 0
                    or not 1 <= len(row["text"]) <= selection.MAX_EXCERPT_CHARS):
                raise SemanticError("invalid_object_window")
            head = self.bridge.base.gateway.expand({"record_id": row["record_id"], "length": 1})
            page = self.bridge.base.gateway.expand({"record_id": row["record_id"], "offset": row["excerpt_start"],
                "length": len(row["text"]), "expected_content_sha256": head["content_sha256"]})
            if page["content"] != row["text"] or selection._metadata(page["record"]) != selection._metadata(row):
                raise SemanticError("object_window_source_changed")
            rows.append({**selection._metadata(row), "text": row["text"], "excerpt_start": row["excerpt_start"],
                "observed_content_sha256": head["content_sha256"], "source_truncated": False,
                "observed_content_chars": head["content_chars"], "retrieval_rank": row["retrieval_rank"],
                "window_origin": "object_scout", "fingerprint_basis": "authoritative_full_record"})
        result = _seed(template, rows, total=prepared["candidate_count"], coverage=prepared["coverage_gap"], complete=True)
        self.bridge.base.verify(result)
        return result

    def run(self, query, *, as_of, limit=10, planner=None, shortlister=None, reviewer=None):
        planning.validate_question(query)
        self.bridge.base.check_snapshot()
        started = self.clock()
        counts = {name: 0 for name in LIMITS}

        def checkpoint():
            self.bridge.base.check_snapshot()
            if self.clock() - started > self.elapsed_limit:
                raise BudgetExceeded("elapsed_budget_exceeded")

        def call(callback, kind, argument):
            checkpoint()
            if counts[kind] >= LIMITS[kind]:
                raise BudgetExceeded("operation_budget_exceeded")
            counts[kind] += 1
            return callback(copy.deepcopy(argument))

        try:
            shadow = planning.run_shadow(query, as_of=as_of, limit=limit,
                runner=lambda args: call(self.bridge.base.gateway.recall, "lexical_operations", args),
                planner=(lambda question: call(planner, "planner", question)) if planner is not None else None)
        except Exception:
            raise SemanticError("original_retrieval_unavailable") from None
        guard = shadow.get("question_guard")
        original_brief = planning._fallback_brief(shadow["original"], query, limit,
            guard if guard and guard["decision"] != "eligible" else None)
        result = {"schema_version": VERSION, "status": "original_only", "production_accepted": False,
                  "brief": original_brief, "audit": {"query_shadow": shadow}}

        def finish():
            self.bridge.base.check_snapshot()
            elapsed = self.clock() - started
            if elapsed > self.elapsed_limit:
                result.update(status="time_budget_exhausted_original_preserved", brief=original_brief)
            if shadow["planner_status"] == "needs_clue":
                # Planning uncertainty cannot veto an unchanged-question search,
                # but it must remain visible even if the reviewer sounds sure.
                brief = copy.deepcopy(result["brief"])
                brief["planning_status"] = "needs_clue"
                brief["needs_more_evidence"] = True
                brief.setdefault("limitations", []).append("planner_requested_clue_original_question_only")
                if len(selection.encoded(brief)) > selection.MAX_BRIEF_BYTES:
                    brief = copy.deepcopy(original_brief)
                    brief.update(planning_status="needs_clue", needs_more_evidence=True)
                    brief["limitations"].append("planner_requested_clue_original_question_only")
                    result["status"] = "pipeline_output_budget_original_preserved"
                result["brief"] = brief
            result["trace"] = {**counts, "model_calls": sum(counts[k] for k in ("planner", "shortlist", "review")),
                "retrieval_operations": sum(counts[k] for k in ("lexical_operations", "hybrid_operations", "object_operations")),
                "elapsed_seconds": round(elapsed, 3), "elapsed_limit": self.elapsed_limit,
                "planner_status": shadow["planner_status"],
                "hybrid_status": result["audit"].get("hybrid", {}).get("semantic_shadow", {}).get("status", "not_run")}
            result["limits"] = {**LIMITS, "model_calls": 3, "retrieval_operations": 6, "catalog_cards": MAX_CATALOG,
                                "brief_bytes": selection.MAX_BRIEF_BYTES}
            return result

        if shadow["planner_status"] not in {"planned", "needs_clue", "no_rewrite"}:
            return finish()
        try:
            packets = [shadow["original"], *shadow["passes"]]
            _source_coherence(packets)
            checkpoint()
            hybrid = call(self.hybrid.recall, "hybrid_operations", {"query": query, "limit": limit, "as_of": as_of})
            result["audit"]["hybrid"] = hybrid
            _source_coherence([*packets, hybrid])
            terms = shadow["plan"]["topic_terms"]
            lexical = [self.bridge.prepare(packet, query=query, topic_terms=terms) for packet in packets]
            seeds = [self.bridge.prepare(hybrid, query=query, topic_terms=terms)]
            plan = shadow["plan"]
            if plan["intent"] == "comparison" and len(terms) == 1:
                scanned = scout.run_object_shadow(shadow, query=query, as_of=as_of,
                    runner=lambda args: call(self.object_runner, "object_operations", args))
                result["audit"]["object_scout"] = scanned
                if scanned["status"] != "prepared_local_only":
                    result["status"] = "object_scout_failed_original_preserved"
                    return finish()
                _source_coherence([*packets, hybrid, *scanned["scans"]])
                seeds.append(self._object_seed(scanned, lexical[0]))
            seeds.extend(lexical)
            seed, catalog = combine_seeds(seeds)
            self.bridge.base.verify(seed)
            result["audit"]["route_seeds"] = seeds
            result["audit"]["catalog"] = catalog
            checkpoint()
            selected = self.bridge.run(seed,
                shortlister=(lambda request: call(shortlister, "shortlist", request)) if shortlister is not None else None,
                reviewer=(lambda request: call(reviewer, "review", request)) if reviewer is not None else None,
                context_seed_builder=lambda focused: restore_selected_windows(focused, catalog))
            result["audit"]["selection"] = selected
            result.update(status=selected["status"], brief=selected["brief"])
            # Known truncation of an earlier route remains a caveat even if
            # the last review happens to see enough facts for a fluent answer.
            truncated = any(s["omitted_candidates"] for s in seeds) or bool(
                result["audit"].get("object_scout", {}).get("scan_limit_reached")) or bool(
                result["audit"].get("object_scout", {}).get("prepared", {}).get("skipped_groups"))
            if truncated and selected["status"] == "reviewed_requires_acceptance":
                plain = codec.expand(result["brief"])
                plain["needs_more_evidence"] = True
                plain["limitations"].append("earlier_retrieval_route_was_bounded")
                packed = codec.pack(plain)
                if len(selection.encoded(packed)) > selection.MAX_BRIEF_BYTES:
                    result.update(status="pipeline_output_budget_original_preserved", brief=original_brief)
                else:
                    result["brief"] = packed
        except BudgetExceeded:
            result.update(status="operation_budget_exhausted_original_preserved", brief=original_brief)
        except Exception:
            result.update(status="source_validation_original_preserved", brief=original_brief)
        return finish()
