"""Durable, single-worker local jobs; browser lifetime never owns a model run."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .storage import digest_json, utc_now
from .runtime_code import RuntimeDispatchDeferred


class JobQueue:
    def __init__(self, data_root: Path):
        self.root = data_root / "workbench"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "jobs.sqlite3"
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.handler = None
        self.on_terminal = None
        self.lifecycle_lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,status TEXT,
                stage TEXT,payload TEXT,result TEXT,error TEXT,created_at TEXT,updated_at TEXT);
              CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,job_id TEXT,stage TEXT,at TEXT);
              CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
              INSERT OR IGNORE INTO settings VALUES('paused','false');
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "request_key" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN request_key TEXT")
            # Older workbench jobs retain all payload/history. Their current
            # payload is the only available request identity during migration.
            for row in db.execute("SELECT id,kind,payload FROM jobs WHERE request_key IS NULL").fetchall():
                key = digest_json({"kind": row["kind"], "payload": json.loads(row["payload"])})
                db.execute("UPDATE jobs SET request_key=? WHERE id=?", (key, row["id"]))
            db.execute("CREATE INDEX IF NOT EXISTS jobs_request_status ON jobs(request_key,status)")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def start(self, handler):
        with self.lifecycle_lock:
            if self.thread and self.thread.is_alive():
                return
            self.handler = handler
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                now = utc_now()
                interrupted = db.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
                db.execute("UPDATE jobs SET status='interrupted',stage='服务中断，已保留产物；可重试',updated_at=? WHERE status='running'", (now,))
                for row in interrupted:
                    db.execute("INSERT INTO events(job_id,stage,at) VALUES(?,?,?)", (row["id"], "服务重启：原运行标为中断，未自动重试", now))
                # Opening a web service is not authorization to resume costly
                # work left over from an earlier service lifetime.
                if db.execute("SELECT 1 FROM jobs WHERE status='queued' LIMIT 1").fetchone():
                    db.execute("UPDATE settings SET value='true' WHERE key='paused'")
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._work, name="wt-web-worker", daemon=True)
            self.thread.start()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)

    @staticmethod
    def decode(row):
        item = dict(row)
        for key in ("payload", "result"):
            item[key] = json.loads(item[key]) if item[key] else None
        return item

    def list(self):
        with self.db() as db:
            return [self.decode(r) for r in db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1000")]

    def get(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("任务不存在")
        return self.decode(row)

    @property
    def paused(self):
        with self.db() as db:
            return db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] == "true"

    def pause(self, paused):
        with self.db() as db:
            db.execute("UPDATE settings SET value=? WHERE key='paused'", ("true" if paused else "false",))
        self.wake.set()

    def enqueue(self, kind, payload):
        if kind not in {"scan", "extract", "publish", "ask", "recheck", "recover"}:
            raise ValueError("未知任务类型")
        if not isinstance(payload, dict):
            raise ValueError("任务参数必须是对象")
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)
        request_key = digest_json({"kind": kind, "payload": payload})
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute("SELECT id FROM jobs WHERE request_key=? AND status IN ('queued','running')", (request_key,)).fetchone()
            if duplicate:
                return duplicate[0]
            identifier = "job-" + uuid.uuid4().hex
            now = utc_now()
            db.execute("INSERT INTO jobs(id,kind,status,stage,payload,result,error,created_at,updated_at,request_key) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (identifier, kind, "queued", "等待处理", encoded, None, None, now, now, request_key))
        self.wake.set()
        return identifier

    def progress(self, job_id, stage, **payload_updates):
        with self.db() as db:
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            payload = json.loads(row[0])
            payload.update(payload_updates)
            now = utc_now()
            db.execute("UPDATE jobs SET stage=?,payload=?,updated_at=? WHERE id=?", (stage, json.dumps(payload, ensure_ascii=False), now, job_id))
            db.execute("INSERT INTO events(job_id,stage,at) VALUES(?,?,?)", (job_id, stage, now))

    def cancel(self, job_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row[0] != "queued":
                raise ValueError("只能取消等待中的任务；暂停队列不会中断正在保存的论文")
            db.execute("UPDATE jobs SET status='cancelled',stage='已取消',updated_at=? WHERE id=?", (utc_now(), job_id))

    def retry(self, job_id):
        original = self.get(job_id)
        if original["status"] not in {"failed", "interrupted", "incomplete", "cancelled"}:
            raise ValueError("仅失败、中断、未完成或已取消任务可以重试")
        if original["kind"] in {"extract", "recover"}:
            raise ValueError("请先查看恢复方案，明确选择继续处理已有结果或重新全文抽取；此入口不会自动从头重抽")
        payload = dict(original["payload"])
        payload["previous_job_id"] = job_id
        if payload.get("run_id"):
            payload["previous_run_id"] = payload.pop("run_id")
        return self.enqueue(original["kind"], payload)

    def run_one(self):
        if self.stop_event.is_set():
            return False
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if (self.stop_event.is_set() or
                    db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] == "true"):
                return False
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return False
            job = self.decode(row)
            db.execute("UPDATE jobs SET status='running',stage='开始处理',updated_at=? WHERE id=?", (utc_now(), job["id"]))
        try:
            result = self.handler(job, lambda stage, **updates: self.progress(job["id"], stage, **updates))
            status = "incomplete" if result.get("incomplete") else "completed"
            with self.db() as db:
                db.execute("UPDATE jobs SET status=?,stage=?,result=?,updated_at=? WHERE id=?", (status,
                    "未完成，已有结果保留" if status == "incomplete" else "已完成",
                    json.dumps(result, ensure_ascii=False, allow_nan=False), utc_now(), job["id"]))
        except RuntimeDispatchDeferred as error:
            # Only the service's pre-dispatch code-version gate can issue this
            # type. No model stage began: keep the same job and any reserved
            # continuation allowance, instead of manufacturing a failed retry.
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                now = utc_now()
                db.execute("UPDATE settings SET value='true' WHERE key='paused'")
                db.execute("UPDATE jobs SET status='queued',stage=?,updated_at=? WHERE id=?",
                           ("等待重启后台，任务尚未执行", now, job["id"]))
                db.execute("INSERT INTO events(job_id,stage,at) VALUES(?,?,?)",
                           (job["id"], "派发已暂停，等待重启：" + str(error)[:1600], now))
            return False
        except Exception as error:
            with self.db() as db:
                db.execute("UPDATE jobs SET status='failed',stage='执行失败，历史产物保留',error=?,updated_at=? WHERE id=?",
                           (f"{type(error).__name__}: {error}"[:1800], utc_now(), job["id"]))
            # Known global resource failures pause dispatch; a bad paper does not.
            if isinstance(error, (MemoryError, PermissionError)) or getattr(error, "errno", None) == 28:
                self.pause(True)
        self._after_terminal(job["id"])
        return True

    def _after_terminal(self, job_id):
        """Only newly finished, explicitly budgeted work may schedule a child.

        A callback never replaces the completed scientific result or the old
        failure. Restart does not replay callbacks on historical terminal jobs.
        Pausing/stopping the queue also suspends automatic budget dispatch.
        """
        if self.on_terminal is None or self.stop_event.is_set() or self.paused:
            return
        job = self.get(job_id)
        if (job["kind"] not in {"extract", "recover"}
                or job["status"] not in {"failed", "incomplete"}
                or not (job.get("payload") or {}).get("budget_policy")
                or (job.get("payload") or {}).get("auto_budget_decision") is not None):
            return
        try:
            decision = self.on_terminal(job)
            if not isinstance(decision, dict):
                raise ValueError("automatic budget callback must return an explicit decision")
            json.dumps(decision, ensure_ascii=False, allow_nan=False)
        except Exception as error:
            decision = {"action": "blocked", "reason": "自动补充额度检查未完成：" +
                        f"{type(error).__name__}: {error}"[:1200]}
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            payload = json.loads(current[0])
            # Keep a concurrent/manual decision instead of replacing it.
            if payload.get("auto_budget_decision") is not None:
                return
            payload["auto_budget_decision"] = decision
            now = utc_now()
            db.execute("UPDATE jobs SET payload=?,updated_at=? WHERE id=?",
                       (json.dumps(payload, ensure_ascii=False, allow_nan=False), now, job_id))
            if decision.get("action") == "resumed":
                label = "已用原额度安排断点续处理：" + decision["reason"]
            else:
                label = "自动补充额度：已创建后续任务" if decision.get("queued_job_id") else "自动续处理：" + str(decision.get("reason") or decision.get("action") or "待处理")
            db.execute("INSERT INTO events(job_id,stage,at) VALUES(?,?,?)", (job_id, label[:1800], now))

    def _work(self):
        while not self.stop_event.is_set():
            if not self.run_one():
                self.wake.wait(timeout=1)
                self.wake.clear()
