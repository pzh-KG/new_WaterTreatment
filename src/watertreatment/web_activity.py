"""Read-only human summaries of registered jobs, runs and retained candidates.

An activity summary is navigation, never a scientific admission decision.
New registered results are indexed once in the background by the existing
admission layer. Polling never starts providers, scans original PDFs or publishes.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import threading

from .storage import digest_json, hash_file, safe_name


COMPLETE = {"candidate_model_audited", "candidate_audited_with_limitations"}
KIND_LABELS = {"extract": "文献抽取", "scan": "目录扫描与附件识别", "publish": "同步知识图谱",
               "ask": "证据问答", "recheck": "原文回查", "recover": "继续处理已有任务"}
RECOVERY_LABELS = {"continue_candidate": "继续处理已有结果", "correct_reading": "补正阅读记录",
                   "complete_audit": "补完审查", "resume_initial": "继续首次阅读与抽取",
                   "resume_extraction": "从已完成全文理解继续抽取",
                   "extend_budget": "追加额度并继续",
                   "repair_identity": "校正文献编号并继续审查", "resume_audit": "继续未完成的局部补审",
                   "reextract": "重新全文抽取", "retry_recheck": "继续原文回查"}
STATE_LABELS = {"not_started": "尚未抽取", "queued": "等待抽取", "running": "正在处理",
                "completed": "自动处理完成", "incomplete": "已有结果，处理未完成",
                "failed": "处理失败", "interrupted": "处理已中断", "cancelled": "已取消"}
TOOL_LABELS = {"wt_read_blocks": "读取正文或 SI 文本块", "wt_page_text": "读取 PDF 页面文字",
    "wt_view": "查看或放大图表", "wt_query_candidate": "核对已有候选数据",
    "wt_patch_record": "修订局部数据", "wt_submit_record": "保存抽取候选",
    "wt_revision_state": "检查修订进度", "wt_record_schema": "查询字段规则",
    "wt_source_inventory": "查看来源目录"}


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0


def _seconds_between(start, end):
    try:
        values = [datetime.fromisoformat(str(v).replace("Z", "+00:00")) for v in (start, end)]
        values = [v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v for v in values]
        result = (values[1] - values[0]).total_seconds()
        return result if result >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _duration(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _stage(key):
    key = str(key or "")
    labels = {"comprehension": ("全文理解", "阅读正文和补充材料，建立最终材料与实验的对应关系。"),
        "extraction": ("结构化抽取", "保存材料、实验、性能、条件、来源及缺失状态。"),
        "verify": ("机器检查", "检查字段结构、实体关联及证据位置。")}
    if key in labels:
        return labels[key]
    if key.startswith("coverage_correction"):
        return "补正阅读档案", "回查缺少处置记录的内容；不把非空缺项自动标成已读。"
    if key.startswith("partial_audit_completion"):
        return "补齐局部审查说明", "继承可核验的旧审查，仅补缺少的具体判断和来源检查。"
    if key.startswith(("revision", "continuation_repair")):
        return "修订抽取结果", "根据审查问题修改相关字段，保留旧候选版本。"
    if key.startswith("audit"):
        suffix = re.search(r"(\d+)$", key)
        return ("原文审查" if not suffix or suffix[1] == "0" else "修订后复核",
                "核对数据归属、实验条件、数值及其原文证据。")
    if key.startswith("recheck"):
        return "定向原文回查", "围绕所选问题重新检查相关原文和候选数据。"
    return "模型处理", "执行本次任务所需的模型步骤。"


def _problem(error):
    """Describe controller failures without copying arbitrary exception text."""
    value = str(error or "").lower()
    if not value:
        return None
    if "timeout" in value or "time budget" in value:
        return "模型处理达到时间上限；已保存的候选和历史记录保留，不能视为审查完成。"
    if "coverage" in value or "dossier" in value:
        return "阅读覆盖或来源处置记录仍不完整，需要补齐相应记录。"
    if "source review delivery" in value or "source_delivery" in value:
        return "原文回查交付检查未通过，部分来源读取或图表查看尚未获得完整证明。"
    if "budget" in value or "prompt" in value or "too large" in value:
        return "本次输入或处理预算不足，流程停止并保留已有产物。"
    if "permission" in value or "filenotfound" in value:
        return "所需文件、程序或访问权限不可用；请检查运行环境。"
    return "本次处理发生错误，未达到完成条件；已有产物和失败记录均保留。"


def _record_result(record):
    stale = bool(record.get("source_change"))
    admitted = record.get("admissible") is True and not stale
    label = ("来源已变化，结果待更新" if stale else "自动处理完成 · 有来源限制"
             if admitted and record.get("status") == "candidate_audited_with_limitations"
             else "自动处理完成" if admitted else "已有候选，尚未通过准入")
    count = record.get("observation_count")
    return {"record_id": record["id"], "status": record.get("status"), "label": label,
            "observation_count": count, "admissible": admitted, "current": bool(record.get("current"))}


class ActivityView:
    def __init__(self, workbench):
        self.workbench = workbench
        self.root = workbench.settings.data_root.resolve()
        self._cache = {}
        self._candidate_cache = {}
        self._lock = threading.RLock()
        self._sync_lock = threading.RLock()
        self._sync_thread = None
        self._changed_indexed_runs = set()
        self._sync_state = {"state": "idle", "message": "已登记结果已同步。"}
        # Workbench has already indexed registered results at startup. Later
        # external/CLI continuations are discovered by metadata, not PDF hashes.
        self._last_sync_inventory = self._terminal_inventory()

    @staticmethod
    def _file_stamp(path):
        try:
            info = path.stat()
            return [info.st_size, info.st_mtime_ns, getattr(info, "st_ino", None)]
        except OSError:
            return None

    def _terminal_inventory(self):
        result = {}
        for meta in self.workbench.store.runs():
            run = self._run_folder(meta["run_id"])
            if run is None:
                continue
            if meta["kind"] == "import":
                result[meta["run_id"]] = ["import", self._file_stamp(run / "sources.json")]
            elif meta["kind"] == "extraction":
                candidate, terminal = self._file_stamp(run / "candidate.json"), self._file_stamp(run / "result.json")
                if candidate and terminal:
                    result[meta["run_id"]] = ["extraction", candidate, terminal,
                        self._file_stamp(run / "model_review.json"), self._file_stamp(run / "failure.json")]
        return result

    def synchronize(self, *, force=False):
        """Index newly completed registered runs once, without blocking polling.

        Source/audit verification remains in WebRecords.refresh. A failed index
        attempt is not retried every poll; a changed terminal or explicit user
        refresh is required. Scientific artifacts and job history are immutable.
        """
        inventory = self._terminal_inventory()
        with self._sync_lock:
            if self._sync_thread and self._sync_thread.is_alive():
                return dict(self._sync_state)
            if not force and inventory == self._last_sync_inventory:
                return dict(self._sync_state)
            changed = {key for key in self._last_sync_inventory if key not in inventory
                       or inventory[key] != self._last_sync_inventory[key]}
            indexed = {r["run_id"] for r in self.workbench.records.list_records()}
            self._changed_indexed_runs.update(changed & indexed)
            self._last_sync_inventory = inventory
            self._sync_state = {"state": "syncing", "message": "正在登记新完成的结果；原结果仍可查看。"}

            def refresh():
                try:
                    self.workbench.library.sync_registered_runs()
                    self.workbench.records.refresh()
                except Exception:
                    with self._sync_lock:
                        self._sync_state = {"state": "error", "message": "新结果同步未完成，请查看运行记录或手动刷新已有结果。"}
                else:
                    with self._sync_lock:
                        self._sync_state = ({"state": "error", "message": "已登记的历史结果文件状态发生变化，相关结果待重新核验；请保留旧文件并通过新版本记录修复。"}
                            if self._changed_indexed_runs else
                            {"state": "synced", "message": "已同步本地续处理结果；历史任务状态保持原样。"})

            if force:
                refresh()
            else:
                self._sync_thread = threading.Thread(target=refresh, name="wt-result-index", daemon=True)
                self._sync_thread.start()
            return dict(self._sync_state)

    def _read(self, path, *, cache=True):
        """Cache immutable/updated JSON by file identity, never persist a view."""
        try:
            path = Path(path).resolve(strict=True)
            if not path.is_relative_to(self.root):
                return {}
            stat = path.stat()
            if not path.is_file() or stat.st_size > 64_000_000:
                return {}
            fingerprint = (stat.st_mtime_ns, stat.st_size, getattr(stat, "st_ino", None))
            with self._lock:
                cached = self._cache.get(path) if cache else None
                if cached and cached[0] == fingerprint:
                    return cached[1]
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    return {}
                if len(self._cache) > 1600:
                    self._cache.clear()
                if cache:
                    self._cache[path] = fingerprint, value
                return value
        except (OSError, ValueError, RuntimeError):
            return {}

    def _run_folder(self, run_id):
        try:
            path = (self.root / "runs" / safe_name(run_id)).resolve(strict=True)
            return path if path.parent == self.root / "runs" and path.is_dir() else None
        except (OSError, ValueError, TypeError, RuntimeError):
            return None

    def _candidate(self, run):
        # Versioned temporary candidates survive provider failure even when no
        # canonical candidate.json/result.json was ever committed.
        versions = sorted((p for p in run.glob("candidate.v*.json") if re.fullmatch(r"candidate\.v\d+\.json", p.name)),
                          key=lambda p: int(p.name.split(".")[1][1:]))
        selected = versions[-1] if versions else run / "candidate.json"
        try:
            stat = selected.stat()
            fingerprint = (stat.st_mtime_ns, stat.st_size, getattr(stat, "st_ino", None))
            cached = self._candidate_cache.get(selected)
            if cached and cached[0] == fingerprint:
                return dict(cached[1])
        except OSError:
            fingerprint = None
        data = self._read(selected, cache=False)
        observations = data.get("observations")
        result = {"has_candidate": bool(data.get("paper")) and isinstance(observations, list),
                "observation_count": len(observations) if isinstance(observations, list) else None,
                "candidate_name": selected.name if data else None}
        self._candidate_cache[selected] = fingerprint, result
        return dict(result)

    def _completion_receipt_time(self, run, result):
        """First recorded completion for these exact result bytes, including an in-place delivery."""
        terminal=run/"result.json"
        if result.get("status") not in COMPLETE or not terminal.is_file() or terminal.resolve().parent!=run:
            return None
        with self.workbench.store.connection() as db:
            rows=db.execute("SELECT created_at,payload FROM run_events WHERE run_id=? AND event='candidate_completed' ORDER BY id",
                            (run.name,)).fetchall()
        if not rows:
            return None
        sha=hash_file(terminal)
        for timestamp, raw in rows:
            receipt=json.loads(raw)
            if receipt.get("result_sha256")==sha and receipt.get("status")==result["status"]:
                return timestamp
        return None

    def _run_summary(self, meta, record_by_run, *, check_completion=False):
        run = self._run_folder(meta["run_id"])
        if run is None:
            return None
        manifest = self._read(run / "manifest.json")
        plan = self._read(run / "plan.json")
        result = self._read(run / "result.json")
        failure = self._read(run / "failure.json")
        retained = self._candidate(run)
        record = record_by_run.get(meta["run_id"])
        paper_id = manifest.get("paper_id") or plan.get("paper_id") or (record or {}).get("paper_id")
        state, label = "not_started", "计划已准备，尚未运行"
        status = result.get("status", "")
        admitted=bool(record and _record_result(record)["admissible"])
        completed_at=self._completion_receipt_time(run,result) if admitted and (failure or check_completion) else None
        if failure and not completed_at:
            state, label = "failed", "处理失败，候选已保留" if retained["has_candidate"] else "抽取失败，尚无候选"
        elif admitted:
            state, label = "completed", _record_result(record)["label"]
        elif result:
            if (status in COMPLETE and result.get("stop_reason") == "audit_completed"
                    and not result.get("reading_check_issues") and not result.get("validation_error_count") and not record):
                state, label = "incomplete", "自动流程已结束，尚待准入检查"
            else:
                state, label = "incomplete", "已有候选，审查未完成" if retained["has_candidate"] else "处理未完成"
        elif retained["has_candidate"] or (run / "calls").is_dir():
            state, label = "incomplete", "已有候选，尚无完成记录" if retained["has_candidate"] else "已有运行记录，尚未完成"
        issues = result.get("reading_check_issues") or []
        reason = _problem(failure.get("error"))
        if not reason and issues:
            reason = "阅读档案仍有缺项。" if any(str(i).startswith("dossier ") for i in issues) else "原文审查或一致性检查仍有待处理项。"
        count_text = f"已保留 {retained['observation_count']} 条观测。" if retained["has_candidate"] else "尚未生成可读取的候选数据。"
        summary = count_text + ("已登记后续交付的原文审查报告。" if failure and completed_at else
                                (reason or ("自动审查完成不等于已逐项人工核验。" if state == "completed" else "")))
        return {"run_id": meta["run_id"], "paper_id": paper_id, "job_id": None,
                "created_at": meta["created_at"], "state": state, "label": label, "summary": summary,
                **retained, "record_id": record["id"] if record else None,
                "failure_reason": reason, "completion_receipt_at":completed_at, "source": "registered_run"}

    def _job_rows(self):
        # This ordinal uses persisted creation order over the complete table,
        # never the currently sorted/filterable page or the 1000-row window.
        with self.workbench.jobs.db() as db:
            rows = db.execute("SELECT *,ROW_NUMBER() OVER (ORDER BY julianday(created_at),id) AS job_number FROM jobs ORDER BY julianday(created_at) DESC,id DESC LIMIT 1000").fetchall()
        return [self.workbench.jobs.decode(row) for row in rows]

    def _snapshot(self):
        papers = self.workbench.library.list_papers()
        records = self.workbench.records.list_records()
        # A changed immutable artifact is not a new admitted version. This is
        # a conservative display restriction, not a mutation of research data.
        records = [{**r, "admissible": False, "source_change": r.get("source_change") or
                    "已登记结果文件状态发生变化，当前索引待重新核验。"}
                   if r["run_id"] in self._changed_indexed_runs else r for r in records]
        by_run = {}
        for record in records:
            by_run.setdefault(record["run_id"], record)
        jobs=self._job_rows()
        failed_runs={(job.get("payload") or {}).get("run_id") or (job.get("result") or {}).get("run_id")
                     for job in jobs if job["kind"] in {"extract","recheck","recover"}
                     and job["status"] in {"failed","interrupted","incomplete"}}
        runs = {}
        for meta in self.workbench.store.runs():
            if meta["kind"] == "extraction":
                item = self._run_summary(meta, by_run,check_completion=meta["run_id"] in failed_runs)
                if item is not None:
                    runs[meta["run_id"]] = item
        records = [{**r, "source_paper_id": (runs.get(r["run_id"]) or {}).get("paper_id") or r["paper_id"]} for r in records]
        return papers, records, jobs, runs

    @staticmethod
    def _job_identity(job, papers, records):
        payload, output = job.get("payload") or {}, job.get("result") or {}
        record_ids = payload.get("record_ids") or [payload.get("record_id") or output.get("record_id")]
        linked = [r for r in records if r["id"] in record_ids]
        paper_id = payload.get("paper_id") or (linked[0].get("source_paper_id", linked[0]["paper_id"]) if len(linked) == 1 else None)
        paper = next((p for p in papers if p["id"] == paper_id), {})
        return {"paper_id": paper_id, "folder_number": paper.get("folder_number"),
                "folder_name": paper.get("folder_name"), "paper_title": paper.get("title") or (linked[0].get("title") if len(linked) == 1 else None),
                "paper_doi": paper.get("doi") or (linked[0].get("doi") if len(linked) == 1 else None)}

    @staticmethod
    def _canonical_records(papers, records):
        known = {p["id"]: p for p in papers}
        grouped = {}
        for record in records:
            if record.get("current"):
                grouped.setdefault(record.get("source_paper_id", record["paper_id"]), []).append(record)
        chosen = {pid: max(items, key=lambda r: (_record_result(r)["admissible"],
            _timestamp(r.get("created_at")), r["id"]))["id"] for pid, items in grouped.items()}
        result = []
        for record in records:
            pid = record.get("source_paper_id", record["paper_id"])
            paper, shown = known.get(pid, {}), _record_result(record)
            current = chosen.get(pid) == record["id"]
            result.append({**record, "source_paper_id": pid, "registry_current": bool(record.get("current")),
                "workspace_current": current, "current": current, "in_library": pid in known,
                "folder_number": paper.get("folder_number"), "folder_name": paper.get("folder_name"),
                "display_status": "completed" if shown["admissible"] else "incomplete",
                "display_status_label": shown["label"],
                "result_summary": f"已保存 {shown['observation_count']} 条观测。{shown['label']}。"})
        return result

    def _enrich_job(self, job, papers, records, runs, paper_views=None):
        run_id = (job.get("payload") or {}).get("run_id") or (job.get("result") or {}).get("run_id")
        outcome = runs.get(run_id)
        result_id = (job.get("result") or {}).get("record_id") or (outcome or {}).get("record_id")
        if result_id not in {r["id"] for r in records}:
            result_id = None
        summary = (outcome or {}).get("summary") or _problem(job.get("error")) or job.get("stage") or STATE_LABELS.get(job["status"], "待处理")
        identity = self._job_identity(job, papers, records)
        current = next((r for r in records if r["current"] and r.get("source_paper_id", r["paper_id"]) == identity["paper_id"]), None)
        paper_view = next((p for p in (paper_views or []) if p["id"] == identity["paper_id"]), {})
        attempt_at = (outcome or {}).get("created_at") or job.get("created_at")
        baseline_id = (job.get("payload") or {}).get("record_id") or (job.get("result") or {}).get("record_id") or run_id
        finished_at=(outcome or {}).get("completion_receipt_at")
        same_run=bool(current and run_id and current.get("run_id")==run_id and current["id"]==result_id
                      and _timestamp(job.get("updated_at")) and _timestamp(finished_at)>_timestamp(job["updated_at"]))
        recovered = bool(job["kind"] in {"extract", "recheck", "recover"}
            and job["status"] in {"failed", "interrupted", "incomplete"}
            and current and _record_result(current)["admissible"]
            and (same_run or (current["id"] != baseline_id and _timestamp(attempt_at)
            and _timestamp(current.get("created_at")) > _timestamp(attempt_at))))
        recovery = ({"record_id": current["id"], "run_id": current.get("run_id"),
            "created_at": finished_at if same_run else current.get("created_at"), "label": "后续处理已完成", "observation_count": current.get("observation_count")}
            if recovered else None)
        return {**job, **identity, "summary": summary, "result_record_id": result_id,
            "kind_label": (RECOVERY_LABELS.get((job.get("payload") or {}).get("recovery_action"), "继续处理已有任务")
                           if job["kind"] == "recover" else KIND_LABELS.get(job["kind"], "后台任务")),
            "current_paper_state": paper_view.get("extraction", {}).get("state"),
            "current_result_id": current["id"] if current else None,
            "recovered": recovered, "recovery": recovery,
            "display_status": "recovered" if recovered else job["status"],
            "display_status_label": "后续已修复完成" if recovered else STATE_LABELS.get(job["status"], job["status"]),
            "resolution_summary": "该次失败记录保留；此文献已有后续完成的当前结果。" if recovered else None}

    def _papers(self, papers, records, jobs, runs):
        output = []
        for paper in papers:
            choices = [dict(r) for r in runs.values() if r["paper_id"] == paper["id"]]
            for job in jobs:
                if job["kind"] not in {"extract", "recover"} or (job.get("payload") or {}).get("paper_id") != paper["id"]:
                    continue
                run_id = job["payload"].get("run_id") or (job.get("result") or {}).get("run_id")
                if run_id in runs:
                    match = next((r for r in choices if r["run_id"] == run_id), None)
                    if match:
                        match["job_id"] = job["id"]
                        if job["status"] in {"running", "queued", "interrupted"}:
                            match.update(state=job["status"], label=STATE_LABELS[job["status"]], summary=job.get("stage") or match["summary"])
                    continue
                choices.append({"run_id": None, "job_id": job["id"], "created_at": job["created_at"],
                    "state": job["status"], "label": STATE_LABELS.get(job["status"], "待处理"),
                    "summary": _problem(job.get("error")) or job.get("stage") or "等待执行",
                    "has_candidate": False, "observation_count": None, "record_id": None})
            ordering = lambda r: (_timestamp(r["created_at"]), r.get("run_id") or r.get("job_id") or "")
            preparations = [r for r in choices if r["state"] == "not_started"]
            attempts = [r for r in choices if r["state"] != "not_started"]
            latest_preparation = max(preparations, key=ordering) if preparations else None
            latest = max(attempts, key=ordering) if attempts else None
            own = [r for r in records if r.get("source_paper_id", r["paper_id"]) == paper["id"]]
            current = next((r for r in own if r.get("current")), None)
            retained_choices = [r for r in choices if r["has_candidate"]]
            retained = max(retained_choices, key=ordering) if retained_choices else None
            if current and (not retained or _timestamp(current["created_at"]) >= _timestamp(retained["created_at"])):
                retained = {"has_candidate": True, "observation_count": current.get("observation_count"), "created_at": current["created_at"]}
            shown = _record_result(current) if current else None
            if shown and shown["admissible"]:
                state, label = "completed", shown["label"]
                summary = f"当前结果已保存 {shown['observation_count']} 条观测。自动完成不等于已逐项人工核验。"
                if latest and latest["run_id"] != current.get("run_id") and _timestamp(latest["created_at"]) > _timestamp(current["created_at"]):
                    summary += " 另有较新的处理尝试，当前完成结果仍保留；请查看最近运行状态。"
            else:
                state, label = (latest["state"], latest["label"]) if latest else (
                    ("incomplete", shown["label"]) if shown else ("not_started", "尚未抽取"))
                summary = latest["summary"] if latest else f"已保存 {shown['observation_count']} 条候选观测。" if shown else (
                    "已准备计划，尚未启动抽取。" if latest_preparation else "仅完成文献登记，尚未启动抽取。")
                if retained and latest and not latest["has_candidate"]:
                    summary += f" 此前已保留 {retained['observation_count']} 条候选观测。"
            extraction = {"state": state, "label": label, "summary": summary,
                "has_candidate": bool(retained), "observation_count": retained["observation_count"] if retained else None,
                "latest_run_id": latest.get("run_id") if latest else None, "latest_job_id": latest.get("job_id") if latest else None,
                "latest_at": latest.get("created_at") if latest else None, "latest_attempt": latest,
                "latest_preparation": latest_preparation,
                "active": bool(latest and latest["state"] in {"queued", "running"}),
                "current_record_id": current["id"] if current else None,
                "available_result": shown}
            output.append({**paper, "record_id": current["id"] if current else None,
                "title": current["title"] if current else paper.get("title"),
                "doi": (current.get("doi") or paper.get("doi")) if current else paper.get("doi"),
                "record_status": current.get("status") if current else None, "extraction": extraction})
        return output

    def workspace(self, *, force_sync=False):
        sync = self.synchronize(force=force_sync)
        papers, records, jobs, runs = self._snapshot()
        records = self._canonical_records(papers, records)
        papers = self._papers(papers, records, jobs, runs)
        jobs = [self._enrich_job(j, papers, records, runs, papers) for j in jobs]
        current = [r for r in records if r["current"] and r["in_library"]]
        memberships = {"papers": [p["id"] for p in papers],
            "records": sorted({r["source_paper_id"] for r in current}),
            "completed": sorted({r["source_paper_id"] for r in current if r["display_status"] == "completed"}),
            "published": sorted({r["source_paper_id"] for r in current if r.get("published_current") and not r.get("source_change")}),
            "reviewed": sorted({r["source_paper_id"] for r in current if r.get("review_status") == "partial_reviewed"}),
            "limitations": sorted({r["source_paper_id"] for r in current if r["display_status"] == "completed" and r["status"] == "candidate_audited_with_limitations"})}
        stats = {key: len(value) for key, value in memberships.items()}
        stats.update(incomplete=stats["records"] - stats["completed"],
            failed=sum(j["status"] in {"failed", "interrupted", "incomplete"} for j in jobs),
            unresolved_tasks=sum(j["status"] in {"failed", "interrupted", "incomplete"} and not j["recovered"] for j in jobs))
        stats.update(failed_historical=stats["failed"], failed_unresolved=stats["unresolved_tasks"],
                     recovered_jobs=sum(j["recovered"] for j in jobs))
        paused = self.workbench.jobs.paused
        revision = digest_json({"papers": [{k: v for k, v in p.items() if k != "files"} for p in papers],
            "records": records, "jobs": jobs, "stats": stats, "paused": paused, "sync": sync})
        return {"activity_revision": revision, "papers": papers, "records": records, "jobs": jobs,
            "stats": stats, "metric_membership": memberships, "paused": paused, "sync_state": sync,
            "counting_basis": "按已登记文献的当前版本去重；已有结果包括未完成候选，自动完成仅计通过准入的当前结果。人工抽查仅计当前版本的实际核查记录。"}

    def papers(self):
        return self.workspace()["papers"]

    def jobs(self):
        return self.workspace()["jobs"]

    def records(self, records=None):
        workspace = self.workspace()
        if records is None:
            return workspace["records"]
        indexed = {r["id"]: r for r in workspace["records"]}
        result = []
        for record in records:
            canonical = indexed.get(record["id"])
            shown = _record_result(record)
            result.append({**(canonical or record), **record,
                **({key: canonical[key] for key in ("source_paper_id", "registry_current", "workspace_current", "current", "in_library", "folder_number", "folder_name")} if canonical else {}),
                "display_status": "completed" if shown["admissible"] else "incomplete",
                "display_status_label": shown["label"], "result_summary": f"已保存 {shown['observation_count']} 条观测。{shown['label']}。"})
        return result

    def detail(self, job_id):
        self.synchronize()
        papers, records, jobs, runs = self._snapshot()
        records = self._canonical_records(papers, records)
        papers = self._papers(papers, records, jobs, runs)
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job is None:
            # Jobs older than the list window still have a stable detail route.
            with self.workbench.jobs.db() as db:
                row = db.execute("SELECT * FROM (SELECT *,ROW_NUMBER() OVER (ORDER BY julianday(created_at),id) AS job_number FROM jobs) WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("job not found")
            job = self.workbench.jobs.decode(row)
        enriched = self._enrich_job(job, papers, records, runs, papers)
        run_id = job["payload"].get("run_id") or (job.get("result") or {}).get("run_id")
        registered = {r["run_id"]: r for r in self.workbench.store.runs()}
        run = self._run_folder(run_id) if run_id in registered else None
        outcome = dict(runs.get(run_id) or {"run_id": run_id if run else None, "record_id": enriched["result_record_id"],
            "state": job["status"], "label": STATE_LABELS.get(job["status"], "待处理"), "summary": enriched["summary"],
            "has_candidate": False, "observation_count": None})
        if job["status"] in {"running", "queued", "interrupted", "cancelled"}:
            outcome.update(state=job["status"], label=STATE_LABELS[job["status"]])
        with self.workbench.jobs.db() as db:
            events = [dict(r) for r in db.execute("SELECT stage,at FROM events WHERE job_id=? ORDER BY id", (job_id,))]
            parent_id = job["payload"].get("previous_job_id")
            parent = db.execute("SELECT job_number FROM (SELECT id,ROW_NUMBER() OVER (ORDER BY julianday(created_at),id) AS job_number FROM jobs) WHERE id=?", (parent_id,)).fetchone() if parent_id else None
        timeline = [{"at": job["created_at"], "label": "任务创建", "detail": "加入本地后台队列。"}]
        timeline += [{"at": item["at"], "label": item["stage"], "detail": "实际记录的阶段更新。"} for item in events]
        if job["status"] not in {"running", "queued"}:
            timeline.append({"at": job["updated_at"], "label": STATE_LABELS.get(job["status"], job["stage"]), "detail": enriched["summary"]})
        calls, tool_totals = [], Counter()
        if run:
            tool_by_call = {}
            for path in sorted((run / "reading_tools").glob("*/request.json")):
                # Submission/revision arguments may contain a whole candidate;
                # only retain the whitelisted operation name and call binding.
                request = self._read(path, cache=False)
                name = request.get("tool")
                if name not in TOOL_LABELS:
                    name = "other_registered_tool"
                tool_totals[name] += 1
                tool_by_call.setdefault(request.get("source_review_call"), Counter())[name] += 1
            for index, path in enumerate(sorted((run / "calls").glob("*/request.json")), 1):
                # Prompts are not part of the web view or its long-lived cache.
                request = self._read(path, cache=False).get("request", {})
                result = self._read(path.with_name("result.json"))
                key = request.get("stage", "")
                label, description = _stage(key)
                state = ("completed" if result.get("status") == "schema_valid_candidate" else "failed" if result
                         else "running" if job["status"] == "running" else "incomplete")
                review_scope = request.get("citation_review")
                if key.startswith("audit") and review_scope:
                    label, description = "局部原文复核", "核对本次修订及其相关实验和原文依赖，继承可核验的旧审查。"
                calls.append({"number": index, "stage": key, "label": label, "status": state,
                    "status_label": {"completed": "本阶段已保存结果", "failed": "本阶段未完成", "running": "进行中", "incomplete": "尚无完成记录"}[state],
                    "summary": _problem(result.get("error")) or description, "elapsed_seconds": _duration(result.get("elapsed_seconds")),
                    "tool_counts": dict(tool_by_call.get(path.parent.name, {})),
                    "tools_attributed": path.parent.name in tool_by_call,
                    "model": result.get("server_selected_model") or request.get("model"),
                    "effort": result.get("server_selected_effort") or request.get("reasoning_effort")})
        stages = [{"key": c["stage"], "label": c["label"], "status": c["status"], "summary": c["summary"], "elapsed_seconds": c["elapsed_seconds"]} for c in calls]
        if not stages:
            stages = [{"key": "task", "label": KIND_LABELS.get(job["kind"], "后台任务"), "status": job["status"], "summary": enriched["summary"], "elapsed_seconds": None}]
        with self.workbench.store.connection() as db:
            run_events = [{"event": r[0], "created_at": r[1]} for r in db.execute("SELECT event,created_at FROM run_events WHERE run_id=? ORDER BY id", (run_id,))] if run else []
        run_started = next((r["created_at"] for r in run_events if r["event"] == "execution_started"), None)
        run_ended = next((r["created_at"] for r in reversed(run_events) if r["event"] in {"candidate_completed", "failed"}), None)
        # A web task starts before source import/plan preparation. Do not count
        # that work as queue delay by using the later model-run start event.
        started = next((e["at"] for e in events if not e["stage"].startswith("服务重启")), None)
        ended = job["updated_at"] if job["status"] not in {"running", "queued"} else None
        queued = _seconds_between(job["created_at"], started)
        elapsed = _seconds_between(started, ended)
        model_values = [c["elapsed_seconds"] for c in calls if c["elapsed_seconds"] is not None]
        concerns = []
        if outcome["state"] not in {"completed", "not_started", "queued", "running", "cancelled"}:
            concerns.append(outcome.get("failure_reason") or _problem(job.get("error")) or "自动流程尚未完成，不能把已保存候选当成已核验结果。")
        if outcome["has_candidate"]:
            concerns.append("观测条数表示已保存记录的数量，不代表准确率或人工核查范围。")
        artifacts = []
        if run:
            for name, label in [("plan.json", "运行计划"), ("reading_dossier.json", "全文阅读档案"),
                    ("model_review.json", "已提交的最终审查报告"), ("failure.json", "失败记录")]:
                artifacts.append({"label": label, "available": bool(self._read(run / name))})
            artifacts.append({"label": "候选抽取数据", "available": outcome["has_candidate"]})
        return {"job": enriched, "summary": enriched["summary"], "outcome": outcome,
            "recovery": enriched["recovery"], "resolution_summary": enriched["resolution_summary"],
            "timings": {"queued_seconds": queued, "execution_seconds": elapsed, "model_seconds": sum(model_values) if model_values else None,
                "run_execution_seconds": _seconds_between(run_started, run_ended),
                "basis": "任务首条执行阶段到任务结束，包含导入与准备；缺少时间记录时不估算。模型调用耗时另列。"},
            "stages": stages, "calls": calls, "timeline": timeline, "concerns": concerns, "artifacts": artifacts,
            "tool_counts": dict(tool_totals), "tool_labels": TOOL_LABELS, "retry_of_job_number": parent["job_number"] if parent else None,
            "tool_attribution_note": "本次运行累计包括未登记调用归属的旧工具日志；各阶段只统计明确关联的调用。",
            "notice": "仅展示实际保存的运行与工具记录；旧日志无法精确关联到某次模型调用的工具只计入总数。不展示模型私有推理。自动完成不等于科学数据零错误。"}
