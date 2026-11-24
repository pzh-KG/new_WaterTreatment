"""Versioned, source-bound evidence additions and temporal semantic repairs.

Synthetic identifiers and claims test compiler boundaries, not scientific truth.
"""
import copy

import pytest

from watertreatment.domain import (Configuration, CoverageEntry, DesignComparison, EvidenceRef, Experiment,
    ExtractionRecord, Fact, MaterialPlacement, MaterialSample, MaterialState, NamedFact, Observation, ObservationPair, Paper,
    ReactionInput, ReferenceAssociation, ResearchDesignScope, ResearchEligibility, ResearchModuleActivation,
    ResearchObservationAssignment, Substance, Target, TimeWindow)
from watertreatment.reading import AUDIT_DIMENSIONS, AuditFinding, ReadingAudit, SourceAnchor
from watertreatment.scientific_revision_audit import (
    NEW_SCOPE_LIMITS, build_scientific_revision_scope, validate_scientific_revision_audit)
from test_scientific_revision_audit import report_for


def case():
    window = TimeWindow(kind="duration", start=Fact(value=0, status="reported", evidence_ids=["original-A"]),
        end=Fact(value=40, status="reported", evidence_ids=["original-A"]),
        unit=Fact(value="min", status="reported", evidence_ids=["original-A"]),
        reference=Fact(value="after irradiation", status="reported", evidence_ids=["original-A"]))
    old = ExtractionRecord(schema_version="0.3.0", rule_version="0.8", extraction_scope="research_core",
        paper=Paper(id="synthetic-paper-k", source_ids=["arbitrary-source", "appendix-id-52"]),
        materials=[MaterialSample(id="final-z"), MaterialSample(id="control-q")],
        experiments=[Experiment(id="joint-x", material_ids=["final-z"],
            conditions=[NamedFact(name="synthetic-condition", fact=Fact(status="not_reported", evidence_ids=["original-A"]))],
            time_window=window.model_copy(deep=True), evidence_ids=["original-A"],
            pairs=[ObservationPair(left_observation_id="obs-flux", right_observation_id="obs-removal")]),
            Experiment(id="unrelated-exp", material_ids=["control-q"])],
        observations=[Observation(id="obs-flux", experiment_id="joint-x", material_id="final-z",
            value=Fact(value=3.2, status="reported", evidence_ids=["original-A"]),
            time_window=window.model_copy(deep=True), evidence_ids=["original-A"]),
            Observation(id="obs-removal", experiment_id="joint-x", material_id="final-z",
                value=Fact(value=80, status="reported", evidence_ids=["original-B"]), evidence_ids=["original-B"]),
            Observation(id="obs-unrelated", experiment_id="unrelated-exp", material_id="control-q",
                value=Fact(value=1.2, status="reported", evidence_ids=["original-B"]), evidence_ids=["original-B"])],
        comparisons=[DesignComparison(id="paired-comparison", observation_ids=["obs-flux", "obs-removal"],
            outcomes=[NamedFact(name="interpreted-outcome", fact=Fact(value="retained description", status="reported",
                evidence_ids=["original-A"]))], evidence_ids=["original-A"])],
        evidence=[EvidenceRef(id="original-A", source_id="arbitrary-source", block_id="block-alpha", quote="Original method"),
            EvidenceRef(id="original-B", source_id="appendix-id-52", block_id="method-beta", quote="Original endpoint")],
        coverage=[CoverageEntry(source_id="arbitrary-source", block_ids=["block-alpha", "new-method-anchor"],
            state="read_structured", text_read=True),
            CoverageEntry(source_id="appendix-id-52", block_ids=["method-beta", "caption-gamma"],
                state="read_structured", text_read=True)])
    baseline = ReadingAudit(findings=[AuditFinding(record_path="observations[0].value", severity="major",
        verdict="unsupported", needs_revision=True, explanation="A synthetic source attribution needs repair.",
        evidence=[SourceAnchor(source_id="arbitrary-source", block_id="block-alpha")])],
        potential_omissions=[], checked_dimensions=list(AUDIT_DIMENSIONS),
        coverage_limitations=["Synthetic retained source limitation."])
    new = old.model_copy(deep=True)
    new.evidence.append(EvidenceRef(id="new-reference:91", source_id="appendix-id-52", block_id="caption-gamma",
        quote="Synthetic newly located support", locator={"section": "Supplementary method"}))
    new.observations[0].value.evidence_ids.append("new-reference:91")
    return old, new, baseline


def build(old, new, baseline, **overrides):
    return build_scientific_revision_scope(old, new, baseline, baseline_origin="fresh_model_audit",
        **{**NEW_SCOPE_LIMITS, "compiler_version": 3, **overrides})


def validate(scope, old, new, baseline, audit):
    return validate_scientific_revision_audit(scope, baseline, audit, old_record=old, new_record=new)


def test_new_evidence_and_revised_consumer_each_require_original_source_review():
    old, new, baseline = case()
    frozen = [x.model_dump(mode="json") for x in (old, new, baseline)]
    scope = build(old, new, baseline)
    assert scope and scope["scope_limits"]["compiler_version"] == 3
    addition = next(c for c in scope["changes"] if c["kind"] == "evidence_addition")
    assert addition["changed_path"] == "evidence[2]" and addition["old_value"] is None
    assert addition["new_value"] == new.evidence[2].model_dump(mode="json")
    refs = {x["record_path"]: x for x in scope["entity_refs"]}
    assert refs["evidence[2]"]["role"] == refs["observations[0]"]["role"] == "affected"
    assert "observations[2]" not in refs
    anchors = {(a["source_id"], a["block_id"]) for a in scope["source_anchors"]}
    assert {("arbitrary-source", "block-alpha"), ("appendix-id-52", "caption-gamma")} <= anchors
    report = report_for(scope, baseline)
    assert validate(scope, old, new, baseline, report) == []
    report.findings = [f for f in report.findings if f.record_path != "evidence[2]"]
    assert any("evidence[2]" in issue for issue in validate(scope, old, new, baseline, report))
    assert frozen == [x.model_dump(mode="json") for x in (old, new, baseline)]


@pytest.mark.parametrize("consumer", ["fact", "observation_references", "evidence_link", "condition", "comparison_fact", "comparison_references", "reference_association"])
def test_appended_evidence_must_have_a_typed_changed_consumer(consumer):
    old, new, baseline = case()
    new.observations[0].value.evidence_ids = ["original-A"]
    identifier = new.evidence[-1].id
    if consumer == "fact":
        new.observations[0].value.evidence_ids.append(identifier)
    elif consumer == "observation_references":
        new.observations[0].evidence_ids.append(identifier)
    elif consumer == "evidence_link":
        # Assignment through a parsed record preserves the typed evidence-link schema.
        data = new.model_dump(mode="json")
        data["observations"][0]["evidence_links"] = [{"evidence_id": identifier,
            "supported_fields": ["value"], "support_type": "direct", "explanation": "Synthetic new endpoint support"}]
        new = ExtractionRecord.model_validate(data)
    elif consumer == "condition":
        new.experiments[0].conditions[0].fact.evidence_ids.append(identifier)
    elif consumer == "comparison_fact":
        new.comparisons[0].outcomes[0].fact.evidence_ids.append(identifier)
    elif consumer == "comparison_references":
        new.comparisons[0].evidence_ids.append(identifier)
    else:
        old.schema_version = new.schema_version = "0.4.0"
        new.reference_associations.append(ReferenceAssociation(id="reference-new", target_kind="experiment",
            target_id="joint-x", observation_ids=["obs-unrelated"], relationship="contextual_comparison",
            applicability="reported", evidence_ids=["original-A", "original-B", identifier],
            rationale="An explicitly source-linked contextual comparison.",
            unresolved_conditions=["No matched-run or independent-maxima pairing is established."],
            limitations=["Contextual comparison only; conditions must not be inherited."]))
    diagnostics = []
    scope = build(old, new, baseline, diagnostics=diagnostics)
    assert scope, diagnostics
    assert validate(scope, old, new, baseline, report_for(scope, baseline)) == []


@pytest.mark.parametrize("mutation,code", [
    ("orphan", "unused_evidence_addition"), ("prose_reference", "unused_evidence_addition"),
    ("foreign_source", "unregistered_evidence_source"), ("crossed_block", "unregistered_evidence_source"),
    ("unknown_block", "unregistered_evidence_source"), ("duplicate_id", "duplicate_evidence_identity"),
    ("alias_old_definition", "aliased_evidence_definition"),
    ("old_id_changed", "existing_evidence_changed"), ("old_anchor_changed", "existing_evidence_changed"),
    ("old_quote_changed", "existing_evidence_changed"), ("old_locator_changed", "existing_evidence_changed"),
    ("old_reordered", "existing_evidence_changed"), ("old_deleted", "existing_evidence_changed"),
    ("old_dangling_reference", "preexisting_evidence_identity")])
def test_evidence_addition_cannot_rewrite_or_invent_source_authority(mutation, code):
    old, new, baseline = case()
    if mutation in {"orphan", "prose_reference"}:
        new.observations[0].value.evidence_ids = ["original-A"]
        if mutation == "prose_reference":
            new.observations[0].limitations.append("Please consider new-reference:91")
    elif mutation == "foreign_source": new.evidence[-1].source_id = "another-paper"
    elif mutation == "crossed_block": new.evidence[-1].block_id = "block-alpha"
    elif mutation == "unknown_block": new.evidence[-1].block_id = "unregistered-block"
    elif mutation == "duplicate_id": new.evidence[-1].id = old.evidence[0].id
    elif mutation == "alias_old_definition":
        new.evidence[-1] = old.evidence[0].model_copy(update={"id": "new-reference:91"})
    elif mutation == "old_id_changed": new.evidence[0].id = "changed-old-id"
    elif mutation == "old_anchor_changed": new.evidence[0].block_id = "new-method-anchor"
    elif mutation == "old_quote_changed": new.evidence[0].quote = "Changed old text"
    elif mutation == "old_locator_changed": new.evidence[0].locator.physical_page = 7
    elif mutation == "old_reordered": new.evidence[0], new.evidence[1] = new.evidence[1], new.evidence[0]
    elif mutation == "old_deleted": new.evidence.pop(0)
    else: old.observations[0].value.evidence_ids.append("new-reference:91")
    diagnostics = []
    assert build(old, new, baseline, diagnostics=diagnostics) is None
    assert diagnostics[0]["code"] == code


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_compilers_reject_new_evidence_and_retain_entity_acceptance_boundary(version):
    old, new, baseline = case()
    assert build(old, new, baseline, compiler_version=version) is None
    unchanged_members = old.model_copy(deep=True)
    unchanged_members.observations[0].value.status = "inferred"
    scope = build(old, unchanged_members, baseline, compiler_version=version)
    assert scope and all(not ref["record_path"].startswith("evidence[") for ref in scope["entity_refs"])
    report = report_for(scope, baseline)
    assert validate(scope, old, unchanged_members, baseline, report) == []
    report.findings.append(AuditFinding(record_path="evidence[0]", severity="minor", verdict="supported",
        needs_revision=False, explanation="This path was not an admissible legacy scientific entity.",
        evidence=[SourceAnchor(source_id="arbitrary-source", block_id="block-alpha")]))
    assert any("non-entity path" in issue for issue in validate(scope, old, unchanged_members, baseline, report))
    assert build_scientific_revision_scope(old, unchanged_members, baseline,
        baseline_origin="fresh_model_audit", **scope["scope_limits"]) == scope


@pytest.mark.parametrize("owner", ["observations", "experiments"])
def test_time_kind_change_freezes_the_entire_window_and_pairing_dependencies(owner):
    old, _, baseline = case()
    new = old.model_copy(deep=True)
    getattr(new, owner)[0].time_window.kind = "elapsed_interval"
    scope = build(old, new, baseline)
    assert scope
    change = next(c for c in scope["changes"] if c["kind"] == "time_window_semantics")
    assert change["changed_path"] == owner + "[0].time_window"
    assert change["old_value"] == getattr(old, owner)[0].time_window.model_dump(mode="json")
    assert change["new_value"] == getattr(new, owner)[0].time_window.model_dump(mode="json")
    refs = {x["record_path"]: x["role"] for x in scope["entity_refs"]}
    assert refs[owner + "[0]"] == "affected"
    assert {"experiments[0]", "observations[0]", "observations[1]", "comparisons[0]"} <= set(refs)
    assert "observations[2]" not in refs
    assert validate(scope, old, new, baseline, report_for(scope, baseline)) == []
    for version in (1, 2):
        assert build(old, new, baseline, compiler_version=version) is None


def test_time_semantics_repair_cannot_hide_removed_alternatives():
    old, _, baseline = case()
    old.observations[0].time_window.end.alternatives = [{"value": 45, "evidence_ids": ["original-A"]}]
    new = old.model_copy(deep=True)
    new.observations[0].time_window.kind = "interval"
    new.observations[0].time_window.end.alternatives = []
    assert build(old, new, baseline) is None


def test_v3_preserves_v2_typed_condition_and_time_value_repair_with_complete_context():
    old, _, baseline = case()
    new = old.model_copy(deep=True)
    new.experiments[0].conditions = [NamedFact(name="categorical-temperature",
        fact=Fact(value="ambient", status="reported", evidence_ids=["original-A"]))]
    new.observations[0].time_window.end.value = 45
    for version in (2, 3):
        scope = build(old, new, baseline, compiler_version=version)
        assert scope
        assert {"scientific_membership", "scientific_condition"} <= {c["kind"] for c in scope["changes"]}
        assert validate(scope, old, new, baseline, report_for(scope, baseline)) == []


@pytest.mark.parametrize("mutation", ["primary_value", "normalized_primary", "ownership", "material_identity", "source_coverage", "source_membership"])
def test_new_evidence_does_not_unlock_protected_scientific_changes(mutation):
    old, new, baseline = case()
    if mutation == "primary_value": new.observations[0].value.value = 3.9
    elif mutation == "normalized_primary": new.observations[0].value.normalized = 3.9
    elif mutation == "ownership": new.observations[0].experiment_id = "unrelated-exp"
    elif mutation == "material_identity": new.materials[0].id = "other-material"
    elif mutation == "source_coverage": new.coverage[0].block_ids.append("invented-block")
    else: new.paper.source_ids.append("external-paper")
    assert build(old, new, baseline) is None


def test_additions_are_charged_to_change_and_entity_limits():
    old, new, baseline = case()
    diagnostics = []
    assert build(old, new, baseline, max_changes=1, diagnostics=diagnostics) is None
    assert diagnostics[0]["code"] == "change_budget"
    diagnostics = []
    assert build(old, new, baseline, max_entities=1, diagnostics=diagnostics) is None
    assert diagnostics[0]["code"] == "entity_budget"


def test_new_anchor_and_original_blocker_cannot_be_omitted_from_the_final_audit():
    old, new, baseline = case()
    scope = build(old, new, baseline)
    report = report_for(scope, baseline)
    for finding in report.findings:
        finding.evidence = [a for a in finding.evidence if a.block_id != "caption-gamma"]
    assert validate(scope, old, new, baseline, report)
    report = report_for(scope, baseline)
    for finding in report.findings:
        finding.evidence = [a for a in finding.evidence if a.block_id != "block-alpha"]
    assert validate(scope, old, new, baseline, report)
    edited_scope = copy.deepcopy(scope)
    edited_scope["source_anchors"] = [a for a in scope["source_anchors"] if a["evidence_id"] != "new-reference:91"]
    assert validate(edited_scope, old, new, baseline, report_for(edited_scope, baseline))


def eligibility_case():
    old, _, baseline = case()
    old.design_scope = ResearchDesignScope(eligibility=[ResearchEligibility(
        use="condition_comparison", target_kind="experiment", target_id="joint-x", status="limited",
        reason="A retained synthetic condition limitation.", evidence_ids=["original-A"],
        limitations=["Common timing has not established identical independent runs."])])
    new = old.model_copy(deep=True)
    new.design_scope.eligibility.append(ResearchEligibility(
        use="performance_pairing", target_kind="experiment", target_id="joint-x", status="limited",
        reason="The revised timing interpretation restricts matched performance use.", evidence_ids=["original-B"],
        limitations=["This source does not establish a common independently timed calculation window."]))
    return old, new, baseline


def test_appended_research_limitation_requires_subject_consumers_and_its_own_source_review():
    old, new, baseline = eligibility_case()
    scope = build(old, new, baseline)
    assert scope
    change = next(c for c in scope["changes"] if c["kind"] == "eligibility_restriction")
    assert change["changed_path"] == "design_scope.eligibility[1]" and change["old_value"] is None
    refs = {r["record_path"]: r for r in scope["entity_refs"]}
    for path in ("design_scope.eligibility[1]", "experiments[0]", "observations[0]", "observations[1]", "comparisons[0]"):
        assert refs[path]["role"] == "affected"
    assert "observations[2]" not in refs
    report = report_for(scope, baseline)
    assert validate(scope, old, new, baseline, report) == []
    report.findings = [f for f in report.findings if f.record_path != "design_scope.eligibility[1]"]
    assert any("design_scope.eligibility[1]" in issue for issue in validate(scope, old, new, baseline, report))
    for version in (1, 2):
        assert build(old, new, baseline, compiler_version=version) is None


def test_new_evidence_can_support_a_restrictive_typed_eligibility_claim():
    old, new, baseline = eligibility_case()
    new.evidence.append(EvidenceRef(id="restriction-source", source_id="appendix-id-52", block_id="caption-gamma"))
    new.design_scope.eligibility[-1].evidence_ids = ["restriction-source"]
    scope = build(old, new, baseline)
    assert scope and {"evidence_addition", "eligibility_restriction"} <= {c["kind"] for c in scope["changes"]}
    assert validate(scope, old, new, baseline, report_for(scope, baseline)) == []


@pytest.mark.parametrize("mutation", ["eligible", "not_assessed", "not_applicable", "blank_reason", "no_limitations",
    "blank_limitation", "no_source", "unknown_source", "duplicate_binding", "unknown_target", "wrong_target_kind",
    "foreign_paper", "old_binding", "reorder", "delete", "remove_old_limit", "promote_old_eligibility"])
def test_eligibility_additions_cannot_promote_replace_or_fabricate_scientific_ownership(mutation):
    old, new, baseline = eligibility_case()
    item = new.design_scope.eligibility[-1]
    if mutation in {"eligible", "not_assessed", "not_applicable"}: item.status = mutation
    elif mutation == "blank_reason": item.reason = " "
    elif mutation == "no_limitations": item.limitations = []
    elif mutation == "blank_limitation": item.limitations = [" "]
    elif mutation == "no_source": item.evidence_ids = []
    elif mutation == "unknown_source": item.evidence_ids = ["unregistered-evidence"]
    elif mutation == "duplicate_binding": item.use = "condition_comparison"
    elif mutation == "unknown_target": item.target_id = "unregistered-experiment"
    elif mutation == "wrong_target_kind": item.target_kind = "material"
    elif mutation == "foreign_paper": item.target_kind = "paper"; item.target_id = "different-paper"
    elif mutation == "old_binding": new.design_scope.eligibility[0].target_id = "unrelated-exp"
    elif mutation == "reorder": new.design_scope.eligibility.reverse()
    elif mutation == "delete": new.design_scope.eligibility.pop(0)
    elif mutation == "remove_old_limit": new.design_scope.eligibility[0].limitations = []
    else: new.design_scope.eligibility[0].status = "eligible"
    assert build(old, new, baseline) is None


def test_restrictive_eligibility_may_bind_only_the_same_registered_paper():
    old, new, baseline = eligibility_case()
    new.design_scope.eligibility[-1].target_kind = "paper"
    new.design_scope.eligibility[-1].target_id = old.paper.id
    scope = build(old, new, baseline)
    assert scope and validate(scope, old, new, baseline, report_for(scope, baseline)) == []


def citation_fanout_case():
    old, _, baseline = case()
    old.substances = [Substance(id="reused-solute", formula=Fact(value="synthetic-formula", status="reported",
        evidence_ids=["original-A"]), evidence_ids=["original-A"])]
    old.experiments = [Experiment(id=f"condition-{i}", material_ids=["final-z"],
        targets=[Target(substance_id="reused-solute", unit=Fact(value="mg/L", status="reported", evidence_ids=["original-A"]))],
        evidence_ids=["original-A"]) for i in range(12)]
    old.observations = [Observation(id=f"endpoint-{i}", experiment_id=f"condition-{i}", substance_id="reused-solute",
        material_id="final-z", value=Fact(value=70+i, status="reported", evidence_ids=["original-B"]),
        evidence_ids=["original-B"]) for i in range(12)]
    old.comparisons = [DesignComparison(id="untouched-comparison", observation_ids=[f"endpoint-{i}" for i in range(12)],
        evidence_ids=["original-B"])]
    baseline.findings = [AuditFinding(record_path="substances[0].formula.evidence_ids", severity="major",
        verdict="unsupported", needs_revision=True, explanation="Only the citation for the unchanged formula needs repair.",
        evidence=[SourceAnchor(source_id="arbitrary-source", block_id="block-alpha")])]
    new = old.model_copy(deep=True)
    new.evidence.append(EvidenceRef(id="fresh-formula-reference", source_id="appendix-id-52", block_id="caption-gamma"))
    new.substances[0].formula.evidence_ids = ["fresh-formula-reference"]
    new.substances[0].evidence_ids.append("fresh-formula-reference")
    return old, new, baseline


def test_citation_only_repair_retains_twelve_direct_consumers_without_semantic_fanout():
    old, new, baseline = citation_fanout_case()
    scope = build(old, new, baseline)
    assert scope
    refs = {r["record_path"]: r for r in scope["entity_refs"]}
    assert {p for p, r in refs.items() if r["role"] == "affected"} == {"substances[0]", "evidence[2]"}
    for i in range(12):
        assert refs[f"experiments[{i}]"]["role"] == refs[f"observations[{i}]"]["role"] == "context"
        assert refs[f"observations[{i}]"]["old_hash"] == refs[f"observations[{i}]"]["new_hash"]
    assert "comparisons[0]" not in refs
    assert scope["blocking_obligations"][0]["paths"] == ["substances[0].formula.evidence_ids"]
    report = report_for(scope, baseline)
    assert validate(scope, old, new, baseline, report) == []
    report.findings = [f for f in report.findings if not f.record_path.startswith("substances[0]")]
    assert validate(scope, old, new, baseline, report)


@pytest.mark.parametrize("blocker", ["substances[0]", "substances[0].formula", "substances[0].formula.value",
    "substances[0].formula.status", "substances[0].name"])
def test_existing_scientific_blocker_defeats_citation_local_boundary(blocker):
    old, new, baseline = citation_fanout_case()
    baseline.findings[0].record_path = blocker
    scope = build(old, new, baseline)
    assert scope
    refs = {r["record_path"]: r["role"] for r in scope["entity_refs"]}
    for i in range(12):
        assert refs[f"experiments[{i}]"] == refs[f"observations[{i}]"] == "affected"


@pytest.mark.parametrize("field,value", [("status", "inferred"), ("raw", "new source phrasing"),
    ("note", "Applicability is inferred."), ("normalization_reason", "A different interpretation."),
    ("alternatives", [{"value": "alternative-formula", "evidence_ids": ["original-B"]}])])
def test_any_non_citation_fact_interpretation_keeps_semantic_propagation(field, value):
    old, new, baseline = citation_fanout_case()
    setattr(new.substances[0].formula, field, value)
    scope = build(old, new, baseline)
    assert scope
    refs = {r["record_path"]: r["role"] for r in scope["entity_refs"]}
    assert all(refs[f"observations[{i}]"] == "affected" for i in range(12))


@pytest.mark.parametrize("field", ["value", "normalized"])
def test_citation_only_boundary_does_not_unlock_formula_or_primary_quantity_changes(field):
    old, new, baseline = citation_fanout_case()
    setattr(new.substances[0].formula, field, "different-formula")
    assert build(old, new, baseline) is None


def test_other_experiment_unit_repair_keeps_true_dependencies_affected():
    old, new, baseline = citation_fanout_case()
    new.experiments[3].targets[0].unit.value = "g/L"
    scope = build(old, new, baseline)
    assert scope
    refs = {r["record_path"]: r["role"] for r in scope["entity_refs"]}
    assert refs["experiments[3]"] == refs["observations[3]"] == refs["comparisons[0]"] == "affected"
    assert refs["observations[4]"] == "context"


def test_real_semantic_dependency_overrides_an_owner_with_only_citation_edits():
    old, new, baseline = citation_fanout_case()
    additional = Substance(id="second-input", formula=Fact(value="second-formula", status="reported", evidence_ids=["original-A"]))
    old.substances.append(additional)
    old.experiments[0].reaction_inputs = [ReactionInput(substance_id="second-input", role="oxidant")]
    new.substances.append(additional.model_copy(deep=True))
    new.experiments[0].reaction_inputs = [x.model_copy(deep=True) for x in old.experiments[0].reaction_inputs]
    new.substances[1].formula.status = "inferred"
    new.experiments[0].evidence_ids.append("original-B")
    scope = build(old, new, baseline)
    assert scope
    refs = {r["record_path"]: r["role"] for r in scope["entity_refs"]}
    assert refs["substances[1]"] == refs["experiments[0]"] == refs["observations[0]"] == "affected"
    assert refs["observations[1]"] == "context"


def test_legacy_v2_retains_original_citation_dependency_propagation():
    old, new, baseline = citation_fanout_case()
    new.evidence.pop()
    new.substances[0].formula.evidence_ids = ["original-B"]
    new.substances[0].evidence_ids = ["original-A", "original-B"]
    scope = build(old, new, baseline, compiler_version=2)
    assert scope
    refs = {r["record_path"]: r["role"] for r in scope["entity_refs"]}
    assert all(refs[f"observations[{i}]"] == "affected" for i in range(12))


@pytest.mark.parametrize("owner", ["material", "experiment", "module", "configuration"])
def test_citation_context_preserves_configuration_placements_states_and_module_references(owner):
    old, _, baseline = case()
    old.materials[0].name = Fact(value="unchanged-final-variant", status="reported", evidence_ids=["original-A"])
    old.configurations = [Configuration(id="configured-system", placements=[MaterialPlacement(material_id="final-z")],
        evidence_ids=["original-A"])]
    old.experiments[0].configuration_id = "configured-system"
    old.experiments[0].material_states = [MaterialState(material_id="final-z")]
    old.design_scope = ResearchDesignScope(modules=[ResearchModuleActivation(id="contextual-module",
        kind="outdoor_application", reason="A synthetic source-supported application.", material_ids=["final-z"],
        experiment_ids=["joint-x"], evidence_ids=["original-A"])],
        observation_assignments=[ResearchObservationAssignment(observation_id="obs-flux", layer="module",
            purpose="application_summary", reason="A synthetic module endpoint.", module_id="contextual-module")])
    new = old.model_copy(deep=True)
    paths = {"material": "materials[0].name.evidence_ids", "experiment": "experiments[0].evidence_ids",
             "module": "design_scope.modules[0].evidence_ids", "configuration": "configurations[0].evidence_ids"}
    baseline.findings[0].record_path = paths[owner]
    if owner == "material": new.materials[0].name.evidence_ids = ["original-B"]
    elif owner == "experiment": new.experiments[0].evidence_ids = ["original-B"]
    elif owner == "module": new.design_scope.modules[0].evidence_ids = ["original-B"]
    else: new.configurations[0].evidence_ids = ["original-B"]
    scope = build(old, new, baseline)
    assert scope
    refs = {r["record_path"]: r["role"] for r in scope["entity_refs"]}
    target = {"material": "materials[0]", "experiment": "experiments[0]",
              "module": "design_scope.modules[0]", "configuration": "configurations[0]"}[owner]
    assert refs[target] == "affected"
    if owner in {"material", "experiment"}: assert refs["design_scope.modules[0]"] == "context"
    if owner == "material": assert refs["configurations[0]"] == "context"
    if owner in {"module", "configuration"}:
        assert refs["materials[0]"] == refs["experiments[0]"] == "context"
    if owner == "module": assert refs["design_scope.observation_assignments[0]"] == "context"
    assert validate(scope, old, new, baseline, report_for(scope, baseline)) == []
