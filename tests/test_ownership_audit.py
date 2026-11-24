"""Structural source-audit boundaries on synthetic scientific records."""
import pytest

from test_scientific_revision_audit import fixture, build, report_for, validate
from test_design_scope import design_record
from watertreatment.audit_execution import scientific_scope_display
from watertreatment.audit_execution import prepare_citation_review, citation_review_issues, citation_review_prompt
from watertreatment.ownership_followup import build_followup_scope, POLICY as FOLLOWUP_POLICY
from watertreatment.domain import (DesignComparison, Experiment, Fact, MaterialSample, NamedFact, Observation,
    ObservationPair, ReferenceAssociation, ResearchObservationAssignment)
from watertreatment.reading import SourceAnchor
from watertreatment.scientific_revision_audit import NEW_SCOPE_LIMITS
from watertreatment.storage import digest_json, hash_file, write_new_json
from watertreatment.validation import validate_record


def ownership_case():
    before, _, baseline = fixture()
    template = design_record()
    before.schema_version, before.rule_version = template.schema_version, template.rule_version
    before.paper.id, before.design_scope = template.paper.id, template.design_scope
    before.experiments[1].id = before.observations[2].experiment_id = "other-experiment"
    before.materials[0].role = "primary_final"
    for observation in before.observations:
        observation.analysis_role = "condition_comparison"
    before.design_scope.observation_assignments = [ResearchObservationAssignment(observation_id=o.id, layer="core",
        purpose="condition_endpoint", reason="Distinct synthetic source condition.") for o in before.observations]
    index = before.design_scope.source_index[0]
    index.block_id, index.retained_observation_ids, index.quantitative_coverage_ids = "page1", ["changed"], []
    before.materials[0].evidence_ids = ["text"]
    for experiment in before.experiments:
        experiment.evidence_ids = ["text"]
    before.experiments[0].pairs = [ObservationPair(left_observation_id="changed",
        right_observation_id="sibling", evidence_ids=["text", "plot"])]
    before.comparisons = [
        DesignComparison(id="screen", observation_ids=["changed", "sibling"], evidence_ids=["text", "plot"]),
        DesignComparison(id="separate-screen", observation_ids=["sibling"], evidence_ids=["plot"]),
    ]
    before.reference_associations = [
        ReferenceAssociation(id="reused", target_kind="comparison", target_id="screen", observation_ids=["changed"],
            relationship="shared_control", applicability="reported", rationale="The source names this comparison.",
            evidence_ids=["text"]),
        ReferenceAssociation(id="other-use", target_kind="comparison", target_id="separate-screen", observation_ids=["changed"],
            relationship="shared_reference", applicability="reported", rationale="Same observed control, second context.",
            evidence_ids=["text", "plot"]),
    ]
    after = before.model_copy(deep=True)
    after.materials.append(MaterialSample(id="variant", name=Fact(value="Variant B", status="reported", evidence_ids=["plot"])))
    after.experiments.append(Experiment(id="separate-control-run", material_ids=["variant"], evidence_ids=["plot"]))
    after.observations[0].experiment_id = "separate-control-run"
    after.observations[0].material_id = "variant"
    after.observations.append(Observation(id="corrected-endpoint", experiment_id="separate-control-run", material_id="variant",
        value=Fact(value=0.21, status="estimated", evidence_ids=["plot"]), origin="plot_estimate",
        analysis_role="condition_comparison", evidence_ids=["plot"]))
    after.design_scope.observation_assignments.append(ResearchObservationAssignment(observation_id="corrected-endpoint",
        layer="core", purpose="condition_endpoint", reason="Separately anchored endpoint."))
    after.reference_associations[0].observation_ids = ["corrected-endpoint"]
    after.reference_associations[0].evidence_ids = ["plot", "text"]
    return before, after, baseline


def test_structural_review_traces_old_new_owners_pairs_and_all_uses_of_shared_control():
    old, new, baseline = ownership_case()
    assert not [issue for r in (old, new) for issue in validate_record(r) if issue.severity == "error"]
    frozen = [digest_json(r.model_dump(mode="json")) for r in (old, new, baseline)]
    scope = build(old, new, baseline, **NEW_SCOPE_LIMITS)
    affected = {r["record_path"] for r in scope["entity_refs"] if r["role"] == "affected"}
    assert {"materials[0]", "materials[1]", "experiments[0]", "experiments[2]", "observations[0]",
            "observations[1]", "observations[3]", "reference_associations[0]", "reference_associations[1]",
            "comparisons[0]", "comparisons[1]"} <= affected
    assert {"experiments[1]", "observations[2]"}.isdisjoint(affected)
    assert {r["old_value"]["observation_ids"][0] for r in scope["changes"] if r["kind"] == "control_relationship"} == {"changed"}
    assert scope["blocking_obligations"][0]["source_blocks"] == [["source", "page1"]]
    assert any(a.get("origin") == "prior_audit_blocker" and a["block_id"] == "page1" for a in scope["source_anchors"])
    assert build(old, new, baseline, **{**NEW_SCOPE_LIMITS, "compiler_version": 3}) is None
    review = report_for(scope, baseline)
    assert validate(scope, old, new, baseline, review) == []
    review.findings = [f for f in review.findings if f.record_path != "reference_associations[1]"]
    assert any("reference_associations[1]" in issue for issue in validate(scope, old, new, baseline, review))
    assert [digest_json(r.model_dump(mode="json")) for r in (old, new, baseline)] == frozen
    assert build(old, new, baseline, **NEW_SCOPE_LIMITS) == scope
    index = scientific_scope_display(scope)["disposition_source_index"]
    assert {p for group in index["groups"] for p in group["affected_paths"]} == affected


@pytest.mark.parametrize("mutation,reason", [
    (lambda r: setattr(r.observations[0].value, "value", 123), "protected_primary_outcome"),
    (lambda r: r.observations.reverse(), "unstable_entity_identity"),
    (lambda r: r.experiments.pop(1), "unstable_entity_identity"),
    (lambda r: setattr(r.coverage[0], "visual_checked", False), "protected_record_or_sources"),
    (lambda r: setattr(r.evidence[0], "block_id", "page2"), "existing_evidence_changed"),
    (lambda r: setattr(r.observations[-1].value, "evidence_ids", []), "unanchored_new_value"),
    (lambda r: setattr(r.reference_associations[0], "target_id", "unregistered"), "invalid_structural_candidate"),
])
def test_structural_scope_declines_unsupported_edits_without_whole_review(mutation, reason):
    old, new, baseline = ownership_case()
    mutation(new)
    why = []
    assert build(old, new, baseline, diagnostics=why, **NEW_SCOPE_LIMITS) is None
    assert why[0]["code"] == reason and not why[0]["automatic_full_review_allowed"]


def test_blocker_preserves_historical_index_when_condition_replaced_and_requires_its_source():
    old, new, baseline = ownership_case()
    old.experiments[0].conditions = [NamedFact(name="unknown first"), NamedFact(name="second")]
    new.experiments[0].conditions = [NamedFact(name="unrelated entry"), NamedFact(name="unknown first")]
    baseline.findings[0].record_path = "experiments[0].conditions[0].fact"
    baseline.findings[0].evidence.append(SourceAnchor(source_id="source", block_id="page3"))
    scope = build(old, new, baseline, **NEW_SCOPE_LIMITS)
    blocker = scope["blocking_obligations"][0]
    assert blocker["historical_paths"] == ["experiments[0].conditions[0].fact"]
    assert blocker["paths"] == ["experiments[0].conditions"] and ["source", "page3"] in blocker["source_blocks"]
    baseline.potential_omissions.append("An unlocated missing endpoint")
    assert build(old, new, baseline, **NEW_SCOPE_LIMITS) is None


def test_only_new_experiment_and_reference_includes_the_existing_control_and_its_comparisons():
    old, _, baseline = ownership_case()
    new = old.model_copy(deep=True)
    new.experiments.append(Experiment(id="independent-screen", material_ids=["material"], evidence_ids=["plot"]))
    new.reference_associations.append(ReferenceAssociation(id="cross-run", target_kind="experiment",
        target_id="independent-screen", observation_ids=["changed"], relationship="shared_reference",
        applicability="inferred", evidence_ids=["text", "plot"], rationale="Two explicit source locations.",
        limitations=["Physical run identity is unresolved."]))
    scope = build(old, new, baseline, **NEW_SCOPE_LIMITS)
    paths = {r["record_path"] for r in scope["entity_refs"] if r["role"] == "affected"}
    assert {"experiments[2]", "observations[0]", "reference_associations[0]", "reference_associations[1]",
            "reference_associations[2]", "comparisons[0]", "comparisons[1]"} <= paths
    assert "observations[2]" not in paths


def test_new_material_with_equal_name_stays_a_distinct_variant_and_requires_its_own_source():
    old, _, baseline = ownership_case()
    old.materials[0].name = Fact(value="same printed formula", status="reported", evidence_ids=["text"])
    new = old.model_copy(deep=True)
    new.materials.append(MaterialSample(id="separate-sample", name=Fact(value="same printed formula", status="reported",
                                                                         evidence_ids=["other"])))
    scope = build(old, new, baseline, **NEW_SCOPE_LIMITS)
    refs = {r["record_path"]:r for r in scope["entity_refs"]}
    assert refs["materials[1]"]["entity_id"] == "separate-sample"
    assert refs["materials[1]"]["required_evidence_ids"] == ["other"]
    assert "experiments[1]" not in refs and "observations[2]" not in refs
    new.materials[-1].name.evidence_ids = []
    why = []
    assert build(old, new, baseline, diagnostics=why, **NEW_SCOPE_LIMITS) is None
    assert why[0]["code"] == "unanchored_structural_entity"


def followup_case():
    root, before, full = ownership_case()
    prior_scope = build(root, before, full, **NEW_SCOPE_LIMITS)
    prior = report_for(prior_scope, full)
    reason = next(f for f in prior.findings if f.record_path == "observations[0]")
    reason.verdict, reason.needs_revision = "unsupported", True
    reason.explanation = "Separate a source-method qualifier from its exact application."
    after = before.model_copy(deep=True)
    after.observations[0].reference.note = "Exact source-specific application remains unresolved."
    after.experiments[0].pairs[0].unresolved_conditions = ["The source leaves the denominator unresolved."]
    return root, full, before, after, prior, prior_scope


def test_followup_inherits_completed_review_and_checks_actual_owners_and_comparison_consumers(tmp_path):
    root, full, before, after, prior, prior_scope = followup_case()
    for name, obj in (("candidate.v00.json", root), ("audit.v00.json", full), ("candidate.v01.json", before),
                      ("audit.v01.json", prior), ("candidate.v02.json", after)):
        write_new_json(tmp_path/name, obj.model_dump(mode="json"))
    write_new_json(tmp_path/"citation_scope.v01.json", prior_scope)
    history = [{"version":i,"validation_errors":0,"reading_check_issues":[],
        "candidate_sha256":hash_file(tmp_path/f"candidate.v{i:02d}.json"),"audit_origin":origin}
        for i,origin in enumerate(["fresh_model_audit","scientific_delta_model_audit"])]
    plan = {"scientific_scope_limits":dict(NEW_SCOPE_LIMITS), "structural_followup_policy":FOLLOWUP_POLICY}
    bound = prepare_citation_review(tmp_path, 2, after, history, plan)
    assert bound is not None
    _, current, _ = bound
    assert current["structural_followup"] == FOLLOWUP_POLICY
    assert current["previous_scope_sha256"] == prior.review_scope_sha256
    assert len(current["changes"]) == 2 and len(current["blocking_obligations"]) == 1
    paths = {r["record_path"] for r in current["entity_refs"] if r["role"] == "affected"}
    assert {"observations[0]","experiments[0]","comparisons[0]","reference_associations[1]"} <= paths
    assert "observations[3]" not in paths
    report = report_for(current, prior)
    assert citation_review_issues(bound, report) == []
    assert "TARGETED FOLLOW-UP" in citation_review_prompt(bound,after)
    report.findings = [f for f in report.findings if f.record_path != "experiments[0]"]
    assert citation_review_issues(bound, report)


def test_followup_requires_the_authenticated_scope_closed_dispositions_and_stable_scientific_identities():
    root, full, before, after, prior, scope = followup_case()
    assert build_followup_scope(root, full, before, after, prior, {**scope,"new_record_hash":"changed"}) is None
    dropped = prior.model_copy(deep=True)
    dropped.findings = [f for f in dropped.findings if f.record_path != "reference_associations[1]"]
    assert build_followup_scope(root, full, before, after, dropped, scope) is None
    after.observations[0].value.value = 77
    assert build_followup_scope(root, full, before, after, prior, scope) is None
