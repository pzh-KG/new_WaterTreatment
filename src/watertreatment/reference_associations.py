"""Source-bound references to observations without changing their ownership.

These are structural safeguards, not proof that a common control is applicable.
Never infer these links from prose, equal numbers, formulas or matching labels.
"""
from __future__ import annotations

from .domain import DESIGN_VERSIONS

from collections import Counter

from .domain import ExtractionRecord


def _observation_evidence(observation):
    return (set(observation.evidence_ids) | set(observation.value.evidence_ids)
            | {link.evidence_id for link in observation.evidence_links})


def reference_association_issues(record: ExtractionRecord):
    from .validation import ValidationIssue

    issues = []

    def error(code, path, message):
        issues.append(ValidationIssue(severity="error", code=code, path=path, message=message))

    links = record.reference_associations
    if not links:
        return issues
    if DESIGN_VERSIONS.get(record.rule_version) != record.schema_version:
        error("REFERENCE_SCOPE_VERSION", "reference_associations",
              "Explicit shared references require a supported design scope; legacy archives are not relabeled.")
    targets = {
        "experiment": {item.id: item for item in record.experiments},
        "series": {item.id: item for item in record.quantitative_coverage},
        "comparison": {item.id: item for item in record.comparisons},
    }
    observations = {item.id: item for item in record.observations}
    evidence = {item.id: item for item in record.evidence}
    assignments = {item.observation_id: item for item in
                   (record.design_scope.observation_assignments if record.design_scope else [])}
    identifier_counts = Counter(item.id for item in links)
    uses = Counter((item.target_kind, item.target_id, oid) for item in links for oid in item.observation_ids)
    for i, link in enumerate(links):
        path = f"reference_associations[{i}]"
        if identifier_counts[link.id] != 1:
            error("REFERENCE_DUPLICATE_ID", path + ".id", "Reference association IDs must be unique.")
        if not link.id.strip() or not link.rationale.strip():
            error("REFERENCE_EMPTY_REASON", path, "Use a nonblank ID and a source-specific applicability explanation.")
        for name in ("matched_conditions", "differing_conditions", "unresolved_conditions", "limitations"):
            values = getattr(link, name)
            if any(not value.strip() for value in values) or len(set(values)) != len(values):
                error("REFERENCE_INVALID_CONTEXT", path + "." + name, "Context statements must be nonblank and unique.")
        if (link.applicability in {"inferred", "ambiguous", "conflict"} or link.differing_conditions
                or link.unresolved_conditions) and not link.limitations:
            error("REFERENCE_LIMITATION_REQUIRED", path + ".limitations",
                  "Uncertain, inferred or differing applicability requires explicit comparison limitations.")
        if set(link.matched_conditions) & (set(link.differing_conditions) | set(link.unresolved_conditions)):
            error("REFERENCE_CONDITION_CONFLICT", path,
                  "The same condition cannot be both matched and differing or unresolved.")
        target = targets[link.target_kind].get(link.target_id)
        if target is None:
            error("REFERENCE_TARGET_MISSING", path + ".target_id", "Reference target does not exist in this record.")
        elif link.target_kind == "series":
            if target.state == "out_of_scope" or target.scope == "external_benchmark":
                error("REFERENCE_EXCLUDED_TARGET", path + ".target_id",
                      "An excluded/external source index is not a study comparison receiving shared controls.")
            target_evidence = {eid for eid, e in evidence.items()
                               if (e.source_id, e.block_id) == (target.source_id, target.block_id)}
            if not target_evidence.intersection(link.evidence_ids):
                error("REFERENCE_TARGET_EVIDENCE_REQUIRED", path + ".evidence_ids",
                      "A series reference needs an anchor in its actual source block; a source match is not scientific validation.")
        elif not set(target.evidence_ids).intersection(link.evidence_ids):
            error("REFERENCE_TARGET_EVIDENCE_REQUIRED", path + ".evidence_ids",
                  "The reference needs an evidence anchor also associated with its actual target.")
        if len(set(link.evidence_ids)) != len(link.evidence_ids) or not set(link.evidence_ids) <= set(evidence):
            error("REFERENCE_EVIDENCE_INVALID", path + ".evidence_ids", "Reference evidence must be known, unique IDs.")
        for j, oid in enumerate(link.observation_ids):
            opath = f"{path}.observation_ids[{j}]"
            if uses[(link.target_kind, link.target_id, oid)] != 1:
                error("REFERENCE_DUPLICATE_USE", opath,
                      "One existing observation has one explicit reference relationship per target, not multiple pseudo-replicates.")
            observation = observations.get(oid)
            if observation is None:
                error("REFERENCE_OBSERVATION_MISSING", opath, "A reference cannot invent or copy a measurement.")
                continue
            if not _observation_evidence(observation).intersection(link.evidence_ids):
                error("REFERENCE_OBSERVATION_EVIDENCE_REQUIRED", path + ".evidence_ids",
                      "Preserve a source anchor for each referenced observation as well as the applicability context.")
            assignment = assignments.get(oid)
            if observation.analysis_role == "dense_series_point" or (assignment and assignment.layer == "detail"):
                error("REFERENCE_OPTIONAL_DETAIL", opath,
                      "A result used as an explicit control/reference is consequential, not hidden optional numerical detail.")
            if target is not None and link.target_kind == "series" and oid in target.observation_ids:
                error("REFERENCE_ALREADY_MEMBER", opath,
                      "A primary series member is already available; do not add another reference membership for the same target.")
            if target is not None and link.target_kind == "experiment" and observation.experiment_id == target.id:
                error("REFERENCE_ALREADY_OWNED", opath,
                      "An experiment already owns this observation; a reference must not make it a second measurement.")
    return issues
