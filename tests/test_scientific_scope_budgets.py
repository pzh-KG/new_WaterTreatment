"""Runtime scope-budget selection preserves old frozen algorithms and identities."""
import copy
import json

import pytest

from test_scientific_audit_execution import stage, report
from watertreatment.audit_execution import prepare_citation_review, select_scientific_scope_limits
from watertreatment.scientific_revision_audit import NEW_SCOPE_LIMITS, POLICY
from watertreatment.storage import digest_json, hash_file, write_new_json


LEGACY = {"max_entities": 200, "max_changes": 180}


def scope_with(limits):
    scope = {"policy": POLICY, "scope_limits": copy.deepcopy(limits)}
    scope["scope_sha256"] = digest_json(scope)
    return scope


def test_new_scope_limits_are_explicit_and_not_a_mutable_global_alias(tmp_path):
    selected = select_scientific_scope_limits(tmp_path)
    assert selected == NEW_SCOPE_LIMITS and selected["compiler_version"] == 6
    selected["max_entities"] = 1
    assert NEW_SCOPE_LIMITS["max_entities"] == 200


def test_existing_frozen_limits_take_precedence_without_injecting_new_defaults(tmp_path):
    prior = scope_with(LEGACY)
    assert select_scientific_scope_limits(tmp_path, previous_scopes=[prior]) == LEGACY
    assert "max_context_entities" not in select_scientific_scope_limits(tmp_path, previous_scopes=[prior])
    assert select_scientific_scope_limits(tmp_path, {"scientific_scope_limits": LEGACY}, previous_scopes=[prior]) == LEGACY
    with pytest.raises(ValueError, match="changed within"):
        select_scientific_scope_limits(tmp_path, {"scientific_scope_limits": NEW_SCOPE_LIMITS}, previous_scopes=[prior])
    with pytest.raises(ValueError, match="changed within"):
        select_scientific_scope_limits(tmp_path, previous_scopes=[prior, scope_with(NEW_SCOPE_LIMITS)])


@pytest.mark.parametrize("limits", [{}, {"max_entities": True, "max_changes": 180},
    {"max_entities": 200, "max_changes": 180, "max_context_entities": None},
    {"max_entities": 200, "max_changes": 180, "max_context_entities": 241},
    {"max_entities": 200, "max_changes": 180, "compiler_version": True},
    {"max_entities": 200, "max_changes": 180, "compiler_version": 7},
    {"max_entities": 200, "max_changes": 180, "ignored": 1}])
def test_invalid_explicit_limits_fail_closed_instead_of_falling_back(tmp_path, limits):
    with pytest.raises(ValueError):
        select_scientific_scope_limits(tmp_path, {"scientific_scope_limits": limits})


def initial_proof(tmp_path):
    scope = scope_with(LEGACY)
    path = tmp_path / "resumed_baseline_initial_scope.json"
    write_new_json(path, scope)
    proof = {"frozen_initial_scope_path": str(path), "frozen_initial_scope_sha256": hash_file(path),
             "initial_scope_sha256": scope["scope_sha256"]}
    return proof, path


def test_legacy_initial_proof_retains_its_own_limits_but_explicit_new_plan_is_separate(tmp_path):
    proof, path = initial_proof(tmp_path)
    before = hash_file(path)
    assert select_scientific_scope_limits(tmp_path, {"resumed_full_audit": proof}) == LEGACY
    assert select_scientific_scope_limits(tmp_path, {"scientific_scope_limits": NEW_SCOPE_LIMITS,
        "resumed_full_audit": proof}) == NEW_SCOPE_LIMITS
    assert hash_file(path) == before and json.loads(path.read_bytes())["scope_limits"] == LEGACY
    proof["scope_limits"] = dict(NEW_SCOPE_LIMITS)
    assert select_scientific_scope_limits(tmp_path, {"resumed_full_audit": proof}) == NEW_SCOPE_LIMITS


def test_tampered_initial_scope_does_not_silently_upgrade_to_new_limits(tmp_path):
    proof, path = initial_proof(tmp_path)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen input"):
        select_scientific_scope_limits(tmp_path, {"resumed_full_audit": proof})


def test_actual_preparation_uses_new_limits_and_cumulative_legacy_limits_remain_stable(tmp_path):
    run, old, first, baseline, history = stage(tmp_path)
    first_review = prepare_citation_review(run, 1, first, history, {"scientific_scope_limits": LEGACY})
    assert first_review and first_review[1]["scope_limits"] == LEGACY
    write_new_json(run / "audit.v01.json", report(first_review).model_dump(mode="json"))
    history.append({"version": 1, "audit_origin": "scientific_delta_model_audit", "validation_errors": 0,
        "reading_check_issues": [], "candidate_sha256": hash_file(run / "candidate.v01.json")})
    second = first.model_copy(deep=True)
    second.observations[0].limitations.append("Preserved source qualification.")
    write_new_json(run / "candidate.v02.json", second.model_dump(mode="json"))
    # A changed plan cannot silently expand the already completed local chain.
    assert prepare_citation_review(run, 2, second, history, {"scientific_scope_limits": NEW_SCOPE_LIMITS}) is None
    assert not (run / "citation_scope.v02.json").exists()
    follow = prepare_citation_review(run, 2, second, history)
    assert follow and follow[1]["scope_limits"] == LEGACY


def test_fresh_runtime_scientific_scope_uses_split_budgets(tmp_path):
    run, old, new, baseline, history = stage(tmp_path)
    prepared = prepare_citation_review(run, 1, new, history)
    assert prepared and prepared[1]["scope_limits"] == NEW_SCOPE_LIMITS


def test_old_unversioned_plan_does_not_silently_gain_v3_after_install(tmp_path):
    selected = select_scientific_scope_limits(tmp_path, {"run_id": "old-retained-run", "rule_version": "0.8"})
    assert selected["compiler_version"] == 2
    assert select_scientific_scope_limits(tmp_path, {"scientific_scope_limits": selected}) == selected
    assert select_scientific_scope_limits(tmp_path,
        {"scientific_scope_limits": NEW_SCOPE_LIMITS})["compiler_version"] == 6


def test_frozen_v4_scope_does_not_gain_contextual_edit_permissions(tmp_path):
    frozen = {**NEW_SCOPE_LIMITS, "compiler_version": 4}
    assert select_scientific_scope_limits(tmp_path, {"scientific_scope_limits":frozen}) == frozen
