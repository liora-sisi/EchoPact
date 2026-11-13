"""Isolated M22/M23 composition. Only ``brief`` is intended for the assistant.

No provider, database writes, retries or production registration live here.
Full retrieval packets remain in ``audit`` for a private local evaluator.
"""
from __future__ import annotations

import copy

from . import query_caretaker as query_core
from . import evidence_selector as selection
from . import object_recall_shadow as scout
from . import object_evidence_review as review

VERSION = "echo-caretaker-pipeline-shadow-v1"
MAX_BASE_OPERATIONS = 1 + query_core.MAX_EXTRA_QUERIES
MAX_OBJECT_OPERATIONS = scout.MAX_SCANS


class PipelineError(ValueError):
    pass


def _source_coherence(packets):
    """Do not join changing source observations across separate stages.

    A gateway may return a shorter prefix than the object scan. Consistent
    truncation is allowed; a changed prefix, role or source timestamp is not.
    """
    seen = {}
    for packet in packets:
        rows = packet.get("memories")
        if not isinstance(rows, list) or len(rows) > scout.SCAN_LIMIT:
            raise PipelineError("unbounded_source_packet")
        for parent in rows:
            children = parent.get("conversation_context", [])
            if not isinstance(children, list) or len(children) > scout.MAX_CONTEXT:
                raise PipelineError("unbounded_source_context")
            for row in [parent, *children]:
                meta, text = selection._metadata(row), scout._text(row)
                identity = tuple(meta[k] for k in ("source_kind", "conversation_id", "record_id"))
                truncated = row.get("content_truncated") is True
                if identity in seen:
                    old_meta, old_text, old_truncated = seen[identity]
                    if (meta != old_meta or not (text == old_text
                            or truncated and old_text.startswith(text)
                            or old_truncated and text.startswith(old_text))):
                        raise PipelineError("source_changed_across_stages")
                    if len(old_text) > len(text) or len(old_text) == len(text) and not old_truncated:
                        continue
                seen[identity] = (meta, text, truncated)


def run_pipeline(query, *, as_of, limit, runner, planner=None,
                 selector=None, object_runner=None, object_reviewer=None):
    """One plan, then at most one evidence review; opt-in at every boundary.

    ``runner`` must already bind the caller's identity and read-only source.
    Five operations means up to three gateway calls plus two object scans,
    not five SQL statements: the existing gateway may have internal passes.
    """
    counts = {"base_operations": 0, "object_operations": 0}

    def bounded(callback, kind, maximum, expected_limit):
        def run(arguments):
            if (not callable(callback) or set(arguments) != {"query", "limit", "as_of"}
                    or arguments["as_of"] != as_of or type(arguments["limit"]) is not int
                    or arguments["limit"] != expected_limit or counts[kind] >= maximum):
                raise PipelineError("retrieval_operation_not_allowed")
            counts[kind] += 1  # Includes failed attempts, never hidden by fallback.
            return copy.deepcopy(callback(copy.deepcopy(arguments)))
        return run

    base_runner = bounded(runner, "base_operations", MAX_BASE_OPERATIONS, limit)
    try:
        shadow = query_core.run_shadow(query, as_of=as_of, limit=limit, runner=base_runner, planner=planner)
    except Exception:
        raise PipelineError("original_retrieval_unavailable") from None
    result = {"schema_version": VERSION, "status": "original_only", "route": "original",
              "brief": copy.deepcopy(shadow["brief"]), "production_accepted": False,
              "audit": {"query_shadow": shadow}, "trace": {}}

    def finish():
        stage = result["audit"].get("selection", {})
        result["trace"] = {**counts, "retrieval_operations": sum(counts.values()),
            "planner_attempts": shadow["planner_attempts"],
            "evidence_review_attempts": stage.get("selector_attempts", stage.get("review_attempts", 0)),
            "planner_status": shadow["planner_status"], "route": result["route"]}
        return result

    # A failed/disabled planner or a constrained question must not silently
    # fall through into a different paid stage merely because it has a callback.
    if shadow["planner_status"] != "planned":
        return finish()
    result["route"] = "planned_local"
    plan = shadow["plan"]
    wants_objects = plan["intent"] == "comparison" and len(plan["topic_terms"]) == 1
    try:
        packets = [shadow["original"], *shadow["passes"]]
        _source_coherence(packets)
        if wants_objects:
            if object_runner is None or object_reviewer is None:
                result["status"] = "object_route_not_enabled"
                return finish()
            result["route"] = "object"
            scanned = scout.run_object_shadow(shadow, query=query, as_of=as_of,
                runner=bounded(object_runner, "object_operations", MAX_OBJECT_OPERATIONS, scout.SCAN_LIMIT))
            result["audit"]["object_scout"] = scanned
            if scanned["status"] != "prepared_local_only":
                result["status"] = "object_scout_fallback"
                return finish()
            _source_coherence([*packets, *scanned["scans"]])
            if not scanned["prepared"]["local"]:
                result["status"] = "no_object_candidates"
                return finish()
            selected = review.refine_object_shadow(scanned, fallback_brief=shadow["brief"],
                                                   reviewer=object_reviewer)
        else:
            if selector is None:
                result["status"] = "selector_not_enabled"
                return finish()
            result["route"] = "general"
            selected = selection.refine_shadow(shadow, query=query, as_of=as_of, selector=selector)
        result["audit"]["selection"] = selected
        result.update(status=selected["status"], brief=copy.deepcopy(selected["brief"]))
    except Exception:
        result.update(status="source_validation_fallback", brief=copy.deepcopy(shadow["brief"]))
    return finish()


def readonly_pipeline(db_path, agent_id):
    """Bind both paths to the same database/principal, outside model input."""
    from ..mcp.readonly_server import ReadonlyGateway

    # Gateway validates the supplied identity through its read-only connector;
    # no model or per-question argument can supply a different identity/path.
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise PipelineError("fixed_agent_required")
    gateway = ReadonlyGateway(str(db_path), agent_id)
    gateway.validate()
    object_runner = scout.readonly_runner(str(db_path), agent_id)

    def run(query, *, as_of, limit=10, planner=None, selector=None, object_reviewer=None):
        return run_pipeline(query, as_of=as_of, limit=limit, runner=gateway.recall, planner=planner,
                            selector=selector, object_runner=object_runner, object_reviewer=object_reviewer)

    return run
