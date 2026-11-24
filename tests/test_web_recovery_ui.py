"""Small UI contracts for explicit, read-only-first recovery controls.

Backend tests cover provenance, budgets and dispatch. These checks protect the
browser's two consequential boundaries: opening a diagnosis cannot submit work,
and a full re-extraction cannot be selected or sent as an automatic fallback.
"""
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "src" / "watertreatment" / "web_static" / "app.js"


def _section(start, end):
    source = SOURCE.read_text(encoding="utf-8")
    return source.split(start, 1)[1].split(end, 1)[0]


def test_recovery_dialog_inspects_without_automatic_job_submission():
    dialog = _section("async function recoveryModal(job){", "  function jobs(){")
    before_submit, submission = dialog.split("async function submit(){", 1)
    assert 'await api("/api/jobs/"+enc(job.id)+"/recovery")' in before_submit
    assert "await post(" not in before_submit
    assert 'submit,"primary","play"' in before_submit
    assert '"/recover",{action:action.id,base_fingerprint:plan.base_fingerprint}' in submission
    assert '"/retry"' not in dialog
    assert "后台尚未提供恢复接口" in dialog


def test_restart_is_never_a_default_or_unconfirmed_recovery_action():
    dialog = _section("async function recoveryModal(job){", "  function jobs(){")
    assert 'a.available===true&&a.id!=="reextract"' in dialog
    assert 'selected=recommended?.id||null' in dialog
    assert 'type:"checkbox",id:"recovery-reextract-confirm"' in dialog
    assert '(action.id==="reextract"&&!confirm.checked)' in dialog
    assert 'stale=true' in dialog
    assert "页面不会自动改为全文抽取" in dialog


def test_unfunded_retained_repair_opens_a_budget_preview_before_queueing():
    dialog = _section("async function recoveryModal(job){", "  function jobs(){")
    preview = 'if(action.budget_preview){dialog.close();budgetExtensionModal(job);return;}'
    assert preview in dialog and dialog.index(preview) < dialog.index('await post(')
    assert 'budgetOption.id!=="reextract"' in dialog


def test_existing_extraction_and_failed_jobs_do_not_hide_reextraction():
    extraction = _section("function extractionModal(ids){", "  const recoveryActionNames=")
    assert "if(existing.length&&!restart.checked)return" in extraction
    assert "重新全文抽取，保留旧版本" in extraction
    actions = _section("function jobActions(j,{details=true}={}){", "  async function recoveryModal(job){")
    assert 'if(jobCanRecover(j))' in actions
    assert '"处理未完成任务",()=>recoveryModal(j)' in actions
    assert '"新建重试"' not in actions


def test_candidate_internal_status_cannot_claim_recovery_is_complete():
    dialog = _section("async function recoveryModal(job){", "  function jobs(){")
    assert 'stateBadge(summary.status' not in dialog
    assert 'stateBadge("candidate_unverified","候选已保存")' in dialog
    assert 'recoveryBudgetView(plan.budget_summary,' in dialog
    assert '"原处理链剩余额度":"原运行剩余额度"' in dialog


def test_audit_resume_uses_retained_lineage_budget_without_an_extension_request():
    source = SOURCE.read_text(encoding="utf-8")
    dialog = _section("async function recoveryModal(job){", "  function jobs(){")
    assert "沿原运行链扣除已消耗的额度" in dialog
    assert "预算不重置，也不重新审查整稿" in dialog
    assert "extend_audit" not in source
    assert "budget_confirmed" not in source


def test_recovery_explains_stage_specific_paths_without_forcing_a_restart():
    source = SOURCE.read_text(encoding="utf-8")
    dialog = _section("async function recoveryModal(job){", "  function jobs(){")
    for action in ("resume_initial", "resume_extraction", "repair_identity", "resume_audit"):
        assert action + ':"' in source
    assert 'plan.recovery_explanation' in dialog
    assert 'action.applicable===false?"不适用"' in dialog
    assert "重新全文抽取是独立选项，并非系统要求" in dialog
    assert "没有旧候选可以直接续审" in dialog
    assert "保留科学数据" in dialog


def test_extraction_recovery_explains_reused_understanding_and_retained_budget():
    source = SOURCE.read_text(encoding="utf-8")
    dialog = _section("async function recoveryModal(job){", "  function jobs(){")
    assert 'resume_extraction:"从已完成全文理解继续抽取"' in source
    assert "复用已核验的全文理解档案" in dialog
    assert "保留必要原文回查" in dialog
    assert "沿原处理链扣除已消耗额度，不重复执行首次全文理解" in dialog


def test_recovery_budget_keeps_previous_model_usage_visible():
    budget = _section("function recoveryBudgetView(budget,title=null){", "  function jobRecoveryNote")
    assert 'budget.used_seconds??budget.used_model_seconds' in budget
    assert 'budget.used_calls' in budget
    assert "已消耗模型时长" in budget
    assert 'budget.policy==="lineage_remaining"' in budget
    assert "原处理链时长上限" in budget
    assert "原处理链调用上限" in budget
