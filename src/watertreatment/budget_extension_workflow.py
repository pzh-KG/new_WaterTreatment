"""Explicit stage continuation under an append-only per-paper budget grant.

The budget ledger establishes spending authority. Checkpoint authentication is
separate and is replayed for both web and direct core execution. Neither a grant
nor a saved JSON constitutes scientific acceptance.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .model_policy import plan_model
from .budget_policy import BudgetPolicy
from .storage import DataStore, digest_json, hash_file, write_new_json


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _artifacts(run):
    patterns = ("*.json", "calls/*/*.json", "reading_tools/*/*.json", "app_server/events.jsonl")
    paths = {p for pattern in patterns for p in run.glob(pattern) if p.is_file()}
    return [{"path": str(p), "sha256": hash_file(p)} for p in sorted(paths)]


def _import_bundle(settings, run):
    from .web_records import WebRecords
    records = WebRecords(settings)
    manifest = _json(run / "manifest.json")
    path = records._safe_data_path(manifest["source_bundle"])
    event = records.store.last_event(path.parent.name, "import_completed")
    if (path.name != "sources.json" or not event or event.get("bundle_sha256") != hash_file(path)
            or _json(path) != _json(run / "sources.json")):
        raise ValueError("原始导入快照不可认证，不能用追加额度替代来源核验")
    return path


def _budget(settings, grant):
    root_plan = _json(settings.data_root / "runs" / grant["root_run"] / "plan.json")
    return {"policy": "explicit_budget_addition", "remaining_seconds": grant["allocated_seconds"],
        "remaining_calls": grant["allocated_calls"],
        "max_revisions": min(root_plan["reading_settings"]["max_revisions"], grant["allocated_calls"] // 2)}


def progress_signature(run):
    versions = sorted(run.glob("candidate.v[0-9][0-9].json"))
    audits = sorted(run.glob("audit.v[0-9][0-9].json"))
    review = _json(run / "model_review.json") if (run / "model_review.json").is_file() else _json(audits[-1]) if audits else None
    if review is not None:
        review = {"findings": sorted(({
            key: finding.get(key) for key in ("record_path", "record_paths", "severity", "verdict", "needs_revision", "evidence")
        } for finding in review.get("findings", [])), key=digest_json),
            "omissions": review.get("potential_omissions", []),
            "coverage_limitations": review.get("coverage_limitations", []),
            "followups": review.get("quantitative_followups", [])}
    return digest_json({"candidate": _json(versions[-1]) if versions else None,
        "dossier": _json(run / "reading_dossier.json") if (run / "reading_dossier.json").is_file() else None,
        "review": review})


def _has_retained_failed_audit_answer(run):
    versions = sorted(run.glob("candidate.v[0-9][0-9].json"))
    if not versions:
        return False
    version = int(versions[-1].stem[-2:])
    for request_path in (run / "calls").glob("*/request.json"):
        if (_json(request_path).get("request", {}).get("stage") in {f"audit_{version}", f"audit_web_{version}"}
                and request_path.with_name("answer.json").is_file()
                and request_path.with_name("result.json").is_file()
                and _json(request_path.with_name("result.json")).get("status") == "failed"):
            return True
    return False


def select_checkpoint(settings, run, grant, *, flow=None):
    """Identify unfinished work without granting authority or rewriting a run."""
    from .web_records import WebRecords
    records = WebRecords(settings)
    run = records._safe_data_path(run)
    plan, _, bundle, documents = records._source(run.name)
    dossier_only = (plan.get("workflow") == "active_reading"
        and (run / "reading_dossier.json").is_file() and not list(run.glob("candidate.v[0-9][0-9].json")))
    # The dossier authenticator replays its own budget ancestry. Avoid replaying
    # that same recursive source proof twice at each continuation depth.
    if plan.get("budget_continuation") and not dossier_only:
        verify_extension_plan(settings, run, plan)
    if (plan.get("cohort_role") not in {"development", "unassigned"}
            or plan.get("rule_version") != settings.rules_version
            or plan_model(plan) is None):
        raise ValueError("追加额度必须保留原规则、文献用途及已登记的 Sol MAX 模型")
    terminal = _json(run / "result.json") if (run / "result.json").is_file() else {}
    if terminal.get("graph_published") or terminal.get("human_validated"):
        raise ValueError("此处只续处理未完成的模型任务，不能改变已发布或人工核验状态")
    budget = _budget(settings, grant)
    selection = None
    if (terminal.get("stop_reason") == "coverage_correction_required" or
            (plan.get("workflow") == "reading_coverage_correction" and not terminal and (run / "failure.json").is_file())):
        from .coverage_correction_workflow import authenticate_coverage_parent
        parent = authenticate_coverage_parent(settings, run)
        if not parent.scope["required_dispositions"]:
            raise ValueError("此阅读记录补正无需模型额度，请直接使用补正阅读记录")
        selection = {"route": "coverage", "label": "补正阅读记录", "minimum_calls": 1,
            "source_proof": parent.receipt, "description": "保留已认证科学审查，仅补阅读处置缺项及必要原文上下文。"}
    elif (terminal.get("stop_reason") == "partial_audit_completion_unresolved"
          and plan.get("workflow") in {"partial_scientific_audit_completion", "partial-audit-remaining-resume-v1"}):
        from .audit_guidance_resume import select_composite_repair
        selection = select_composite_repair(settings, run, budget)
    elif plan.get("workflow") in {"partial_scientific_audit_completion", "partial-audit-remaining-resume-v1"}:
        from .partial_audit_remaining_resume import authenticate_failed_completion
        _, _, _, parent, receipt = authenticate_failed_completion(settings, run, allow_exhausted=True)
        selection = {"route": "partial_resume", "label": "继续局部补审", "minimum_calls": 1,
            "source_proof": receipt, "description": "沿用已认证的局部审查链，仅补尚未交付的审查结论。"}
    elif terminal.get("stop_reason") == "audit_report_incomplete":
        from .audit_addendum_workflow import authenticate_addendum_parent
        parent = authenticate_addendum_parent(settings, run, allow_exhausted=True)
        selection = {"route": "addendum", "label": "补完审查报告", "minimum_calls": 1,
            "source_proof": parent.receipt, "description": "保留候选和原有科学疑点，补齐受限审查报告。"}
    elif terminal.get("status") == "candidate_incomplete_failure" and _has_retained_failed_audit_answer(run):
        # A delivered partial report has its own protocol and cannot be dropped
        # in favour of the older candidate simply because its budget ran out.
        from .partial_audit_completion_workflow import authenticate_partial_audit_parent
        parent = authenticate_partial_audit_parent(settings, run, allow_exhausted=True)
        selection = {"route": "partial", "label": "完成局部原文审查", "minimum_calls": 1,
            "source_proof": parent.receipt, "description": "复用已保留的局部原文交付，补齐审查义务。"}
    else:
        versions = sorted(run.glob("candidate.v[0-9][0-9].json"))
        if not versions:
            if (run / "reading_dossier.json").is_file():
                from .dossier_extraction_resume import select_for_addition
                proof = select_for_addition(settings, run)
                selection = {"route": "dossier", "label": "从已完成全文理解继续抽取", "minimum_calls": 2,
                    "dossier_path": proof["dossier_path"], "bundle_path": proof["bundle_path"],
                    "source_proof": proof, "description": "复用已认证理解，完成尚未完成的抽取和首次原文审查。"}
            else:
                from .budget_grants import authenticate_unfinished_reading
                proof = authenticate_unfinished_reading(settings, run)
                selection = {"route": "reading", "label": "继续未完成的首次理解", "minimum_calls": 3,
                    "bundle_path": str(_import_bundle(settings, run)), "source_proof": proof,
                    "description": "尚无已完成理解或候选；复用导入来源，完成理解、抽取和首次审查。"}
        else:
            candidate = _json(versions[-1])
            dossier = _json(run / "reading_dossier.json")
            if candidate.get("paper", {}).get("id") != plan["paper_id"] or dossier.get("paper_id") != plan["paper_id"]:
                from .identity_resume import authenticate_identity_parent
                _, _, proof = authenticate_identity_parent(settings, run, bundle, plan["cohort_role"], allow_exhausted=True)
                selection = {"route": "identity", "label": "校正系统编号并继续审查", "minimum_calls": 1,
                    "bundle_path": str(_import_bundle(settings, run)), "source_proof": proof,
                    "description": "仅校正有原始模型证明的系统身份字段，保留科学内容并完成首次审查。"}
            else:
                if flow is None:
                    from types import SimpleNamespace
                    from .web_recovery import RecoveryWorkflow
                    flow = RecoveryWorkflow(SimpleNamespace(settings=settings, store=records.store))
                selection = flow._continuation(run, plan, terminal, budget_override=budget)
                selection.pop("budget", None)
                selection.update(label="继续已有候选的修订与审查", minimum_calls=1,
                    description="保留已有候选及阅读档案，从未完成阶段继续；已完成审查及其疑点继续生效。")
                selected_path = Path(selection["candidate_path"])
                audit_path = selected_path.with_name(selected_path.name.replace("candidate.", "audit."))
                if selection.get("seed_kind") != "staged" and audit_path.is_file():
                    from .reading import ReadingAudit
                    audit = ReadingAudit.model_validate(_json(audit_path))
                    if audit.potential_omissions or any(f.needs_revision or f.verdict == "unsupported" for f in audit.findings):
                        from .audit_guidance_resume import authenticate_guidance_parent
                        proof = authenticate_guidance_parent(settings, run, selected_path)
                        selection.update(guidance=True, minimum_calls=2, source_proof=proof["receipt"],
                            description="继承原完整和局部审查的全部疑点，先修订，再审改动及受影响的原文范围。")
    if selection is None:
        raise ValueError("没有可核验的续处理阶段；追加预算不能替代来源或科学问题诊断")
    if budget["remaining_calls"] < selection["minimum_calls"] or budget["remaining_seconds"] < 60:
        raise ValueError("累计上限内的追加额度不足以覆盖剩余必要阶段及审查")
    selection.update(parent_run=run.name, paper_id=plan["paper_id"], cohort_role=plan["cohort_role"],
        source_artifacts=_artifacts(run), budget=budget)
    # Progress means retained source-bound work, not elapsed time or extra runs.
    selection["progress_signature"] = progress_signature(run)
    return selection


def verify_extension_plan(settings, run, plan=None):
    """Shared source-and-spending authority for web, CLI and final admission."""
    from .budget_grants import verify_grant_plan
    run = Path(run).resolve(strict=True)
    plan = plan or _json(run / "plan.json")
    grant = verify_grant_plan(settings, run, plan)
    if grant is None:
        raise ValueError("追加续处理缺少已登记预算授权")
    descriptor = plan.get("budget_checkpoint") or {}
    path = Path(descriptor.get("path", ""))
    if path != run / "budget_checkpoint.json" or hash_file(path) != descriptor.get("sha256"):
        raise ValueError("追加续处理的冻结阶段证明不完整或已改变")
    selected = _json(path)
    parent = settings.data_root / "runs" / grant["parent_run"]
    current = select_checkpoint(settings, parent, grant)
    if (selected != current or digest_json(selected) != grant["selection_fingerprint"]
            or plan.get("cohort_role") != selected["cohort_role"]):
        raise ValueError("追加执行前来源、候选、审查或剩余工作发生变化")
    return grant


def _freeze_checkpoint(settings, new_run, selection):
    path = new_run / "budget_checkpoint.json"
    write_new_json(path, selection)
    plan_path = new_run / "plan.json"
    plan = _json(plan_path)
    write_new_json(new_run / "plan.before-budget-checkpoint.json", plan)
    plan["budget_checkpoint"] = {"path": str(path), "sha256": hash_file(path)}
    plan["input_paths"].append(plan["budget_checkpoint"])
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    DataStore(settings.data_root).event(new_run.name, "prepared", {"plan_sha256": hash_file(plan_path)})


def prepare_extension(settings, descriptor, selection):
    from .budget_grants import load_grant, bind_grant
    from .workflow import prepare, execute_plan
    grant = load_grant(settings, descriptor)
    parent = settings.data_root / "runs" / grant["parent_run"]
    if select_checkpoint(settings, parent, grant) != selection or digest_json(selection) != grant["selection_fingerprint"]:
        raise ValueError("准备前的追加方案已变化，未调用模型")
    bounded = settings.model_copy(deep=True)
    bounded.codex.max_calls_per_run = grant["allocated_calls"]
    bounded.reading.total_timeout_seconds = grant["allocated_seconds"]
    bounded.codex.timeout_seconds = min(bounded.codex.timeout_seconds, grant["allocated_seconds"])
    bounded.reading.max_revisions = selection["budget"]["max_revisions"]
    route = selection["route"]
    executor = execute_plan
    if route in {"reading", "dossier", "candidate", "identity", "composite_repair"}:
        kwargs = {}
        if route in {"dossier", "candidate", "composite_repair"}:
            kwargs["reuse_dossier"] = Path(selection["dossier_path"])
        if route == "composite_repair":
            kwargs["reuse_composite_run"] = parent
            kwargs["budget_grant"] = descriptor
        if route == "candidate":
            kwargs["reuse_staged_candidate" if selection.get("seed_kind") == "staged" else "reuse_candidate"] = Path(selection["candidate_path"])
            kwargs["reuse_source_audit"] = selection["reuse_source_audit"]
        if route == "identity":
            kwargs.update(reuse_identity_corrected_run=parent, budget_grant=descriptor)
        new_run = prepare(bounded, Path(selection["bundle_path"]), cohort_role=selection["cohort_role"], **kwargs)
        if selection.get("guidance") and route != "composite_repair":
            from .audit_guidance_resume import freeze_guidance
            freeze_guidance(bounded, new_run, parent, Path(selection["candidate_path"]))
    else:
        if route == "coverage":
            from .coverage_correction_workflow import prepare as special, execute as executor
            kwargs = {"budget_grant": descriptor} if selection["source_proof"].get("coverage_resume") else {}
            path = special(bounded, parent, timeout_seconds=min(900, grant["allocated_seconds"]), **kwargs)
        elif route == "partial":
            from .partial_audit_completion_workflow import prepare as special, execute as executor
            path = special(bounded, parent, timeout_seconds=min(900, grant["allocated_seconds"]), budget_grant=descriptor)
        elif route == "partial_resume":
            from .partial_audit_remaining_resume import prepare as special, execute as executor
            path = special(bounded, parent, timeout_seconds=min(900, grant["allocated_seconds"]), budget_grant=descriptor)
        elif route == "addendum":
            from .audit_addendum_workflow import prepare_audit_addendum as special, execute_audit_addendum as executor
            path = special(bounded, parent, timeout_seconds=min(1200, grant["allocated_seconds"]), budget_grant=descriptor)
        else:
            raise ValueError("没有受支持的剩余阶段执行器")
        new_run = path.parent
    _freeze_checkpoint(bounded, new_run, selection)
    bind_grant(bounded, new_run, descriptor)
    return bounded, new_run, executor


class BudgetExtensionWorkflow:
    def __init__(self, recovery):
        self.flow, self.w, self.settings = recovery, recovery.w, recovery.settings

    def _context(self, job_id, exclude_job=None):
        state, _ = self.flow._inspect(job_id, exclude_job=exclude_job)
        job = self.w.jobs.get(job_id)
        if job["kind"] not in {"extract", "recover"}:
            raise ValueError("追加抽取额度只适用于文献抽取及其后续任务")
        if state["blockers"]:
            raise ValueError(state["blockers"][0])
        if not state["parent_run_id"]:
            raise ValueError("尚无已登记运行，不能确定已用额度和恢复点")
        run, _ = self.flow._run(state["parent_run_id"])
        paper = self.w.library.get(state["paper_id"])
        self.w.library.roles_for_import(state["paper_id"])
        self.flow._source_match(paper, run)
        reusable_jobs = set()
        retained = job.get("payload", {}).get("budget_grant")
        if retained and job["status"] in {"cancelled", "failed"} and not job["payload"].get("run_id"):
            from .budget_grants import load_grant, BOUND, _events
            reserved = load_grant(self.settings, retained)
            if (reserved["parent_run"] == run.name
                    and not any(event["grant_id"] == reserved["grant_id"] for _, event in _events(self.w.store, BOUND))):
                reusable_jobs = {item["id"] for item in self.w.jobs.list()
                    if item["kind"] == "recover" and item["status"] in {"cancelled", "failed"}
                    and not item["payload"].get("run_id") and item["payload"].get("budget_grant") == retained}
        prior = self.flow._prior_attempt(run.name, exclude_job=exclude_job, exclude_jobs=reusable_jobs)
        if prior:
            raise ValueError(prior)
        return job, run, state

    def inspect(self, job_id, budget_policy=None, *, automatic=False, exclude_job=None):
        from .budget_grants import preview_grant, lineage_snapshot, _policy, load_grant, BOUND, _events
        public = {"job_id": job_id, "available": False, "reason": None, "policy_editable": False,
            "model": self.settings.codex.model, "effort": self.settings.codex.reasoning_effort, "graph_published": False}
        selection = grant = None
        try:
            job, run, state = self._context(job_id, exclude_job)
            public.update(paper_id=state["paper_id"], parent_run_id=run.name)
            snapshot = lineage_snapshot(self.settings, run)
            policy, frozen = _policy(self.settings, snapshot, budget_policy)
            public.update(policy=policy, policy_editable=not frozen,
                budget_summary={"used_seconds": snapshot["used_seconds"], "used_calls": snapshot["used_calls"],
                    "max_total_seconds": policy["max_total_seconds"], "max_total_calls": policy["max_total_calls"]})
            retained = job.get("payload", {}).get("budget_grant")
            reuse_reserved = bool(retained and job["status"] in {"cancelled", "failed"}
                and not job["payload"].get("run_id") and not automatic)
            if reuse_reserved:
                grant = load_grant(self.settings, retained)
                if (grant["parent_run"] != run.name or grant["policy"] != policy
                        or any(event["grant_id"] == grant["grant_id"] for _, event in _events(self.w.store, BOUND))):
                    raise ValueError("已预留额度对应的运行已变化或已经绑定，请从其最新任务继续")
            else:
                grant = preview_grant(self.settings, run, policy, mode="automatic" if automatic else "manual")
            selection = select_checkpoint(self.settings, run, grant, flow=self.flow)
            if reuse_reserved and digest_json(selection) != grant["selection_fingerprint"]:
                raise ValueError("已预留额度的恢复成果或原文已变化，不能直接重用")
            public["budget_summary"].update(
                allocated_seconds=grant["authorized_seconds"] + (grant["added_seconds"] if reuse_reserved else 0),
                allocated_calls=grant["authorized_calls"] + (grant["added_calls"] if reuse_reserved else 0),
                auto_additions=grant["auto_additions_used"] + int(reuse_reserved and grant["mode"] == "automatic"))
            public.update(reuse_reserved=reuse_reserved,
                proposed_addition={"seconds": 0 if reuse_reserved else grant["added_seconds"],
                    "calls": 0 if reuse_reserved else grant["added_calls"]},
                continuation={"label": selection["label"], "description": selection["description"]})
            if reuse_reserved:
                public["continuation"]["description"] += " 本次沿用此前已预留额度，不重复增加额度或自动次数。"
            if automatic:
                self._automatic_eligible(job, run, selection, grant)
            public.update(available=True, base_fingerprint=digest_json({"job_id": job_id,
                "parent_run_id": run.name, "paper_id": state["paper_id"], "grant": grant, "selection": selection}))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            public["reason"] = str(exc)
            public.setdefault("base_fingerprint", None)
        return public, selection, grant

    def _automatic_eligible(self, job, run, selection, grant):
        from .budget_grants import load_grant
        if not job.get("payload", {}).get("budget_policy"):
            raise ValueError("旧任务没有批次预授权，需明确确认后才能追加")
        if self.w.jobs.paused or self.w.jobs.stop_event.is_set():
            raise ValueError("队列已暂停或正在停止，未自动预留追加额度")
        if selection["route"] == "reading":
            raise ValueError("尚无已完成理解或候选，不能确认有效进展；需先检查阅读失败原因")
        remaining_seconds = grant["authorized_seconds"] - grant["used_seconds"]
        remaining_calls = grant["authorized_calls"] - grant["used_calls"]
        original = self.flow._remaining(run, _json(run / "plan.json"))
        needs_repair = selection.get("guidance", False)
        if (remaining_seconds >= 60 and remaining_calls >= selection["minimum_calls"]
                and original["remaining_seconds"] >= 60 and original["remaining_calls"] >= selection["minimum_calls"]
                and not (needs_repair and original["max_revisions"] == 0)):
            raise ValueError("当前仍有可用原额度，请先使用相应的已有结果续处理入口")
        plan = _json(run / "plan.json")
        if plan.get("budget_continuation"):
            previous = load_grant(self.settings, plan["budget_continuation"])
            checkpoint = _json(Path(plan["budget_checkpoint"]["path"]))
            if checkpoint["progress_signature"] == selection["progress_signature"]:
                raise ValueError("上一次追加未产生新的已认证候选或审查进展，停止自动追加并保留现状")

    def queue(self, job_id, base_fingerprint, budget_policy=None, *, automatic=False):
        from .budget_grants import reserve_grant
        with self.flow.lock:
            for job in self.w.jobs.list():
                args = job.get("payload") or {}
                if (job["kind"] == "recover" and job["status"] in {"queued", "running"}
                        and args.get("previous_job_id") == job_id and args.get("recovery_action") == "extend_budget"
                        and args.get("base_fingerprint") == base_fingerprint):
                    return job["id"]
            state, selected, grant = self.inspect(job_id, budget_policy, automatic=automatic)
            if not state["available"] or state["base_fingerprint"] != base_fingerprint:
                raise ValueError(state.get("reason") or "追加方案已变化，请重新检查；没有启动模型")
            if automatic and (self.w.jobs.paused or self.w.jobs.stop_event.is_set()):
                raise ValueError("预留前队列已暂停或停止，未自动追加")
            original = self.w.jobs.get(job_id)
            descriptor = (original["payload"]["budget_grant"] if state.get("reuse_reserved") else
                reserve_grant(self.settings, self.settings.data_root / "runs" / state["parent_run_id"],
                    grant, digest_json(selected), mode="automatic" if automatic else "manual"))
            payload = {"previous_job_id": job_id, "parent_run_id": state["parent_run_id"],
                "paper_id": state["paper_id"], "recovery_action": "extend_budget", "base_fingerprint": base_fingerprint,
                "budget_grant": descriptor, "recovery_selection": selected, "budget_policy": state["policy"],
                "root_job_id": original["payload"].get("root_job_id", job_id),
                "batch_id": original["payload"].get("batch_id"), "auto_publish": False}
            return self.w.jobs.enqueue("recover", payload)

    def automatic(self, job_id):
        state, _, _ = self.inspect(job_id, automatic=True)
        if not state["available"]:
            reason = state.get("reason") or "没有可执行的追加方案"
            action = "no_progress" if "进展" in reason else "waiting_budget" if any(x in reason for x in ("上限", "次数", "额度")) else "not_eligible"
            return {"action": action, "reason": reason}
        job_id_new = self.queue(job_id, state["base_fingerprint"], state["policy"], automatic=True)
        return {"action": "queued", "reason": "按批次预授权追加有限额度，从已有成果继续；原失败及累计用量保留。", "queued_job_id": job_id_new}

    def execute(self, job, progress):
        from .budget_grants import load_grant
        args = job["payload"]
        if args.get("auto_publish") is not False:
            raise ValueError("追加续处理不自动写入图谱")
        _, parent, state = self._context(args["previous_job_id"], exclude_job=job["id"])
        grant = load_grant(self.settings, args["budget_grant"])
        selection = select_checkpoint(self.settings, parent, grant, flow=self.flow)
        if (parent.name != args["parent_run_id"] or state["paper_id"] != args["paper_id"]
                or selection != args["recovery_selection"] or digest_json(selection) != grant["selection_fingerprint"]
                or BudgetPolicy.model_validate(args["budget_policy"]).model_dump(mode="json") != grant["policy"]):
            raise ValueError("排队后来源、恢复点或预算已变化；没有启动模型")
        progress("核验追加额度和已有成果，准备" + selection["label"])
        settings, run, executor = prepare_extension(self.settings, args["budget_grant"], selection)
        progress(selection["label"] + f"，使用 {self.settings.codex.model} / {self.settings.codex.reasoning_effort}", run_id=run.name)
        verify_extension_plan(settings, run)
        frozen = _json(run / "plan.json")
        from .app_server import AppServerClient
        from .settings import CodexSettings
        client = AppServerClient(CodexSettings.model_validate(frozen["model_settings"]), live=True,
            total_timeout_seconds=frozen["reading_settings"]["total_timeout_seconds"])
        try:
            kwargs = {"client": client}
            if selection["route"] in {"reading", "dossier", "candidate", "identity", "composite_repair"}:
                kwargs["verify"] = True
            executor(settings, run / "plan.json", **kwargs)
        except ValueError as failure:
            from .retained_review import complete_failed_revision
            client.close()
            client = None
            run = complete_failed_revision(settings,run,failure,progress=progress)
        finally:
            if client is not None:
                client.close()
        self.w.library.roles_for_import(state["paper_id"])
        self.flow._source_match(self.w.library.get(state["paper_id"]), parent)
        self.w.records.refresh()
        terminal = _json(run / "result.json")
        detail = self.w.records.get(run.name)
        return {"run_id": run.name, "record_id": run.name, "status": terminal["status"],
            "recovery_action": "extend_budget", "parent_run_id": parent.name,
            "incomplete": not detail["summary"]["admissible"], "graph_published": False}
