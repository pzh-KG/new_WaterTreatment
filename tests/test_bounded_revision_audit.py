"""Synthetic dependency/source checks, not provider calls or scientific gold."""
from __future__ import annotations

import copy

import pytest

from watertreatment.audit_execution import citation_review_issues, citation_review_prompt
from watertreatment.bounded_revision_audit import (
    POLICY, build_revision_audit_scope, finding_paths, validate_revision_audit,
)
from watertreatment.domain import (
    DesignComparison, EvidenceRef, Experiment, ExtractionRecord, Fact, MaterialSample,
    NamedFact, Observation, ObservationEvidenceLink, ObservationPair, Paper, QuantitativeCoverage,
)
from watertreatment.reading import AUDIT_DIMENSIONS, AuditFinding, ReadingAudit, SourceAnchor
from watertreatment.storage import digest_json


def fixture():
    old = ExtractionRecord(schema_version="0.3.0", rule_version="0.7", extraction_scope="research_core",
        paper=Paper(id="synthetic", source_ids=["source"]),
        materials=[MaterialSample(id="material"), MaterialSample(id="other-material")],
        experiments=[Experiment(id="experiment", material_ids=["material"]), Experiment(id="other")],
        observations=[Observation(id="changed", experiment_id="experiment", material_id="material",
            value=Fact(value=5.5, status="reported", evidence_ids=["text"]), evidence_ids=["text"],
            coordinates=[NamedFact(name="clock_time", fact=Fact(value="14:00", status="reported", evidence_ids=["text"]))],
            evidence_links=[ObservationEvidenceLink(evidence_id="text", supported_fields=["value", "coordinates"],
                explanation="Printed result and time.")]),
            Observation(id="sibling", experiment_id="experiment", material_id="material",
                value=Fact(value=2.3, status="reported", evidence_ids=["plot"])),
            Observation(id="unrelated", experiment_id="other", material_id="material",
                value=Fact(value=9.1, status="reported", evidence_ids=["other"]))],
        evidence=[EvidenceRef(id="text", source_id="source", block_id="page1"),
                  EvidenceRef(id="plot", source_id="source", block_id="page2"),
                  EvidenceRef(id="other", source_id="source", block_id="page3")])
    new = old.model_copy(deep=True)
    fact = new.observations[0].coordinates[0].fact
    fact.status, fact.evidence_ids = "conflict", ["text", "plot"]
    fact.alternatives = [{"value": "14:00", "evidence_ids": ["text"]},
                         {"value": "13:00", "evidence_ids": ["plot"]}]
    # Revalidate dictionaries assigned above through the public model boundary.
    new = ExtractionRecord.model_validate(new.model_dump(mode="json"))
    new.observations[0].evidence_links[0].supported_fields = ["value"]
    new.observations[0].evidence_links[0].explanation = "Text does not resolve the conflicting plotted time."
    baseline = ReadingAudit(findings=[AuditFinding(record_path="observations[0].coordinates[0].fact", severity="major",
        verdict="unsupported", needs_revision=True, explanation="Preserve the conflicting time source.",
        evidence=[SourceAnchor(source_id="source", block_id="page1"), SourceAnchor(source_id="source", block_id="page2")])],
        potential_omissions=[], coverage_limitations=["Unknown source resolution remains."],
        checked_dimensions=list(AUDIT_DIMENSIONS))
    return old, new, baseline


def build(old, new, baseline, **kwargs):
    return build_revision_audit_scope(old, new, baseline, baseline_origin="fresh_model_audit", **kwargs)


def final_report(scope, baseline):
    report = baseline.model_copy(deep=True)
    report.review_scope, report.review_scope_sha256 = "citation_delta", scope["scope_sha256"]
    report.dimension_notes = ["Unchanged dimensions inherited; metadata and dependency consequences rechecked."]
    report.findings[0] = AuditFinding(record_path="observations[0]", severity="minor", verdict="supported",
        needs_revision=False, explanation="Conflict is now qualified without changing values; dependencies remain limited.",
        evidence=[SourceAnchor(source_id="source", block_id="page1"), SourceAnchor(source_id="source", block_id="page2")])
    return report


def test_coordinate_qualification_expands_temporal_siblings_but_not_common_material_uses():
    old, new, baseline = fixture()
    frozen = [x.model_dump(mode="json") for x in (old, new, baseline)]
    scope = build(old, new, baseline)
    assert scope and scope["policy"] == POLICY
    refs = {r["record_path"]: r["role"] for r in scope["entity_refs"]}
    assert refs == {"materials[0]": "context", "experiments[0]": "context",
                    "observations[0]": "affected", "observations[1]": "affected"}
    assert len(scope["changes"]) == 2
    assert validate_revision_audit(scope, baseline, final_report(scope, baseline)) == []
    assert frozen == [x.model_dump(mode="json") for x in (old, new, baseline)]


@pytest.mark.parametrize("mutate", [
    lambda r: setattr(r.observations[0].value, "value", 6),
    lambda r: setattr(r.observations[0].unit, "value", "new unit"),
    lambda r: setattr(r.observations[0].coordinates[0].fact, "value", "13:00"),
    lambda r: setattr(r.observations[0].coordinates[0].unit, "value", "min"),
    lambda r: setattr(r.observations[0].coordinates[0].fact, "status", "reported"),
    lambda r: setattr(r.observations[0], "experiment_id", "other"),
    lambda r: setattr(r.observations[0], "analysis_role", "mechanistic_support"),
    lambda r: setattr(r.evidence[0], "block_id", "replaced"),
    lambda r: r.observations.reverse(),
    lambda r: r.observations.pop(),
    lambda r: r.observations[0].coordinates.append(NamedFact(name="new-condition")),
    lambda r: setattr(r.experiments[0], "conditions", [NamedFact(name="changed-condition")]),
])
def test_changes_outside_bounded_metadata_require_full_audit(mutate):
    old, new, baseline = fixture()
    mutate(new)
    assert build(old, new, baseline) is None


def test_existing_conflict_alternatives_and_limitations_cannot_be_erased():
    old, new, baseline = fixture()
    old.observations[0].coordinates[0].fact = new.observations[0].coordinates[0].fact.model_copy(deep=True)
    new.observations[0].coordinates[0].fact.alternatives.pop()
    assert build(old, new, baseline) is None
    old, new, baseline = fixture()
    old.observations[0].limitations = ["unknown denominator"]
    assert build(old, new, baseline) is None


@pytest.mark.parametrize("defect", ["incomplete", "omission", "delta", "unlocated", "outside", "no_blocker"])
def test_full_baseline_and_localized_blockers_are_required(defect):
    old, new, baseline = fixture()
    if defect == "incomplete": baseline.checked_dimensions.pop()
    elif defect == "omission": baseline.potential_omissions.append("missing outcome")
    elif defect == "delta": baseline.review_scope = "citation_delta"
    elif defect == "unlocated": baseline.findings[0].record_path = "some outdoor fields"
    elif defect == "outside": baseline.findings[0].record_path = "observations[2].value"
    else: baseline.findings = []
    assert build(old, new, baseline) is None
    assert build_revision_audit_scope(old, new, fixture()[2], baseline_origin="citation_delta_model_audit") is None


def test_many_evidence_explanations_and_comma_blockers_are_bounded_without_full_values():
    old, _, baseline = fixture()
    for i in range(20):
        obs = old.observations[0].model_copy(deep=True)
        obs.id = f"synthetic-{i}"
        old.observations.append(obs)
    new = old.model_copy(deep=True)
    selected = [0, *range(3, 23)]
    for i in selected:
        new.observations[i].evidence_links[0].supported_fields = ["value"]
        new.observations[i].evidence_links[0].explanation = "Narrowed source support."
    baseline.findings[0].record_path = ", ".join(f"observations[{i}].evidence_links[0]" for i in selected)
    baseline.findings[0].evidence = [SourceAnchor(source_id="source", block_id="page1")]
    scope = build(old, new, baseline)
    assert scope and len(scope["changes"]) == 21
    report = final_report(scope, baseline)
    report.findings = [AuditFinding(record_path=f"observations[{i}]", severity="minor", verdict="supported",
        needs_revision=False, explanation="Source support range checked.",
        evidence=[SourceAnchor(source_id="source", block_id="page1")]) for i in selected]
    assert validate_revision_audit(scope, baseline, report) == []
    assert build(old, new, baseline, max_entities=10) is None
    assert build(old, new, baseline, max_changes=20) is None


def test_series_pairs_comparisons_are_in_dependency_closure_and_coverage_restrictions_are_append_only():
    old, new, baseline = fixture()
    for record in (old, new):
        record.quantitative_coverage = [QuantitativeCoverage(id="series", source_id="source", block_id="page2",
            label="Synthetic curve", observation_ids=["changed", "sibling"])]
        record.observations[0].series_id = "series"
        record.experiments[0].pairs = [ObservationPair(left_observation_id="changed", right_observation_id="unrelated")]
        record.comparisons = [DesignComparison(id="comparison", observation_ids=["sibling", "unrelated"])]
    new.quantitative_coverage[0].limitations = ["Do not treat conflicting clocks as simultaneous."]
    scope = build(old, new, baseline)
    assert scope
    assert {"observations[2]", "experiments[1]", "quantitative_coverage[0]", "comparisons[0]"}.issubset(
        r["record_path"] for r in scope["entity_refs"])
    old.quantitative_coverage[0].limitations = ["A prior source limitation"]
    assert build(old, new, baseline) is None


def test_changed_support_requires_old_and_new_source_anchors_not_blanket_agreement():
    old, new, baseline = fixture()
    scope = build(old, new, baseline)
    report = final_report(scope, baseline)
    report.findings[0].evidence.pop()
    assert validate_revision_audit(scope, baseline, report)
    report = final_report(scope, baseline)
    report.findings[0].record_path = "observations"
    assert validate_revision_audit(scope, baseline, report)
    report = final_report(scope, baseline)
    report.findings[0].needs_revision, report.findings[0].verdict = True, "unsupported"
    assert validate_revision_audit(scope, baseline, report) == []  # controller still rejects scientific finding


def test_unresolved_findings_and_limits_must_remain_explicit():
    old, new, baseline = fixture()
    baseline.findings.append(AuditFinding(record_path="observations[2].value", severity="minor", verdict="unresolved",
        needs_revision=False, explanation="Independent source remains uncertain.", evidence=[]))
    scope = build(old, new, baseline)
    report = final_report(scope, baseline)
    assert validate_revision_audit(scope, baseline, report) == []
    report.findings.pop()
    assert validate_revision_audit(scope, baseline, report)
    report = final_report(scope, baseline)
    report.coverage_limitations.clear()
    assert validate_revision_audit(scope, baseline, report)


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(changed_paths=[]),
    lambda s: s.update(old_record_hash="not-a-hash"),
    lambda s: s.update(incremental_depth=True),
    lambda s: s["changes"][0].update(required_evidence_ids=[]),
    lambda s: s["changes"][0].update(new_value_hash="0" * 64),
    lambda s: s["entity_refs"][0].update(index=True),
    lambda s: s.update(source_anchors=[]),
    lambda s: s.update(baseline_blocking_paths=[]),
])
def test_rehashed_malformed_scope_is_rejected(mutate):
    old, new, baseline = fixture()
    scope = build(old, new, baseline)
    mutate(scope)
    scope["scope_sha256"] = digest_json({k: v for k, v in scope.items() if k != "scope_sha256"})
    assert validate_revision_audit(scope, baseline, final_report(scope, baseline))


def test_prompt_uses_exact_changes_and_dependency_directory_not_full_candidate():
    old, new, baseline = fixture()
    scope = build(old, new, baseline)
    prepared = {}, scope, baseline
    prompt = citation_review_prompt(prepared, new)
    assert "BOUNDED REVISION SOURCE RECHECK" in prompt
    assert "EXACT CURRENT SCOPE ENTITIES" not in prompt
    assert "14:00" in prompt and "13:00" in prompt
    assert "9.1" not in prompt  # unrelated observation content never inserted
    assert citation_review_issues(prepared, final_report(scope, baseline)) == []
    assert finding_paths("observations[0].value, observations[1].value")
    assert finding_paths("observations[*].value") is None
    assert finding_paths("observations[0].value; run code") is None


def test_mapping_inputs_and_malformed_input_do_not_mutate():
    old, new, baseline = fixture()
    data = [x.model_dump(mode="json") for x in (old, new, baseline)]
    saved = copy.deepcopy(data)
    assert build(*data) == build(old, new, baseline)
    assert data == saved
    assert build({}, new, baseline) is None
    assert validate_revision_audit({}, baseline, baseline)
