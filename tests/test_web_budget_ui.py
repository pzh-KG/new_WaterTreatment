"""Offline UI budget contracts; no server jobs, providers or graph connections."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "src/watertreatment/web_static/app.js"


def section(start, end):
    return SOURCE.read_text(encoding="utf-8").split(start, 1)[1].split(end, 1)[0]


def parse_in_browser(values):
    # Evaluate the actual browser validator, without DOM/network/bootstrapping.
    node = shutil.which("node")
    assert node, "Node.js is required for budget UI contract checks; do not skip."
    code = "const budgetPolicyFields=" + section("const budgetPolicyFields=", "  function budgetPolicyForm(")
    harness = code + """
const values=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
try { process.stdout.write(JSON.stringify({policy:parseBudgetPolicy(values)})); }
catch(error) { process.stdout.write(JSON.stringify({error:error.message})); }
"""
    # The bundled runtime path contains "codex"; use Node's verified command
    # name so the offline provider guard does not mistake that parent directory
    # for the Codex executable. Keep the guard itself unchanged.
    result = subprocess.run(["node", "-e", harness], input=json.dumps(values),
        capture_output=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def values():
    return {"initial_seconds": "60", "initial_calls": "5", "addition_seconds": "15",
        "addition_calls": "2", "max_auto_additions": "1", "max_total_seconds": "75",
        "max_total_calls": "7"}


def test_minutes_are_converted_once_and_call_counts_remain_counts():
    result = parse_in_browser(values())["policy"]
    assert result == {"initial_seconds": 3600, "initial_calls": 5, "addition_seconds": 900,
        "addition_calls": 2, "max_auto_additions": 1, "max_total_seconds": 4500, "max_total_calls": 7}


@pytest.mark.parametrize("changes", [
    {"initial_seconds": ""}, {"initial_calls": "2"}, {"initial_calls": "5.5"},
    {"max_auto_additions": "-1"}, {"max_auto_additions": "6"},
    {"addition_seconds": "61"}, {"addition_calls": "11"},
    {"max_total_seconds": "361"}, {"max_total_calls": "21"},
    {"initial_seconds": "60", "max_total_seconds": "59"},
    {"initial_calls": "7", "max_total_calls": "6"}, {"addition_seconds": "NaN"},
])
def test_invalid_or_over_cap_policies_cannot_be_submitted(changes):
    assert "error" in parse_in_browser({**values(), **changes})


def test_zero_auto_additions_still_preserves_manual_headroom():
    result = parse_in_browser({**values(), "max_auto_additions": "0"})["policy"]
    assert result["max_auto_additions"] == 0
    assert result["max_total_calls"] > result["initial_calls"]


def test_accumulated_cap_can_be_lower_than_all_possible_additions():
    result = parse_in_browser({**values(), "max_auto_additions": "5"})["policy"]
    assert result["max_total_calls"] < result["initial_calls"] + 5 * result["addition_calls"]


def test_batch_submission_freezes_explicit_per_paper_policy():
    modal = section("function extractionModal(ids){", "  const recoveryActionNames=")
    assert "S.status.budget_policy_defaults" in modal
    assert "budget_policy:budget.getPolicy()" in modal
    assert "!budget?.valid" in modal
    assert "设定次数与累计上限内自动追加" in modal
    assert "后台尚未提供批次额度设置" in modal


def test_extension_dialog_loads_diagnosis_before_explicit_submission():
    modal = section("async function budgetExtensionModal(job){", "  function jobActions(")
    assert 'await api("/api/jobs/"+enc(job.id)+"/budget-extension")' in modal
    assert '"/budget-extension/preview",{budget_policy:policyDraft}' in modal
    assert "async function submit(){" in modal
    submit = modal.split("async function submit(){", 1)[1]
    assert "busy||stale||plan.available!==true||!plan.base_fingerprint" in submit
    assert '"/budget-extension",request' in submit
    assert "base_fingerprint:plan.base_fingerprint" in submit
    assert "request.budget_policy=editor.getPolicy()" in submit
    assert "editor.onChange(()=>{stale=true;update();})" in modal
    assert "await load();" in modal and "await submit()" not in modal
    assert '"/extractions"' not in modal and '"/retry"' not in modal


def test_legacy_policy_cannot_edit_historical_initial_allocation():
    form = section("function budgetPolicyForm(", "  function frozenBudgetPolicyView(")
    assert 'readonly:legacy&&key.startsWith("initial_")' in form
    assert "不能改写历史初始额度" in form
    modal = section("async function budgetExtensionModal(job){", "  function jobActions(")
    assert "plan.policy_editable===true" in modal
    assert "editor?editor.element:frozenBudgetPolicyView(plan.policy)" in modal


def test_automatic_followup_is_presented_without_rewriting_old_result_status():
    note = section("function jobBudgetDecisionNote(j){", "  function extensionBudgetView(")
    assert "j.payload?.auto_budget_decision" in note
    assert "decision.queued_job_id" in note
    assert "后续任务仍须完成审查" in note
    assert "未完成状态不会自动变为审查通过" in note
    assert "j.status=" not in note and "j.result=" not in note


def test_extension_summary_exposes_usage_limits_and_the_actual_added_amount():
    summary = section("function extensionBudgetView(plan){", "  async function budgetExtensionModal(")
    for key in ("used_seconds", "used_calls", "allocated_seconds", "allocated_calls",
                "max_total_seconds", "max_total_calls", "auto_additions"):
        assert '"' + key + '"' in summary
    assert "plan.proposed_addition" in summary
    assert "尚未取得可核验的累计用量" in summary
