"""Shared scientific controls are references, not duplicate experiments or data."""
import pytest

from watertreatment.core_view import build_native_core_view
from watertreatment.domain import (
    DesignComparison, EvidenceRef, Experiment, ExtractionRecord, Fact,
    Observation, Paper, QuantitativeCoverage, ReferenceAssociation,
    ResearchObservationAssignment, ResearchSourceIndex,
)
from watertreatment.graph_store import _rows
from watertreatment.projection import project_record
from watertreatment.reference_associations import reference_association_issues
from watertreatment.storage import digest_json
from watertreatment.validation import validate_record
from watertreatment.web_graph import GraphQuery, ENTITY_LABELS, NORMALIZATION_INSTRUCTION
from test_design_scope import design_record


def reference_record(target_kind="series"):
    record = design_record()
    record.experiments[0].evidence_ids = ["evidence"]
    record.experiments.append(Experiment(id="variant", material_ids=["material"], purpose="coupled",
                                         evidence_ids=["group-evidence"]))
    record.evidence.append(EvidenceRef(id="group-evidence", source_id="source", block_id="group-page"))
    record.observations.append(Observation(id="variant-endpoint", experiment_id="variant", material_id="material",
        analysis_role="condition_comparison", origin="reported_measurement", metric_kind="treatment_efficiency",
        value=Fact(value=84, status="reported", evidence_ids=["group-evidence"]),
        evidence_ids=["group-evidence"], series_id="condition-series"))
    record.quantitative_coverage.append(QuantitativeCoverage(id="condition-series", source_id="source",
        block_id="group-page", label="Condition screen with separately reported standard reference",
        data_kind="condition_series", state="partial", observation_ids=["variant-endpoint"],
        reason="The standard reference is reused, not independently repeated here."))
    record.comparisons.append(DesignComparison(id="comparison", observation_ids=["variant-endpoint"],
                                               evidence_ids=["group-evidence"]))
    record.design_scope.observation_assignments.append(ResearchObservationAssignment(
        observation_id="variant-endpoint", layer="core", purpose="condition_endpoint", reason="Condition endpoint."))
    record.design_scope.source_index.append(ResearchSourceIndex(id="group-index", source_id="source", block_id="group-page",
        reading_state="read", content_types=["figure"], summary="Condition endpoint and a shared reference are displayed.",
        material_relation="Final material under a changed condition.", reason="Source-specific comparison context.",
        retained_observation_ids=["variant-endpoint"], quantitative_coverage_ids=["condition-series"]))
    record.reference_associations.append(ReferenceAssociation(id="shared-standard", target_kind=target_kind,
        target_id={"series": "condition-series", "experiment": "variant", "comparison": "comparison"}[target_kind],
        observation_ids=["endpoint"], relationship="shared_control", applicability="inferred",
        evidence_ids=["evidence", "group-evidence"], rationale="The panel reuses the reference already reported in the standard group.",
        matched_conditions=["nominal protocol"], unresolved_conditions=["independent physical run identity"],
        limitations=["Reference reuse does not establish the same physical run or independent replication."]))
    return record


def errors(record):
    return {issue.code for issue in validate_record(record) if issue.severity == "error"}


@pytest.mark.parametrize("target_kind", ["series", "experiment", "comparison"])
def test_shared_reference_preserves_primary_ownership_and_values(target_kind):
    record = reference_record(target_kind)
    before = record.model_dump(mode="json")
    assert not errors(record)
    graph = project_record(record)
    assert record.model_dump(mode="json") == before
    observation_nodes = [node for node in graph.nodes if node.label == "Observation"]
    assert len(observation_nodes) == 2
    reference = next(node for node in graph.nodes if node.label == "ReferenceAssociation")
    assert reference.properties["applicability"] == "inferred"
    assert reference.properties["independent_replicates_added"] == 0
    assert reference.properties["physical_run_identity_inferred"] is False
    assert reference.properties["automatic_pairing"] is False
    by_id = {node.id: node for node in graph.nodes}
    original = next(node for node in observation_nodes if node.properties["source_observation_id"] == "endpoint")
    owning = [edge for edge in graph.edges if edge.relationship == "REPORTS" and edge.target == original.id]
    assert len(owning) == 1
    assert by_id[owning[0].source].properties["source_experiment_id"] == "experiment"
    memberships = [edge for edge in graph.edges if edge.relationship == "IN_SERIES" and edge.source == original.id]
    assert len(memberships) == 1
    assert not any(edge.relationship == "PAIRED_WITH" for edge in graph.edges)
    refs = [edge for edge in graph.edges if edge.relationship == "REFERENCES_OBSERVATION"]
    assert len(refs) == 1 and refs[0].target == original.id
    assert refs[0].properties == {"ownership_changed": False, "independent_replicate": False}
    nodes, edges = _rows(graph, "offline-snapshot")  # Whitelist only; no database is used.
    assert "ReferenceAssociation" in nodes and "USES_REFERENCE" in edges and "REFERENCES_OBSERVATION" in edges


def test_reference_series_does_not_fake_point_counts_or_add_same_owner_membership():
    record = reference_record()
    record.quantitative_coverage[1].state = "complete"
    record.quantitative_coverage[1].expected_points = 2
    # A reference is not another extracted point or an independent measurement.
    assert "SERIES_COMPLETENESS_CONFLICT" in errors(record)
    record.quantitative_coverage[1].state = "partial"
    record.quantitative_coverage[1].observation_ids.append("endpoint")
    assert {"SERIES_OWNER_MISMATCH", "REFERENCE_ALREADY_MEMBER"} <= errors(record)


@pytest.mark.parametrize("change,code", [
    (lambda r: setattr(r.reference_associations[0], "target_id", "missing"), "REFERENCE_TARGET_MISSING"),
    (lambda r: r.reference_associations[0].observation_ids.append("missing"), "REFERENCE_OBSERVATION_MISSING"),
    (lambda r: r.reference_associations[0].evidence_ids.remove("group-evidence"), "REFERENCE_TARGET_EVIDENCE_REQUIRED"),
    (lambda r: r.reference_associations[0].evidence_ids.remove("evidence"), "REFERENCE_OBSERVATION_EVIDENCE_REQUIRED"),
    (lambda r: r.reference_associations[0].evidence_ids.append("unknown"), "REFERENCE_EVIDENCE_INVALID"),
    (lambda r: r.reference_associations[0].limitations.clear(), "REFERENCE_LIMITATION_REQUIRED"),
    (lambda r: r.reference_associations[0].matched_conditions.append("independent physical run identity"), "REFERENCE_CONDITION_CONFLICT"),
    (lambda r: setattr(r.reference_associations[0], "rationale", "  "), "REFERENCE_EMPTY_REASON"),
    (lambda r: r.reference_associations[0].observation_ids.append("endpoint"), "REFERENCE_DUPLICATE_USE"),
    (lambda r: r.reference_associations.append(r.reference_associations[0].model_copy(deep=True)), "REFERENCE_DUPLICATE_ID"),
    (lambda r: setattr(r.observations[0], "analysis_role", "dense_series_point"), "REFERENCE_OPTIONAL_DETAIL"),
    (lambda r: setattr(r.quantitative_coverage[1], "state", "out_of_scope"), "REFERENCE_EXCLUDED_TARGET"),
    (lambda r: setattr(r, "rule_version", "0.7"), "REFERENCE_SCOPE_VERSION"),
])
def test_invalid_or_unsupported_reference_cannot_be_projected(change, code):
    record = reference_record()
    change(record)
    assert code in errors(record)
    with pytest.raises(ValueError, match="failed validation"):
        project_record(record)
    diagnostic = project_record(record, allow_invalid=True)
    assert not any(node.label == "ReferenceAssociation" for node in diagnostic.nodes)


def test_multiple_targets_reuse_the_one_original_observation_without_duplicate_values():
    record = reference_record()
    reference = record.reference_associations[0].model_copy(deep=True)
    reference.id, reference.target_kind, reference.target_id = "experiment-reference", "experiment", "variant"
    record.reference_associations.append(reference)
    assert not reference_association_issues(record)
    graph = project_record(record)
    assert len([node for node in graph.nodes if node.label == "Observation"]) == 2
    assert len([edge for edge in graph.edges if edge.relationship == "REFERENCES_OBSERVATION"]) == 2
    assert len([edge for edge in graph.edges if edge.relationship == "REPORTS"]) == 2


def test_reference_can_qualify_existing_comparison_evidence_without_erasing_it():
    record = reference_record("comparison")
    # Comparison evidence is not primary ownership. An explicit reference can
    # explain its role without forcing removal of an existing dependency.
    record.comparisons[0].observation_ids.append("endpoint")
    assert not errors(record)
    graph = project_record(record)
    assert len([node for node in graph.nodes if node.label == "Observation"]) == 2
    assert len([edge for edge in graph.edges if edge.relationship == "USES_EVIDENCE"]) == 2
    assert len([edge for edge in graph.edges if edge.relationship == "REFERENCES_OBSERVATION"]) == 1
    assert len([edge for edge in graph.edges if edge.relationship == "REPORTS"]) == 2


def test_core_view_exposes_reference_separately_and_does_not_augment_primary_series():
    record = reference_record()
    before = record.model_dump(mode="json")
    view = build_native_core_view(record, source_sha256="a" * 64)
    assert record.model_dump(mode="json") == before
    assert view["counts"]["source_observations"] == view["counts"]["retained_observations"] == 2
    series = next(row for row in view["series"] if row["id"] == "condition-series")
    assert series["observation_ids"] == ["variant-endpoint"]
    assert series["referenced_observation_ids"] == ["endpoint"]
    ref = series["reference_entry_refs"][0]
    assert view["observations"][int(ref["view_pointer"].split("/")[-1])]["id"] == "endpoint"
    assert view["reference_associations"] == before["reference_associations"]


def test_reference_cannot_be_hidden_by_native_scope_mapping():
    record = reference_record()
    record.design_scope.observation_assignments[0].layer = "detail"
    assert "REFERENCE_OPTIONAL_DETAIL" in {issue.code for issue in reference_association_issues(record)}
    with pytest.raises(ValueError):
        build_native_core_view(record, source_sha256="a" * 64)


@pytest.mark.parametrize("version,rule", [("0.1.0", "0.5"), ("0.2.0", "0.6"), ("0.3.0", "0.7"), ("0.4.0", "0.8")])
def test_absent_reference_extension_preserves_old_serialization(version, rule):
    record = ExtractionRecord(schema_version=version, rule_version=rule, paper=Paper(id="archive"))
    old = record.model_dump(mode="json")
    assert "reference_associations" not in old
    restored = ExtractionRecord.model_validate(old)
    assert restored.model_dump(mode="json") == old
    assert digest_json(restored.model_dump(mode="json")) == digest_json(old)
    assert not reference_association_issues(restored)


def test_graph_reference_navigation_is_fixed_and_explicit_about_reuse():
    assert GraphQuery(entity="reference", mode="relationships").entity == "reference"
    assert ENTITY_LABELS["reference"] == "ReferenceAssociation"
    assert "independent replication" in NORMALIZATION_INSTRUCTION
    assert "Primary REPORTS/IN_SERIES ownership is unchanged" in NORMALIZATION_INSTRUCTION
    with pytest.raises(ValueError):
        GraphQuery(entity="ReferenceAssociation MATCH arbitrary")
