"""Local workbench orchestration; every costly operation is an explicit job."""
from __future__ import annotations

import json
import random
import re
import stat
import threading
import uuid
from pathlib import Path

from .model_policy import require_active_model
from .storage import DataStore, hash_file, safe_name, utc_now, write_new_json
from .web_evidence import EvidenceView
from .web_jobs import JobQueue
from .web_library import CLASSIFIER_VERSION, EXTENSIONS, WebLibrary
from .web_records import WebRecords
from .collection_policy import load_collection_policy
from .web_activity import ActivityView
from .runtime_code import PROCESS_CODE_GUARD, RuntimeRestartRequired, RuntimeDispatchDeferred


class Workbench:
    def __init__(self, settings, *, runtime_code_guard=None):
        require_active_model(settings.codex)
        self.settings = settings
        self.runtime_code = runtime_code_guard or PROCESS_CODE_GUARD
        self.store = DataStore(settings.data_root)
        self.store.initialize()
        self.library = WebLibrary(settings)
        self.records = WebRecords(settings)
        self.evidence = EvidenceView(settings, self.records)
        self.jobs = JobQueue(settings.data_root)
        self.samples_root = settings.data_root / "workbench" / "samples"
        self.samples_root.mkdir(exist_ok=True)
        self.library.sync_registered_runs()
        self.records.refresh()
        self.activity = ActivityView(self)
        from .web_recovery import RecoveryWorkflow
        self.recovery = RecoveryWorkflow(self)
        self.jobs.on_terminal = self._after_terminal_job
        self._lock_file = None
        # Package startup and service readiness bracket application imports.
        # Keep read-only access available even when a concurrent edit is found.
        self.runtime_code.complete_startup()

    def runtime_status(self):
        return self.runtime_code.status()

    def require_model_ready(self):
        return self.runtime_code.require_model_ready()

    def enqueue_model_job(self, kind, payload):
        if kind not in {"extract", "recover", "recheck", "ask"}:
            raise ValueError("此入口仅接收模型处理任务")
        self.require_model_ready()
        return self.jobs.enqueue(kind, payload)

    def retry_job(self, job_id):
        if self.jobs.get(job_id)["kind"] in {"extract", "recover", "recheck", "ask"}:
            self.require_model_ready()
        return self.jobs.retry(job_id)

    def set_queue_paused(self, paused):
        if not paused:
            with self.jobs.db() as db:
                waiting_models = db.execute("SELECT 1 FROM jobs WHERE status='queued' "
                    "AND kind IN ('extract','recover','recheck','ask') LIMIT 1").fetchone()
            if waiting_models:
                self.require_model_ready()
        # Pausing is always available; a queue containing only local scans or
        # other non-model jobs is not prevented from resuming by this gate.
        self.jobs.pause(paused)
        return self.jobs.paused

    def start(self):
        # A second service must never reset or duplicate the first one's jobs.
        path = self.settings.data_root / "workbench" / "service.lock"
        lock = path.open("a+b")
        try:
            lock.seek(0)
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            import os
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise ValueError("此数据目录已有网页服务运行，请打开现有地址") from None
        self._lock_file = lock
        self.jobs.start(self.handle_job)

    def close(self):
        self.jobs.close()
        if self._lock_file:
            lock = self._lock_file
            self._lock_file = None
            worker = self.jobs.thread
            if worker and worker.is_alive():
                # Keep exclusive ownership if an in-process server is restarted
                # while the bounded current job is still completing. Process
                # exit also releases the OS lock, without resetting any history.
                def release_after_worker():
                    worker.join()
                    lock.close()
                threading.Thread(target=release_after_worker, daemon=True).start()
            else:
                lock.close()

    def papers(self):
        return self.activity.papers()

    def job_summaries(self):
        return self.activity.jobs()

    def job_detail(self, job_id):
        return self.activity.detail(job_id)

    def recovery_options(self, job_id):
        return self.recovery.inspect(job_id)

    def queue_recovery(self, job_id, action, base_fingerprint):
        self.require_model_ready()
        return self.recovery.queue(job_id, action, base_fingerprint)

    def extraction_budget(self):
        from .budget_policy import BudgetPolicy, default_policy
        properties = BudgetPolicy.model_json_schema()["properties"]
        return {"defaults": default_policy(self.settings), "limits": {
            name: {"min": field["minimum"], "max": field["maximum"]}
            for name, field in properties.items() if "minimum" in field and "maximum" in field}}

    def budget_extension_options(self, job_id, budget_policy=None):
        return self.recovery.extension_options(job_id, budget_policy=budget_policy)

    def queue_budget_extension(self, job_id, base_fingerprint, budget_policy=None):
        self.require_model_ready()
        return self.recovery.queue_extension(job_id, base_fingerprint,
                                             budget_policy=budget_policy, automatic=False)

    def _after_terminal_job(self, job):
        # The queue invokes this only after the original outcome is durable.
        # Recovery owns eligibility, lineage, progress, caps and atomic grants.
        try:
            self.require_model_ready()
        except RuntimeRestartRequired as exc:
            return {"action": "restart_required", "reason": str(exc)}
        decision = self.recovery.automatic_resume(job)
        if decision is not None:
            return decision
        return self.recovery.automatic_budget_extension(job["id"])

    def record_summaries(self):
        return self.activity.records()

    def workspace_snapshot(self, *, force_sync=False):
        return self.activity.workspace(force_sync=force_sync)

    def paper(self, paper_id):
        return next((p for p in self.papers() if p["id"] == paper_id), None) or self.library.get(paper_id)

    @staticmethod
    def _folder_picker_root(path):
        """Metadata-only check; a configured root may have changed since load."""
        try:
            metadata = path.lstat()
            if (stat.S_ISLNK(metadata.st_mode) or
                    getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
                return False, "文献根目录是符号链接或目录连接，请配置实际目录"
            if not stat.S_ISDIR(metadata.st_mode):
                return False, "文献根目录不是文件夹"
            if path.resolve(strict=True) != path:
                return False, "文献根目录的位置已改变，请检查配置"
        except (OSError, RuntimeError):
            return False, "文献根目录不可用，请检查磁盘及访问权限"
        return True, None

    def scan_folders(self, root_index=0):
        """List direct paper folders without parsing, hashing, registering or jobs.

        The browser chooses a configured root by index. A source document never
        supplies filesystem authority; links and Windows reparse points are not
        followed, even if their targets happen to remain inside the root.
        """
        if type(root_index) is not int or root_index < 0:
            raise ValueError("请选择有效的文献根目录")
        roots, reasons = [], {}
        for index, path in enumerate(self.settings.source_roots):
            available, reason = self._folder_picker_root(path)
            roots.append({"index": index, "path": str(path), "available": available})
            if reason:
                reasons[index] = reason
        if not roots:
            if root_index != 0:
                raise ValueError("请选择已配置的文献根目录")
            return {"roots": [], "root_index": 0, "root_path": None, "folders": [],
                    "errors": [{"path": "", "message": "尚未配置文献根目录，请检查项目配置"}]}
        if root_index >= len(roots):
            raise ValueError("请选择已配置的文献根目录")
        root = self.settings.source_roots[root_index]
        output = {"roots": roots, "root_index": root_index, "root_path": str(root), "folders": [], "errors": []}
        if not roots[root_index]["available"]:
            output["errors"].append({"path": str(root), "message": reasons[root_index]})
            return output
        # Read only existing inventory summaries. No library scan or extraction
        # refresh occurs merely because someone opened the directory selector.
        registered = {Path(p["folder"]): p for p in self.library.list_papers()}
        records = {r["paper_id"]: r for r in self.records.list_records() if r["current"]}
        try:
            children = list(root.iterdir())
        except OSError:
            roots[root_index]["available"] = False
            output["errors"].append({"path": str(root), "message": "无法列出文献根目录，请检查访问权限"})
            return output
        for child in children:
            try:
                metadata = child.lstat()
                if (stat.S_ISLNK(metadata.st_mode) or
                        getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
                    output["errors"].append({"path": str(child), "message": "已略过符号链接或目录连接"})
                    continue
                if not stat.S_ISDIR(metadata.st_mode):
                    continue
                folder = child.resolve(strict=True)
                if folder.parent != root or folder != child:
                    output["errors"].append({"path": str(child), "message": "文件夹位置不属于所选文献根目录，已略过"})
                    continue
                files = []
                for file in folder.iterdir():
                    if file.suffix.casefold() not in EXTENSIONS:
                        continue
                    try:
                        file_stat = file.lstat()
                        if (stat.S_ISLNK(file_stat.st_mode) or
                                getattr(file_stat, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
                            output["errors"].append({"path": str(file), "message": "已略过链接附件，未读取链接目标"})
                            continue
                        if stat.S_ISREG(file_stat.st_mode) and file.resolve(strict=True).parent == folder:
                            files.append(file)
                    except (OSError, RuntimeError):
                        output["errors"].append({"path": str(file), "message": "无法读取附件的文件信息"})
                existing = registered.get(folder)
                record = records.get(existing["id"]) if existing else None
                number = re.match(r"^(\d+)(?:_|$)", child.name)
                output["folders"].append({"path": str(folder), "name": folder.name,
                    "folder_number": int(number[1]) if number else None,
                    "file_count": len(files), "formats": sorted({p.suffix[1:].casefold() for p in files}),
                    "registered": bool(existing), "paper_id": existing["id"] if existing else None,
                    "attachment_status": existing.get("attachment_status") if existing else None,
                    "record_id": record["id"] if record else None})
            except (OSError, RuntimeError):
                output["errors"].append({"path": str(child), "message": "无法列出此文献文件夹，其他文件夹仍可选择"})
        output["folders"].sort(key=lambda item: (item["folder_number"] is None,
            item["folder_number"] if item["folder_number"] is not None else 0, item["name"].casefold()))
        return output

    def status(self):
        workspace = self.workspace_snapshot()
        # This reports the constant imported by the running process and the
        # manifest selected by its settings. Loading a collection statement
        # reads only the bounded manifest, never its source documents.
        policy_status, policy_hash = "not_configured", None
        try:
            policy = load_collection_policy(self.settings)
            if policy is not None:
                policy_status, policy_hash = "loaded", policy.manifest_sha256
        except (OSError, ValueError):
            policy_status = "unavailable"
        budget = self.extraction_budget()
        return {"version": "0.2.0", "model": self.settings.codex.model, "effort": self.settings.codex.reasoning_effort,
            "runtime_code": self.runtime_status(),
            "budget_policy_defaults": budget["defaults"], "budget_policy_limits": budget["limits"],
            "rules_version": self.settings.rules_version,
            "attachment_classifier_version": CLASSIFIER_VERSION,
            "collection_policy_path": str(self.settings.collection_policy_path) if self.settings.collection_policy_path else None,
            "collection_policy_manifest_sha256": policy_hash,
            "collection_policy_status": policy_status,
            "default_extraction_scope": "research_core" if self.settings.rules_version in {"0.7", "0.8", "0.9"} else None,
            "content_scope_policy": "design-scope-v1" if self.settings.rules_version in {"0.8", "0.9"} else None,
            "source_roots": [str(p) for p in self.settings.source_roots], "paused": workspace["paused"],
            **{key: workspace[key] for key in ("stats", "metric_membership", "activity_revision", "sync_state", "counting_basis")},
            "neo4j": {"enabled": self.settings.neo4j.enabled, "database": self.settings.neo4j.database,
                      "connection_checked": False}}

    def set_role(self, paper_id, **arguments):
        before = self.library.get(paper_id)
        after = self.library.set_role(paper_id, **arguments)
        if before["source_fingerprint"] != after["source_fingerprint"]:
            self.records.mark_source_stale(paper_id, "附件角色或归属已改变，需要按新来源重新抽取")
        return after

    def queue_extractions(self, paper_ids, auto_publish=False, cohort_role="unassigned", budget_policy=None):
        from .budget_policy import BudgetPolicy, default_policy
        self.require_model_ready()
        if not self.settings.reading.enabled:
            raise ValueError("完整阅读流程未启用，不能使用简化抽取替代")
        policy = BudgetPolicy.model_validate(default_policy(self.settings) if budget_policy is None else budget_policy).model_dump(mode="json")
        if cohort_role not in {"development", "heldout", "unassigned"}:
            raise ValueError("请选择有效的研究队列")
        identifiers = list(dict.fromkeys(paper_ids))
        if not identifiers:
            raise ValueError("请选择至少一篇文献")
        with self.recovery.lock:
            # Validate the entire selection before any job is enqueued. A new
            # batch ID must not bypass the earlier active-request deduplication.
            wanted = {}
            for paper_id in identifiers:
                self.library.roles_for_import(paper_id)
                role = "development" if self.library.get(paper_id)["cohort_role"] == "development" else cohort_role
                wanted[paper_id] = {"paper_id": paper_id, "auto_publish": auto_publish,
                                    "cohort_role": role, "budget_policy": policy}
            with self.jobs.db() as db:
                active = [self.jobs.decode(row) for row in db.execute(
                    "SELECT * FROM jobs WHERE status IN ('queued','running') AND kind IN ('extract','recover','recheck')")]
            existing = {}
            for job in active:
                payload = job.get("payload") or {}
                paper_id = payload.get("paper_id")
                if not paper_id and payload.get("record_id"):
                    summary = self.records.get(payload["record_id"])["summary"]
                    paper_id = summary.get("source_paper_id") or summary["paper_id"]
                if paper_id not in wanted:
                    continue
                if (job["kind"] == "extract" and paper_id not in existing
                        and all(payload.get(k) == v for k, v in wanted[paper_id].items())):
                    existing[paper_id] = job["id"]
                else:
                    raise ValueError("同篇文献已有排队或运行中的处理任务；不能改变其冻结额度或并行重复抽取")
            batch_id = "batch-" + uuid.uuid4().hex
            return [existing[paper_id] if paper_id in existing else self.jobs.enqueue("extract",
                    {**wanted[paper_id], "batch_id": batch_id}) for paper_id in identifiers]

    def _digitization_scope(self, record_id, series_ids):
        """Validate an explicit, bounded series request before any provider use."""
        if self.settings.rules_version not in {"0.7", "0.8", "0.9"}:
            raise ValueError("请先使用新版核心抽取规则启动服务，再提交按需曲线补抽")
        if (not isinstance(series_ids, list) or not 1 <= len(series_ids) <= 12
                or any(not isinstance(identifier, str) or not identifier.strip() for identifier in series_ids)
                or len(set(series_ids)) != len(series_ids)):
            raise ValueError("请选择 1—12 个不重复的曲线系列")
        detail = self.records.get(record_id)
        summary, record = detail["summary"], detail["record"]
        if (not summary.get("current") or not summary.get("admissible") or summary.get("source_change")):
            raise ValueError("按需补抽仅适用于来源未变、自动审查可接纳的当前版本")
        if (record.get("rule_version") not in {"0.7", "0.8", "0.9"}
                or record.get("extraction_scope") not in {"research_core", "series_digitization"}):
            raise ValueError("旧版本保留原范围，不能自动升级补抽；请先完成新版核心结果审核")
        allowed = {entry["id"] for entry in record.get("quantitative_coverage", [])
                   if entry.get("data_kind") == "time_series"
                   and entry.get("scope") != "external_benchmark" and entry.get("state") != "out_of_scope"}
        if not set(series_ids).issubset(allowed):
            raise ValueError("所选系列必须属于当前记录的本研究时间曲线，不能混入其他论文或范围外数据")
        return list(series_ids)

    def queue_digitization(self, record_id, series_ids, question=""):
        self.require_model_ready()
        targets = self._digitization_scope(record_id, series_ids)
        if not isinstance(question, str) or len(question) > 8000:
            raise ValueError("补抽说明不能超过 8000 字")
        return self.jobs.enqueue("recheck", {
            "record_id": record_id, "digitization_targets": targets,
            "question": question.strip() or "按需补抽选中的时间曲线；保留原记录与来源限制，仅增加有原文依据的点。",
        })

    def _recheck_recovery(self, job):
        """Select only the failed stage bound to this explicit queue retry.

        Artifact presence selects a route, never certifies its contents. Reaudit
        verifies the source, record and model receipts before starting work; a
        failed verification must propagate without a fallback or publication.
        """
        args = job["payload"]
        if "previous_job_id" not in args and "previous_run_id" not in args:
            return {}
        previous_job_id = args.get("previous_job_id")
        if not isinstance(previous_job_id, str) or not previous_job_id or previous_job_id == job.get("id"):
            raise ValueError("回查恢复缺少有效的原重试任务")
        previous = self.jobs.get(previous_job_id)
        if previous["kind"] != "recheck" or previous["status"] not in {"failed", "interrupted", "incomplete", "cancelled"}:
            raise ValueError("回查恢复只能使用原失败、中断、未完成或取消的回查任务")
        original = previous["payload"]
        if any(args.get(key) != original.get(key) for key in ("record_id", "question", "observation_id", "digitization_targets")):
            raise ValueError("重试的文献、问题、观测或曲线范围与原回查任务不一致")
        # A retry cancelled before execution may still refer to an earlier run.
        expected_run_id = original.get("run_id") or original.get("previous_run_id")
        run_id = args.get("previous_run_id")
        if run_id != expected_run_id:
            raise ValueError("恢复运行不属于原回查任务")
        if run_id is None:
            return {}
        if not isinstance(run_id, str):
            raise ValueError("恢复运行标识无效")
        safe_name(run_id)
        registered = next((r for r in self.store.runs() if r["run_id"] == run_id), None)
        if registered is None or registered["kind"] != "web_reaudit":
            raise ValueError("恢复运行不是已登记的网页回查")
        prior = self.store.root / "runs" / run_id
        if prior.resolve(strict=True) != prior or not prior.is_dir():
            raise ValueError("恢复运行目录位置已改变")
        if (prior / "result.json").is_file():
            if (prior / "result.json").resolve(strict=True) != prior / "result.json":
                raise ValueError("终态回查结果位置已改变")
            terminal = json.loads((prior / "result.json").read_text(encoding="utf-8"))
            if isinstance(terminal, dict) and terminal.get("status") == "needs_revision":
                if previous["status"] != "incomplete" or terminal.get("stop_reason") != "revision_budget_exhausted":
                    raise ValueError("终态候选恢复仅允许重试同一未完成回查任务")
                return {"resume_staged_run_id": run_id}
        if not (prior / "failure.json").is_file():
            return {}
        if any(path.is_file() for path in (prior / "reading_tools").glob("*/submitted_record.json")):
            return {"resume_staged_run_id": run_id}
        if (prior / "audit.v00.json").is_file():
            return {"repair_audit_run_id": run_id}
        return {}

    def handle_job(self, job, progress):
        args, kind = job["payload"], job["kind"]
        if kind in {"extract", "recover", "recheck", "ask"}:
            try:
                self.require_model_ready()
            except RuntimeRestartRequired as exc:
                # No executor has been entered. The queue may preserve this
                # exact job as queued, without a fake model failure or retry.
                self.jobs.pause(True)
                raise RuntimeDispatchDeferred(str(exc)) from exc
        if kind == "recover":
            return self.recovery.execute(job, progress)
        if kind == "scan":
            before = {p["id"]: p["source_fingerprint"] for p in self.library.list_papers()}
            folders = args.get("folders")
            papers, errors = [], []
            if folders is None:
                folders = []
                for root in self.settings.source_roots:
                    if not root.is_dir():
                        errors.append({"folder": str(root), "error": "文献根目录不可用"})
                    else:
                        folders.extend(str(p) for p in sorted(root.iterdir()) if p.is_dir() and not p.is_symlink())
            old_by_folder = {p["folder"]: p["id"] for p in self.library.list_papers()}
            for index, folder in enumerate(folders):
                progress(f"识别附件 {index+1}/{len(folders)}；不进行全文抽取")
                try:
                    scanned = self.library.scan([folder])
                    if not scanned and folder in old_by_folder:
                        self.records.mark_source_stale(old_by_folder[folder], "原文献目录已无可用附件，请检查来源")
                    for p in scanned:
                        if p["id"] in before and before[p["id"]] != p["source_fingerprint"]:
                            self.records.mark_source_stale(p["id"], "文件清单、内容或角色发生变化，请按新来源重新抽取")
                    papers.extend(scanned)
                except (ValueError, OSError) as error:
                    errors.append({"folder": folder, "error": str(error)[:600]})
                    if folder in old_by_folder:
                        self.records.mark_source_stale(old_by_folder[folder], "重新识别失败，来源完整性需要确认")
            return {"papers": len(papers), "needs_confirmation": sum(p["attachment_status"] != "ready" for p in papers),
                    "errors": errors, "incomplete": bool(errors)}
        if kind == "extract":
            return self._extract({**args, "_job_id": job.get("id")}, progress)
        if kind == "publish":
            progress("检查当前版本和来源，写入独立图谱快照")
            return self.records.publish(args["record_id"], live=True)
        if kind == "recheck":
            from .web_model import reaudit
            question = args["question"]
            if args.get("observation_id"):
                question += "\n关注 observation_id: " + args["observation_id"]
            recovery = self._recheck_recovery(job)
            if "digitization_targets" in args:
                recovery["digitization_targets"] = self._digitization_scope(args["record_id"], args["digitization_targets"])
            output = reaudit(self.settings, self.records, args["record_id"], question, progress=progress, **recovery)
            if output.get("revision", {}).get("accepted") is False:
                output["incomplete"] = True
            return output
        if kind == "ask":
            from .web_model import ask
            details, graph = self.question_scope(args["scope"], args.get("record_ids", []))
            return ask(self.settings, details, args["question"], scope=args["scope"], graph_context=graph, progress=progress)
        raise ValueError("不支持的任务")

    def _extract(self, args, progress):
        from .app_server import AppServerClient
        from .workflow import import_paper, prepare, execute_plan
        from .budget_policy import apply_initial_policy, freeze_initial_policy
        settings = apply_initial_policy(self.settings, args["budget_policy"]) if args.get("budget_policy") else self.settings
        paper = self.library.get(args["paper_id"])
        roles = self.library.roles_for_import(paper["id"])
        progress("保存来源快照，解析正文与补充材料")
        acquisition = {}
        if (paper.get("attachment_review_details") or {}).get("state") == "applied":
            acquisition["acquisition_context"] = {"review_pointer": paper["attachment_review"],
                "files": [{"id": f["id"], "path": f["path"], "sha256": f["sha256"]}
                          for f in paper["files"]]}
        imported = import_paper(settings, Path(paper["folder"]), paper_id=paper["id"], render=True,
                                source_roles=roles, include_files=list(roles), **acquisition)
        progress("准备完整阅读计划", import_run_id=imported.name)
        run = prepare(settings, imported / "sources.json", cohort_role=args["cohort_role"])
        if args.get("budget_policy"):
            freeze_initial_policy(settings, run, args["budget_policy"],
                                  args.get("batch_id"), job_id=args.get("_job_id"))
        plan_path = run / "plan.json"
        progress("全文理解与主动回查", run_id=run.name)
        frozen = json.loads(plan_path.read_text(encoding="utf-8"))
        from .settings import CodexSettings
        client = AppServerClient(CodexSettings.model_validate(frozen["model_settings"]), live=True,
                                total_timeout_seconds=frozen["reading_settings"]["total_timeout_seconds"])
        finished = threading.Event()
        def monitor():
            previous = None
            labels = {"comprehension": "阅读全文，建立最终材料与实验对应", "extraction": "抽取材料、实验与证据"}
            while not finished.wait(2):
                event = self.store.last_event(run.name, "reading_stage_started")
                stage = event.get("stage") if event else None
                if stage and stage != previous:
                    previous = stage
                    progress(labels.get(stage, "原文审查" if stage.startswith("audit") else "依据原文自动修订"), model_stage=stage)
        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        try:
            try:
                execute_plan(settings, plan_path, client=client, verify=True)
            finally:
                finished.set()
                watcher.join(timeout=3)
                client.close()
        except ValueError as failure:
            from .retained_review import complete_failed_revision
            run = complete_failed_revision(settings,run,failure,progress=progress)
        self.records.refresh()
        result = json.loads((run / "result.json").read_text(encoding="utf-8"))
        if not (run / "candidate.json").is_file():
            return {"run_id": run.name, "record_id": None, "status": result.get("status", "incomplete"),
                    "stop_reason": result.get("stop_reason"), "incomplete": True}
        detail = self.records.get(run.name)
        # Only this successful extraction can clear a source-change block.
        if self.library.get(paper["id"])["source_fingerprint"] != paper["source_fingerprint"]:
            self.records.mark_source_stale(paper["id"], "抽取期间来源角色或归属发生变化")
            raise ValueError("抽取期间来源配置改变，已保留结果，请按新来源重试")
        try:
            self.library.roles_for_import(paper["id"])
        except (ValueError, OSError):
            self.records.mark_source_stale(paper["id"], "抽取期间附件被替换、增加或移除，请重新识别")
            raise
        summary = detail["summary"]
        if (summary.get("source_change") and summary["current"] and not summary["validation_errors"] and result.get("stop_reason") == "audit_completed"
                and not (set(summary["admission_reasons"]) - {"source_changed"})):
            self.records.clear_source_changed(paper["id"], run.name)
            detail = self.records.get(run.name)
        output = {"run_id": run.name, "record_id": run.name, "status": result["status"],
                  "incomplete": not detail["summary"]["admissible"]}
        if args.get("auto_publish") and detail["summary"]["admissible"]:
            output["publish_job_id"] = self.jobs.enqueue("publish", {"record_id": run.name})
        return output

    def question_scope(self, scope, identifiers):
        if scope == "selected":
            if not identifiers:
                raise ValueError("请先选择问答使用的记录")
            details = [self.records.get(i) for i in dict.fromkeys(identifiers)]
            if any(not d["summary"]["current"] for d in details):
                raise ValueError("所选记录已有更新版本，请选择当前版本提问")
            if any(d["summary"]["source_change"] for d in details):
                raise ValueError("所选记录的附件已变更，请更新抽取后再提问")
            return details, None
        publications = self.records.publication_scope()
        if not publications:
            raise ValueError("当前还没有通过工作台发布的版本，请先入库或选择记录提问")
        if any(p["stale"] for p in publications):
            raise ValueError("已发布版本存在待更新来源或修订，请更新入库，或明确选择记录提问")
        graph = self._graph_context(publications)
        return [self.records.get(p["record_id"]) for p in publications], graph

    def _graph_context(self, publications):
        """Parameter-only, read-only retrieval over exactly the current publications."""
        from . import neo4j_connection as connection
        from neo4j import Query
        config = self.settings.neo4j
        if not config.enabled:
            raise ValueError("Neo4j 未启用，不能使用已入库范围")
        snapshots = [p["snapshot_id"] for p in publications]
        try:
            with connection._driver_factory(config.uri, auth=(config.username, connection._password(config)),
                    connection_timeout=config.timeout_seconds, connection_acquisition_timeout=config.timeout_seconds) as driver:
                with driver.session(database=config.database, default_access_mode="READ") as session:
                    rows = list(session.run(Query("MATCH (s:WTProjection) WHERE s.id IN $ids OPTIONAL MATCH (s)-[:CONTAINS]->(n) RETURN s.id AS snapshot_id,s.record_fingerprint AS record_fingerprint,count(n) AS entities", timeout=config.timeout_seconds), ids=snapshots).data())
                    if {r["snapshot_id"] for r in rows} != set(snapshots):
                        raise ValueError("部分已发布图谱快照缺失")
                    edges = list(session.run(Query("MATCH (a:WTEntity)-[r]->(b:WTEntity) WHERE a.wt_snapshot IN $ids AND b.wt_snapshot=a.wt_snapshot RETURN a.wt_snapshot AS snapshot_id,type(r) AS relation,count(*) AS count", timeout=config.timeout_seconds), ids=snapshots).data())
        except Exception:
            raise ValueError("无法读取指定新数据库的当前快照；请检查 Neo4j 是否启动以及入库状态") from None
        # Full facts and evidence are read through the version-bound record tools;
        # graph statistics establish scope, not a substitute for original evidence.
        return {"database": config.database, "snapshots": rows, "relations": edges,
                "record_mapping": publications, "scope": "current_workbench_publications"}

    def sample(self, count, random_count):
        current = [r for r in self.records.list_records() if r["current"] and r["admissible"]]
        if not current:
            raise ValueError("暂无完成自动审查的当前记录")
        count = min(count, len(current))
        random_count = min(random_count, count)
        rng = random.SystemRandom()
        random_ids = [r["id"] for r in rng.sample(current, random_count)]
        remaining = [r for r in current if r["id"] not in random_ids]
        remaining.sort(key=lambda r: (r["status"] != "candidate_audited_with_limitations", -r["observation_count"]))
        targeted = [r["id"] for r in remaining[:count-random_count]]
        sample = {"id": "sample-" + uuid.uuid4().hex, "created_at": utc_now(), "random_ids": random_ids,
            "targeted_ids": targeted, "record_ids": random_ids+targeted,
            "population": [{"id": r["id"], "hash": r["record_hash"]} for r in current],
            "targeted_reason": "优先有明确局限且观察数量多的记录",
            "interpretation": "随机抽查与定向抽查分别留痕；未审核文献不自动标为人工验证"}
        write_new_json(self.samples_root / (sample["id"]+".json"), sample)
        return sample

    def samples(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.samples_root.glob("sample-*.json"), reverse=True)]

    def answers(self):
        with self.store.connection() as db:
            rows = db.execute("SELECT run_id FROM runs WHERE kind='web_question' ORDER BY created_at DESC LIMIT 100").fetchall()
        answers = []
        for row in rows:
            path = self.settings.data_root / "runs" / row[0] / "answer.json"
            if path.is_file():
                answer = json.loads(path.read_text(encoding="utf-8"))
                answers.append({"id": row[0], **answer})
        return answers
