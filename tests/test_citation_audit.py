"""Synthetic scope/provenance checks; no provider, Neo4j or scientific gold."""
from __future__ import annotations

import copy

import pytest

from watertreatment.citation_audit import build_citation_audit_scope, validate_citation_audit
from watertreatment.domain import (
    DesignComparison, EvidenceRef, Experiment, ExtractionRecord, Fact, MaterialSample,
    NamedFact, Observation, ObservationPair, Paper, QuantitativeCoverage,
)
from watertreatment.reading import AUDIT_DIMENSIONS, AuditFinding, QuantitativeFollowup, ReadingAudit, SourceAnchor
from watertreatment.storage import digest_json


def fixture():
    old = ExtractionRecord(schema_version="0.2.0", rule_version="0.6",
        paper=Paper(id="synthetic", source_ids=["main"]),
        materials=[MaterialSample(id="material"), MaterialSample(id="unrelated")],
        experiments=[Experiment(id="group", material_ids=["material"]), Experiment(id="other")],
        observations=[
            Observation(id="first", experiment_id="group", material_id="material",
                value=Fact(value=1.2, status="reported", evidence_ids=["printed"])),
            Observation(id="sibling", experiment_id="group", material_id="material",
                value=Fact(value=0.7, status="reported", evidence_ids=["printed"])),
            Observation(id="separate", experiment_id="other",
                value=Fact(value=2.0, status="reported", evidence_ids=["outside"]))],
        evidence=[EvidenceRef(id="printed", source_id="main", block_id="p1"),
                  EvidenceRef(id="method", source_id="main", block_id="p2"),
                  EvidenceRef(id="outside", source_id="main", block_id="p3")])
    baseline = ReadingAudit(
        findings=[AuditFinding(record_path="observations[0].value.evidence_ids", severity="minor",
            verdict="supported", needs_revision=True, explanation="Add the exact supporting source.",
            evidence=[SourceAnchor(source_id="main", block_id="p2")])],
        potential_omissions=[], coverage_limitations=["Retained source resolution limitation."],
        checked_dimensions=list(AUDIT_DIMENSIONS))
    new = old.model_copy(deep=True)
    new.observations[0].value.evidence_ids.append("method")
    return old, new, baseline


def build(old, new, baseline, **kwargs):
    return build_citation_audit_scope(old, new, baseline, baseline_origin="fresh_model_audit", **kwargs)


def report(baseline, path="observations[0].value"):
    audit = baseline.model_copy(deep=True)
    audit.findings[0] = AuditFinding(record_path=path, severity="minor", verdict="supported",
        needs_revision=False, explanation="The changed citation supports this fact.",
        evidence=[SourceAnchor(source_id="main", block_id="p2")])
    return audit


def test_one_citation_builds_complete_bounded_context_and_does_not_mutate_inputs():
    old, new, baseline = fixture()
    before = old.model_dump(), new.model_dump(), baseline.model_dump()
    scope = build(old, new, baseline)
    assert scope["changed_paths"] == ["observations[0].value.evidence_ids"]
    assert scope["changes"][0]["changed_evidence_ids"] == ["method"]
    refs = {item["record_path"]: item["role"] for item in scope["entity_refs"]}
    assert refs == {"materials[0]": "context", "experiments[0]": "affected",
                    "observations[0]": "affected", "observations[1]": "affected"}
    assert scope["old_record_hash"] == digest_json(old.model_dump(mode="json"))
    assert scope["new_record_hash"] == digest_json(new.model_dump(mode="json"))
    assert scope["incremental_depth"] == 1
    assert validate_citation_audit(scope, baseline, report(baseline)) == []
    assert (old.model_dump(), new.model_dump(), baseline.model_dump()) == before


def test_mapping_inputs_have_identical_scope_without_mutation():
    old, new, baseline = fixture()
    inputs = [x.model_dump(mode="json") for x in (old, new, baseline)]
    before = copy.deepcopy(inputs)
    assert build(*inputs) == build(old, new, baseline)
    assert inputs == before


@pytest.mark.parametrize("mutation", [
    lambda r: setattr(r.observations[0].value, "value", 1.3),
    lambda r: setattr(r.observations[0].value, "status", "estimated"),
    lambda r: setattr(r.observations[0].value, "raw", "new raw text"),
    lambda r: setattr(r.observations[0].value, "note", "new interpretation"),
    lambda r: setattr(r.observations[0], "experiment_id", "other"),
    lambda r: setattr(r.observations[0], "id", "renamed"),
    lambda r: setattr(r.experiments[0].time_window.end, "value", 60),
    lambda r: setattr(r.paper, "id", "changed-paper"),
    lambda r: setattr(r, "rule_version", "0.7"),
    lambda r: setattr(r.evidence[0], "block_id", "changed-source"),
    lambda r: r.observations.reverse(),
    lambda r: r.observations.pop(),
    lambda r: r.experiments[0].evidence_ids.append("method"),
    lambda r: r.observations[0].evidence_ids.append("method"),
])
def test_any_non_fact_citation_or_scientific_change_requires_full_audit(mutation):
    old, new, baseline = fixture()
    mutation(new)
    assert build(old, new, baseline) is None


def test_booleans_do_not_compare_equal_to_numeric_facts():
    old, new, baseline = fixture()
    old.observations[0].value.value = 1
    new.observations[0].value.value = True
    assert build(old, new, baseline) is None


def test_coverage_and_membership_changes_require_full_review():
    old, new, baseline = fixture()
    curve = QuantitativeCoverage(id="curve", source_id="main", block_id="p1", label="curve",
        data_kind="time_series", state="partial", observation_ids=["first", "sibling"])
    old.quantitative_coverage = [curve]
    new.quantitative_coverage = [curve.model_copy(deep=True)]
    new.quantitative_coverage[0].state = "complete"
    assert build(old, new, baseline) is None


@pytest.mark.parametrize("origin", ["incremental_model_audit", "citation_only_audit", "reused_repair_guidance", None, ""])
def test_no_chained_or_unauthenticated_baseline(origin):
    old, new, baseline = fixture()
    assert build_citation_audit_scope(old, new, baseline, baseline_origin=origin) is None


@pytest.mark.parametrize("scope,scope_hash", [("citation_delta", None), ("citation_delta", "a" * 64),
                                            ("full", "a" * 64)])
def test_baseline_metadata_cannot_disguise_a_delta_as_fresh(scope, scope_hash):
    old, new, baseline = fixture()
    baseline.review_scope = scope
    baseline.review_scope_sha256 = scope_hash
    assert build(old, new, baseline) is None


@pytest.mark.parametrize("defect", ["missing_dimension", "omission", "not_checked", "unscoped_blocker"])
def test_baseline_must_be_complete_and_all_blockers_local(defect):
    old, new, baseline = fixture()
    if defect == "missing_dimension":
        baseline.checked_dimensions.pop()
    elif defect == "omission":
        baseline.potential_omissions.append("Missing important outcome.")
    elif defect == "not_checked":
        baseline.findings[0].verdict = "not_checked"
    else:
        baseline.findings[0].record_path = "observations[2].value"
    assert build(old, new, baseline) is None


def test_modified_evidence_references_expand_to_other_experiments():
    old, new, baseline = fixture()
    for record in (old, new):
        record.observations[2].unit.evidence_ids = ["method"]
    scope = build(old, new, baseline)
    paths = {x["record_path"] for x in scope["entity_refs"]}
    assert {"observations[2]", "experiments[1]"}.issubset(paths)


def test_pairs_series_and_design_comparisons_are_in_dependency_closure():
    old, new, baseline = fixture()
    for record in (old, new):
        record.experiments[0].pairs = [ObservationPair(left_observation_id="first", right_observation_id="separate")]
        record.observations[0].series_id = "curve"
        record.quantitative_coverage = [QuantitativeCoverage(id="curve", source_id="main", block_id="p1",
            label="synthetic curve", data_kind="time_series", state="partial", observation_ids=["first", "sibling"])]
        record.comparisons = [DesignComparison(id="comparison", observation_ids=["sibling", "separate"])]
    paths = {x["record_path"] for x in build(old, new, baseline)["entity_refs"]}
    assert {"comparisons[0]", "quantitative_coverage[0]", "observations[2]", "experiments[1]"}.issubset(paths)


def test_changed_material_citation_expands_to_its_experiments():
    old, _, baseline = fixture()
    old.materials[0].name = Fact(value="sample", status="reported", evidence_ids=["printed"])
    new = old.model_copy(deep=True)
    new.materials[0].name.evidence_ids.append("method")
    baseline.findings[0].record_path = "materials[0].name"
    scope = build(old, new, baseline)
    paths = {x["record_path"] for x in scope["entity_refs"]}
    assert {"materials[0]", "experiments[0]", "observations[0]", "observations[1]"}.issubset(paths)


def test_shared_unchanged_material_context_does_not_create_global_review_scope():
    old, new, baseline = fixture()
    for record in (old, new):
        record.experiments[1].material_ids = ["material"]
        record.observations[2].material_id = "material"
    paths = {x["record_path"] for x in build(old, new, baseline)["entity_refs"]}
    assert "materials[0]" in paths
    assert "experiments[1]" not in paths and "observations[2]" not in paths


def test_comparison_fact_citation_change_is_eligible_without_identity_change():
    old, _, baseline = fixture()
    old.comparisons = [DesignComparison(id="comparison", observation_ids=["first"],
        changed_conditions=[NamedFact(name="matrix", fact=Fact(value="water", status="reported", evidence_ids=["printed"]))])]
    new = old.model_copy(deep=True)
    new.comparisons[0].changed_conditions[0].fact.evidence_ids.append("method")
    baseline.findings[0].record_path = "comparisons[0].changed_conditions[0].fact.evidence_ids"
    scope = build(old, new, baseline)
    assert scope and validate_citation_audit(scope, baseline, report(baseline, "comparisons[0]")) == []


@pytest.mark.parametrize("options", [{"max_entities": 3}, {"max_entities": True}, {"max_entities": 501},
    {"max_changes": 0}, {"max_changes": True}])
def test_invalid_or_exceeded_bounds_fall_back_to_full_audit(options):
    old, new, baseline = fixture()
    assert build(old, new, baseline, **options) is None


def test_unknown_or_duplicate_evidence_and_no_actual_changes_fail_closed():
    old, new, baseline = fixture()
    new.observations[0].value.evidence_ids.append("unknown")
    assert build(old, new, baseline) is None
    new.observations[0].value.evidence_ids = ["printed", "method", "method"]
    assert build(old, new, baseline) is None
    assert build(old, old, baseline) is None


def test_more_fact_changes_than_budget_or_duplicate_entity_ids_fail_closed():
    old, new, baseline = fixture()
    new.observations[0].unit.evidence_ids.append("method")
    assert build(old, new, baseline, max_changes=1) is None
    old, new, baseline = fixture()
    for record in (old, new):
        record.experiments[1].id = "group"
    assert build(old, new, baseline) is None


@pytest.mark.parametrize("path", ["observations[0]", "observations[0].value", "observations[0].value.evidence_ids"])
def test_fact_entity_or_exact_citation_finding_is_acceptable(path):
    old, new, baseline = fixture()
    assert validate_citation_audit(build(old, new, baseline), baseline, report(baseline, path)) == []


@pytest.mark.parametrize("path", ["observations", "observations[1].value", "observations[0].value.raw", "unknown"])
def test_generic_or_unrelated_findings_cannot_cover_changed_fact(path):
    old, new, baseline = fixture()
    assert validate_citation_audit(build(old, new, baseline), baseline, report(baseline, path))


def test_missing_or_wrong_source_anchor_cannot_cover_new_citation():
    old, new, baseline = fixture()
    current = report(baseline)
    current.findings[0].evidence = [SourceAnchor(source_id="main", block_id="p1")]
    assert validate_citation_audit(build(old, new, baseline), baseline, current)


def test_unaffected_unresolved_finding_and_all_limits_must_be_preserved():
    old, new, baseline = fixture()
    unresolved = AuditFinding(record_path="materials[1].name", severity="minor", verdict="unresolved",
        needs_revision=False, explanation="Source ambiguity remains.", evidence=[SourceAnchor(source_id="main", block_id="p3")])
    baseline.findings.append(unresolved)
    scope = build(old, new, baseline)
    current = report(baseline)
    assert validate_citation_audit(scope, baseline, current) == []
    current.findings.pop()
    assert any("Unchanged unresolved" in item for item in validate_citation_audit(scope, baseline, current))
    current = report(baseline)
    current.coverage_limitations.clear()
    assert any("coverage limitations" in item for item in validate_citation_audit(scope, baseline, current))


def test_new_report_can_reject_change_without_scope_validator_fabricating_approval():
    old, new, baseline = fixture()
    current = report(baseline)
    current.findings[0].verdict = "unsupported"
    current.findings[0].needs_revision = True
    assert validate_citation_audit(build(old, new, baseline), baseline, current) == []
    assert current.findings[0].needs_revision  # ordinary admission must still reject it


def test_tampered_scope_or_baseline_is_rejected():
    old, new, baseline = fixture()
    scope = build(old, new, baseline)
    scope["new_record_hash"] = "0" * 64
    assert validate_citation_audit(scope, baseline, report(baseline))
    scope = build(old, new, baseline)
    different = baseline.model_copy(deep=True)
    different.coverage_limitations.append("New restriction.")
    assert validate_citation_audit(scope, different, report(baseline))


@pytest.mark.parametrize("change", [
    lambda s: s.pop("old_record_hash"),
    lambda s: s.update(incremental_depth=True),
    lambda s: s.update(old_record_hash=s["new_record_hash"]),
    lambda s: s.update(changed_paths=[]),
    lambda s: s["changes"][0].update(changed_path="observations[0].value.raw"),
    lambda s: s["changes"][0].update(changed_evidence_ids=[]),
    lambda s: s["entity_refs"][0].update(index=True),
    lambda s: s["scope_limits"].update(max_entities=1),
    lambda s: s.update(source_anchors=[]),
])
def test_self_rehashed_but_malformed_manifest_is_still_rejected(change):
    old, new, baseline = fixture()
    scope = build(old, new, baseline)
    change(scope)
    scope["scope_sha256"] = digest_json({k: v for k, v in scope.items() if k != "scope_sha256"})
    assert validate_citation_audit(scope, baseline, report(baseline))


def test_optional_quantitative_followups_are_not_silently_lost():
    old, new, baseline = fixture()
    baseline.quantitative_followups = [QuantitativeFollowup(coverage_ids=["synthetic-curve"],
        evidence=[SourceAnchor(source_id="main", block_id="p3")],
        affected_query_types=["pointwise_comparison"], recommended_action="Read optional separable markers.")]
    scope = build(old, new, baseline)
    current = report(baseline)
    assert validate_citation_audit(scope, baseline, current) == []
    current.quantitative_followups.clear()
    assert any("followups" in item for item in validate_citation_audit(scope, baseline, current))


def test_json_reports_and_malformed_inputs_are_handled_without_mutation():
    old, new, baseline = fixture()
    scope = build(old, new, baseline)
    payloads = scope, baseline.model_dump(mode="json"), report(baseline).model_dump(mode="json")
    before = copy.deepcopy(payloads)
    assert validate_citation_audit(*payloads) == []
    assert payloads == before
    assert validate_citation_audit({}, baseline, report(baseline))
    assert build({}, new, baseline) is None
