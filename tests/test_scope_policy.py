"""Frozen authority fields must be typed and bound to the actual prior record."""
import pytest

from test_research_scope import record
from watertreatment.domain import ExtractionRecord, Paper
from watertreatment.scope_policy import frozen_scope, scope_plan_issues, validate_scope_baseline
from watertreatment.storage import digest_json


@pytest.mark.parametrize("arguments", [
    {"mode": []}, {"mode": None}, {"mode": True}, {"mode": "unknown"},
    {"targets": None}, {"targets": "curve"}, {"targets": {"curve"}},
    {"targets": [["curve"]]}, {"targets": [{}]}, {"targets": [True]},
    {"targets": [""]}, {"targets": [" "]}, {"targets": [" curve "]},
    {"allow_new_dense": "false"}, {"allow_new_dense": "true"},
    {"allow_new_dense": 0}, {"allow_new_dense": 1}, {"allow_new_dense": []},
    {"baseline": {}}, {"baseline": "record-path"},
    {"mode": "research_core", "allow_new_dense": True},
])
def test_scope_policy_constructor_rejects_malformed_inputs_as_value_errors(arguments):
    with pytest.raises(ValueError):
        frozen_scope(**arguments)


@pytest.mark.parametrize("targets", [[], ["curve", "curve"], ["absent"]])
def test_scope_policy_digitization_needs_valid_unique_selected_series(targets):
    with pytest.raises(ValueError):
        frozen_scope("series_digitization", targets, record())


@pytest.mark.parametrize("field,value", [("scope", "external_benchmark"), ("state", "out_of_scope"), ("data_kind", "scalar")])
def test_scope_policy_target_must_be_eligible_in_actual_baseline(field, value):
    baseline = record()
    setattr(baseline.quantitative_coverage[0], field, value)
    with pytest.raises(ValueError):
        frozen_scope("series_digitization", ["curve"], baseline)


def test_scope_policy_does_not_coerce_authority_or_mutate_inputs():
    baseline = record()
    original = baseline.model_dump_json()
    targets = ["curve"]
    policy = frozen_scope("series_digitization", targets, baseline, allow_new_dense=False)
    assert policy["allow_new_dense"] is False
    assert validate_scope_baseline(policy, baseline) is None
    targets.clear()
    assert policy["digitization_targets"] == ["curve"]
    assert baseline.model_dump_json() == original


@pytest.mark.parametrize("field,value", [
    ("mode", []), ("mode", None), ("digitization_targets", None),
    ("digitization_targets", "curve"), ("digitization_targets", [["curve"]]),
    ("allow_new_dense", "false"), ("allow_new_dense", 1),
    ("preserved_observation_ids", [[]]), ("preserved_observation_ids", None),
    ("preserved_observation_ids", ["end", "end"]), ("preserved_observation_ids", [True]),
    ("baseline_record_fingerprint", 123), ("baseline_record_fingerprint", []),
    ("baseline_record_fingerprint", "a" * 63), ("baseline_record_fingerprint", "A" * 64),
    ("version", "future-unapproved"),
])
def test_scope_policy_malformed_frozen_fields_fail_closed_without_type_errors(field, value):
    baseline = record()
    policy = frozen_scope(baseline=baseline)
    policy[field] = value
    findings = scope_plan_issues(baseline, {"rule_version": "0.7", "extraction_policy": policy})
    assert findings and {issue.code for issue in findings} == {"SCOPE_PLAN_REQUIRED"}
    with pytest.raises(ValueError):
        validate_scope_baseline(policy, baseline)


@pytest.mark.parametrize("policy", [None, [], "scope", {}, {"version": "research-scope-v1"}])
def test_scope_policy_missing_structure_is_a_validation_issue(policy):
    findings = scope_plan_issues(record(), {"rule_version": "0.7", "extraction_policy": policy})
    assert [issue.code for issue in findings] == ["SCOPE_PLAN_REQUIRED"]
    with pytest.raises(ValueError):
        validate_scope_baseline(policy, record())


def test_scope_policy_unknown_fields_cannot_silently_change_authority():
    policy = frozen_scope()
    policy["skip_audit"] = True
    with pytest.raises(ValueError):
        validate_scope_baseline(policy, None)
    assert scope_plan_issues(record(), {"rule_version": "0.7", "extraction_policy": policy})


def test_scope_policy_fresh_run_has_no_invented_baseline():
    policy = frozen_scope()
    assert policy["preserved_observation_ids"] == []
    assert policy["baseline_record_fingerprint"] is None
    assert validate_scope_baseline(policy, None) is None
    policy["preserved_observation_ids"] = ["invented"]
    with pytest.raises(ValueError):
        validate_scope_baseline(policy, None)
    assert scope_plan_issues(record(), {"rule_version": "0.7", "extraction_policy": policy})


def test_scope_policy_empty_real_baseline_keeps_real_fingerprint():
    baseline = ExtractionRecord(schema_version="0.3.0", rule_version="0.7",
        extraction_scope="research_core", paper=Paper(id="empty-synthetic"))
    policy = frozen_scope(baseline=baseline)
    assert policy["preserved_observation_ids"] == []
    assert policy["baseline_record_fingerprint"] == digest_json(baseline.model_dump(mode="json"))
    assert not scope_plan_issues(baseline, {"rule_version": "0.7", "extraction_policy": policy})
    assert validate_scope_baseline(policy, baseline) is None
    with pytest.raises(ValueError):
        validate_scope_baseline(policy, None)


@pytest.mark.parametrize("tampering", ["empty_ids", "extra_id", "reorder_ids", "wrong_hash", "fresh_disguise"])
def test_scope_policy_admission_rebuild_catches_changed_retention_binding(tampering):
    baseline = record()
    policy = frozen_scope(baseline=baseline)
    if tampering == "empty_ids": policy["preserved_observation_ids"] = []
    if tampering == "extra_id": policy["preserved_observation_ids"].append("invented")
    if tampering == "reorder_ids": policy["preserved_observation_ids"].reverse()
    if tampering == "wrong_hash": policy["baseline_record_fingerprint"] = "0" * 64
    if tampering == "fresh_disguise": policy = frozen_scope()
    with pytest.raises(ValueError, match="exactly bind"):
        validate_scope_baseline(policy, baseline)


def test_scope_policy_structure_check_does_not_pretend_to_know_absent_baseline():
    baseline = record()
    policy = frozen_scope(baseline=baseline)
    candidate = baseline.model_copy(deep=True)
    candidate.observations = candidate.observations[:1]
    policy["preserved_observation_ids"] = []
    # This is structurally possible for a genuinely empty prior record. Only
    # admission, where that actual prior record is available, can reject it.
    assert not scope_plan_issues(candidate, {"rule_version": "0.7", "extraction_policy": policy})
    with pytest.raises(ValueError, match="exactly bind"):
        validate_scope_baseline(policy, baseline)


def test_scope_policy_binding_catches_same_ids_but_changed_source_values():
    baseline = record()
    policy = frozen_scope(baseline=baseline)
    changed = baseline.model_copy(deep=True)
    changed.observations[0].value.value = 0.3
    with pytest.raises(ValueError, match="exactly bind"):
        validate_scope_baseline(policy, changed)


@pytest.mark.parametrize("plan,candidate", [(None, record()), ([], record()), ({"rule_version": "0.7"}, None)])
def test_scope_policy_wrong_argument_shapes_return_controlled_findings(plan, candidate):
    assert [issue.code for issue in scope_plan_issues(candidate, plan)] == ["SCOPE_PLAN_REQUIRED"]


def test_scope_policy_legacy_without_new_policy_remains_untouched():
    candidate = ExtractionRecord(paper=Paper(id="legacy"))
    before = candidate.model_dump_json()
    assert not scope_plan_issues(candidate, {"rule_version": "0.5"})
    assert candidate.model_dump_json() == before
