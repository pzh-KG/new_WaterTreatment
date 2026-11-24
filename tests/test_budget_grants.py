"""Execution-budget contracts on real prepared synthetic source runs.

The synthetic usage ledger tests accounting; it is not scientific provenance.
"""
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_workflow import workspace, read_json
from test_active_reading import active, FakeReader
from watertreatment.budget_policy import BudgetPolicy, apply_initial_policy, default_policy, freeze_initial_policy, verify_initial_policy
from watertreatment.budget_grants import (lineage_snapshot, preview_grant, reserve_grant, bind_grant,
    verify_grant_plan, verify_lineage_plan, load_grant)
from watertreatment.storage import DataStore, digest_json, hash_file, write_new_json
from watertreatment.workflow import execute_plan
from watertreatment.web_recovery import RecoveryWorkflow


def rewrite(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def usage(run, calls=7, seconds=7200):
    for index in range(calls):
        request = {"stage": "synthetic-stage-" + str(index), "model": "gpt-6.2-sol", "reasoning_effort": "max"}
        folder = run / "calls" / str(index)
        write_new_json(folder / "request.json", {"request": request, "fingerprint": digest_json(request)})
        write_new_json(folder / "result.json", {"status": "failed", "elapsed_seconds": seconds if index == 0 else 0})
    write_new_json(run / "failure.json", {"error": "synthetic exhaustion", "model_calls": calls})


@pytest.fixture
def budget_parent(active):
    ws, run, bundle, _ = active
    policy = default_policy(ws.settings)
    freeze_initial_policy(ws.settings, run, policy, "synthetic-batch", "synthetic-job")
    usage(run)
    return ws, run, policy


def child_for(settings, parent, grant):
    store = DataStore(settings.data_root)
    old = read_json(parent / "plan.json")
    child = store.new_run("extraction", {"paper_id": old["paper_id"], "parent_run": parent.name})
    shutil.copyfile(parent / "sources.json", child / "sources.json")
    plan = {**old, "run_id": child.name, "max_calls": grant["allocated_calls"],
        "model_settings": {**old["model_settings"], "max_calls_per_run": grant["allocated_calls"]},
        "reading_settings": {**old["reading_settings"], "total_timeout_seconds": grant["allocated_seconds"]},
        "input_paths": []}
    plan.pop("budget_policy", None); plan.pop("budget_continuation", None)
    write_new_json(child / "plan.json", plan)
    store.event(child.name, "prepared", {"plan_sha256": hash_file(child / "plan.json")})
    return child


def test_real_prepare_freeze_and_normal_core_execution_preserve_model(active):
    ws, run, bundle, original = active
    assert set(DataStore(ws.data).last_event(run.name, "prepared")) > {"plan_sha256"}
    before = hash_file(run / "plan.json")
    frozen = freeze_initial_policy(ws.settings, run, default_policy(ws.settings), "batch-real-prepare")
    assert hash_file(run / "plan.before-budget-policy.json") == before
    assert verify_initial_policy(ws.settings, run) == frozen
    client = FakeReader(ws.settings, bundle)
    client.total_timeout_seconds = ws.settings.reading.total_timeout_seconds
    execute_plan(ws.settings, run / "plan.json", client=client)
    assert client.stages[0][0] == "comprehension"
    assert read_json(run / "plan.json")["model_settings"]["reasoning_effort"] == "max"


def test_default_policy_supports_readonly_low_call_settings(workspace):
    assert default_policy(workspace.settings)["initial_calls"] == 3
    workspace.settings.codex.max_calls_per_run = 20
    assert default_policy(workspace.settings)["max_auto_additions"] == 0


@pytest.mark.parametrize("field,value", [("initial_calls", True), ("initial_seconds", 0),
    ("max_total_calls", 21), ("max_total_seconds", 21601), ("max_auto_additions", -1)])
def test_policy_rejects_invalid_or_unbounded_limits(active, field, value):
    ws, *_ = active
    policy = default_policy(ws.settings); policy[field] = value
    with pytest.raises(ValueError): BudgetPolicy.model_validate(policy)


def test_addition_has_real_ledger_and_cannot_double_reserve_or_bind(budget_parent):
    ws, parent, policy = budget_parent
    frozen = {p: hash_file(p) for p in parent.rglob('*') if p.is_file()}
    preview = preview_grant(ws.settings, parent, mode="automatic")
    assert (preview["used_calls"], preview["allocated_calls"], preview["allocated_seconds"]) == (7, 2, 1800)
    descriptor = reserve_grant(ws.settings, parent, preview, "a" * 64, mode="automatic")
    with pytest.raises(ValueError): reserve_grant(ws.settings, parent, preview, "a" * 64, mode="automatic")
    grant = load_grant(ws.settings, descriptor)
    child = child_for(ws.settings, parent, grant)
    bind_grant(ws.settings, child, descriptor)
    assert verify_grant_plan(ws.settings, child)["added_calls"] == 2
    assert {p: hash_file(p) for p in frozen} == frozen
    with pytest.raises(ValueError): bind_grant(ws.settings, child_for(ws.settings, parent, grant), descriptor)


def test_unknown_usage_is_never_zero_and_changed_parent_invalidates_grant(budget_parent):
    ws, parent, _ = budget_parent
    preview = preview_grant(ws.settings, parent)
    descriptor = reserve_grant(ws.settings, parent, preview, "b" * 64)
    path = parent / "calls/0/result.json"
    value = read_json(path); value["elapsed_seconds"] = None; rewrite(path, value)
    with pytest.raises(ValueError): lineage_snapshot(ws.settings, parent)
    with pytest.raises(ValueError): load_grant(ws.settings, descriptor)


def test_stale_preview_does_not_reserve(budget_parent):
    ws, parent, _ = budget_parent
    selected = preview_grant(ws.settings, parent)
    path = parent / "calls/0/result.json"; value = read_json(path); value["elapsed_seconds"] += 1; rewrite(path, value)
    with pytest.raises(ValueError, match="已改变"):
        reserve_grant(ws.settings, parent, selected, "c" * 64)
    assert DataStore(ws.data).last_event(parent.name, "budget_grant_reserved_v1") is None


def test_cumulative_caps_survive_child_failure(budget_parent):
    ws, parent, _ = budget_parent
    preview = preview_grant(ws.settings, parent)
    descriptor = reserve_grant(ws.settings, parent, preview, "d" * 64)
    child = child_for(ws.settings, parent, load_grant(ws.settings, descriptor))
    bind_grant(ws.settings, child, descriptor)
    usage(child, calls=2, seconds=1800)
    snapshot = lineage_snapshot(ws.settings, child)
    assert snapshot["used_calls"] == 9 and snapshot["used_seconds"] == 9000
    with pytest.raises(ValueError, match="累计上限"):
        preview_grant(ws.settings, child)


def test_auto_count_is_limited_but_manual_respects_remaining_cap(active):
    ws, parent, _, _ = active
    policy = {**default_policy(ws.settings), "max_total_seconds": 12000, "max_total_calls": 12}
    freeze_initial_policy(ws.settings, parent, policy, "policy-with-headroom")
    usage(parent)
    preview = preview_grant(ws.settings, parent, mode="automatic")
    descriptor = reserve_grant(ws.settings, parent, preview, "e" * 64, mode="automatic")
    child = child_for(ws.settings, parent, load_grant(ws.settings, descriptor))
    bind_grant(ws.settings, child, descriptor)
    usage(child, calls=2, seconds=1800)
    with pytest.raises(ValueError, match="自动补充次数"):
        preview_grant(ws.settings, child, mode="automatic")
    assert preview_grant(ws.settings, child)["added_calls"] == 2


def test_later_non_grant_continuation_blocks_old_ancestor(budget_parent):
    ws, parent, _ = budget_parent
    child = child_for(ws.settings, parent, {"allocated_seconds": 60, "allocated_calls": 1})
    usage(child, calls=1, seconds=10)
    with pytest.raises(ValueError, match="最新成果"):
        preview_grant(ws.settings, parent)


def test_ordinary_independent_supplement_cannot_bypass_cumulative_authority(budget_parent):
    ws, parent, _ = budget_parent
    child = child_for(ws.settings, parent, {"allocated_seconds": 900, "allocated_calls": 1})
    with pytest.raises(ValueError, match="累计预算"):
        verify_lineage_plan(ws.settings, child)


def test_grant_registry_requires_actual_child_ownership(budget_parent):
    ws, parent, _ = budget_parent
    preview = preview_grant(ws.settings, parent)
    descriptor = reserve_grant(ws.settings, parent, preview, "f" * 64)
    child = child_for(ws.settings, parent, load_grant(ws.settings, descriptor))
    plan = read_json(child / "plan.json"); plan["budget_continuation"] = descriptor; rewrite(child / "plan.json", plan)
    with pytest.raises(ValueError, match="真实追加额度"):
        verify_grant_plan(ws.settings, child)


def test_concurrent_same_parent_additions_reserve_only_once(budget_parent):
    ws, parent, _ = budget_parent
    preview = preview_grant(ws.settings, parent)
    def submit(_):
        try:
            return reserve_grant(ws.settings, parent, preview, "1" * 64)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(submit, range(2)))
    assert sum(value is not None for value in replies) == 1
    assert load_grant(ws.settings, next(value for value in replies if value))["added_calls"] == 2


def test_execution_rechecks_a_competing_non_grant_branch(budget_parent):
    ws, parent, _ = budget_parent
    preview = preview_grant(ws.settings, parent)
    descriptor = reserve_grant(ws.settings, parent, preview, "2" * 64)
    child = child_for(ws.settings, parent, load_grant(ws.settings, descriptor))
    bind_grant(ws.settings, child, descriptor)
    sibling = child_for(ws.settings, parent, {"allocated_seconds": 60, "allocated_calls": 1})
    # Historical proof remains replayable; actual new execution must first
    # resolve the competing branch rather than spending the same allowance.
    assert verify_grant_plan(ws.settings, child)
    with pytest.raises(ValueError, match="最新成果"):
        verify_lineage_plan(ws.settings, child)
    with pytest.raises(ValueError, match="最新成果"):
        verify_lineage_plan(ws.settings, sibling)


@pytest.mark.parametrize("seconds,calls,structural,expected", [
    (7100, 6, False, 100), (7141, 6, False, None), (7100, 7, False, None),
    (7200, 7, True, 900)])
def test_coverage_recovery_uses_managed_remaining_authority(active, monkeypatch, seconds, calls, structural, expected):
    ws, parent, _, _ = active
    freeze_initial_policy(ws.settings, parent, default_policy(ws.settings), "coverage-policy")
    usage(parent, calls=calls, seconds=seconds)
    for name in ("candidate.json", "reading_dossier.json", "model_review.json"):
        write_new_json(parent / name, {})
    # Isolate only the coverage-science admission (covered in its own suite).
    # The new policy, ancestry, actual ledger and web allowance are real here.
    monkeypatch.setattr("watertreatment.coverage_correction_workflow.authenticate_coverage_parent",
        lambda *args: SimpleNamespace(scope={"required_dispositions": [] if structural else ["missing-context"]}))
    flow = RecoveryWorkflow(SimpleNamespace(settings=ws.settings, store=DataStore(ws.data)))
    terminal = {"status": "candidate_incomplete_reading_coverage", "stop_reason": "coverage_correction_required"}
    if expected is None:
        with pytest.raises(ValueError, match="追加额度"):
            flow._special("correct_reading", parent, terminal)
    else:
        choice = flow._special("correct_reading", parent, terminal)
        assert choice["budget"]["seconds"] == expected
        assert choice["budget"]["calls"] == (0 if structural else 1)
        assert choice["budget"]["policy"] == "cumulative_remaining"
