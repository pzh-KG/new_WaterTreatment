"""Synthetic safety boundaries for source-bound interpretation repair.

No scientific gold, model call, live service, or real paper data is involved.
Passing these tests demonstrates protocol checks, not extraction accuracy.
"""
from __future__ import annotations

import copy
import json

import pytest

from watertreatment.domain import (
    Component, CoverageEntry, DesignComparison, EvidenceRef, Experiment, ExtractionRecord,
    Fact, MaterialSample, NamedFact, Observation, ObservationPair, Paper,
    QuantitativeCoverage, Substance, Target,
)
from watertreatment.reading import (
    AUDIT_DIMENSIONS, AuditFinding, QuantitativeFollowup, ReadingAudit, SourceAnchor,
)
from watertreatment.scientific_revision_audit import (
    POLICY, NEW_SCOPE_LIMITS, build_scientific_revision_scope, finding_paths,
    validate_scientific_revision_audit,
)
from watertreatment.storage import digest_json
from test_reference_associations import reference_record

V2_SCOPE_LIMITS = {**NEW_SCOPE_LIMITS, "compiler_version": 2}


def fixture():
    old = ExtractionRecord(
        schema_version="0.3.0", rule_version="0.7", extraction_scope="research_core",
        paper=Paper(id="synthetic", source_ids=["source"]),
        materials=[MaterialSample(id="material")],
        experiments=[Experiment(id="experiment", material_ids=["material"],
            conditions=[NamedFact(name="unreported-condition")]),
            Experiment(id="other", material_ids=["material"])],
        observations=[
            Observation(id="changed", experiment_id="experiment", material_id="material",
                value=Fact(value=5.5, status="reported", evidence_ids=["text"]),
                unit=Fact(value="synthetic-unit", status="reported", evidence_ids=["text"]),
                evidence_ids=["text"]),
            Observation(id="sibling", experiment_id="experiment", material_id="material",
                value=Fact(value=2.3, status="reported", evidence_ids=["plot"]), evidence_ids=["plot"]),
            Observation(id="unrelated", experiment_id="other", material_id="material",
                value=Fact(value=9.1, status="reported", evidence_ids=["other"]), evidence_ids=["other"]),
        ],
        evidence=[EvidenceRef(id="text", source_id="source", block_id="page1"),
                  EvidenceRef(id="plot", source_id="source", block_id="page2"),
                  EvidenceRef(id="other", source_id="source", block_id="page3")],
        coverage=[CoverageEntry(source_id="source", block_ids=["page1", "page2", "page3"],
            state="read_structured", text_read=True, visual_checked=True)],
    )
    new = old.model_copy(deep=True)
    new.observations[0].value.status = "inferred"
    new.observations[0].value.note = "Source supports an interpretation, not an explicitly printed value."
    new.observations[0].limitations = ["The inference must not be compared as an author-reported measurement."]
    baseline = ReadingAudit(
        findings=[AuditFinding(record_path="observations[0].value", severity="major",
            verdict="unsupported", needs_revision=True, explanation="Correct the attribution of this unchanged value.",
            evidence=[SourceAnchor(source_id="source", block_id="page1")])],
        potential_omissions=[], coverage_limitations=["Unknown experimental denominator remains."],
        checked_dimensions=list(AUDIT_DIMENSIONS),
    )
    return old, new, baseline


def build(old, new, baseline, **kwargs):
    return build_scientific_revision_scope(old, new, baseline,
        baseline_origin="fresh_model_audit", **kwargs)


def report_for(scope, baseline):
    """Explicitly address all scoped entities, changes and inherited blockers."""
    report = baseline.model_copy(deep=True)
    report.review_scope = "scientific_delta"
    report.review_scope_sha256 = scope["scope_sha256"]
    report.potential_omissions = []
    report.findings = [f.model_copy(deep=True) for f in baseline.findings
                       if f.verdict == "unresolved" and not f.needs_revision]
    paths = set(scope["changed_paths"])
    paths.update(x["record_path"] for x in scope["entity_refs"])
    paths.update(p for item in scope["blocking_obligations"] for p in item["paths"])
    anchors = [SourceAnchor(source_id=x["source_id"], block_id=x["block_id"])
               for x in scope["source_anchors"]]
    for path in sorted(paths):
        report.findings.append(AuditFinding(record_path=path, severity="minor", verdict="supported",
            needs_revision=False, explanation="The scoped claim and compatibility consequences were source-checked.",
            evidence=copy.deepcopy(anchors)))
    return report


def validate(scope, old, new, baseline, report, **kwargs):
    return validate_scientific_revision_audit(scope, baseline, report,
        old_record=old, new_record=new, **kwargs)


def test_same_value_provenance_repair_has_explicit_source_obligations_without_full_record_payload():
    old, new, baseline = fixture()
    frozen = [r.model_dump(mode="json") for r in (old, new, baseline)]
    scope = build(old, new, baseline)
    assert scope and scope["policy"] == POLICY
    assert {x["kind"] for x in scope["changes"]} == {"fact_interpretation", "restriction"}
    fact = next(x for x in scope["changes"] if x["kind"] == "fact_interpretation")
    assert fact["old_value"]["value"] == fact["new_value"]["value"] == 5.5
    assert fact["required_evidence_ids"] == ["text"]
    assert not any(x["record_path"] == "observations[2]" for x in scope["entity_refs"])
    assert all("value" not in x for x in scope["entity_refs"])
    assert validate(scope, old, new, baseline, report_for(scope, baseline)) == []
    assert frozen == [r.model_dump(mode="json") for r in (old, new, baseline)]


def test_native_word_source_requirements_are_explicit_and_legacy_scope_replay_is_unchanged():
    old, new, baseline = fixture()
    legacy_options = {k: v for k, v in NEW_SCOPE_LIMITS.items() if k != 'source_requirement_policy'}
    legacy = build(old, new, baseline, **legacy_options)
    current = build(old, new, baseline, **NEW_SCOPE_LIMITS)
    assert 'source_requirement_policy' not in legacy
    assert 'source_requirement_policy' not in legacy['scope_limits']
    assert current['source_requirement_policy'] == 'native-word-linked-objects-v2'
    assert current['scope_limits']['source_requirement_policy'] == 'native-word-linked-objects-v2'
    assert current['scope_sha256'] != legacy['scope_sha256']
    assert current['changes'] == legacy['changes'] and current['source_anchors'] == legacy['source_anchors']
    assert build(old, new, baseline, **legacy_options) == legacy
    assert validate(legacy, old, new, baseline, report_for(legacy, baseline)) == []
    assert validate(current, old, new, baseline, report_for(current, baseline)) == []
    tampered = copy.deepcopy(legacy)
    tampered['source_requirement_policy'] = 'native-word-structure-v1'
    tampered['scope_sha256'] = digest_json({k: v for k, v in tampered.items() if k != 'scope_sha256'})
    assert validate(tampered, old, new, baseline, report_for(tampered, baseline))
    assert build(old, new, baseline, source_requirement_policy='skip-native-tables') is None


@pytest.mark.parametrize("mutation", [
    lambda r: setattr(r.observations[0].value, "value", 6.0),
    lambda r: setattr(r.observations[0].value, "normalized", 0.55),
    lambda r: setattr(r.observations[0].unit, "value", "other-unit"),
    lambda r: setattr(r.observations[0], "id", "replaced"),
    lambda r: setattr(r.observations[0], "experiment_id", "other"),
    lambda r: setattr(r.materials[0], "id", "replaced-material"),
    lambda r: setattr(r.experiments[0].conditions[0].fact, "value", 1),
    lambda r: setattr(r.evidence[0], "block_id", "other-page"),
    lambda r: setattr(r.evidence[0].locator, "physical_page", 7),
    lambda r: setattr(r.coverage[0], "visual_checked", False),
    lambda r: r.coverage[0].block_ids.pop(),
    lambda r: r.paper.source_ids.append("new-source"),
    lambda r: r.observations.reverse(),
    lambda r: r.observations.pop(),
    lambda r: r.experiments[0].conditions.append(NamedFact(name="new-condition")),
])
def test_primary_scientific_changes_and_coverage_cannot_use_interpretation_shortcut(mutation):
    old, new, baseline = fixture()
    mutation(new)
    assert build(old, new, baseline) is None


def test_null_missingness_transition_requires_source_and_does_not_invent_a_value():
    old, _, baseline = fixture()
    new = old.model_copy(deep=True)
    missing = new.experiments[0].conditions[0].fact
    missing.status = "not_reported"
    baseline.findings[0].record_path = "experiments[0].conditions[0].fact"
    # No factual/owner anchor exists for this missing condition yet.
    assert build(old, new, baseline) is None
    missing.evidence_ids = ["text"]
    missing.note = "The source methods were read and do not state this condition."
    scope = build(old, new, baseline)
    assert scope and missing.value is None
    assert {"observations[0]", "observations[1]"} <= {x["record_path"] for x in scope["entity_refs"]}
    report = report_for(scope, baseline)
    assert validate(scope, old, new, baseline, report) == []
    for finding in report.findings:
        finding.evidence = []
    assert validate(scope, old, new, baseline, report)


def test_shared_reference_addition_includes_both_sides_without_ownership_or_data_duplication():
    new = reference_record()
    old = new.model_copy(deep=True)
    old.reference_associations = []
    baseline = fixture()[2]
    baseline.findings[0].record_path = "quantitative_coverage[id=condition-series]"
    baseline.findings[0].evidence = [SourceAnchor(source_id="source", block_id="group-page")]
    scope = build(old, new, baseline)
    assert scope
    refs = {x["record_path"] for x in scope["entity_refs"]}
    assert {"reference_associations[0]", "observations[0]", "observations[1]",
            "quantitative_coverage[1]", "experiments[0]", "experiments[1]"} <= refs
    assert {x["evidence_id"] for x in scope["source_anchors"]} == {"evidence", "group-evidence"}
    assert old.observations == new.observations and old.experiments == new.experiments
    assert len(new.observations) == 2
    assert validate(scope, old, new, baseline, report_for(scope, baseline)) == []


def test_existing_reference_association_cannot_be_rewritten_or_removed_in_append_only_repair():
    old = reference_record()
    new = old.model_copy(deep=True)
    baseline = fixture()[2]
    baseline.findings = []
    new.reference_associations[0].applicability = "reported"
    assert build(old, new, baseline) is None
    new.reference_associations = []
    assert build(old, new, baseline) is None


def finding(path, *, paths=None):
    return AuditFinding(record_path=path, record_paths=paths or [], severity="minor",
        verdict="supported", needs_revision=False, explanation="Synthetic locator only.", evidence=[])


def test_typed_paths_and_legacy_semicolon_id_selectors_resolve_without_rewriting_audit():
    record = fixture()[0].model_dump(mode="json")
    typed = finding("human label", paths=["observations[0].value", "observations[1].value"])
    legacy = finding("observations[id=changed].value; observations[1].value")
    expected = [("observations", 0, "value"), ("observations", 1, "value")]
    assert finding_paths(typed, record) == finding_paths(legacy, record) == expected
    assert finding_paths(finding("observations[0,1].value"), record) == expected
    assert legacy.record_path == "observations[id=changed].value; observations[1].value"


@pytest.mark.parametrize("path", [
    "observations[0].value; run code", "observations[*].value", "observations[-1].value",
    "observations[id=missing].value", "observations[0].value;", "observations[0].missing",
    "observations[0,0].value", "observations[0].value; observations[0].value",
    "observations[0:2].value", "observations[__import__('os').system('echo unsafe')].value",
    "observations[0].value.__class__", "observations[0]]", "observations[[0]]",
])
def test_invalid_selector_or_trailing_prose_is_never_executed_or_silently_ignored(path):
    with pytest.raises(ValueError):
        finding_paths(finding(path), fixture()[0].model_dump(mode="json"))


def test_every_change_and_old_blocker_requires_specific_original_source_review():
    old, new, baseline = fixture()
    baseline.findings.append(AuditFinding(record_path="observations[1].value", severity="major",
        verdict="unsupported", needs_revision=True, explanation="Recheck a second earlier blocker.",
        evidence=[SourceAnchor(source_id="source", block_id="page2")]))
    scope = build(old, new, baseline)
    report = report_for(scope, baseline)
    assert validate(scope, old, new, baseline, report) == []
    report.findings = [f for f in report.findings if not f.record_path.startswith("observations[1]")]
    assert any("blocking" in x.lower() for x in validate(scope, old, new, baseline, report))
    report = report_for(scope, baseline)
    for item in report.findings:
        item.evidence = [e for e in item.evidence if e.block_id != "page1"]
    assert any("source review" in x.lower() for x in validate(scope, old, new, baseline, report))


def test_bare_collection_claim_cannot_certify_all_individual_changes():
    old, new, baseline = fixture()
    scope = build(old, new, baseline)
    report = report_for(scope, baseline)
    report.findings = [AuditFinding(record_path="observations", severity="minor", verdict="supported",
        needs_revision=False, explanation="Blanket claim without individual review.",
        evidence=[SourceAnchor(source_id="source", block_id="page1")])]
    assert validate(scope, old, new, baseline, report)


def test_invalid_extra_audit_path_is_rejected_instead_of_silently_dropped():
    old, new, baseline = fixture()
    scope = build(old, new, baseline)
    report = report_for(scope, baseline)
    report.findings.append(finding("observations[0].value; unsupported prose"))
    assert validate(scope, old, new, baseline, report)


def test_direct_consumers_require_explicit_review_without_common_material_explosion():
    old, new, baseline = fixture()
    for record in (old, new):
        record.quantitative_coverage = [QuantitativeCoverage(id="series", source_id="source", block_id="page2",
            observation_ids=["changed", "sibling"], label="Synthetic response series")]
        record.comparisons = [DesignComparison(id="comparison", observation_ids=["changed", "sibling"],
                                               evidence_ids=["plot"])]
        record.experiments[0].pairs = [ObservationPair(left_observation_id="changed", right_observation_id="sibling")]
    scope = build(old, new, baseline)
    assert scope
    refs = {x["record_path"]: x["role"] for x in scope["entity_refs"]}
    assert refs["comparisons[0]"] == refs["quantitative_coverage[0]"] == refs["experiments[0]"] == "affected"
    assert "observations[2]" not in refs and "experiments[1]" not in refs
    report = report_for(scope, baseline)
    assert validate(scope, old, new, baseline, report) == []
    # Checking the edited value alone must not certify its comparisons or pair.
    report.findings = [f for f in report.findings if f.record_path.startswith("observations[0]")]
    assert validate(scope, old, new, baseline, report)


@pytest.mark.parametrize("defect", ["limitation", "unresolved", "followup"])
def test_inherited_limits_unresolved_findings_and_optional_followups_must_survive(defect):
    old, new, baseline = fixture()
    baseline.findings.append(AuditFinding(record_path="observations[2].value", severity="minor", verdict="unresolved",
        needs_revision=False, explanation="Unrelated source remains unresolved.", evidence=[]))
    baseline.quantitative_followups = [QuantitativeFollowup(coverage_ids=["deferred-series"],
        evidence=[SourceAnchor(source_id="source", block_id="page3")],
        affected_query_types=["pointwise_comparison"], recommended_action="Read optional interior markers on demand.")]
    scope = build(old, new, baseline)
    report = report_for(scope, baseline)
    assert validate(scope, old, new, baseline, report) == []
    if defect == "limitation":
        report.coverage_limitations.clear()
    elif defect == "unresolved":
        report.findings = [f for f in report.findings if f.verdict != "unresolved"]
    else:
        report.quantitative_followups.clear()
    assert validate(scope, old, new, baseline, report)


@pytest.mark.parametrize("mutation", [
    lambda scope: scope.update(changed_paths=[]),
    lambda scope: scope.update(old_record_hash="0" * 64),
    lambda scope: scope.update(baseline_audit_hash="0" * 64),
    lambda scope: scope.update(entity_refs=[]),
    lambda scope: scope.update(blocking_obligations=[]),
    lambda scope: scope["source_anchors"][0].update(block_id="forged-source"),
    lambda scope: scope["changes"][0].update(required_evidence_ids=[]),
])
def test_rehashed_scope_tamper_cannot_bypass_record_and_source_binding(mutation):
    old, new, baseline = fixture()
    scope = build(old, new, baseline)
    mutation(scope)
    scope["scope_sha256"] = digest_json({k: v for k, v in scope.items() if k != "scope_sha256"})
    assert validate(scope, old, new, baseline, report_for(scope, baseline))


def test_changed_source_and_current_record_reject_old_scope_even_with_matching_report_hash():
    old, new, baseline = fixture()
    scope = build(old, new, baseline)
    report = report_for(scope, baseline)
    new.evidence[0].block_id = "tampered-source"
    assert validate(scope, old, new, baseline, report)


def test_located_omission_is_explicit_and_unlocated_omission_cannot_be_assumed_resolved():
    old, new, baseline = fixture()
    baseline.potential_omissions = ["The attribution explanation is missing."]
    assert build(old, new, baseline) is None
    baseline.findings[0].omission_indices = [0]
    scope = build(old, new, baseline)
    assert scope and validate(scope, old, new, baseline, report_for(scope, baseline)) == []
    baseline.findings[0].omission_indices = [1]
    assert build(old, new, baseline) is None


@pytest.mark.parametrize("defect", ["dimension", "not_checked", "origin", "bound", "overflow"])
def test_untrusted_incomplete_or_unbounded_baseline_cannot_enable_scoped_review(defect):
    old, new, baseline = fixture()
    if defect == "dimension":
        baseline.checked_dimensions.pop()
    elif defect == "not_checked":
        baseline.findings[0].verdict = "not_checked"
    elif defect == "origin":
        assert build_scientific_revision_scope(old, new, baseline, baseline_origin="reused_audit") is None
        return
    elif defect == "bound":
        assert build(old, new, baseline, max_entities=True) is None
        return
    else:
        assert build(old, new, baseline, max_changes=1) is None
        return
    assert build(old, new, baseline) is None


def test_mapping_inputs_and_invalid_scope_fail_without_mutating_originals():
    old, new, baseline = fixture()
    mappings = [x.model_dump(mode="json") for x in (old, new, baseline)]
    saved = copy.deepcopy(mappings)
    assert build(*mappings) == build(old, new, baseline)
    assert mappings == saved
    assert build({}, new, baseline) is None
    assert validate({}, old, new, baseline, baseline)


def test_frozen_scope_with_blockers_survives_json_roundtrip_exactly():
    old, new, baseline = fixture()
    scope = build(old, new, baseline)
    frozen = json.loads(json.dumps(scope))
    assert frozen == scope
    assert validate(frozen, old, new, baseline, report_for(frozen, baseline)) == []


@pytest.mark.parametrize("side", ["evidence", "group-evidence"])
def test_reference_addition_requires_both_source_sides_structurally_and_in_review(side):
    new = reference_record()
    old = new.model_copy(deep=True)
    old.reference_associations = []
    baseline = fixture()[2]
    baseline.findings = []
    scope = build(old, new, baseline)
    assert scope
    report = report_for(scope, baseline)
    block = next(e.block_id for e in new.evidence if e.id == side)
    for item in report.findings:
        item.evidence = [e for e in item.evidence if e.block_id != block]
    assert validate(scope, old, new, baseline, report)
    new.reference_associations[0].evidence_ids.remove(side)
    assert build(old, new, baseline) is None


def test_existing_reference_is_reconsidered_when_target_experiment_conditions_change():
    old = reference_record("experiment")
    old.experiments[1].conditions = [NamedFact(name="missing-condition")]
    new = old.model_copy(deep=True)
    new.experiments[1].conditions[0].fact = Fact(status="not_reported", evidence_ids=["group-evidence"])
    baseline = fixture()[2]
    baseline.findings = []
    scope = build(old, new, baseline)
    assert scope
    refs = {x["record_path"]: x["role"] for x in scope["entity_refs"]}
    assert refs["reference_associations[0]"] == "affected"
    assert refs["observations[0]"] == refs["observations[1]"] == "affected"
    assert validate(scope, old, new, baseline, report_for(scope, baseline)) == []


def test_reference_to_experiment_keeps_target_results_as_context_without_remeasurement():
    new = reference_record("experiment")
    old = new.model_copy(deep=True)
    old.reference_associations = []
    baseline = fixture()[2]
    baseline.findings = []
    scope = build(old, new, baseline)
    assert scope
    refs = {x["record_path"]: x["role"] for x in scope["entity_refs"]}
    assert refs["observations[1]"] == "context"
    assert refs["observations[0]"] == "affected"
    assert new.observations == old.observations


def test_reference_target_series_is_reconsidered_when_its_primary_result_interpretation_changes():
    old = reference_record("series")
    new = old.model_copy(deep=True)
    new.observations[1].value.status = "inferred"
    baseline = fixture()[2]
    baseline.findings = []
    scope = build(old, new, baseline)
    assert scope
    refs = {x["record_path"]: x["role"] for x in scope["entity_refs"]}
    assert refs["reference_associations[0]"] == refs["observations[0]"] == "affected"
    assert refs["quantitative_coverage[1]"] == "affected"


def test_substance_interpretation_propagates_to_explicit_experiment_and_observation_consumers():
    old, _, baseline = fixture()
    old.substances = [Substance(id="target", name=Fact(value="Synthetic target", status="reported", evidence_ids=["text"]))]
    old.experiments[0].targets = [Target(substance_id="target")]
    new = old.model_copy(deep=True)
    new.substances[0].name.status = "inferred"
    baseline.findings = []
    scope = build(old, new, baseline)
    assert scope
    refs = {x["record_path"]: x["role"] for x in scope["entity_refs"]}
    assert all(refs[path] == "affected" for path in ("substances[0]", "experiments[0]", "observations[0]", "observations[1]"))
    assert "observations[2]" not in refs


def test_recipe_null_missingness_does_not_reread_all_performance_but_normalized_consumers_are_included():
    old, _, baseline = fixture()
    old.substances = [Substance(id="component")]
    old.materials[0].components = [Component(substance_id="component", evidence_ids=["text"])]
    new = old.model_copy(deep=True)
    new.materials[0].components[0].amount = Fact(status="not_reported", evidence_ids=["text"])
    baseline.findings = []
    scope = build(old, new, baseline)
    assert scope
    refs = {x["record_path"]: x["role"] for x in scope["entity_refs"]}
    assert refs == {"materials[0]": "affected", "substances[0]": "context"}
    for record in (old, new):
        record.observations[0].value.normalized = 0.55
    scope = build(old, new, baseline)
    assert scope
    refs = {x["record_path"]: x["role"] for x in scope["entity_refs"]}
    assert refs["observations[0]"] == "affected" and "observations[2]" not in refs


@pytest.mark.parametrize("field", ["measured_object", "time_reference", "basis"])
def test_text_repair_exceptions_cannot_change_a_numeric_fact(field):
    old, _, baseline = fixture()
    if field == "measured_object":
        old.observations[0].measured_object = Fact(value=1, status="reported", evidence_ids=["text"])
    elif field == "time_reference":
        old.observations[0].time_window.reference = Fact(value=1, status="reported", evidence_ids=["text"])
    else:
        old.experiments[0].conditions[0].basis = Fact(value=1, status="reported", evidence_ids=["text"])
    new = old.model_copy(deep=True)
    if field == "measured_object":
        new.observations[0].measured_object.value = 2
    elif field == "time_reference":
        new.observations[0].time_window.reference.value = 2
    else:
        new.experiments[0].conditions[0].basis.value = 2
    assert build(old, new, baseline) is None


def test_grouped_exact_entity_dispositions_are_allowed_without_one_finding_per_entity():
    old, new, baseline = fixture()
    scope = build(old, new, baseline)
    report = report_for(scope, baseline)
    paths = [f.record_path for f in report.findings]
    grouped = report.findings[0].model_copy(deep=True)
    grouped.record_path, grouped.record_paths = "Grouped exact source checks", paths
    report.findings = [grouped]
    assert validate(scope, old, new, baseline, report) == []


def test_legacy_default_scope_hashes_and_propagation_are_unchanged():
    old, new, baseline = fixture()
    scope = build(old, new, baseline)
    assert scope["scope_sha256"] == "32daba4ef9560a0ab55696ac2a3a3ed138d513ec48a7ab7111b9d653ec5e7e13"
    assert scope == build(old, new, baseline, max_context_entities=None)
    assert set(scope["scope_limits"]) == {"max_entities", "max_changes"}
    new = reference_record()
    old = new.model_copy(deep=True)
    old.reference_associations = []
    baseline.findings = []
    scope = build(old, new, baseline)
    assert scope["scope_sha256"] == "f0eb1f796bda2021fccc7556e8ac639a7cd049cd60b03e6aac80b912ac06bc78"
    assert next(r for r in scope["entity_refs"] if r["record_path"] == "observations[0]")["role"] == "affected"


def split_reference_fixture():
    new = reference_record()
    old = new.model_copy(deep=True)
    old.reference_associations = []
    baseline = fixture()[2]
    baseline.findings = []
    return old, new, baseline


def test_explicit_context_budget_does_not_reaudit_unchanged_reference_values_or_their_consumers():
    old, new, baseline = split_reference_fixture()
    for record in (old, new):
        record.experiments[0].pairs = [ObservationPair(left_observation_id="endpoint", right_observation_id="variant-endpoint")]
    scope = build(old, new, baseline, **NEW_SCOPE_LIMITS)
    assert scope and scope["scope_limits"] == NEW_SCOPE_LIMITS
    refs = {r["record_path"]: r for r in scope["entity_refs"]}
    assert refs["reference_associations[0]"]["role"] == refs["quantitative_coverage[1]"]["role"] == "affected"
    assert refs["observations[0]"]["role"] == refs["observations[1]"]["role"] == "context"
    assert refs["experiments[0]"]["role"] == "context"
    assert "quantitative_coverage[0]" not in refs
    assert set(refs["reference_associations[0]"]["required_evidence_ids"]) == {"evidence", "group-evidence"}
    assert validate(scope, old, new, baseline, report_for(scope, baseline)) == []
    assert old.observations == new.observations


@pytest.mark.parametrize("trigger", ["actual_change", "prior_blocker"])
def test_split_budget_never_demotes_an_actually_changed_or_blocked_reference_source(trigger):
    old, new, baseline = split_reference_fixture()
    if trigger == "actual_change":
        new.observations[0].value.status = "inferred"
    else:
        baseline.findings = [AuditFinding(record_path="observations[0].value", severity="major",
            verdict="unsupported", needs_revision=True, explanation="This original control needs a source disposition.",
            evidence=[SourceAnchor(source_id="source", block_id="page")])]
    scope = build(old, new, baseline, **NEW_SCOPE_LIMITS)
    assert scope
    refs = {r["record_path"]: r["role"] for r in scope["entity_refs"]}
    assert refs["observations[0]"] == refs["quantitative_coverage[0]"] == "affected"
    assert refs["reference_associations[0]"] == refs["quantitative_coverage[1]"] == "affected"


def test_existing_reference_target_change_keeps_unchanged_control_context_under_split_budget():
    old = reference_record()
    new = old.model_copy(deep=True)
    new.observations[1].value.status = "inferred"
    baseline = fixture()[2]
    baseline.findings = []
    scope = build(old, new, baseline, **NEW_SCOPE_LIMITS)
    assert scope
    refs = {r["record_path"]: r["role"] for r in scope["entity_refs"]}
    assert refs["observations[1]"] == refs["reference_associations[0]"] == "affected"
    assert refs["observations[0]"] == "context"
    assert "quantitative_coverage[0]" not in refs


def test_affected_and_context_caps_are_independent_and_frozen_for_recompilation():
    old, new, baseline = split_reference_fixture()
    scope = build(old, new, baseline, **NEW_SCOPE_LIMITS)
    affected = sum(r["role"] == "affected" for r in scope["entity_refs"])
    context = sum(r["role"] == "context" for r in scope["entity_refs"])
    assert affected > 1 and context > 1
    exact = build(old, new, baseline, max_entities=affected, max_context_entities=context)
    assert exact
    assert build(old, new, baseline, max_entities=affected - 1, max_context_entities=context) is None
    assert build(old, new, baseline, max_entities=affected, max_context_entities=context - 1) is None
    assert build(old, new, baseline, max_entities=affected) is None  # legacy counts the complete union
    assert validate(json.loads(json.dumps(exact)), old, new, baseline, report_for(exact, baseline)) == []
    changed_context = new.model_copy(deep=True)
    changed_context.observations[0].value.value = 71
    assert validate(exact, old, changed_context, baseline, report_for(exact, baseline))


@pytest.mark.parametrize("invalid", [False, True, 0, -1, 241, 10_000, 1.5, "240", [], {}])
def test_context_navigation_budget_is_bounded_integer_only(invalid):
    old, new, baseline = split_reference_fixture()
    assert build(old, new, baseline, max_context_entities=invalid) is None


@pytest.mark.parametrize("section", ["source_index", "quantitative_coverage"])
def test_typed_source_inventory_restrictions_use_exact_registered_block_in_new_mode_only(section):
    old = reference_record()
    new = old.model_copy(deep=True)
    baseline = fixture()[2]
    baseline.findings = []
    target = new.design_scope.source_index[0] if section == "source_index" else new.quantitative_coverage[0]
    target.limitations.append("A shared panel does not identify the same physical run.")
    assert build(old, new, baseline) is None  # legacy semantics/hash boundary
    scope = build(old, new, baseline, **NEW_SCOPE_LIMITS)
    assert scope and len(scope["changes"]) == 1
    assert scope["changes"][0]["required_evidence_ids"] == ["evidence"]
    assert validate(scope, old, new, baseline, report_for(scope, baseline)) == []
    for record in (old, new):
        bad = record.design_scope.source_index[0] if section == "source_index" else record.quantitative_coverage[0]
        bad.block_id = "unregistered-block"
    assert build(old, new, baseline, **NEW_SCOPE_LIMITS) is None


def test_existing_reference_metadata_can_be_qualified_without_changing_its_binding_in_new_mode():
    old = reference_record()
    new = old.model_copy(deep=True)
    reference = new.reference_associations[0]
    reference.applicability = "ambiguous"
    reference.rationale = "The source permits a contextual comparison but leaves protocol applicability unresolved."
    reference.matched_conditions = []
    reference.unresolved_conditions.append("nominal protocol applicability")
    reference.limitations.append("No automatic quantitative comparison is established.")
    baseline = fixture()[2]
    baseline.findings = []
    assert build(old, new, baseline) is None
    scope = build(old, new, baseline, **NEW_SCOPE_LIMITS)
    assert scope and len(scope["changes"]) == 1
    assert scope["changes"][0]["kind"] == "reference_association"
    assert scope["changes"][0]["old_value"]["id"] == scope["changes"][0]["new_value"]["id"]
    assert set(scope["changes"][0]["required_evidence_ids"]) == {"evidence", "group-evidence"}
    assert validate(scope, old, new, baseline, report_for(scope, baseline)) == []


@pytest.mark.parametrize("mutation", [
    lambda r: setattr(r, "id", "replaced-reference"),
    lambda r: setattr(r, "target_kind", "comparison"),
    lambda r: setattr(r, "target_id", "series"),
    lambda r: setattr(r, "observation_ids", ["variant-endpoint"]),
    lambda r: setattr(r, "relationship", "contextual_comparison"),
    lambda r: r.limitations.clear(),
    lambda r: r.unresolved_conditions.clear(),
])
def test_existing_reference_repair_cannot_change_binding_or_erase_prior_uncertainty(mutation):
    old = reference_record()
    new = old.model_copy(deep=True)
    baseline = fixture()[2]
    baseline.findings = []
    mutation(new.reference_associations[0])
    assert build(old, new, baseline, **NEW_SCOPE_LIMITS) is None


def test_v2_condition_and_unit_correction_reviews_owned_results_and_keeps_other_experiment_out():
    old, _, baseline = fixture()
    old.experiments[0].conditions = [NamedFact(name="temperature",
        fact=Fact(value=20, status="reported", evidence_ids=["text"]),
        unit=Fact(value="degree", status="reported", evidence_ids=["text"]))]
    new = old.model_copy(deep=True)
    new.experiments[0].conditions[0].fact.value = "room temperature"
    new.experiments[0].conditions[0].unit = Fact(status="not_applicable", evidence_ids=["text"],
        note="The source supplies a categorical temperature only.")
    baseline.findings[0].record_path = "experiments[0].conditions[0]"
    assert build(old, new, baseline) is None  # frozen compiler v1 is not redefined
    scope = build(old, new, baseline, **V2_SCOPE_LIMITS)
    assert scope and scope["scope_limits"]["compiler_version"] == 2
    affected = {r["record_path"] for r in scope["entity_refs"] if r["role"] == "affected"}
    assert {"experiments[0]", "observations[0]", "observations[1]"} <= affected
    assert "observations[2]" not in affected
    assert {c["kind"] for c in scope["changes"]} == {"scientific_condition"}
    assert validate(scope, old, new, baseline, report_for(scope, baseline)) == []
    report = report_for(scope, baseline)
    report.findings = [f for f in report.findings if f.record_path != "observations[1]"]
    assert any("dependency" in error for error in validate(scope, old, new, baseline, report))


def test_v2_condition_membership_repair_maps_historical_indices_to_entire_changed_list():
    old, _, baseline = fixture()
    old.experiments[0].conditions = [NamedFact(name=name,
        fact=Fact(value=i, status="reported", evidence_ids=["text"]))
        for i, name in enumerate(["first", "last"])]
    new = old.model_copy(deep=True)
    new.experiments[0].conditions = [NamedFact(name="source-stated condition",
        fact=Fact(value="categorical only", status="reported", evidence_ids=["text"]))]
    baseline.findings[0].record_path = "experiments[0].conditions[1].fact"
    scope = build(old, new, baseline, **V2_SCOPE_LIMITS)
    assert scope and scope["changes"][0]["kind"] == "scientific_membership"
    assert scope["blocking_obligations"][0]["paths"] == ["experiments[0].conditions"]
    assert scope["blocking_obligations"][0]["historical_paths"] == ["experiments[0].conditions[1].fact"]
    assert validate(scope, old, new, baseline, report_for(scope, baseline)) == []


@pytest.mark.parametrize("mutation,code,path", [
    (lambda r: setattr(r.observations[0].value, "value", 6), "protected_quantity_or_identity_changed", "observations[0].value"),
    (lambda r: setattr(r.observations[0], "experiment_id", "other"), "protected_identity_or_coverage_changed", "observations[0].experiment_id"),
    (lambda r: setattr(r.evidence[0], "block_id", "different"), "protected_identity_or_coverage_changed", "evidence[0].block_id"),
    (lambda r: setattr(r.coverage[0], "visual_checked", False), "protected_identity_or_coverage_changed", "coverage[0].visual_checked"),
    (lambda r: r.observations.pop(), "protected_membership_changed", "observations"),
])
def test_v2_returns_actionable_rejection_without_relaxing_identity_sources_or_primary_values(mutation, code, path):
    old, new, baseline = fixture()
    mutation(new)
    diagnostics = []
    assert build(old, new, baseline, diagnostics=diagnostics, **V2_SCOPE_LIMITS) is None
    assert diagnostics[0]["code"] == code and diagnostics[0]["path"] == path
    assert diagnostics[0]["recoverable"] and not diagnostics[0]["automatic_full_review_allowed"]


def test_v2_atomic_condition_changes_cannot_drop_source_alternatives():
    old, _, baseline = fixture()
    old.experiments[0].conditions[0].fact = Fact(value=None, status="conflict",
        alternatives=[{"value": 20, "evidence_ids": ["text"]}], evidence_ids=["text"])
    new = old.model_copy(deep=True)
    new.experiments[0].conditions = [NamedFact(name="renamed",
        fact=Fact(value=20, status="reported", evidence_ids=["text"]))]
    diagnostics = []
    assert build(old, new, baseline, diagnostics=diagnostics, **V2_SCOPE_LIMITS) is None
    assert diagnostics[0]["code"] == "source_restriction_removed"


def test_v2_keeps_prior_auditor_caption_anchor_without_inventing_candidate_evidence():
    old, new, baseline = fixture()
    for record in [old, new]:
        record.coverage[0].block_ids.append("caption")
    baseline.findings[0].evidence.append(SourceAnchor(source_id="source", block_id="caption"))
    frozen = new.model_dump(mode="json")
    assert build(old, new, baseline) is None
    scope = build(old, new, baseline, **V2_SCOPE_LIMITS)
    extras = [a for a in scope["source_anchors"] if a.get("origin") == "prior_audit_blocker"]
    assert len(extras) == 1 and extras[0]["block_id"] == "caption"
    assert new.model_dump(mode="json") == frozen
    assert validate(scope, old, new, baseline, report_for(scope, baseline)) == []
    baseline.findings[0].evidence[-1].block_id = "unregistered"
    diagnostics = []
    assert build(old, new, baseline, diagnostics=diagnostics, **V2_SCOPE_LIMITS) is None
    assert diagnostics[0]["code"] == "unregistered_blocker_source"
