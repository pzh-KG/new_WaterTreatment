"""Finish a timed-out report-only addendum with authenticated cumulative limits.

This is a single reserved descendant of the *latest failed completion*, not a
retry of the older scientific candidate. Extra allowance needs a registered grant.
"""
from __future__ import annotations

import math
import json
from datetime import datetime
from pathlib import Path

from . import partial_audit_completion_workflow as original
from .model_policy import plan_model, require_active_model
from .partial_audit_completion import PartialAuditAddendum, partial_audit_completion_prompt
from .storage import DataStore, digest_json, hash_file, write_new_json

POLICY = "partial-audit-remaining-resume-v1"
ORIGIN = "partial_scientific_audit_remaining_composite"
_json, _copy_new = original._json, original._copy_new


def _timeout_protocol(run, completed):
    """Inspect public envelopes only; never expose reasoning/event text."""
    events = run / "app_server" / "events.jsonl"
    before = hash_file(events)
    key = completed["thread_id"], completed["turn_id"]
    starts = 0
    for line in events.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("direction") != "receive":
            continue
        message = event.get("message", {})
        params = message.get("params", {})
        method = message.get("method")
        if method in {"turn/started", "turn/completed"}:
            turn = params.get("turn", {})
            if (params.get("threadId"), turn.get("id")) != key:
                continue
            if method == "turn/started":
                starts += 1
            elif turn.get("status") == "completed":
                raise ValueError("completed public turn cannot be retried as an unanswered timeout")
        if (method == "item/completed" and (params.get("threadId"), params.get("turnId")) == key
                and params.get("item", {}).get("type") == "agentMessage"
                and params.get("item", {}).get("phase") == "final_answer"):
            raise ValueError("retained final answer needs assessment rather than timeout continuation")
    if starts != 1 or before != hash_file(events):
        raise ValueError("ambiguous or changed public timeout protocol")
    return before


def _reservation(store, failed, scientific_parent, *, current=None, require_latest=True):
    """One descendant and no competing sibling can spend the same allowance."""
    rows = store.runs()
    failed_row = next(r for r in rows if r["run_id"] == failed)
    failed_manifest = _json(failed_row["manifest_path"])
    ancestors, ancestor = set(), failed
    while ancestor and ancestor not in ancestors:
        ancestors.add(ancestor)
        ancestor_row = next((r for r in rows if r["run_id"] == ancestor), None)
        if ancestor_row is None or ancestor == scientific_parent:
            break
        ancestor = _json(ancestor_row["manifest_path"]).get("parent_run")
    instant = lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))
    for row in rows:
        manifest = _json(row["manifest_path"])
        if row["run_id"] == current or row["run_id"] in ancestors:
            continue
        if (manifest.get("operation") == original.POLICY
                and manifest.get("parent_run") == scientific_parent
                and row["run_id"] != failed):
            raise ValueError("scientific parent has another reserved completion attempt")
        if (manifest.get("operation") == POLICY and
                (manifest.get("parent_run") == failed or
                 manifest.get("scientific_parent_run") == scientific_parent)):
            raise ValueError("remaining completion budget already has a retained reservation")
        if (manifest.get("parent_run") == failed and manifest.get("operation")):
            raise ValueError("failed completion already has another retained descendant")
        if (require_latest and row["kind"] == "extraction"
                and manifest.get("paper_id") == failed_manifest.get("paper_id")
                and instant(row["created_at"]) > instant(failed_row["created_at"])):
            raise ValueError("a newer registered paper run exists; do not resume an older timeout")


def authenticate_failed_completion(settings, failed_run, *, allow_exhausted=False, _seen=()):
    """Replay ancestry, frozen plan, candidate binding and the failed call."""
    from .staged_source_resume import _call

    failed_path = Path(failed_run).resolve(strict=True)
    if failed_path.name in _seen or len(_seen) >= 32:
        raise ValueError("failed report-continuation lineage is cyclic or too deep")
    raw_plan = _json(failed_path / "plan.json")
    prior_receipt = None
    if raw_plan.get("workflow") == POLICY:
        records, failed, plan, parent, prior_receipt = _authenticate_plan(settings,
            failed_path / "plan.json", _seen=(*_seen, failed_path.name))
    else:
        records, failed, plan, parent = original._authenticate_plan(settings, failed_path / "plan.json")
    manifest = _json(failed / "manifest.json")
    rows = [r for r in records.store.runs() if r["run_id"] == failed.name]
    if (len(rows) != 1 or rows[0]["kind"] != "extraction"
            or Path(rows[0]["manifest_path"]).resolve() != failed / "manifest.json"
            or manifest.get("operation") != (POLICY if prior_receipt else original.POLICY)
            or manifest.get("parent_run") != (prior_receipt["failed_run"] if prior_receipt else parent.run.name)
            or manifest.get("paper_id") != parent.record.paper.id
            or plan.get("rule_version") != settings.rules_version
            or parent.scope["source_anchors"]):
        raise ValueError("only a registered report-only partial completion can use the remaining lineage")
    if ((failed / "result.json").exists() or (failed / "partial_audit_addendum.json").exists()
            or (failed / "candidate.json").exists() or (failed / "model_review.json").exists()):
        raise ValueError("a saved answer/result requires its own assessment, not a timeout restart")
    calls = list((failed / "calls").glob("*/request.json"))
    if len(calls) != 1:
        raise ValueError("remaining lineage requires exactly one failed completion attempt")
    call = calls[0].parent
    request, completed, inputs, _ = _call(records, failed, calls[0], plan,
        stage="partial_audit_completion_0", status="failed")
    failure = _json(failed / "failure.json")
    scope_receipt = {"scope_path": str(failed / "partial_completion_scope.json"),
        "scope_file_sha256": hash_file(failed / "partial_completion_scope.json")}
    if (failure.get("model_calls") != 1 or failure.get("error") != completed.get("error")
            or not str(completed.get("error", "")).startswith("TimeoutError:")
            or (call / "answer.json").exists()
            or request.get("citation_review") != scope_receipt
            or request.get("source_review_call") != call.name
            or request.get("source_review_scope_sha256") != parent.scope["scope_sha256"]
            or not records._queried_candidate_snapshot(failed, parent.record, request, inputs)):
        raise ValueError("failed completion is not an authenticated timeout on the exact candidate/scope")
    for path, checksum in inputs.items():
        if hash_file(records._safe_data_path(path)) != checksum:
            raise ValueError("failed completion input changed")
    elapsed = completed.get("elapsed_seconds")
    if (not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool)
            or not math.isfinite(elapsed) or elapsed < plan["reading_settings"]["total_timeout_seconds"]):
        raise ValueError("failed completion has no authenticated elapsed timeout budget")
    events_hash = _timeout_protocol(failed, completed)
    used = (prior_receipt["budget"]["used_seconds"] if prior_receipt else parent.receipt["used_model_seconds"]) + elapsed
    used_calls = (prior_receipt["budget"]["used_calls"] if prior_receipt else parent.receipt["used_model_calls"]) + 1
    remaining = math.floor(parent.receipt["original_total_model_seconds"] - used)
    calls_left = parent.receipt["original_max_calls"] - used_calls
    if not allow_exhausted and (remaining < 60 or calls_left < 1):
        raise ValueError("original scientific lineage has less than 60 seconds or one call remaining")
    budget = {"policy": "lineage_remaining", "original_seconds": parent.receipt["original_total_model_seconds"],
        "used_seconds": used, "remaining_seconds": remaining,
        "original_calls": parent.receipt["original_max_calls"], "used_calls": used_calls,
        "remaining_calls": calls_left, "seconds": min(900, remaining), "calls": 1, "max_revisions": 0}
    artifacts = [{"path": str(records._safe_data_path(p)), "sha256": hash_file(p)}
        for p in sorted(failed.rglob("*")) if p.is_file()]
    receipt = {"policy": POLICY, "failed_run": failed.name, "scientific_parent_run": parent.run.name,
        "candidate_sha256": parent.receipt["candidate_sha256"], "scope_sha256": parent.scope["scope_sha256"],
        "failed_call": str(call), "failed_call_seconds": elapsed,
        "events_sha256": events_hash,
        "failed_model": completed["server_selected_model"], "failed_effort": completed["server_selected_effort"],
        "budget": budget, "artifact_hashes": artifacts,
        "scientific_validation": "not_established"}
    if plan.get("partial_completion_prompt_version") == 3:
        from .partial_report_transport import public_drafts
        drafts = dict((prior_receipt or {}).get("public_finding_drafts", {}))
        drafts.update(public_drafts(failed, call, parent.scope, completed))
        receipt["public_finding_drafts"] = drafts
    return records, failed, plan, parent, receipt


def _authorized_balance(settings, failed_run):
    """Freeze already-granted unused resources separately from the old receipt."""
    from .budget_grants import remaining_authority, lineage_snapshot
    allowance = remaining_authority(settings, failed_run)
    if allowance is None:
        return None
    snapshot = lineage_snapshot(settings, failed_run)
    return {"policy": "existing-authorized-partial-balance-v1", "parent_run": Path(failed_run).name,
        "allowance": allowance, "usage_snapshot": snapshot, "usage_sha256": digest_json(snapshot)}


def _balance_budget(balance):
    allowance = balance["allowance"]
    if allowance["remaining_seconds"] < 60 or allowance["remaining_calls"] < 1:
        raise ValueError("already authorized cumulative balance has less than 60 seconds or one call remaining")
    return {"policy": "existing_authorized_balance", "seconds": min(900, allowance["remaining_seconds"]),
        "calls": 1, "max_revisions": 0, **allowance}


def select(settings, failed_run):
    balance = _authorized_balance(settings, failed_run)
    records, failed, _, parent, receipt = (authenticate_failed_completion(settings, failed_run, allow_exhausted=True) if balance
        else authenticate_failed_completion(settings, failed_run))
    _reservation(records.store, failed.name, parent.run.name)
    selected = {"route": "partial_resume", "budget": _balance_budget(balance) if balance else receipt["budget"], "receipt": receipt,
        "description": "只完成已保留局部审查的缺项结论；累计扣除先前调用，不重新抽取或增加总预算。"}
    if balance:
        selected["authorized_balance"] = balance
    if original.report_version(parent.scope) == 3:
        selected["report_summary"] = (f"保留候选和已交付的原文，本次逐项补齐 {len(parent.scope['targets'])} 个局部审查结论。"
            f"可复用 {len(receipt.get('public_finding_drafts', {}))} 条已暂存的审查判断。")
    return selected


def _resume_prompt(failed, parent, receipt, version, *, include_progress):
    text = partial_audit_completion_prompt(parent.scope, parent.audit, version=version,
        documents=parent.documents, drafts=receipt.get("public_finding_drafts"))
    if include_progress:
        from .interrupted_reading import progress_prompt, public_progress
        progress = public_progress(failed, _json(Path(receipt["failed_call"]) / "result.json"))
        if progress["events_sha256"] != receipt["events_sha256"]:
            raise ValueError("Interrupted public notes no longer match the authenticated review turn")
        text += progress_prompt(progress)
    return text


def prepare(settings, failed_run, *, timeout_seconds=None, budget_grant=None):
    from .codex import code_identity
    from .workflow import runtime_versions, _image_manifest

    from .budget_grants import preparation_grant
    addition = preparation_grant(settings, failed_run, budget_grant)
    balance = _authorized_balance(settings, failed_run) if not addition else None
    records, failed, _, parent, receipt = (authenticate_failed_completion(settings, failed_run, allow_exhausted=True) if addition or balance
        else authenticate_failed_completion(settings, failed_run))
    _reservation(records.store, failed.name, parent.run.name)
    cap = min(900, addition["allocated_seconds"]) if addition else _balance_budget(balance)["seconds"] if balance else receipt["budget"]["seconds"]
    timeout = cap if timeout_seconds is None else timeout_seconds
    if type(timeout) is not int or not 60 <= timeout <= cap:
        raise ValueError("remaining completion must fit the original scientific lineage budget")
    require_active_model(settings.codex)
    run = records.store.new_run("extraction", {"paper_id": parent.record.paper.id,
        "cohort_role": parent.plan["cohort_role"], "rule_version": parent.plan["rule_version"],
        "schema_version": parent.record.schema_version, "workflow": POLICY, "operation": POLICY,
        "parent_run": failed.name, "scientific_parent_run": parent.run.name})
    for name in ("sources.json", "candidate.v00.json", "reading_dossier.json", "partial_parent_answer.json"):
        _copy_new(failed / name, run / name)
    for name, value in {"partial_resume_receipt.json": receipt,
            "partial_parent_receipt.json": parent.receipt, "partial_parent_source_progress.json": parent.progress,
            "partial_original_scope.json": parent.full_scope, "partial_completion_scope.json": parent.scope,
            "partial_baseline_candidate.json": parent.old_record.model_dump(mode="json"),
            "partial_baseline_audit.json": parent.baseline.model_dump(mode="json"),
            "reading_dossier.effective.json": parent.dossier.model_dump(mode="json"),
            "response_schema.json": original.report_schema(original.report_version(parent.scope))}.items():
        write_new_json(run / name, value)
    (run / "partial_completion_prompt.txt").write_text(
        _resume_prompt(failed, parent, receipt, original.report_version(parent.scope), include_progress=True), encoding="utf-8")
    if balance:
        write_new_json(run / "partial_remaining_balance.json", balance)
    paths = [p for p in run.iterdir() if p.is_file()]
    paths += [Path(d.source.path) for d in parent.documents]
    paths += [Path(x["path"]) for x in _image_manifest(parent.bundle)]
    paths = list(dict.fromkeys(paths))
    plan = {"version": 1, "run_id": run.name, "paper_id": parent.record.paper.id,
        "cohort_role": parent.plan["cohort_role"], "rule_version": parent.plan["rule_version"],
        "schema_version": parent.record.schema_version, "workflow": POLICY, "workdir": str(run),
        "input_paths": [{"path": str(p), "sha256": hash_file(p)} for p in paths],
        "reading_artifacts": [], "image_paths": [], "code_identity": code_identity(settings.project_root),
        "runtime": runtime_versions(), "model_settings": {**settings.codex.model_dump(mode="json"),
            "max_calls_per_run": 1, "timeout_seconds": timeout}, "max_calls": 1,
        "reading_settings": {"enabled": True, "max_revisions": 0, "max_tool_calls": 40,
            "total_timeout_seconds": timeout}, "partial_remaining_parent": receipt,
        "partial_completion_prompt_version": original.report_version(parent.scope), "partial_public_progress": True,
        "status": "prepared_no_model_calls",
        "partial_citation_context_policy": original.CAPTION_POLICY,
        "extraction_policy": parent.plan.get("extraction_policy")}
    if addition:
        plan["budget_grant_preparation"] = budget_grant
    if balance:
        plan["partial_remaining_balance"] = {"path": str(run / "partial_remaining_balance.json"),
            "sha256": hash_file(run / "partial_remaining_balance.json")}
    write_new_json(run / "plan.json", plan)
    records.store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    records.store.record_development_use(run.name, parent.record.paper.id,
        [d.source.sha256 for d in parent.documents], "Report-only audit continuation within original ancestor budget; not independent validation.")
    return run / "plan.json"


def _authenticate_plan(settings, plan_path, *, _seen=()):
    from .web_records import WebRecords
    records = WebRecords(settings)
    path = records._safe_data_path(plan_path)
    run = path.parent
    if path.name != "plan.json" or run.parent != (settings.data_root / "runs").resolve():
        raise ValueError("invalid remaining-completion path")
    plan, _, bundle, _ = records._source(run.name)
    receipt = plan["partial_remaining_parent"]
    from .budget_grants import prepared_grant
    parent_path = settings.data_root / "runs" / receipt["failed_run"]
    addition = prepared_grant(settings, run, plan, parent_path)
    descriptor = plan.get("partial_remaining_balance")
    balance = None
    if descriptor:
        path = run / "partial_remaining_balance.json"
        if addition or descriptor != {"path": str(path), "sha256": hash_file(path)}:
            raise ValueError("remaining-balance authority is missing, changed or mixed with a new grant")
        balance = _json(path)
        if balance != _authorized_balance(settings, parent_path):
            raise ValueError("remaining-balance source usage or already-authorized limits changed")
        _balance_budget(balance)
    _, failed, _, parent, expected_receipt = (authenticate_failed_completion(settings, parent_path, allow_exhausted=True, _seen=_seen) if addition or balance
        else authenticate_failed_completion(settings, parent_path, _seen=_seen))
    manifest = _json(run / "manifest.json")
    rows = [r for r in records.store.runs() if r["run_id"] == run.name]
    expected = {"partial_resume_receipt.json": expected_receipt,
        "partial_parent_receipt.json": parent.receipt, "partial_parent_source_progress.json": parent.progress,
        "partial_original_scope.json": parent.full_scope, "partial_completion_scope.json": parent.scope,
        "partial_baseline_candidate.json": parent.old_record.model_dump(mode="json"),
        "partial_baseline_audit.json": parent.baseline.model_dump(mode="json"),
        "reading_dossier.effective.json": parent.dossier.model_dump(mode="json"),
        "response_schema.json": original.report_schema(plan.get("partial_completion_prompt_version"))}
    if (len(rows) != 1 or rows[0]["kind"] != "extraction" or manifest.get("operation") != POLICY
            or manifest.get("parent_run") != failed.name or manifest.get("scientific_parent_run") != parent.run.name
            or manifest.get("created_at") != rows[0]["created_at"]
            or any(manifest.get(k) != plan.get(k) for k in ("paper_id", "cohort_role", "rule_version", "schema_version", "workflow"))
            or receipt != expected_receipt or any(_json(run / p) != v for p, v in expected.items())
            or bundle != parent.bundle or plan.get("workflow") != POLICY
            or any(hash_file(run / p) != hash_file(failed / p) for p in
                ("candidate.v00.json", "reading_dossier.json", "partial_parent_answer.json"))
            or (run / "partial_completion_prompt.txt").read_text(encoding="utf-8") !=
                _resume_prompt(failed, parent, expected_receipt, plan.get("partial_completion_prompt_version"),
                    include_progress=plan.get("partial_public_progress") is True)
            or plan.get("partial_completion_prompt_version") not in {2, 3}
            or plan["max_calls"] != 1 or plan["model_settings"]["max_calls_per_run"] != 1
            or plan_model(plan) is None
            or not 60 <= plan["reading_settings"]["total_timeout_seconds"] <= (min(900, addition["allocated_seconds"]) if addition
                else _balance_budget(balance)["seconds"] if balance else receipt["budget"]["seconds"])
            or plan["reading_settings"].get("max_revisions") != 0 or plan["reading_settings"].get("max_tool_calls") != 40
            or plan["model_settings"]["timeout_seconds"] != plan["reading_settings"]["total_timeout_seconds"]):
        raise ValueError("remaining completion frozen lineage, candidate, scope or budget changed")
    return records, run, plan, parent, receipt


def _finish_state(run, parent, receipt, supplement, call_proof, progress, tool_calls, *, settings=None, plan=None):
    from .reader_workflow import reading_checks
    from .quantitative import quantitative_summary
    from .validation import validate_record

    union = original._union(run, parent, call_proof, progress)
    merged, science = original.merge_completion(plan or {}, parent, supplement, progress)
    reading = reading_checks(parent.dossier, parent.record, merged, parent.documents)
    machine = validate_record(parent.record, source_documents=parent.documents)
    errors = sum(i.severity == "error" for i in machine)
    complete = not science and not errors and all(i.startswith("dossier ") for i in reading)
    stop = ("audit_completed" if not science and not reading and not errors else
        "coverage_correction_required" if complete else "partial_audit_completion_unresolved")
    status = ("candidate_audited_with_limitations" if stop == "audit_completed" else
        "candidate_incomplete_reading_coverage" if stop == "coverage_correction_required" else "candidate_incomplete_budget")
    budget = receipt["budget"]
    used = budget["used_seconds"] + call_proof["elapsed_seconds"]
    lineage = {"original_total_model_seconds": budget["original_seconds"], "original_max_calls": budget["original_calls"],
        "used_model_seconds": used, "used_model_calls": budget["used_calls"] + 1,
        "remaining_model_seconds": max(0, budget["original_seconds"] - used),
        "remaining_model_calls": max(0, budget["original_calls"] - budget["used_calls"] - 1)}
    proof = {"policy": POLICY, "resume_receipt_sha256": hash_file(run / "partial_resume_receipt.json"),
        "parent_receipt_sha256": hash_file(run / "partial_parent_receipt.json"),
        "addendum_sha256": hash_file(run / "partial_audit_addendum.json"), **call_proof,
        "failed_run": receipt["failed_run"], "scientific_parent_run": receipt["scientific_parent_run"],
        "scope_sha256": parent.scope["scope_sha256"], "budget_lineage": lineage,
        "interpretation": "Original completed partial audit plus a new bounded report, following a retained timeout; all elapsed calls charged to the original lineage. No full-paper re-audit or scientific validation asserted."}
    result = {"status": status, "stop_reason": stop, "model_calls": 1, "reading_tool_calls": tool_calls,
        "revisions": 0, "validation_error_count": errors, "reading_check_issues": science + reading,
        "human_validated": False, "graph_published": False, "scientific_accuracy": "not_established",
        "dataset_role": parent.plan["cohort_role"], "partial_remaining_proof": proof,
        "scientific_audit_state": "completed_with_inherited_limitations" if complete else "incomplete",
        "reading_coverage_state": "source_disposition_correction_required" if stop == "coverage_correction_required" else "checked_with_limitations",
        "budget_lineage": lineage, "quantitative_coverage_summary": quantitative_summary(parent.record),
        "history": [{"version": 0, "candidate_sha256": parent.receipt["candidate_sha256"],
            "audit_origin": ORIGIN, "validation_errors": errors, "needs_revision": bool(science or errors),
            "reading_check_issues": science + reading}]}
    if plan and plan.get("budget_continuation"):
        from .budget_grants import addition_usage
        additional = addition_usage(settings, run, plan, call_proof["elapsed_seconds"], 1)
        result["budget_addition"] = additional
        proof["budget_addition"] = additional
        proof["interpretation"] = "Original completed partial audit plus a new bounded report after retained timeout; original and explicitly added budgets are separately retained. No full-paper re-audit or scientific validation asserted."
    if plan and plan.get("partial_remaining_balance"):
        balance = _json(run / "partial_remaining_balance.json")
        allowance = balance["allowance"]
        usage = {"policy": balance["policy"], "root_run": allowance["root_run"],
            "balance_sha256": plan["partial_remaining_balance"]["sha256"], "new_grants": 0,
            "authorized_seconds": allowance["authorized_seconds"], "authorized_calls": allowance["authorized_calls"],
            "cumulative_used_seconds": allowance["used_seconds"] + call_proof["elapsed_seconds"],
            "cumulative_used_calls": allowance["used_calls"] + 1}
        result["authorized_balance_usage"] = usage
        proof["authorized_balance_usage"] = usage
        proof["interpretation"] = "Original receipts are unchanged. This report consumes unused previously authorized cumulative resources without a new grant; no full-paper re-audit or scientific validation asserted."
    return result, proof, merged, union, machine


def delivered_result(settings, run):
    """Replay an already delivered addendum even when the registration step previously failed."""
    records, run, plan, parent, receipt = _authenticate_plan(settings, Path(run) / "plan.json")
    supplement = PartialAuditAddendum.model_validate(_json(run / "partial_audit_addendum.json"))
    call_proof, progress = original._call_auth(records, run, plan, parent, supplement)
    count = len(list((run / "reading_tools").glob("tool-*/request.json")))
    return records, run, _finish_state(run,parent,receipt,supplement,call_proof,progress,count,
                                       settings=settings,plan=plan)


def _register_result(records, run, state):
    from .storage import write_new_bytes
    result, proof, merged, union, machine = state
    payloads = {"audit.v00.json":merged.model_dump(mode="json"), "model_review.json":merged.model_dump(mode="json"),
        "combined_source_review_proof.json":union, "validation.json":{"issues":[i.model_dump(mode="json") for i in machine],
        "semantic_accuracy":"not_established"}, "partial_remaining_proof.json":proof, "result.json":result}
    old = run/"candidate.v00.json"
    candidate = run/"candidate.json"
    if candidate.exists():
        if hash_file(candidate) != hash_file(old):
            raise ValueError("Existing partial completion candidate differs from the frozen candidate")
    else:
        write_new_bytes(candidate,old.read_bytes())
    for name, value in payloads.items():
        dest = run/name
        if dest.exists():
            if _json(dest) != value:
                raise ValueError("Existing partial completion artifact differs: "+name)
        else:
            write_new_json(dest,value)
    event={"status":result["status"],"model_calls":1,"result_sha256":hash_file(run/"result.json"),
           "proof_sha256":hash_file(run/"partial_remaining_proof.json")}
    saved=records.store.last_event(run.name,"candidate_completed")
    if saved is None:
        records.store.event(run.name,"candidate_completed",event)
    elif saved != event:
        raise ValueError("An existing completion receipt differs from the delivered addendum")
    return result


def finalize_delivered(settings, run):
    return _register_result(*delivered_result(settings,run))


def execute(settings, plan_path, *, client):
    from .codex import code_identity
    from .reading_tools import ReadingTools
    from .workflow import runtime_versions

    records, run, plan, parent, receipt = _authenticate_plan(settings, plan_path)
    if plan["max_calls"]:
        require_active_model(settings.codex)
        if plan_model(plan) != settings.codex.model:
            raise ValueError("当前模型与冻结计划不一致，请为该检查点创建续处理计划")
    _reservation(records.store, receipt["failed_run"], parent.run.name, current=run.name)
    from .budget_grants import verify_lineage_plan
    verify_lineage_plan(settings, run, plan)
    if plan.get("budget_continuation"):
        from .budget_extension_workflow import verify_extension_plan
        verify_extension_plan(settings, run, plan)
    if (plan["code_identity"] != code_identity(settings.project_root) or plan["runtime"] != runtime_versions()
            or client.calls_started != 0 or client.settings.model_dump(mode="json") != plan["model_settings"]
            or client.total_timeout_seconds != plan["reading_settings"]["total_timeout_seconds"]
            or (run / "calls").exists() or (run / "result.json").exists() or (run / "failure.json").exists()):
        raise ValueError("remaining completion requires frozen code and a fresh explicit bounded client")
    tools = ReadingTools(parent.bundle, run, max_calls=40, expected_inputs=plan["input_paths"])
    tools.bind_candidate(parent.record, run / "candidate.v00.json")
    client.bind_tools(tools)
    try:
        records.store.event(run.name, "execution_started", {"operation": POLICY, "max_calls": 1})
        supplement = client.complete(PartialAuditAddendum,
            prompt=(run / "partial_completion_prompt.txt").read_text(encoding="utf-8"),
            input_paths=[Path(x["path"]) for x in plan["input_paths"]], workdir=run, audit_root=run / "calls",
            identity={"stage": "partial_audit_completion_0", "rule_version": plan["rule_version"],
                **({"partial_report_version": 3} if plan.get("partial_completion_prompt_version") == 3 else {}),
                "code_identity": plan["code_identity"], "runtime": plan["runtime"],
                "citation_review": {"scope_path": str(run / "partial_completion_scope.json"),
                    "scope_file_sha256": hash_file(run / "partial_completion_scope.json")}},
            image_paths=[], conversation_key="partial-remaining-completion-0")
        if client.calls_started != 1:
            raise ValueError("remaining completion exceeded its single reserved model call")
        write_new_json(run / "partial_audit_addendum.json", supplement.model_dump(mode="json"))
        if getattr(client, "events", None):
            client.events.flush()
        call_proof, progress = original._call_auth(records, run, plan, parent, supplement)
        state = _finish_state(run, parent, receipt, supplement, call_proof, progress, tools.calls_started, settings=settings, plan=plan)
        _register_result(records,run,state)
    except BaseException as error:
        if not (run / "failure.json").exists():
            write_new_json(run / "failure.json", {"error": f"{type(error).__name__}: {error}", "model_calls": client.calls_started})
        records.store.event(run.name, "failed", {"error": str(error)})
        raise
    finally:
        client.close()
    return run


def verify_remaining_completion_run(settings, run, record, parsed, *, for_repair=False):
    try:
        records, run, plan, parent, receipt = _authenticate_plan(settings, Path(run) / "plan.json")
        _reservation(records.store, receipt["failed_run"], parent.run.name, current=run.name, require_latest=False)
        supplement = PartialAuditAddendum.model_validate(_json(run / "partial_audit_addendum.json"))
        call_proof, progress = original._call_auth(records, run, plan, parent, supplement)
        result = _json(run / "result.json")
        expected, proof, merged, union, machine = _finish_state(run, parent, receipt, supplement, call_proof, progress, result["reading_tool_calls"], settings=settings, plan=plan)
        event = records.store.last_event(run.name, "candidate_completed")
        return (record == parent.record and parsed == merged and result == expected
            and result["scientific_audit_state"] == ("incomplete" if for_repair else "completed_with_inherited_limitations")
            and hash_file(run / "candidate.json") == parent.receipt["candidate_sha256"]
            and _json(run / "audit.v00.json") == merged.model_dump(mode="json")
            and _json(run / "model_review.json") == merged.model_dump(mode="json")
            and _json(run / "combined_source_review_proof.json") == union
            and _json(run / "partial_remaining_proof.json") == proof
            and _json(run / "validation.json") == {"issues": [i.model_dump(mode="json") for i in machine], "semantic_accuracy": "not_established"}
            and event.get("result_sha256") == hash_file(run / "result.json")
            and event.get("proof_sha256") == hash_file(run / "partial_remaining_proof.json"))
    except (ValueError, OSError, KeyError, TypeError, IndexError, AttributeError):
        return False


def authenticate_composite_coverage_parent(settings, parent_run):
    """Expose coverage-only work after replaying the complete composite audit.

The effective dossier already includes an authenticated structural correction;
the original is kept separately in this run. Neither is silently re-labelled as
read. The following coverage workflow must copy ``receipt.dossier_path``.
"""
    from .coverage_correction_workflow import CoverageParent
    from .coverage_correction import build_coverage_correction_scope, POLICY as COVERAGE_POLICY
    from .reader_workflow import reading_checks
    from .reading import ReadingAudit, ReadingDossier
    from .workflow import validate_plan_metadata

    records, run, plan, parent, _ = _authenticate_plan(settings, Path(parent_run) / "plan.json")
    terminal = _json(run / "result.json")
    audit = ReadingAudit.model_validate(_json(run / "model_review.json"))
    dossier_path = run / "reading_dossier.effective.json"
    dossier = ReadingDossier.model_validate(_json(dossier_path))
    gaps = reading_checks(dossier, parent.record, audit, parent.documents)
    is_gap = lambda x: x.startswith(("dossier lacks explicit coverage disposition for ",
        "dossier coverage references unknown source/blocks."))
    if (not verify_remaining_completion_run(settings, run, parent.record, audit)
            or terminal.get("status") != "candidate_incomplete_reading_coverage"
            or terminal.get("stop_reason") != "coverage_correction_required"
            or not gaps or any(not is_gap(x) for x in gaps)
            or terminal.get("reading_check_issues") != gaps
            or any(i.severity == "error" for i in validate_plan_metadata(
                parent.record, plan, parent.documents, store=records.store))
            or audit.potential_omissions
            or any(f.needs_revision or f.verdict == "unsupported" for f in audit.findings)):
        raise ValueError("composite parent has defects beyond authenticated coverage-only gaps")
    proof = terminal["partial_remaining_proof"]
    paths = [p for p in sorted(run.rglob("*")) if p.is_file()]
    receipt = {"policy": COVERAGE_POLICY, "parent_run": run.name, "parent_version": 0,
        "candidate_path": str(run / "candidate.v00.json"),
        "candidate_sha256": hash_file(run / "candidate.v00.json"),
        "audit_path": str(run / "model_review.json"), "audit_sha256": hash_file(run / "model_review.json"),
        "dossier_path": str(dossier_path), "dossier_sha256": hash_file(dossier_path),
        "source_bundle_sha256": hash_file(run / "sources.json"),
        "scientific_audit_origin": ORIGIN,
        "completed_composite_addendum_call": proof["completed_call"],
        "events_sha256": proof["events_sha256"],
        "composite_proof_sha256": hash_file(run / "partial_remaining_proof.json"),
        "scientific_parent_run": proof["scientific_parent_run"], "failed_run": proof["failed_run"],
        "original_dossier_path": str(run / "reading_dossier.json"),
        "original_dossier_sha256": hash_file(run / "reading_dossier.json"),
        "artifact_hashes": [{"path": str(records._safe_data_path(p)), "sha256": hash_file(p)} for p in paths],
        "original_reading_check_issues": terminal["reading_check_issues"],
        "replayed_reading_check_issues": gaps, "scientific_validation": "not_established"}
    return CoverageParent(run, plan, parent.bundle, parent.documents, parent.record, dossier,
        audit, build_coverage_correction_scope(dossier, parent.documents), receipt)
