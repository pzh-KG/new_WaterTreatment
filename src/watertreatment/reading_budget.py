"""Pure, bounded tool-budget planning from frozen candidate size.

Budgets govern work, not scientific completeness. Unknown candidate size reserves
the capped query allowance without pretending a source inventory was measured.
"""
from __future__ import annotations

import copy
import re

from .domain import ExtractionRecord
from .storage import digest_json


POLICY = "candidate-source-pools-v1"
PAGE_SIZE = 20
NAVIGATION_RESERVE = 20
MAX_CANDIDATE_QUERIES = 800
MAX_SOURCE_CALLS = 300
MAX_MODEL_TURNS = 20


def _integer(value, name, minimum, maximum=None):
    if type(value) is not int or value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum if maximum is not None else 'unbounded'}]")
    return value


def validate_call_limits(source_limit, candidate_query_limit):
    """Validate opt-in split limits, returning the hard overall attempt cap."""
    _integer(source_limit, "source_limit", 1, MAX_SOURCE_CALLS)
    _integer(candidate_query_limit, "candidate_query_limit", 1, MAX_CANDIDATE_QUERIES)
    return source_limit + candidate_query_limit


def _candidate_identity(record):
    candidate = record if isinstance(record, ExtractionRecord) else ExtractionRecord.model_validate(record)
    counts = {"paper": 1}
    counts.update({name: len(getattr(candidate, name)) for name in type(candidate).model_fields
                   if isinstance(getattr(candidate, name), list)
                   and (name != "digitization_targets" or candidate.extraction_scope is not None)})
    return counts, digest_json(candidate.model_dump(mode="json"))


def _plan(counts, content_hash, turns, source_limit):
    _integer(turns, "max_model_turns", 1, MAX_MODEL_TURNS)
    _integer(source_limit, "source_limit", 1, MAX_SOURCE_CALLS)
    pages = sum((count + PAGE_SIZE - 1) // PAGE_SIZE for count in counts.values()) if counts is not None else None
    requested = max(160, turns * (pages + NAVIGATION_RESERVE)) if pages is not None else MAX_CANDIDATE_QUERIES
    candidate_limit = min(MAX_CANDIDATE_QUERIES, requested)
    return {"policy": POLICY, "source_limit": source_limit, "candidate_query_limit": candidate_limit,
        "total_attempt_limit": source_limit + candidate_limit, "max_model_turns": turns,
        "page_size": PAGE_SIZE, "per_turn_navigation_reserve": NAVIGATION_RESERVE,
        "candidate_known": counts is not None, "candidate_counts": counts,
        "candidate_content_hash": content_hash, "candidate_pages": pages,
        "candidate_budget_capped": requested > MAX_CANDIDATE_QUERIES,
        "candidate_budget_basis": "frozen_candidate_size" if counts is not None else "maximum_reserve_unknown_candidate"}


def reaudit_source_allowance(documents, image_manifest, time_budget):
    """Size several visits to the frozen originals and keep capacity for later audits."""
    text_ranges = sum((len(doc["blocks"]) + PAGE_SIZE - 1) // PAGE_SIZE for doc in documents)
    views = len(image_manifest)
    per_pass = text_ranges + views
    turns = time_budget["max_model_turns"]
    revisions = time_budget["max_revisions"]
    # Preserve existing small-paper capacity. Re-audits can use extra local tool
    # attempts within the existing cap and the original model-turn deadlines.
    total = min(MAX_SOURCE_CALLS, max(160, turns * per_pass))
    reserved = min(per_pass, (total - 160) // max(1, revisions))
    return {"policy": "reaudit-source-allowance-v1", "text_ranges": text_ranges,
            "visual_targets": views, "source_limit": total, "reserve_per_later_audit": reserved}


def reading_tool_budget(record, *, max_model_turns, source_limit) -> dict:
    """Plan immutable limits; pass None before a first extraction exists."""
    counts, content_hash = (None, None) if record is None else _candidate_identity(record)
    return _plan(counts, content_hash, max_model_turns, source_limit)


def validate_reading_tool_budget(budget, record=None) -> dict:
    """Validate a frozen policy, optionally checking a known candidate identity.

    An unknown-size reserve stays unknown and capped even after extraction; it
    is never silently recalculated using a mutable or later candidate.
    """
    if not isinstance(budget, dict):
        raise ValueError("reading tool budget must be a frozen policy object")
    for key in ("candidate_known", "candidate_budget_capped"):
        if type(budget.get(key)) is not bool:
            raise ValueError(f"{key} must be boolean")
    for key in ("source_limit", "candidate_query_limit", "total_attempt_limit", "max_model_turns",
                "page_size", "per_turn_navigation_reserve"):
        _integer(budget.get(key), key, 1)
    validate_call_limits(budget["source_limit"], budget["candidate_query_limit"])
    counts, content_hash = budget.get("candidate_counts"), budget.get("candidate_content_hash")
    if budget["candidate_known"]:
        if not isinstance(counts, dict) or counts.get("paper") != 1:
            raise ValueError("known candidate counts must include exactly one paper")
        for name, count in counts.items():
            if not isinstance(name, str) or not name:
                raise ValueError("candidate count keys must be field names")
            _integer(count, f"candidate_counts.{name}", 0)
        _integer(budget.get("candidate_pages"), "candidate_pages", 1)
        if not isinstance(content_hash, str) or re.fullmatch(r"[a-f0-9]{64}", content_hash) is None:
            raise ValueError("candidate_content_hash must be a lowercase SHA-256")
    elif counts is not None or content_hash is not None or budget.get("candidate_pages") is not None:
        raise ValueError("unknown candidate reserve cannot claim observed counts or a content hash")
    expected = _plan(copy.deepcopy(counts), content_hash, budget["max_model_turns"], budget["source_limit"])
    if budget != expected:
        raise ValueError("frozen reading tool budget does not match its policy, counts and limits")
    if record is not None and budget["candidate_known"]:
        actual_counts, actual_hash = _candidate_identity(record)
        if actual_counts != counts or actual_hash != content_hash:
            raise ValueError("frozen tool budget candidate identity differs from the supplied record")
    return copy.deepcopy(expected)
