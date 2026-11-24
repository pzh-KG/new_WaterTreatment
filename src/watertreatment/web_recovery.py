"""Explicit, bounded recovery routes for retained web/CLI research runs.

Discovery never prepares a run or calls a provider. A displayed route is an
eligibility check, not approval of the candidate: existing source/protocol
authenticators run again before any model call. No recovery publishes a graph.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from .native_repair_review import available as native_repair_available
import re
import threading
from datetime import datetime

from .storage import digest_json, hash_file, safe_name


TERMINAL = {"failed", "interrupted", "incomplete", "cancelled"}
LABELS = {
    "continue_candidate": ("继续处理已有结果", "复用已保存候选和阅读档案，完成尚未完成的审查或修订；不重新抽取。"),
    "correct_reading": ("补正阅读记录", "仅补查阅读覆盖缺项；科学候选和已有审查保持不变，发现新科学疑点则保留未完成状态。"),
    "complete_audit": ("补完审查", "仅补充可验证的审查缺项及来源交付，不重写候选或整篇重新审查。"),
    "resume_initial": ("继续首次阅读与抽取", "复用已导入的正文和 SI，从此前尚未执行的首次阅读开始；没有旧候选可续审，不重复导入或覆盖失败记录。"),
    "resume_extraction": ("从已完成全文理解继续抽取", "保留有原始模型完成证明的全文理解，从失败的首次抽取继续并完成原文审查；不重做全文理解，不增加原预算。"),
    "repair_identity": ("校正文献编号并继续审查", "依据已认证来源另存系统编号更正，保留科学数据和阅读内容，使用原剩余额度完成首次原文审查。"),
    "resume_audit": ("继续未完成的局部补审", "复用最新失败补审及其已有审查，只补缺失的审查结论；沿原审查链扣除全部已用时间和调用，不增加总预算。"),
    "reextract": ("重新全文抽取", "重新导入并完整阅读、抽取、审查，保留全部旧结果；这是独立的新抽取，不是断点续做。"),
    "retry_recheck": ("重试定向回查", "沿用原回查问题和记录；存在可验证暂存修订或已完成审查时按原回查恢复路径处理。"),
    "extend_budget": ("追加额度并继续", "明确登记有限追加额度，保留累计用量和已有成果，继续尚未完成阶段。"),
}


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class RecoveryWorkflow:
    def __init__(self, workbench):
        self.w = workbench
        self.settings = workbench.settings
        self.lock = threading.RLock()

    def _path(self, path):
        path = Path(path)
        resolved = path.resolve(strict=True)
        root = (self.settings.data_root / "runs").resolve()
        if path != resolved or root not in resolved.parents or not resolved.is_file():
            raise ValueError("恢复文件位置不属于配置的原运行目录，或已变为链接")
        return resolved

    def _run(self, run_id, *, registered=None):
        safe_name(run_id)
        row = (registered.get(run_id) if registered is not None else
               next((r for r in self.w.store.runs() if r["run_id"] == run_id), None))
        if row is None:
            raise ValueError("未找到已登记的原运行；不能从任意 JSON 恢复")
        run = self.settings.data_root / "runs" / run_id
        manifest = self._path(run / "manifest.json")
        if Path(row["manifest_path"]) != manifest:
            raise ValueError("原运行登记位置不一致")
        return run, row

    def _artifact_snapshot(self, run):
        if run is None:
            return []
        patterns = ("*.json", "calls/*/request.json", "calls/*/result.json", "calls/*/record.json",
                    "reading_tools/*/submitted_record.json")
        paths = {p for pattern in patterns for p in run.glob(pattern) if p.is_file()}
        return [{"path": str(p.relative_to(run)), "sha256": hash_file(self._path(p))}
                for p in sorted(paths)]

    def _remaining(self, run, plan):
        calls = sorted((run / "calls").glob("*/request.json"))
        elapsed, revisions = 0.0, 0
        for request in calls:
            result_path = self._path(request.with_name("result.json"))
            seconds = _json(result_path).get("elapsed_seconds")
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds) or seconds < 0:
                raise ValueError("原调用缺少可核验耗时，不能重新授予剩余预算")
            elapsed += seconds
            stage = _json(self._path(request)).get("request", {}).get("stage", "")
            revisions += int(bool(re.fullmatch(r"revision_\d+", stage)))
        total = plan.get("reading_settings", {}).get("total_timeout_seconds")
        maximum = plan.get("max_calls")
        if type(total) is not int or type(maximum) is not int or total < 60 or maximum < 1:
            raise ValueError("原计划缺少明确的调用次数或总时长预算")
        remaining = math.floor(total - elapsed)
        count = maximum - len(calls)
        return {"policy": "parent_remaining", "original_seconds": total,
            "used_seconds": round(elapsed, 3), "remaining_seconds": max(0, remaining),
            "original_calls": maximum, "used_calls": len(calls), "remaining_calls": max(0, count),
            "max_revisions": max(0, plan.get("reading_settings", {}).get("max_revisions", 0) - revisions)}

    def _prior_attempt(self, parent_id, *, exclude_job=None, exclude_jobs=(), allow_failed_finalize=False):
        for job in self.w.jobs.list():
            args = job.get("payload") or {}
            selection = args.get("recovery_selection") or {}
            if (allow_failed_finalize and job["status"] == "failed" and args.get("parent_run_id") == parent_id
                    and args.get("recovery_action") == "complete_audit" and selection.get("route") == "retained_finalize"
                    and selection.get("budget") == {"seconds": 0, "calls": 0}):
                continue
            if (job["id"] != exclude_job and job["id"] not in exclude_jobs and job["kind"] == "recover" and
                    args.get("parent_run_id") == parent_id and
                    args.get("recovery_action") != "reextract" and
                    not (job["status"] == "cancelled" and not args.get("run_id"))):
                return "已有保留的恢复任务，请查看该任务及其最新结果；不能重复使用同一父运行重置预算"
        # Includes desktop-created continuations and one-shot supplements, not
        # only web jobs. A preparation is itself retained and may own a budget.
        for row in self.w.store.runs():
            if row["run_id"] == parent_id:
                continue
            path = self.settings.data_root / "runs" / row["run_id"] / "plan.json"
            if not path.is_file():
                continue
            plan = _json(self._path(path))
            refs = [plan.get(k, {}) for k in ("reused_candidate", "coverage_correction_parent",
                    "partial_audit_parent", "audit_addendum_parent", "addendum_parent", "dossier_extraction_recovery")]
            manifest = _json(self._path(Path(row["manifest_path"])))
            if (manifest.get("parent_run") == parent_id and manifest.get("operation") or
                    any(isinstance(ref, dict) and ref.get("parent_run") == parent_id for ref in refs)):
                return "原运行已有后续恢复计划或结果，请使用后续任务；不能重复从旧候选恢复"
        return None

    def _continuation(self, run, plan, terminal, *, budget_override=None):
        """Select a bounded route; never fall back to an earlier candidate."""
        budget = dict(budget_override) if budget_override is not None else self._remaining(run, plan)
        if budget["remaining_calls"] < 1 or budget["remaining_seconds"] < 60:
            raise ValueError("原预算已用尽或不足 1 次调用/60 秒；需单独评估运行问题和恢复预算，不应直接重抽")
        if plan.get("cohort_role") not in {"unassigned", "development"}:
            raise ValueError("保留验证集不能通过修复变为独立验证；请先明确研究队列用途")
        if plan.get("rule_version") != self.settings.rules_version:
            raise ValueError("原科学规则与当前规则不同，不能在续审中悄悄升级")
        if not (run / "reading_dossier.json").is_file():
            raise ValueError("没有完整阅读档案，不能跳过全文理解直接续审")
        versions = sorted(run.glob("candidate.v[0-9][0-9].json"))
        if not versions:
            raise ValueError("没有可恢复的版本化候选；请先检查失败原因")
        candidate = self._path(versions[-1])
        if terminal.get("last_candidate_version") and terminal["last_candidate_version"] != candidate.name:
            raise ValueError("最后保存候选与终态记录不一致，不能自动选择恢复点")
        from .domain import ExtractionRecord
        seed_kind = "completed"
        staged = sorted((run / "reading_tools").glob("*/submitted_record.json"))
        if staged and _json(self._path(staged[-1])) != _json(candidate):
            # A newer saved revision cannot disappear merely because its turn
            # failed before candidate.vNN was written. Authenticate that exact
            # checkpoint or stop; never fall back to the older candidate.
            candidate = self._path(staged[-1])
            seed_kind = "staged"
        record = ExtractionRecord.model_validate(_json(candidate))
        if record.paper.id != plan.get("paper_id") or record.rule_version != plan.get("rule_version"):
            raise ValueError("候选身份或规则与原计划不同，需要先处理来源归属")
        # Existing compatibility checks are read-only and bind the dossier and
        # candidate to completed model submissions, not a downloaded/edited file.
        from .workflow import _reusable_candidate, _reusable_dossier, _reusable_staged_candidate
        bundle = _json(self._path(run / "sources.json"))
        dossier = self._path(Path(plan.get("reused_dossier", {}).get("path", run / "reading_dossier.json")))
        if hash_file(dossier) != hash_file(self._path(run / "reading_dossier.json")):
            raise ValueError("继承阅读档案与原档案不一致")
        _reusable_dossier(self.settings, dossier, bundle, plan["cohort_role"])
        if seed_kind == "staged":
            _reusable_staged_candidate(self.settings, candidate, bundle, plan["cohort_role"])
            repair_audit = None
        else:
            _, repair_audit, _ = _reusable_candidate(self.settings, candidate, bundle, plan["cohort_role"])
        budget["minimum_calls"] = 2 if repair_audit else 1
        if repair_audit and budget["max_revisions"] < 1:
            raise ValueError("原计划的修订轮次已用尽，不能通过续审重新授予修订预算")
        if budget["remaining_calls"] < budget["minimum_calls"]:
            raise ValueError("原预算不足以完成已有修订要求及后续审查；请先评估恢复预算")
        # Source-audit inheritance is mandatory for a completed native revision
        # awaiting audit. Otherwise honor its current audit and do a fresh audit
        # of the saved candidate, without repeating extraction/comprehension.
        reuse_audit = seed_kind == "completed" and int(candidate.stem[-2:]) > 0 and not candidate.with_name(candidate.name.replace("candidate.", "audit.")).is_file()
        if reuse_audit:
            from .completed_native_resume import authenticate_completed_revision
            authenticate_completed_revision(self.settings, candidate)
        source_bundle = self._path(Path(_json(self._path(run / "manifest.json")).get("source_bundle", "")))
        imported = self.w.store.last_event(source_bundle.parent.name, "import_completed")
        if not imported or imported.get("bundle_sha256") != hash_file(source_bundle) or _json(source_bundle) != bundle:
            raise ValueError("原始导入快照不可核验，不能准备候选续审")
        return {"route": "candidate", "candidate_path": str(candidate), "bundle_path": str(source_bundle),
                "dossier_path": str(dossier), "reuse_source_audit": reuse_audit, "seed_kind": seed_kind,
                "cohort_role": plan["cohort_role"], "budget": budget}

    def _source_match(self, paper, run):
        bundle = _json(self._path(run / "sources.json"))
        expected = {}
        for doc in bundle.get("documents", []):
            source = doc["source"]
            original = source.get("metadata", {}).get("original_path")
            if not original:
                raise ValueError("旧来源缺少原始附件定位，需先核对来源")
            expected[str(Path(original).resolve())] = (source["sha256"], source["role"])
        actual = {str(Path(f["path"]).resolve()): (f["sha256"], f["role"]) for f in paper["files"]
                  if f["role"] in {"main_article", "supporting_information", "main_article_with_supporting_information"}
                  and f.get("status") != "duplicate"}
        if not expected or expected != actual or bundle.get("paper_id") != paper["id"]:
            raise ValueError("当前正文/SI的文件、角色或归属与原运行不同，不能继承旧候选；请先检查来源")

    def _active_conflict(self, paper_id, *, exclude_job=None):
        for row in self.w.jobs.list():
            if row["id"] == exclude_job or row["status"] not in {"queued", "running"} or row["kind"] not in {"extract", "recover", "recheck"}:
                continue
            payload = row.get("payload") or {}
            other_paper = payload.get("paper_id")
            if not other_paper and payload.get("record_id"):
                try:
                    summary = self.w.records.get(payload["record_id"])["summary"]
                    other_paper = summary.get("source_paper_id") or summary["paper_id"]
                except (ValueError, KeyError, OSError):
                    continue
            if other_paper == paper_id:
                return "同篇文献已有排队或运行中的处理任务，请先等待该任务完成"
        return None

    def _special(self, action, run, terminal):
        if not terminal or not (run / "candidate.json").is_file() or not (run / "reading_dossier.json").is_file():
            raise ValueError("尚无可继承的已完成审查及候选，不能仅补记录或补审说明")
        if action == "correct_reading":
            if (terminal.get("status") not in {"candidate_incomplete_reading_coverage", "candidate_audited_with_limitations", "candidate_model_audited"}
                    or terminal.get("stop_reason") not in {"coverage_correction_required", "audit_completed"}
                    or not (run / "model_review.json").is_file()):
                raise ValueError("科学审查尚未完成，不能仅补正阅读记录")
            from .coverage_correction_workflow import authenticate_coverage_parent
            parent = authenticate_coverage_parent(self.settings, run)
            from .budget_grants import remaining_authority
            allowance = remaining_authority(self.settings, run)
            calls = int(bool(parent.scope["required_dispositions"]))
            if allowance is not None:
                if calls and (allowance["remaining_seconds"] < 60 or allowance["remaining_calls"] < 1):
                    raise ValueError("单篇已授权累计余额不足 60 秒或一次调用；需先追加额度才能补正阅读记录")
                return {"route": "coverage", "budget": {"policy": "cumulative_remaining",
                    "seconds": min(900, allowance["remaining_seconds"]) if calls else 900,
                    "calls": calls,
                    "description": "阅读记录补正使用单篇已授权累计余额，最多 900 秒、1 次调用；结构补正不消耗模型调用。"}}
            return {"route": "coverage", "budget": {"policy": "one_shot_coverage", "seconds": 900,
                "calls": calls,
                "description": "独立的一次性阅读记录补正预算，最多 900 秒、1 次调用；不能重复重置"}}
        if terminal.get("stop_reason") == "audit_report_incomplete":
            from .audit_addendum_workflow import authenticate_addendum_parent
            parent = authenticate_addendum_parent(self.settings, run)
            seconds = min(1200, math.floor(parent.remaining_seconds))
            if seconds < 1:
                raise ValueError("原运行剩余预算不足补审要求，不能重新授予预算")
            return {"route": "addendum", "budget": {"policy": "parent_remaining", "seconds": seconds, "calls": 1}}
        if terminal.get("status") != "candidate_incomplete_failure" or not (run / "failure.json").is_file():
            raise ValueError("没有符合条件的已完成但交付不全的局部审查；普通超时不能直接补写通过说明")
        from .partial_audit_completion_workflow import authenticate_partial_audit_parent
        parent = authenticate_partial_audit_parent(self.settings, run)
        seconds = min(900, math.floor(parent.receipt["remaining_model_seconds"]))
        if seconds < 60:
            raise ValueError("原运行剩余预算不足补审要求的 60 秒")
        return {"route": "partial", "budget": {"policy": "parent_remaining", "seconds": seconds, "calls": 1}}

    def _stage_recovery(self, action, run):
        """Dedicated authenticators for checkpoints outside ordinary audit resume."""
        if action == "resume_initial":
            from .web_initial_recovery import select_initial, select_unfinished
            terminal = _json(run / "result.json") if (run / "result.json").is_file() else {}
            return (select_unfinished if terminal.get("model_calls") == 1 else select_initial)(self.settings, run)
        if action == "resume_extraction":
            from .dossier_extraction_resume import select
            return select(self.settings, run)
        if action == "repair_identity":
            from .web_initial_recovery import select_identity
            return select_identity(self.settings, run)
        if action == "resume_audit":
            from .partial_audit_remaining_resume import select
            if (run/"partial_audit_addendum.json").is_file() and not (run/"result.json").exists():
                from .partial_audit_remaining_resume import delivered_result
                _,_,(_,proof,_,_,_) = delivered_result(self.settings,run)
                return {"route":"partial_finalize","proof_hash":digest_json(proof),
                        "budget":{"seconds":0,"calls":0}, "report_summary":"接收已交付的补审报告，复算来源证明。"}
            return select(self.settings, run)
        raise ValueError("不支持的阶段恢复方式")

    @staticmethod
    def _stage_actions(run, plan):
        """Cheap applicability hints only; each route still needs authentication."""
        if not run or not plan:
            return []
        if plan.get("workflow") in {"partial_scientific_audit_completion", "partial-audit-remaining-resume-v1"}:
            # A terminal failure can also have a result file. Only a completed
            # addendum has a report to assess instead of an interrupted call.
            completed_report = (run / "result.json").is_file() and (run / "partial_audit_addendum.json").is_file()
            return [] if completed_report else ["resume_audit"]
        candidates = sorted(run.glob("candidate.v[0-9][0-9].json"))
        if not candidates and not (run / "candidate.json").exists():
            return ["resume_extraction"] if (run / "reading_dossier.json").is_file() else ["resume_initial"]
        if candidates and (run / "reading_dossier.json").is_file():
            try:
                if (_json(candidates[-1]).get("paper", {}).get("id") != plan.get("paper_id")
                        or _json(run / "reading_dossier.json").get("paper_id") != plan.get("paper_id")):
                    return ["repair_identity"]
            except (ValueError, OSError, TypeError, AttributeError):
                pass
        return []

    def _inspect(self, job_id, *, exclude_job=None, automatic_resume=False):
        job = self.w.jobs.get(job_id)
        args, result = job.get("payload") or {}, job.get("result") or {}
        run_id = args.get("run_id") or result.get("run_id")
        original_run_id = run_id
        paper_id = args.get("paper_id")
        run = plan = terminal = None
        blockers = []
        registered = {row["run_id"]:row for row in self.w.store.runs()}
        if job["kind"] not in {"extract", "recover", "recheck"} or job["status"] not in TERMINAL:
            blockers.append("仅失败、中断、未完成或已取消的文献处理任务可以选择恢复方式")
        if run_id:
            try:
                run, row = self._run(run_id, registered=registered)
                plan = _json(self._path(run / "plan.json"))
                paper_id = paper_id or plan.get("paper_id")
                if plan.get("paper_id") and paper_id != plan["paper_id"]:
                    raise ValueError("任务和原运行的文献身份不一致")
                terminal = _json(self._path(run / "result.json")) if (run / "result.json").is_file() else {}
            except (ValueError, OSError, KeyError) as exc:
                blockers.append(str(exc))
        # Desktop continuations are registered runs, not necessarily web jobs.
        # Bind the newest executed extraction for this source paper. Never pick
        # a merely prepared run, or skip a later failed execution to reuse an
        # older, easier candidate. The action receipt makes this choice visible.
        if paper_id and job["kind"] in {"extract", "recover"}:
            executed = []
            for row in registered.values():
                if row["kind"] != "extraction":
                    continue
                candidate_run, _ = self._run(row["run_id"], registered=registered)
                manifest = _json(self._path(candidate_run / "manifest.json"))
                if manifest.get("paper_id") != paper_id or not (candidate_run / "plan.json").is_file():
                    continue
                if not ((candidate_run / "execution_started.json").is_file() or
                        list((candidate_run / "calls").glob("*/request.json")) or
                        (candidate_run / "result.json").is_file()):
                    continue
                executed.append((datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")), row["run_id"]))
            if executed:
                newest = max(executed)[1]
                run, _ = self._run(newest, registered=registered)
                run_id = newest
                plan = _json(self._path(run / "plan.json"))
                terminal = _json(self._path(run / "result.json")) if (run / "result.json").is_file() else {}
                if plan.get("paper_id") != paper_id:
                    blockers.append("后续运行的计划与登记文献身份不一致，不能自动恢复")
                if not (run / "result.json").is_file() and not (run / "failure.json").is_file():
                    blockers.append("最新运行尚未结束或中断状态未确认，请先确认其已停止，不能并发续审")
        if job["kind"] == "recheck":
            try:
                detail = self.w.records.get(args["record_id"])
                paper_id = detail["summary"].get("source_paper_id") or detail["summary"]["paper_id"]
            except (ValueError, OSError, KeyError) as exc:
                blockers.append(str(exc))
        paper = None
        source_error = None
        old_source_error = None
        if paper_id:
            try:
                paper = self.w.library.get(paper_id)
                self.w.library.roles_for_import(paper_id)
                if run:
                    try:
                        self._source_match(paper, run)
                    except (ValueError, OSError, KeyError) as exc:
                        old_source_error = str(exc)
            except (ValueError, OSError, KeyError) as exc:
                source_error = "原文附件需要先处理：" + str(exc)
        else:
            source_error = "无法确定已登记的文献，请先处理任务来源归属"
        conflict = self._active_conflict(paper_id, exclude_job=exclude_job)
        if conflict:
            blockers.append(conflict)
        current_rows = [r for r in self.w.records.list_records() if r.get("current") and
                        (r.get("source_paper_id") or r.get("paper_id")) == paper_id]
        current_rows.sort(key=lambda r: (bool(r.get("admissible") and not r.get("source_change")),
            datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))), reverse=True)
        # A later accepted result makes the failure historical; a newer pending
        # candidate must also not be overwritten by resuming an older failure.
        if run and current_rows:
            created = registered[run.name]["created_at"]
            instant = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
            if any(r.get("run_id", r["id"]) != run.name and instant(r["created_at"]) > instant(created) for r in current_rows):
                blockers.append("已有更晚的当前结果，请从最新任务或结果处理，不能恢复旧版本覆盖它")
            if any(r.get("run_id", r["id"]) == run.name and r.get("admissible") and not r.get("source_change") for r in current_rows):
                blockers.append("此运行的当前结果已完成自动处理，请直接打开结果")
        # The queue appends its dispatch receipt after committing a child. It
        # is not a scientific input. Keep old manual fingerprints replayable.
        fingerprint_args = {k:v for k,v in args.items() if k != "auto_budget_decision"} if automatic_resume else args
        fingerprint = digest_json({"job_id": job_id, "status": job["status"], "payload": fingerprint_args,
            "result": result, "paper": paper, "current": current_rows,
            "artifacts": self._artifact_snapshot(run), "blockers": blockers,
            "source_error": source_error, "old_source_error": old_source_error})
        actions, selections = [], {}
        stage_actions = [] if job["kind"] == "recheck" else self._stage_actions(run, plan)
        ids = ["retry_recheck"] if job["kind"] == "recheck" else ["continue_candidate", "correct_reading", "complete_audit", *stage_actions, "reextract"]
        prior = self._prior_attempt(run_id, exclude_job=exclude_job) if run else None
        from .retained_tool_retry import is_source_interruption
        interrupted_source_tools = (run is not None and (plan or {}).get("workflow") == "retained-issue-source-review-v1"
                                    and is_source_interruption(run))
        delivered_local_review = (run is not None and (plan or {}).get("workflow") == "retained-issue-source-review-v1"
                                  and not terminal and any((run / "calls").glob("*/answer.json")))
        for action in ids:
            label, description = LABELS[action]
            selection = {"route": action}
            technical_reason = None
            needs_budget = False
            reason = (blockers or [source_error])[0]
            if not reason and action != "reextract":
                reason = old_source_error or prior
                if prior and not old_source_error and action == "complete_audit" and delivered_local_review:
                    # Replaying a signed, fully checked receipt allocates no new calls or time.
                    reason = self._prior_attempt(run_id, exclude_job=exclude_job, allow_failed_finalize=True)
            if not reason:
                try:
                    if action == "reextract":
                        if not self.settings.reading.enabled:
                            raise ValueError("完整阅读流程未启用")
                    elif action == "retry_recheck":
                        if not detail["summary"].get("current") or detail["summary"].get("source_change"):
                            raise ValueError("回查基准不是来源未变的当前版本，请从当前结果重新指定问题")
                    elif run is None or plan is None:
                        raise ValueError("尚无可复用的已登记候选或审查运行")
                    elif (action == "complete_audit" and plan.get("workflow") == "retained-issue-source-review-v1"
                          and terminal.get("stop_reason") == "local_review_unresolved"
                          and not terminal.get("deferred_source_review_corrections")):
                        from .reviewer_text import authenticate
                        prescribed = authenticate(self.settings, run)
                        label, description = "应用模型给出的确切措辞", "逐字应用有来源依据的单条文字修订，校验全部字段差异，保留原审查报告。"
                        selection = {"route":"reviewer_text", "proof_hash":digest_json(prescribed.proof),
                                     "budget":{"seconds":0, "calls":0}}
                    elif action == "complete_audit" and delivered_local_review:
                        from .retained_review import _bound, _outcome, _public
                        records, parent, proposed, frozen, packet = _bound(self.settings, run)
                        report, proof = _public(records, run, frozen, packet)
                        _outcome(run, parent, proposed, packet, report, proof)
                        label, description = "接收已交付的局部审查", "核验已有模型报告并登记结果，此操作使用 0 次模型调用。"
                        selection = {"route": "retained_finalize", "packet_hash": packet["packet_hash"],
                                     "budget": {"seconds": 0, "calls": 0}}
                    elif (action == "complete_audit" and ((plan.get("audit_guidance")
                          and (run / "scope_rejection.v01.json").is_file() and (run / "candidate.v01.json").is_file())
                          or native_repair_available(run) or
                          plan.get("workflow")=="retained-issue-source-review-v1" and (
                              terminal.get("deferred_source_review_corrections") or interrupted_source_tools))):
                        from .retained_review import guided_proposal
                        from .budget_grants import retained_stage_budget
                        _, packet_hash = guided_proposal(self.settings, run)
                        budget = retained_stage_budget(self.settings, run)
                        label, description = "补审已保存的修订稿", "核对最新修订涉及的字段、关联对照及剩余阅读记录。"
                        selection = {"route":"retained_guided", "packet_hash":packet_hash,
                                     "budget":{**budget, "seconds":min(900, budget["remaining_seconds"]), "calls":1}}
                        needs_budget = budget["remaining_calls"] < 1 or budget["remaining_seconds"] < 60
                    elif (action == "correct_reading" and plan.get("workflow") == "reading_coverage_correction"
                          and not terminal and (run / "failure.json").is_file()):
                        from .coverage_correction_workflow import authenticate_coverage_parent
                        from .budget_grants import retained_stage_budget
                        parent = authenticate_coverage_parent(self.settings, run)
                        budget = retained_stage_budget(self.settings, run)
                        residual = parent.receipt["coverage_resume"]
                        calls = int(bool(parent.scope["required_dispositions"]))
                        label = "继续补正阅读记录"
                        description = (f"已核验并继承 {residual['inherited_blocks']} 个阅读处置，"
                            f"剩余 {residual['remaining_blocks']} 个原文块，保留 {len(residual['scientific_concerns'])} 项原报告疑点。")
                        selection = {"route": "coverage", "budget": {**budget,
                            "seconds": min(900, budget["remaining_seconds"]) if calls else 900, "calls": calls},
                            "source_proof": residual}
                        needs_budget = bool(calls and (budget["remaining_calls"] < 1 or budget["remaining_seconds"] < 60))
                    elif (action == "continue_candidate" and terminal.get("stop_reason") == "partial_audit_completion_unresolved"
                          and plan.get("workflow") in {"partial_scientific_audit_completion", "partial-audit-remaining-resume-v1"}):
                        from .audit_guidance_resume import select_composite_repair
                        from .budget_grants import retained_stage_budget
                        budget = retained_stage_budget(self.settings, run)
                        selection = select_composite_repair(self.settings, run, budget)
                        label, description = selection["label"], selection["description"]
                        needs_budget = budget["remaining_calls"] < 2 or budget["remaining_seconds"] < 60
                    elif action in stage_actions:
                        selection = self._stage_recovery(action, run)
                        if action == "resume_audit" and selection.get("report_summary"):
                            description = selection["report_summary"]
                    elif action == "continue_candidate":
                        if native_repair_available(run) or (run / "scope_rejection.v01.json").is_file():
                            raise ValueError("已有原文首审和修订稿，应从补审已保存的修订稿继续，保留首审")
                        if terminal.get("stop_reason") == "coverage_correction_required":
                            raise ValueError("此运行应先使用补正阅读记录，不需要重新审查科学候选")
                        if automatic_resume and plan.get("workflow") != "active_reading":
                            raise ValueError("已有独立审查步骤，请从对应的原文补审或阅读补正恢复")
                        selection = self._continuation(run, plan, terminal)
                        if automatic_resume and selection["seed_kind"] == "completed":
                            candidate_path = Path(selection["candidate_path"])
                            audit_path = candidate_path.with_name(candidate_path.name.replace("candidate.", "audit."))
                            if not audit_path.is_file():
                                stage = f"audit_{int(candidate_path.stem[-2:])}"
                                if any(p.with_name("answer.json").is_file() and
                                       _json(p).get("request", {}).get("stage") == stage
                                       for p in (run / "calls").glob("*/request.json")):
                                    raise ValueError("已有审查报告交付，请先补验该报告")
                            if audit_path.is_file():
                                from .audit_guidance_resume import authenticate_guidance_parent
                                guidance = authenticate_guidance_parent(self.settings, run, candidate_path)
                                budget = selection["budget"]
                                if budget["remaining_calls"] < 2 or budget["max_revisions"] < 1:
                                    raise ValueError("原额度不足以修订并局部复审已保留的审查问题")
                                selection.update(guidance=True, minimum_calls=2, source_proof=guidance["receipt"])
                    else:
                        selection = self._special(action, run, terminal)
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    reason = str(exc)
                    if not re.search(r"[\u4e00-\u9fff]", reason) or isinstance(exc, OSError):
                        technical_reason = reason
                        reason = ("不满足该补正/补审的来源与审查条件；请查看技术原因或运行详情" if action in {"correct_reading", "complete_audit"}
                                  else "无法核验已有候选、阅读档案或来源证明；需先诊断该运行，不能自动重抽替代")
            if needs_budget:
                required = selection.get("minimum_calls", 1)
                reason = f"此阶段需要 {required} 次调用，单篇剩余 {budget['remaining_calls']} 次、{budget['remaining_seconds']} 秒。可先查看追加额度。"
            available = reason is None
            item = {"id": action, "label": label, "description": description, "available": available,
                    "reason": reason, "budget": selection.get("budget"),
                    "requires_model": selection.get("budget", {}).get("calls", 1) != 0,
                    "technical_reason": technical_reason,
                    "preflight_note": "执行前再次核验来源、候选和模型调用证明；不通过则停止，不会自动退回全文重抽"}
            actions.append(item)
            if needs_budget:
                item["budget_preview"] = True
            if available:
                selections[action] = selection
        if "correct_reading" in selections and "continue_candidate" in selections:
            selections.pop("continue_candidate")
            next(a for a in actions if a["id"] == "continue_candidate").update(available=False,
                reason="科学审查已经完成，此运行只需补正阅读记录，不应重复审查整篇候选")
        if "complete_audit" in selections and "continue_candidate" in selections:
            selections.pop("continue_candidate")
            next(a for a in actions if a["id"] == "continue_candidate").update(available=False,
                reason="已保留可继承的部分审查，此运行应使用补完审查，避免重复提交整个候选")
        candidate = None
        if run:
            versions = sorted(run.glob("candidate.v[0-9][0-9].json"))
            candidate = versions[-1] if versions else run / "candidate.json"
        summary = {"has_candidate": bool(candidate and candidate.is_file()), "status": (terminal or {}).get("status")}
        if summary["has_candidate"]:
            try:
                parsed = _json(self._path(candidate))
                summary.update(candidate_file=candidate.name, candidate_id=run_id,
                               observation_count=len(parsed.get("observations", [])), readable=True)
            except (ValueError, OSError, TypeError, AttributeError):
                summary.update(candidate_file=candidate.name, candidate_id=run_id, readable=False,
                    summary="候选文件无法完整解析；恢复已禁用，需要先检查原运行")
                for item in actions:
                    if item["id"] != "reextract":
                        item.update(available=False, reason=summary["summary"])
                        selections.pop(item["id"], None)
        if "continue_candidate" in selections and selections["continue_candidate"].get("seed_kind") == "staged":
            path = Path(selections["continue_candidate"]["candidate_path"])
            summary.update(has_candidate=True, candidate_file=str(path.relative_to(run)), candidate_id=run_id,
                           observation_count=len(_json(path).get("observations", [])), staged=True)
        recommended = next((a for a in ("correct_reading", "complete_audit", "resume_audit", "repair_identity", "resume_extraction", "resume_initial", "continue_candidate", "retry_recheck") if a in selections), None)
        budget_summary = None
        if run and plan:
            try:
                if plan.get("workflow") == "retained-issue-source-review-v1":
                    from .budget_grants import retained_stage_budget
                    budget_summary = retained_stage_budget(self.settings, run)
                else:
                    budget_summary = self._remaining(run, plan)
                budget_summary["exhausted"] = budget_summary["remaining_calls"] < 1 or budget_summary["remaining_seconds"] < 60
            except (ValueError, OSError, KeyError, TypeError) as exc:
                budget_summary = {"message": str(exc), "unknown": True}
        # A child supplement's own limit is not the unused allowance of the
        # entire audit chain. Show only the authenticated lineage calculation.
        if ((recommended and selections[recommended].get("budget", {}).get("policy") == "lineage_remaining")
                or recommended in {"resume_initial", "resume_extraction", "repair_identity", "resume_audit"}):
            budget_summary = selections[recommended]["budget"]
        pending_budget = next((a for a in actions if a.get("budget_preview")), None)
        if pending_budget and not recommended:
            budget_summary = pending_budget["budget"]
        explanations = {
            "resume_initial": "此前停止在首次阅读开始之前，尚无候选 JSON。请使用“继续首次阅读与抽取”；已有结果续审、阅读记录补正和补完审查不适用于这个阶段。",
            "resume_extraction": "此前已完成全文理解，首次抽取失败且尚无候选 JSON。保留已认证理解，从抽取继续并完成首次原文审查；下方额度扣除整条恢复链已用调用和耗时。",
            "repair_identity": "已有候选和阅读档案需要先校正系统文献编号。该操作保留科学内容，并接续尚未开始的首次原文审查。",
            "resume_audit": "最新补审自己的额度已结束，但原审查链可能仍有未分配余额。下方可用额度已扣除整条链的实际消耗；只继续局部补审，不重审整篇。",
        }
        explanation = explanations.get(recommended)
        if (not recommended and run and plan and plan.get("workflow") == "retained-issue-source-review-v1"
                and terminal.get("stop_reason") == "local_review_unresolved" and not blockers):
            explanation = ("最新候选和原文审查回执均已保留。可根据该回执指出的剩余字段提出局部修订，交给独立原文审查。"
                           "下方列出该文献整个处理过程的已用和剩余额度，局部修订须独立核对原文。")
            for item in actions:
                if item["id"] == "continue_candidate" and not item["available"]:
                    item["reason"] = "本次保存的是局部原文审查回执，请从它注明的剩余问题继续。"

        if not explanation and stage_actions:
            explanation = "阶段恢复入口尚未通过来源、完成记录或预算核验，请查看各项原因；不会自动改为全文重抽。"
        if recommended in stage_actions:
            stage_reasons = {
                "resume_initial": {
                    "continue_candidate": "首次阅读尚未开始，没有已有候选可续审。",
                    "correct_reading": "尚未生成阅读档案，不能仅补正阅读记录。",
                    "complete_audit": "尚未开始科学审查，不能仅补交审查说明。"},
                "resume_extraction": {
                    "continue_candidate": "尚无候选 JSON；请从已完成的全文理解继续生成候选。",
                    "correct_reading": "科学候选与首次审查尚未完成，此时不能仅补正阅读记录。",
                    "complete_audit": "尚未开始首次原文审查，需要先完成抽取。"},
                "repair_identity": {
                    "continue_candidate": "请先校正系统文献编号，再沿用已有候选继续首次审查。",
                    "correct_reading": "首次科学审查尚未完成，此时不能仅补正阅读记录。",
                    "complete_audit": "本篇尚未开始首次原文审查，不能仅补交局部审查说明。"},
                "resume_audit": {
                    "continue_candidate": "当前子运行额度已用尽；原审查链仍有余额，请使用“继续未完成的局部补审”。",
                    "correct_reading": "需先补齐科学审查结论，再判断阅读记录是否需要补正。",
                    "complete_audit": "本篇已有一次失败的补审，需沿最新补审记录续做，不能从旧父审查重新启动。"},
            }
            for item in actions:
                if item["id"] in {"continue_candidate", "correct_reading", "complete_audit"} and not item["available"]:
                    item["applicable"] = False
                    item["reason"] = stage_reasons[recommended][item["id"]]
        if pending_budget and not recommended:
            explanation = pending_budget["description"] + pending_budget["reason"]
        fingerprint = digest_json({"base": fingerprint, "selections": selections})
        public = {"job_id": job_id, "paper_id": paper_id, "parent_run_id": run_id,
            "original_run_id": original_run_id,
            "base_fingerprint": fingerprint, "recommended_action": recommended, "actions": actions,
            "candidate_summary": summary, "blockers": blockers + ([source_error] if source_error else []),
            "budget_summary": budget_summary, "current_result_id": current_rows[0]["id"] if current_rows else None,
            "recovery_explanation": explanation,
            "model": self.settings.codex.model, "effort": self.settings.codex.reasoning_effort, "graph_published": False}
        return public, selections

    def inspect(self, job_id):
        return self._inspect(job_id)[0]

    def extension_options(self, job_id, budget_policy=None):
        from .budget_extension_workflow import BudgetExtensionWorkflow
        return BudgetExtensionWorkflow(self).inspect(job_id, budget_policy)[0]

    def queue_extension(self, job_id, base_fingerprint, budget_policy=None, *, automatic=False):
        from .budget_extension_workflow import BudgetExtensionWorkflow
        return BudgetExtensionWorkflow(self).queue(job_id, base_fingerprint, budget_policy, automatic=automatic)

    def automatic_budget_extension(self, job_id):
        from .budget_extension_workflow import BudgetExtensionWorkflow
        return BudgetExtensionWorkflow(self).automatic(job_id)

    def automatic_resume(self, job):
        """Queue an authenticated unfinished stage against the same paper's existing allowance."""
        from .budget_extension_workflow import progress_signature

        args = job.get("payload") or {}
        run_id = args.get("run_id") or (job.get("result") or {}).get("run_id")
        if not args.get("budget_policy") or not run_id:
            return None
        if job["status"] not in {"failed", "incomplete"} or job["kind"] not in {"extract", "recover"}:
            return {"action":"not_eligible", "reason":"此任务状态不适合自动续处理。"}
        if re.search(r"\b(?:http(?:/\S+)?|status(?: code)?)\s*[:=]?\s*(?:401|403|429)\b|"
                     r"\b(?:401 unauthorized|403 forbidden|429 too many requests)\b|"
                     r"insufficient_quota|usage[ _-]limit|rate[ _-]limit|not authenticated|unauthorized|out of disk|no space left",
                     job.get("error") or "", re.I):
            return {"action":"not_eligible", "reason":"服务返回账户、额度或资源错误，已保留进度，等待恢复。"}
        with self.lock:
            if self.w.jobs.paused or self.w.jobs.stop_event.is_set():
                return {"action":"not_eligible", "reason":"队列已暂停或正在停止。"}
            state, selections = self._inspect(job["id"], automatic_resume=True)
            if state["blockers"] or state["parent_run_id"] != run_id:
                return {"action":"not_eligible", "reason":next(iter(state["blockers"]), "同篇已有更新运行，请查看最新任务。")}
            run, _ = self._run(run_id)
            signature = progress_signature(run)
            action = state["recommended_action"]
            zero_calls = bool(action and selections[action]["budget"].get("calls") == 0)
            ancestor = job
            seen = set()
            while True:
                if ancestor["id"] in seen:
                    raise ValueError("续处理任务链存在循环")
                seen.add(ancestor["id"])
                previous = ancestor.get("payload") or {}
                grant_signature = (previous.get("recovery_selection") or {}).get("progress_signature")
                if ((previous.get("automatic_checkpoint") == signature or grant_signature == signature)
                        and not (zero_calls and previous.get("recovery_action") != action)):
                    return {"action":"no_progress", "reason":"上次续处理没有产生新的候选、阅读档案或审查结论，已停止自动重试。"}
                if not previous.get("automatic_checkpoint") or not previous.get("previous_job_id"):
                    break
                ancestor = self.w.jobs.get(previous["previous_job_id"])
                if ancestor["payload"].get("paper_id") != state["paper_id"]:
                    raise ValueError("续处理任务链文献身份不一致")
            if not action:
                return None
            selection = selections[action]
            # Never restart initial reading simply because a failed task had no
            # saved reading or candidate. The explicit preprovider route proves
            # no model turn started. Interrupted reading must have public work.
            if selection["route"] == "unfinished_reading" and not any(
                    selection["public_progress"].get(k) for k in ("source_views","public_comments")):
                return {"action":"no_progress","reason":"首次阅读尚无可继承的原文交付或公开进度，需要先检查失败原因。"}
            if action == "reextract" or action == "retry_recheck":
                return {"action":"not_eligible","reason":"此操作需手动选择。"}
            budget = selection["budget"]
            calls = budget.get("calls", budget.get("remaining_calls"))
            if not self._within_original_allowance(run, job, selection):
                return None
            if self.w.jobs.paused or self.w.jobs.stop_event.is_set():
                return {"action":"not_eligible", "reason":"排队前服务已暂停或正在停止。"}
            child = self._enqueue_selection(job, state, action, selection, automatic_checkpoint=signature)
            label = next(a["label"] for a in state["actions"] if a["id"] == action)
            return {"action":"resumed", "queued_job_id":child, "reason":label + (
                "。已交付的结果可在本机核验，此步骤使用 0 次模型调用。" if calls == 0 else "。沿用单篇已授权的剩余额度。"),
                "added_seconds":0, "added_calls":0}

    def _within_original_allowance(self, run, job, selection):
        from .budget_grants import ADOPTED, remaining_authority
        from .budget_policy import verify_initial_policy
        args = job["payload"]
        available = remaining_authority(self.settings, run)
        if available is None:
            raise ValueError("没有可核验的单篇批次预授权。")
        root = self.settings.data_root / "runs" / available["root_run"]
        frozen = verify_initial_policy(self.settings, root)
        if frozen:
            authorized = (frozen["policy"] == args["budget_policy"] and frozen["paper_id"] == args["paper_id"]
                          and frozen["batch_id"] == args.get("batch_id"))
        else:
            # A historical job may have adopted a policy by an explicit grant.
            # remaining_authority already replayed that same paper's chain and
            # verified the adopted source hash. Batch defaults confer no rights.
            adopted = self.w.store.last_event(root.name, ADOPTED) or {}
            authorized = adopted.get("manual_authorization") is True and adopted.get("policy") == args["budget_policy"]
        if not authorized:
            raise ValueError("当前批次与原始冻结的单篇额度不一致。")
        budget = selection["budget"]
        calls = budget.get("calls", budget.get("remaining_calls"))
        seconds = budget.get("seconds", budget.get("remaining_seconds"))
        if type(calls) is not int or type(seconds) is not int or min(calls, seconds) < 0:
            raise ValueError("未能核验续处理额度")
        return calls == 0 or (calls <= available["remaining_calls"] and seconds <= available["remaining_seconds"])

    def queue(self, job_id, action, base_fingerprint):
        if action == "extend_budget":
            return self.queue_extension(job_id, base_fingerprint)
        if action not in LABELS:
            raise ValueError("未知的恢复方式")
        with self.lock:
            for item in self.w.jobs.list():
                args = item.get("payload") or {}
                if (item["kind"] == "recover" and item["status"] in {"queued", "running"} and
                        args.get("previous_job_id") == job_id and args.get("recovery_action") == action and
                        args.get("base_fingerprint") == base_fingerprint):
                    return item["id"]
            state, selections = self._inspect(job_id)
            if state["base_fingerprint"] != base_fingerprint:
                raise ValueError("原运行、来源或当前结果已变化，请重新查看恢复方案")
            if action not in selections:
                reason = next((a["reason"] for a in state["actions"] if a["id"] == action), "此任务不支持该恢复方式")
                raise ValueError(reason)
            if action == "retry_recheck":
                return self.w.jobs.retry(job_id)
            original = self.w.jobs.get(job_id)
            return self._enqueue_selection(original, state, action, selections[action])

    def _enqueue_selection(self, original, state, action, selection, *, automatic_checkpoint=None):
        job_id, args = original["id"], original["payload"]
        payload = {"previous_job_id": job_id, "parent_run_id": state["parent_run_id"],
            "paper_id": state["paper_id"], "recovery_action": action, "base_fingerprint": state["base_fingerprint"],
            "recovery_selection": selection, "auto_publish": False, "root_job_id":args.get("root_job_id", job_id)}
        for key in ("budget_policy", "batch_id"):
            if key in args:
                payload[key] = args[key]
        if automatic_checkpoint:
            payload["automatic_checkpoint"] = automatic_checkpoint
        if action == "reextract" and "budget_policy" not in payload:
            from .budget_policy import default_policy
            payload["budget_policy"] = default_policy(self.settings)
            payload["budget_policy"]["max_auto_additions"] = 0
        return self.w.jobs.enqueue("recover", payload)

    def execute(self, job, progress):
        args = job["payload"]
        action = args.get("recovery_action")
        if action == "extend_budget":
            from .budget_extension_workflow import BudgetExtensionWorkflow
            return BudgetExtensionWorkflow(self).execute(job, progress)
        inspection = {"automatic_resume": True} if args.get("automatic_checkpoint") else {}
        state, selections = self._inspect(args["previous_job_id"], exclude_job=job["id"], **inspection)
        if (state["base_fingerprint"] != args.get("base_fingerprint") or action not in selections or
                selections[action] != args.get("recovery_selection") or state["paper_id"] != args.get("paper_id") or
                state["parent_run_id"] != args.get("parent_run_id") or args.get("auto_publish") is not False):
            raise ValueError("排队后来源、恢复点或预算已改变，请重新检查恢复方案；未调用模型")
        if action == "reextract":
            progress("明确启动新的全文抽取，保留旧版本，不自动入库")
            paper = self.w.library.get(state["paper_id"])
            return self.w._extract({"paper_id": state["paper_id"], "auto_publish": False,
                "cohort_role": "development" if paper.get("cohort_role") == "development" else "unassigned",
                "budget_policy": args.get("budget_policy"), "batch_id": args.get("batch_id"),
                "_job_id": job["id"]}, progress)
        selection = selections[action]
        run = self.settings.data_root / "runs" / state["parent_run_id"]
        if args.get("automatic_checkpoint") and not self._within_original_allowance(run, job, selection):
            raise ValueError("排队后单篇可用额度已改变，请重新核验。")
        progress("核验原运行并准备" + LABELS[action][0])
        if selection["route"] in {"retained_finalize", "retained_guided", "reviewer_text","partial_finalize"}:
            from . import retained_review
            settings = self.settings
            if selection["route"] == "retained_guided":
                proposal, packet_hash = retained_review.guided_proposal(settings, run)
                if packet_hash != selection["packet_hash"]:
                    raise ValueError("已保存的修订稿、来源或问题范围发生变化")
                new_run = retained_review.prepare(settings, run, proposal, seconds=selection["budget"]["seconds"])
                progress("正在复核修订涉及的来源与对照", run_id=new_run.name)
                retained_review.execute(settings, new_run, live=True)
            elif selection["route"] == "reviewer_text":
                from .reviewer_text import apply as apply_reviewer_text
                new_run = apply_reviewer_text(settings, run, expected_proof=selection["proof_hash"])
            elif selection["route"] == "partial_finalize":
                from .partial_audit_remaining_resume import finalize_delivered
                new_run = run
                finalize_delivered(settings,new_run)
            else:
                new_run = run
                retained_review.finalize(settings, new_run)
            self.w.library.roles_for_import(state["paper_id"])
            self._source_match(self.w.library.get(state["paper_id"]), run)
            self.w.records.refresh()
            detail = self.w.records.get(new_run.name)
            return {"run_id":new_run.name, "record_id":new_run.name, "status":_json(new_run/"result.json")["status"],
                    "recovery_action":action, "parent_run_id":run.name, "incomplete":not detail["summary"]["admissible"],
                    "graph_published":False}
        if selection["route"] in {"initial_reading", "unfinished_reading", "identity_candidate"}:
            from .web_initial_recovery import prepare_selected, execute_selected
            settings, new_run = prepare_selected(self.settings, run, selection)
            plan_path, executor = new_run / "plan.json", execute_selected
        elif selection["route"] == "dossier_extraction":
            from .dossier_extraction_resume import prepare_selected, execute_selected
            settings, new_run = prepare_selected(self.settings, run, selection)
            plan_path, executor = new_run / "plan.json", execute_selected
        elif selection["route"] == "composite_repair":
            from .workflow import prepare, execute_plan
            budget = selection["budget"]
            settings = self.settings.model_copy(deep=True)
            settings.codex.max_calls_per_run = min(settings.codex.max_calls_per_run, budget["remaining_calls"])
            settings.reading.total_timeout_seconds = min(settings.reading.total_timeout_seconds, budget["remaining_seconds"])
            settings.reading.max_revisions = min(settings.reading.max_revisions, settings.codex.max_calls_per_run // 2)
            new_run = prepare(settings, Path(selection["bundle_path"]), cohort_role=selection["cohort_role"],
                reuse_dossier=Path(selection["dossier_path"]), reuse_composite_run=run)
            plan_path, executor = new_run / "plan.json", execute_plan
        elif selection["route"] == "candidate":
            from .workflow import prepare, execute_plan
            budget = selection["budget"]
            settings = self.settings.model_copy(deep=True)
            settings.codex.max_calls_per_run = min(settings.codex.max_calls_per_run, budget["remaining_calls"])
            settings.reading.total_timeout_seconds = min(settings.reading.total_timeout_seconds, budget["remaining_seconds"])
            settings.reading.max_revisions = min(settings.reading.max_revisions, budget["max_revisions"])
            settings.codex.timeout_seconds = min(settings.codex.timeout_seconds, settings.reading.total_timeout_seconds)
            seed = {"reuse_staged_candidate" if selection.get("seed_kind") == "staged" else "reuse_candidate": Path(selection["candidate_path"])}
            new_run = prepare(settings, Path(selection["bundle_path"]), cohort_role=selection["cohort_role"],
                reuse_dossier=Path(selection["dossier_path"]), **seed,
                reuse_source_audit=selection["reuse_source_audit"])
            if selection.get("guidance"):
                from .audit_guidance_resume import freeze_guidance
                freeze_guidance(settings, new_run, run, Path(selection["candidate_path"]))
            plan_path, executor = new_run / "plan.json", execute_plan
        else:
            settings = self.settings
            if selection["route"] == "coverage":
                from .coverage_correction_workflow import prepare, execute as executor
            elif selection["route"] == "partial":
                from .partial_audit_completion_workflow import prepare, execute as executor
            elif selection["route"] == "addendum":
                from .audit_addendum_workflow import prepare_audit_addendum as prepare, execute_audit_addendum as executor
            elif selection["route"] == "partial_resume":
                from .partial_audit_remaining_resume import prepare, execute as executor
            else:
                raise ValueError("不支持的冻结恢复路径；不会自动重抽")
            plan_path = prepare(settings, run, timeout_seconds=selection["budget"]["seconds"])
            new_run = plan_path.parent
        progress(LABELS[action][0] + f"，使用 {settings.codex.model} / {settings.codex.reasoning_effort}", run_id=new_run.name)
        frozen = _json(plan_path)
        from .app_server import AppServerClient
        from .settings import CodexSettings
        client = None
        try:
            if frozen.get("max_calls", 0):
                config = CodexSettings.model_validate(frozen["model_settings"])
                client = AppServerClient(config, live=True, total_timeout_seconds=frozen["reading_settings"]["total_timeout_seconds"])
            if selection["route"] in {"candidate", "composite_repair", "initial_reading", "unfinished_reading", "identity_candidate", "dossier_extraction"}:
                executor(settings, plan_path, client=client, verify=True)
            else:
                executor(settings, plan_path, client=client)
        except ValueError as failure:
            from .retained_review import complete_failed_revision
            if client is not None:
                client.close()
                client = None
            new_run = complete_failed_revision(settings,new_run,failure,progress=progress)
        finally:
            if client is not None:
                client.close()
        try:
            self.w.library.roles_for_import(state["paper_id"])
            self._source_match(self.w.library.get(state["paper_id"]), run)
        except (ValueError, OSError, KeyError):
            self.w.records.mark_source_stale(state["paper_id"], "恢复期间来源文件、角色或归属发生变化；新旧结果均保留")
            raise
        self.w.records.refresh()
        terminal = _json(new_run / "result.json")
        detail = self.w.records.get(new_run.name)
        return {"run_id": new_run.name, "record_id": new_run.name, "status": terminal["status"],
            "recovery_action": action, "parent_run_id": run.name,
            "incomplete": not detail["summary"]["admissible"], "graph_published": False}
