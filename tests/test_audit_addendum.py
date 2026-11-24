"""A source supplement cannot rewrite or impersonate a complete scientific audit."""
from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from test_scientific_revision_audit import fixture, build, report_for
from watertreatment.audit_addendum import (
    POLICY, AuditAddendum, audit_addendum_issues, audit_addendum_prompt,
    build_audit_addendum_scope, merge_audit_addendum,
)
from watertreatment.domain import EvidenceRef
from watertreatment.reading import AuditFinding, SourceAnchor
from watertreatment.scientific_revision_audit import NEW_SCOPE_LIMITS, validate_scientific_revision_audit
from watertreatment.storage import digest_json


def pending():
    old, new, baseline = fixture()
    new.observations[0].value.evidence_ids = ["text", "plot"]
    full = build(old, new, baseline, **NEW_SCOPE_LIMITS)
    parent = report_for(full, baseline)
    # The changed fact itself was checked, but its entity-level disposition
    # accidentally cited only one of two sources. Nothing scientific is changed.
    for finding in parent.findings:
        if finding.record_path == "observations[0]":
            finding.evidence = [e for e in finding.evidence if e.block_id != "page2"]
    issues = validate_scientific_revision_audit(full, baseline, parent, old_record=old, new_record=new)
    assert issues == ["Affected entity/dependency lacks explicit source disposition: observations[0]"]
    return old, new, baseline, full, parent


def scope_for(inputs, **kwargs):
    old, new, baseline, full, parent = inputs
    return build_audit_addendum_scope(full, baseline, parent, old_record=old, new_record=new, **kwargs)


def supplement_for(scope):
    return AuditAddendum(scope_sha256=scope["scope_sha256"], parent_audit_hash=scope["parent_audit_hash"],
        candidate_hash=scope["candidate_hash"], findings=[AuditFinding(
            record_path="Exact local dispositions", record_paths=[t["record_path"] for t in scope["targets"]],
            severity="minor", verdict="supported", needs_revision=False,
            explanation="The listed sources were checked against this exact retained interpretation.",
            evidence=[SourceAnchor(source_id=a["source_id"], block_id=a["block_id"]) for a in scope["source_anchors"]])])


def merge(inputs, scope, supplement):
    old, new, baseline, full, parent = inputs
    return merge_audit_addendum(scope, full, baseline, parent, supplement, old_record=old, new_record=new)


def issues(inputs, scope, supplement):
    old, new, baseline, full, parent = inputs
    return audit_addendum_issues(scope, full, baseline, parent, supplement, old_record=old, new_record=new)


def test_small_scope_includes_all_required_sources_not_only_the_missing_citation():
    inputs = pending()
    scope = scope_for(inputs)
    assert scope and scope["policy"] == POLICY
    assert scope["targets"][0]["record_path"] == "observations[0]"
    assert set(scope["targets"][0]["required_evidence_ids"]) == {"text", "plot"}
    assert {a["block_id"] for a in scope["source_anchors"]} == {"page1", "page2"}
    assert json.loads(json.dumps(scope)) == scope
    assert scope["scope_sha256"] == digest_json({k: v for k, v in scope.items() if k != "scope_sha256"})


def test_append_only_merge_preserves_parent_findings_limits_dimensions_and_original_scope():
    inputs = pending()
    scope = scope_for(inputs)
    supplement = supplement_for(scope)
    parent = inputs[-1]
    before = parent.model_dump(mode="json")
    merged = merge(inputs, scope, supplement)
    assert merged.findings[:len(parent.findings)] == parent.findings
    assert merged.findings[len(parent.findings):] == supplement.findings
    for field in type(parent).model_fields:
        if field != "findings":
            assert getattr(merged, field) == getattr(parent, field)
    assert merged.review_scope == "scientific_delta"
    assert merged.review_scope_sha256 == inputs[3]["scope_sha256"] != scope["scope_sha256"]
    assert parent.model_dump(mode="json") == before
    assert issues(inputs, scope, supplement) == []


@pytest.mark.parametrize("field", ["scope_sha256", "parent_audit_hash", "candidate_hash"])
def test_response_must_bind_the_exact_parent_candidate_and_addendum_scope(field):
    inputs = pending()
    scope = scope_for(inputs)
    supplement = supplement_for(scope)
    setattr(supplement, field, "0" * 64)
    assert issues(inputs, scope, supplement)
    with pytest.raises(ValueError):
        merge(inputs, scope, supplement)


@pytest.mark.parametrize("tamper", ["target", "anchor", "parent_hash", "candidate_hash", "parent_scope", "issues", "budget"])
def test_even_rehashed_scope_tamper_is_rejected(tamper):
    inputs = pending()
    scope = scope_for(inputs)
    if tamper == "target": scope["targets"][0]["record_path"] = "observations[1]"
    elif tamper == "anchor": scope["source_anchors"][0]["block_id"] = "replacement"
    elif tamper == "parent_hash": scope["parent_audit_hash"] = "0" * 64
    elif tamper == "candidate_hash": scope["candidate_hash"] = "0" * 64
    elif tamper == "parent_scope": scope["parent_scope_sha256"] = "0" * 64
    elif tamper == "issues": scope["parent_protocol_issues"] = []
    else: scope["scope_limits"]["max_paths"] = 9
    scope["scope_sha256"] = digest_json({k: v for k, v in scope.items() if k != "scope_sha256"})
    assert issues(inputs, scope, supplement_for(scope))


@pytest.mark.parametrize("defect", ["limit_dropped", "scope_wrong", "invalid_path", "not_checked", "science_blocker", "omission", "missing_changed_claim"])
def test_addendum_is_not_a_shortcut_for_other_protocol_or_scientific_failures(defect):
    inputs = pending()
    parent = inputs[-1]
    if defect == "limit_dropped": parent.coverage_limitations.clear()
    elif defect == "scope_wrong": parent.review_scope_sha256 = "0" * 64
    elif defect == "invalid_path": parent.findings.append(AuditFinding(record_path="invalid prose", severity="minor",
        verdict="supported", needs_revision=False, explanation="Invalid.", evidence=[]))
    elif defect == "not_checked": parent.findings[0].verdict = "not_checked"
    elif defect == "science_blocker": parent.findings[0].needs_revision = True
    elif defect == "omission": parent.potential_omissions = ["A real scientific omission."]
    else:
        parent.findings = [f for f in parent.findings if not f.record_path.startswith("observations[0]")]
    assert scope_for(inputs) is None


@pytest.mark.parametrize("path", ["observations[1]", "observations", "observations[0].value", "observations[*]"])
def test_successful_supplement_may_only_name_exact_permitted_entity_paths(path):
    inputs = pending()
    scope = scope_for(inputs)
    supplement = supplement_for(scope)
    supplement.findings[0].record_paths = [path]
    assert issues(inputs, scope, supplement)


def test_only_adding_the_missing_citation_does_not_replace_full_current_source_disposition():
    inputs = pending()
    scope = scope_for(inputs)
    supplement = supplement_for(scope)
    supplement.findings[0].evidence = [SourceAnchor(source_id="source", block_id="page2")]
    # The complete original validator can combine old/new citations, but this
    # addendum additionally requires an explicit current disposition of both.
    assert any("complete current source" in x for x in issues(inputs, scope, supplement))


@pytest.mark.parametrize("verdict,needs_revision", [("unsupported", True), ("supported", True), ("not_checked", False)])
def test_new_scientific_blocker_is_preserved_even_outside_scope_and_prevents_completion(verdict, needs_revision):
    inputs = pending()
    scope = scope_for(inputs)
    supplement = supplement_for(scope)
    blocker = AuditFinding(record_path="observations[2].value", severity="major", verdict=verdict,
        needs_revision=needs_revision, explanation="The local source check reveals a broader scientific concern.",
        evidence=[SourceAnchor(source_id="source", block_id="page1")])
    supplement.findings.append(blocker)
    merged = merge(inputs, scope, supplement)
    assert merged.findings[-1] == blocker
    assert any("blocker" in x for x in issues(inputs, scope, supplement))


def test_unresolved_nonblocking_disposition_is_retained_as_uncertainty_without_fabricated_agreement():
    inputs = pending()
    scope = scope_for(inputs)
    supplement = supplement_for(scope)
    supplement.findings[0].verdict = "unresolved"
    assert merge(inputs, scope, supplement).findings[-1].verdict == "unresolved"
    assert issues(inputs, scope, supplement) == []


def test_schema_cannot_impersonate_reading_audit_or_modify_candidate():
    data = supplement_for(scope_for(pending())).model_dump(mode="json")
    for key, value in {"checked_dimensions": [], "review_scope": "full", "candidate": {}, "coverage_limitations": []}.items():
        with pytest.raises(ValidationError):
            AuditAddendum.model_validate({**data, key: value})


def many_missing_paths(count):
    old, new, baseline = fixture()
    baseline.findings = []
    for i in range(1, count):
        observation = old.observations[0].model_copy(deep=True)
        observation.id = f"synthetic-addendum-{i}"
        old.observations.append(observation)
    new = old.model_copy(deep=True)
    selected = [0, *range(3, 3 + count - 1)]
    for i in selected:
        new.observations[i].value.status = "inferred"
        new.observations[i].value.evidence_ids = ["text", "plot"]
    full = build(old, new, baseline, **NEW_SCOPE_LIMITS)
    parent = report_for(full, baseline)
    for finding in parent.findings:
        if finding.record_path in {f"observations[{i}]" for i in selected}:
            finding.evidence = [e for e in finding.evidence if e.block_id != "page2"]
    return old, new, baseline, full, parent


def test_addendum_never_expands_beyond_eight_paths_or_six_original_blocks():
    assert scope_for(many_missing_paths(8))
    assert scope_for(many_missing_paths(9)) is None
    old, new, baseline, _, _ = pending()
    for i in range(5):
        for record in (old, new):
            record.evidence.append(EvidenceRef(id=f"extra-{i}", source_id="source", block_id=f"extra-page-{i}"))
        new.observations[0].value.evidence_ids.append(f"extra-{i}")
    full = build(old, new, baseline, **NEW_SCOPE_LIMITS)
    parent = report_for(full, baseline)
    for finding in parent.findings:
        if finding.record_path == "observations[0]":
            finding.evidence = []
    assert scope_for((old, new, baseline, full, parent)) is None


@pytest.mark.parametrize("kwargs", [{"max_paths": True}, {"max_paths": 0}, {"max_paths": 9},
    {"max_source_blocks": False}, {"max_source_blocks": 0}, {"max_source_blocks": 7}])
def test_addendum_budgets_cannot_be_inflated_or_coerced(kwargs):
    assert scope_for(pending(), **kwargs) is None


def test_prompt_requests_only_a_bound_supplement_and_retains_the_parent_claim():
    inputs = pending()
    scope = scope_for(inputs)
    prompt = audit_addendum_prompt(scope, inputs[-1])
    assert "not a new full-paper audit" in prompt
    assert "every required original source block" in prompt
    assert "AuditAddendum" in prompt and scope["parent_audit_hash"] in prompt
    assert "9.1" not in prompt  # an unrelated outcome is not resubmitted
