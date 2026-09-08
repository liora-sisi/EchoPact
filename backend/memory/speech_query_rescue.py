"""Bounded speech-to-text query rescue for read-only recall.

This module owns no vocabulary and never rewrites stored records.  A caller
may inject a small, rebuildable overlay derived from its own speech-to-text
logs.  The overlay is consulted only when the first recall pass is empty or
does not support the observed term, and every candidate remains visible in
the response trace.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any, Dict, List, Mapping, MutableMapping, Sequence, Tuple


SPEECH_QUERY_RESCUE_SCHEMA_VERSION = "echo-pact-speech-query-rescue-v1"
SPEECH_QUERY_RESCUE_MATCH_SCHEMA_VERSION = (
    "echo-pact-speech-query-rescue-match-v1"
)
MAX_SPEECH_QUERY_MAPPINGS = 64
MAX_SPEECH_QUERY_CANDIDATES = 2


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def _required_term(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = _normalize(value)
    if len(normalized) < 2:
        raise ValueError(f"{field} must contain at least two characters")
    return normalized


@dataclass(frozen=True)
class SpeechQueryMapping:
    """One observed speech-to-text form and its intended retrieval form."""

    observed: str
    intended: str

    def __post_init__(self) -> None:
        observed = _required_term(self.observed, "observed")
        intended = _required_term(self.intended, "intended")
        if observed.casefold() == intended.casefold():
            raise ValueError("observed and intended must differ")
        object.__setattr__(self, "observed", observed)
        object.__setattr__(self, "intended", intended)


@dataclass(frozen=True)
class SpeechQueryRescuePolicy:
    """Caller-supplied, non-persistent policy for bounded rescue passes."""

    mappings: Tuple[SpeechQueryMapping, ...]
    protected_terms: Tuple[str, ...] = ()
    max_candidates: int = MAX_SPEECH_QUERY_CANDIDATES

    def __post_init__(self) -> None:
        mappings = tuple(self.mappings)
        if not mappings:
            raise ValueError("mappings must not be empty")
        if len(mappings) > MAX_SPEECH_QUERY_MAPPINGS:
            raise ValueError(
                f"mappings must not exceed {MAX_SPEECH_QUERY_MAPPINGS} entries"
            )
        observed_terms: set[str] = set()
        for item in mappings:
            if not isinstance(item, SpeechQueryMapping):
                raise ValueError("mappings must contain SpeechQueryMapping values")
            key = item.observed.casefold()
            if key in observed_terms:
                raise ValueError("observed terms must be unique")
            observed_terms.add(key)
        protected_terms = tuple(
            dict.fromkeys(
                _required_term(term, "protected_term")
                for term in self.protected_terms
            )
        )
        if (
            isinstance(self.max_candidates, bool)
            or not isinstance(self.max_candidates, int)
            or not 1 <= self.max_candidates <= MAX_SPEECH_QUERY_CANDIDATES
        ):
            raise ValueError(
                "max_candidates must be between 1 and "
                f"{MAX_SPEECH_QUERY_CANDIDATES}"
            )
        object.__setattr__(self, "mappings", mappings)
        object.__setattr__(self, "protected_terms", protected_terms)


def _term_spans(text: str, term: str) -> List[tuple[int, int]]:
    folded_text = text.casefold()
    folded_term = term.casefold()
    spans: List[tuple[int, int]] = []
    start = 0
    while True:
        index = folded_text.find(folded_term, start)
        if index < 0:
            return spans
        spans.append((index, index + len(folded_term)))
        start = index + len(folded_term)


def _overlaps(
    left: tuple[int, int], right: tuple[int, int]
) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _supported_by_initial_evidence(
    observed: str, memories: Sequence[Mapping[str, Any]]
) -> bool:
    folded = observed.casefold()
    return any(
        folded in _normalize(str(memory.get("content") or "")).casefold()
        for memory in memories
    )


def _base_trace(query: str) -> Dict[str, Any]:
    return {
        "schema_version": SPEECH_QUERY_RESCUE_SCHEMA_VERSION,
        "enabled": True,
        "triggered": False,
        "trigger_reason": None,
        "original_query": query,
        "protected_term_matches": [],
        "candidates": [],
        "passes": [],
        "selected_hits": [],
        "safety": (
            "caller-supplied retrieval overlay only; original query retained; "
            "no stored evidence, projection, claim, or database mutation"
        ),
    }


def plan_speech_query_rescue(
    query: str,
    initial_result: Mapping[str, Any],
    policy: SpeechQueryRescuePolicy,
) -> tuple[List[tuple[str, str]], Dict[str, Any]]:
    """Return bounded candidate passes plus an audit trace.

    A non-empty first pass does not automatically block rescue: if none of its
    evidence contains the observed form, the hits are treated as unanchored
    for this narrow purpose.  Conversely, an exact observed term in evidence
    prevents correction even when the result set is small.
    """

    normalized_query = _normalize(query)
    trace = _base_trace(query)
    protected_spans: List[tuple[str, tuple[int, int]]] = []
    for term in policy.protected_terms:
        for span in _term_spans(normalized_query, term):
            protected_spans.append((term, span))
    trace["protected_term_matches"] = list(
        dict.fromkeys(term for term, _ in protected_spans)
    )

    memories = [
        memory
        for memory in initial_result.get("memories") or []
        if isinstance(memory, Mapping)
    ]
    eligible: List[SpeechQueryMapping] = []
    blocked_by_protection = False
    observed_supported_by_evidence = False
    intended_supported_by_evidence = False
    for mapping in policy.mappings:
        mapping_spans = _term_spans(normalized_query, mapping.observed)
        if not mapping_spans:
            continue
        if any(
            _overlaps(mapping_span, protected_span)
            for mapping_span in mapping_spans
            for _, protected_span in protected_spans
        ):
            blocked_by_protection = True
            continue
        if _supported_by_initial_evidence(mapping.observed, memories):
            observed_supported_by_evidence = True
            continue
        if _supported_by_initial_evidence(mapping.intended, memories):
            intended_supported_by_evidence = True
            continue
        eligible.append(mapping)

    if not eligible:
        if blocked_by_protection:
            trace["trigger_reason"] = "protected_term_match"
        elif observed_supported_by_evidence:
            trace["trigger_reason"] = "initial_evidence_supports_observed_term"
        elif intended_supported_by_evidence:
            trace["trigger_reason"] = "initial_evidence_supports_intended_term"
        else:
            trace["trigger_reason"] = "no_candidate_mapping"
        return [], trace

    trace["triggered"] = True
    trace["trigger_reason"] = (
        "no_results" if not memories else "weak_unanchored_results"
    )
    planned: List[tuple[str, str]] = []
    for index, mapping in enumerate(
        eligible[: policy.max_candidates], start=1
    ):
        candidate_query = re.sub(
            re.escape(mapping.observed),
            lambda _: mapping.intended,
            normalized_query,
            count=1,
            flags=re.IGNORECASE,
        )
        pass_name = f"speech_query_rescue_{index}"
        planned.append((pass_name, candidate_query))
        trace["candidates"].append(
            {
                "pass": pass_name,
                "observed": mapping.observed,
                "intended": mapping.intended,
                "candidate_query": candidate_query,
            }
        )
    return planned, trace


def complete_speech_query_rescue_trace(
    trace: Dict[str, Any],
    pass_results: Sequence[tuple[str, Mapping[str, Any]]],
    selected_memories: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Attach executed pass counts and selected record identifiers."""

    completed = dict(trace)
    rescue_results = {
        name: response
        for name, response in pass_results
        if name.startswith("speech_query_rescue_")
    }
    completed["passes"] = [
        {
            "pass": name,
            "result_count": len(response.get("memories") or []),
            "record_ids": [
                str(memory.get("record_id"))
                for memory in response.get("memories") or []
                if memory.get("record_id")
            ],
        }
        for name, response in rescue_results.items()
    ]
    selected_hits: List[Dict[str, str]] = []
    for memory in selected_memories:
        record_id = str(memory.get("record_id") or "")
        for pass_name in memory.get("adaptive_match_passes") or []:
            if record_id and pass_name in rescue_results:
                selected_hits.append(
                    {"record_id": record_id, "pass": str(pass_name)}
                )
    completed["selected_hits"] = selected_hits
    return completed


def annotate_speech_query_rescue_matches(
    trace: Mapping[str, Any],
    selected_memories: Sequence[MutableMapping[str, Any]],
) -> None:
    """Label selected evidence that entered through a rescue candidate."""

    candidates = {
        str(candidate.get("pass")): candidate
        for candidate in trace.get("candidates") or []
        if isinstance(candidate, Mapping) and candidate.get("pass")
    }
    for memory in selected_memories:
        matches = []
        for pass_name in memory.get("adaptive_match_passes") or []:
            candidate = candidates.get(str(pass_name))
            if candidate is None:
                continue
            matches.append(
                {
                    "pass": str(pass_name),
                    "observed": candidate.get("observed"),
                    "intended": candidate.get("intended"),
                    "candidate_query": candidate.get("candidate_query"),
                }
            )
        if matches:
            memory["speech_query_rescue_match"] = {
                "schema_version": SPEECH_QUERY_RESCUE_MATCH_SCHEMA_VERSION,
                "status": "candidate_query_hit",
                "matches": matches,
                "evidence_note": (
                    "retrieval used a labelled candidate query; stored "
                    "evidence content remains unchanged"
                ),
            }
