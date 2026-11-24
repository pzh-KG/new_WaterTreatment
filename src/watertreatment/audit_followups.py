"""Pure structural checks for scoped audit followups, never scientific approval.

These checks only constrain where a reviewer may record additional visible
points. They never clear findings, needs_revision, or potential_omissions.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from pydantic import ValidationError

from .quantitative import COORDINATE_SERIES_KINDS, has_source_numeric_point
from .reading import QuantitativeFollowup


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _nonblank(value):
    return isinstance(value, str) and bool(value.strip())


def _referenced_panel_point(candidate, entry, observations, by_id):
    """A shared endpoint qualifies only with direct value evidence on the exact panel."""
    locator_fields = ("figure","panel","table_index","row_index","cell_index")
    locator = _get(entry,"locator",{})
    wanted = {k:_get(locator,k) for k in locator_fields if _get(locator,k) is not None}
    if not wanted:
        return False
    panel_evidence = {_get(e,"id") for e in _get(candidate,"evidence",[])
                      if all(_get(e,k)==_get(entry,k) for k in ("source_id","block_id"))
                      and all(_get(_get(e,"locator",{}),k)==v for k,v in wanted.items())}
    for link in _get(candidate,"reference_associations",[]):
        if (_get(link,"target_kind") != "series" or _get(link,"target_id") != _get(entry,"id")
                or _get(link,"relationship") != "contextual_comparison" or _get(link,"applicability") != "reported"):
            continue
        shared = panel_evidence.intersection(_get(link,"evidence_ids",[]))
        for oid in _get(link,"observation_ids",[]):
            obs = observations.get(oid)
            owner = by_id.get(_get(obs,"series_id"))
            if (owner and oid in _get(owner,"observation_ids",[]) and has_source_numeric_point(obs)
                    and shared.intersection(_get(_get(obs,"value",{}),"evidence_ids",[]))):
                return True
    return False


def quantitative_followup_issues(candidate, audit, documents) -> list[str]:
    """Check typed scope and existing partial dispositions without I/O.

    A source anchor or a numeric point proves neither semantic support nor that
    all major results are present. Those remain fresh source-audit judgments.
    Model-provided prose is retained, never interpreted as a gating instruction.
    """
    raw_followups = _get(audit, "quantitative_followups", [])
    if raw_followups == []:
        return []  # Preserve legacy audits exactly; no inference from their prose.
    if not isinstance(raw_followups, list):
        return ["QUANTITATIVE_FOLLOWUP_SCHEMA: quantitative_followups must be a list"]
    known = {_get(_get(doc, "source", {}), "source_id"):
             {_get(block, "block_id") for block in _get(doc, "blocks", [])} for doc in documents}
    entries = list(_get(candidate, "quantitative_coverage", []))
    counts = Counter(_get(entry, "id") for entry in entries)
    by_id = {_get(entry, "id"): entry for entry in entries}
    observations = {_get(observation, "id"): observation for observation in _get(candidate, "observations", [])}
    issues, claimed = [], set()
    for index, raw in enumerate(raw_followups):
        prefix = f"quantitative_followups[{index}]"
        try:
            followup = raw if isinstance(raw, QuantitativeFollowup) else QuantitativeFollowup.model_validate(raw)
        except ValidationError:
            issues.append(f"QUANTITATIVE_FOLLOWUP_SCHEMA: {prefix} does not match the typed followup contract")
            continue
        if not _nonblank(followup.recommended_action):
            issues.append(f"QUANTITATIVE_FOLLOWUP_ACTION: {prefix} requires a nonblank action")
        if len(set(followup.affected_query_types)) != len(followup.affected_query_types):
            issues.append(f"QUANTITATIVE_FOLLOWUP_QUERY_DUPLICATE: {prefix}")
        anchors = {(anchor.source_id, anchor.block_id) for anchor in followup.evidence}
        for source_id, block_id in anchors:
            if block_id not in known.get(source_id, set()):
                issues.append(f"QUANTITATIVE_FOLLOWUP_ANCHOR: {prefix} references an unavailable source/block")
        for coverage_id in followup.coverage_ids:
            if not _nonblank(coverage_id) or counts[coverage_id] != 1:
                issues.append(f"QUANTITATIVE_FOLLOWUP_COVERAGE: {prefix} requires one existing unambiguous coverage id: {coverage_id}")
                continue
            if coverage_id in claimed:
                issues.append(f"QUANTITATIVE_FOLLOWUP_DUPLICATE: {prefix} repeats coverage id {coverage_id}")
            claimed.add(coverage_id)
            entry = by_id[coverage_id]
            if _get(entry, "state") != "partial":
                issues.append(f"QUANTITATIVE_FOLLOWUP_NOT_PARTIAL: {prefix} cannot relabel {coverage_id} as a nonblocking followup")
            if (_get(entry, "data_kind") not in COORDINATE_SERIES_KINDS
                    or _get(entry, "scope") == "external_benchmark"):
                issues.append(f"QUANTITATIVE_FOLLOWUP_NOT_POINT_SERIES: {prefix} must concern an in-scope numeric series")
            if (not _nonblank(_get(entry, "reason"))
                    or not any(_nonblank(limit) for limit in _get(entry, "limitations", []))):
                issues.append(f"QUANTITATIVE_FOLLOWUP_UNSCOPED: {prefix} requires the existing partial reason and limitations for {coverage_id}")
            source_pair = (_get(entry, "source_id"), _get(entry, "block_id"))
            if source_pair not in anchors:
                issues.append(f"QUANTITATIVE_FOLLOWUP_SOURCE_MISMATCH: {prefix} lacks an anchor for {coverage_id}")
            identifiers = _get(entry, "observation_ids", [])
            if any(identifier not in observations for identifier in identifiers):
                issues.append(f"QUANTITATIVE_FOLLOWUP_POINT_REFERENCE: {prefix} references a missing observation")
            points = [observations[identifier] for identifier in identifiers if identifier in observations]
            if any(_get(point, "series_id") != coverage_id for point in points):
                issues.append(f"QUANTITATIVE_FOLLOWUP_POINT_OWNER: {prefix} has observations belonging to another series")
            if not any(has_source_numeric_point(point) for point in points) and not _referenced_panel_point(candidate,entry,observations,by_id):
                issues.append(f"QUANTITATIVE_FOLLOWUP_NO_EXISTING_POINTS: {prefix} cannot treat a wholly missing numerical result as additional points")
    return issues
