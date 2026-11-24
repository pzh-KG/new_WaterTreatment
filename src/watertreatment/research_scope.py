"""Explicit research purpose and graph scope, independent of scientific truth.

An extraction role is a model assertion that still needs source review.  These
checks prevent obvious scope misuse; they do not prove that an interior point
is unimportant.  Existing raw records are never edited by graph selection.
"""
from __future__ import annotations

from .domain import DESIGN_VERSIONS

from .domain import ExtractionRecord


SCOPE_RULE_VERSION = "0.7"
SCOPE_SCHEMA_VERSION = "0.3.0"
SCOPE_VERSIONS = {"0.7": "0.3.0", **DESIGN_VERSIONS}
SCOPES = {"research_core", "series_digitization"}


def scoped_projection(record: ExtractionRecord) -> bool:
    return (record.rule_version in SCOPE_VERSIONS
            and record.schema_version == SCOPE_VERSIONS[record.rule_version]
            and record.extraction_scope in SCOPES)


def protected_observation_ids(record: ExtractionRecord) -> set[str]:
    """Explicit numerical pairing/design evidence must stay queryable as facts."""
    protected = {oid for comparison in record.comparisons for oid in comparison.observation_ids}
    for experiment in record.experiments:
        for pair in experiment.pairs:
            protected.update((pair.left_observation_id, pair.right_observation_id))
    # Typed extensions can carry an important observation-specific interpretation.
    protected.update(extension.observation_id for extension in record.extensions if extension.observation_id)
    return protected


def archived_dense_ids(record: ExtractionRecord) -> set[str]:
    """Return explicitly classified optional points, never heuristic deletions.

    Even diagnostic allow_invalid projection retains structurally suspect
    classifications rather than silently hiding a key comparison.
    """
    if not scoped_projection(record):
        return set()
    series = {entry.id: entry for entry in record.quantitative_coverage}
    protected = protected_observation_ids(record)
    authorized_detail = None
    if record.rule_version in DESIGN_VERSIONS:
        # Diagnostic projection must fail closed even for an invalid candidate:
        # a dense flag alone cannot hide an A/B result or bypass task authority.
        authorized_detail = {assignment.observation_id
            for assignment in (record.design_scope.observation_assignments if record.design_scope else [])
            if assignment.layer == "detail" and assignment.purpose == "requested_detail"
            and record.extraction_scope == "series_digitization"}
    return {observation.id for observation in record.observations
            if observation.analysis_role == "dense_series_point"
            and observation.role_reason and observation.role_reason.strip()
            and observation.id not in protected
            and observation.series_id in series
            and series[observation.series_id].data_kind == "time_series"
            and observation.id in series[observation.series_id].observation_ids
            and (authorized_detail is None or (observation.id in authorized_detail
                 and observation.series_id in record.digitization_targets))}


def scope_issues(record: ExtractionRecord):
    # Local import allows validate_record to call this without a module cycle.
    from .validation import Severity, ValidationIssue
    from .design_scope import design_scope_issues
    issues = design_scope_issues(record)

    def error(code, path, message):
        issues.append(ValidationIssue(severity=Severity.ERROR, code=code, path=path, message=message))

    if record.rule_version not in SCOPE_VERSIONS:
        # Old records may not opt into new selection semantics by adding a flag.
        if (record.extraction_scope is not None or record.digitization_targets
                or any(o.analysis_role != "unclassified" or o.role_reason is not None
                       for o in record.observations)):
            error("SCOPE_VERSION_MISMATCH", "extraction_scope",
                  "Explicit research scope and observation roles require rule 0.7 and schema 0.3.0; legacy records stay unchanged.")
        return issues
    if record.schema_version != SCOPE_VERSIONS[record.rule_version]:
        error("SCOPE_VERSION_MISMATCH", "schema_version", f"Rule {record.rule_version} requires schema {SCOPE_VERSIONS[record.rule_version]}.")
    if record.extraction_scope not in SCOPES:
        error("EXTRACTION_SCOPE_REQUIRED", "extraction_scope", f"Rule {record.rule_version} requires an explicit frozen extraction scope.")
    series = {entry.id: entry for entry in record.quantitative_coverage}
    if record.extraction_scope == "research_core" and record.digitization_targets:
        error("CORE_DIGITIZATION_TARGETS", "digitization_targets",
              "Research-core extraction does not authorize an additional point-digitization task.")
    if record.extraction_scope == "series_digitization" and not record.digitization_targets:
        error("DIGITIZATION_TARGETS_REQUIRED", "digitization_targets",
              "A digitization task must name its target time-series IDs; it is not a whole-paper sweep.")
    if len(set(record.digitization_targets)) != len(record.digitization_targets):
        error("DUPLICATE_DIGITIZATION_TARGET", "digitization_targets", "Digitization targets must be unique.")
    for index, identifier in enumerate(record.digitization_targets):
        if (identifier not in series or series[identifier].data_kind != "time_series"
                or series[identifier].scope == "external_benchmark"
                or series[identifier].state == "out_of_scope"):
            error("INVALID_DIGITIZATION_TARGET", f"digitization_targets[{index}]",
                  "Target must identify an existing in-scope time_series entry from this study, not an external benchmark.")
    protected = protected_observation_ids(record)
    for index, observation in enumerate(record.observations):
        path = f"observations[{index}]"
        if observation.analysis_role == "unclassified":
            error("OBSERVATION_ROLE_REQUIRED", path + ".analysis_role",
                  "Classify the observation's research use; importance=core is not a substitute.")
        if observation.analysis_role != "dense_series_point":
            continue
        if not observation.role_reason or not observation.role_reason.strip():
            error("DENSE_ROLE_REASON_REQUIRED", path + ".role_reason",
                  "Explain why this point is optional dense interior data, not a key result or change/failure event.")
        entry = series.get(observation.series_id)
        if entry is None or entry.data_kind != "time_series" or observation.id not in entry.observation_ids:
            error("DENSE_SERIES_REQUIRED", path + ".series_id",
                  "An optional dense point must belong to a declared time series that lists this observation.")
        if observation.id in protected:
            error("DENSE_KEY_RELATION", path + ".analysis_role",
                  "A point used by a performance pair, design comparison or observation-specific extension must retain a key research role.")
    return issues
