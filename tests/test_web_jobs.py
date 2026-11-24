"""Offline queue lifecycle checks, using local handlers rather than providers."""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from watertreatment.web_jobs import JobQueue


@pytest.fixture
def queue(tmp_path):
    result = JobQueue(tmp_path / "data")
    yield result
    result.close()


def test_start_with_restored_jobs_waits_for_explicit_continue(queue, monkeypatch):
    calls = []
    job_id = queue.enqueue("extract", {"paper_id": "synthetic-study"})
    # One real worker pass is sufficient to detect accidental dispatch, without
    # depending on a wall-clock race with a continuously running thread.
    monkeypatch.setattr(queue, "_work", lambda: queue.run_one())
    queue.start(lambda job, progress: calls.append(job["id"]) or {})
    queue.thread.join(timeout=2)
    assert not queue.thread.is_alive()
    assert calls == []
    assert queue.paused
    assert queue.get(job_id)["status"] == "queued"
    queue.pause(False)
    assert queue.run_one()
    assert calls == [job_id]
    assert queue.get(job_id)["status"] == "completed"


def test_restart_preserves_old_result_and_marks_running_interrupted(queue, monkeypatch):
    running = queue.enqueue("extract", {"paper_id": "a"})
    pending = queue.enqueue("publish", {"record_id": "b"})
    with queue.db() as db:
        db.execute("UPDATE jobs SET status='running',payload=? WHERE id=?",
                   (json.dumps({"paper_id": "a", "run_id": "retained-extraction"}), running))
    monkeypatch.setattr(queue, "_work", lambda: queue.run_one())
    queue.start(lambda *_: pytest.fail("startup must not invoke a model or graph handler"))
    queue.thread.join(timeout=2)
    assert queue.get(running)["status"] == "interrupted"
    assert queue.get(running)["payload"]["run_id"] == "retained-extraction"
    assert queue.get(pending)["status"] == "queued"
    assert queue.paused
    with queue.db() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE job_id=?", (running,)).fetchone()[0] == 1


def test_new_empty_start_does_not_call_handler_and_can_accept_new_work(queue, monkeypatch):
    calls = []
    monkeypatch.setattr(queue, "_work", lambda: queue.run_one())
    queue.start(lambda job, progress: calls.append(job["id"]) or {"ready": True})
    queue.thread.join(timeout=2)
    assert calls == []
    assert not queue.paused
    identifier = queue.enqueue("ask", {"question": "Synthetic question"})
    assert queue.run_one()
    assert calls == [identifier]
    assert queue.get(identifier)["result"] == {"ready": True}


def test_explicit_pause_persists_across_an_empty_restart(queue, monkeypatch):
    queue.pause(True)
    reopened = JobQueue(queue.root.parent)
    monkeypatch.setattr(reopened, "_work", lambda: reopened.run_one())
    try:
        reopened.start(lambda *_: pytest.fail("paused queue cannot execute"))
        reopened.thread.join(timeout=2)
        assert reopened.paused
    finally:
        reopened.close()


def test_duplicate_requests_ignore_object_key_order(queue):
    first = queue.enqueue("extract", {"paper_id": "a", "policy": {"publish": False, "cohort": "development"}})
    again = queue.enqueue("extract", {"policy": {"cohort": "development", "publish": False}, "paper_id": "a"})
    assert again == first
    assert len(queue.list()) == 1
    assert queue.enqueue("recheck", {"paper_id": "a", "policy": {"publish": False, "cohort": "development"}}) != first


def test_progress_does_not_change_original_request_deduplication(queue):
    original = {"paper_id": "a", "publish": False}
    identifier = queue.enqueue("extract", original)

    def process(job, progress):
        progress("已冻结来源", run_id="run-example", source_fingerprint="example")
        assert queue.enqueue("extract", original) == identifier
        return {"record_id": "candidate-example"}

    queue.handler = process
    assert queue.run_one()
    assert len(queue.list()) == 1
    assert queue.get(identifier)["payload"]["run_id"] == "run-example"
    # An explicit later request after completion is a new operation.
    assert queue.enqueue("extract", original) != identifier


def test_failure_retry_preserves_old_job_and_links_new_run(queue):
    identifier = queue.enqueue("recheck", {"record_id": "a", "question": "check the existing value"})

    def fail(job, progress):
        progress("保留已完成阶段", run_id="prior-run")
        raise ValueError("synthetic source failure")

    queue.handler = fail
    assert queue.run_one()
    before = queue.get(identifier)
    assert before["status"] == "failed"
    assert "synthetic source failure" in before["error"]
    assert not queue.paused  # One failed paper must not stop other papers.
    retried = queue.retry(identifier)
    assert retried != identifier
    assert queue.retry(identifier) == retried
    payload = queue.get(retried)["payload"]
    assert payload["previous_job_id"] == identifier
    assert payload["previous_run_id"] == "prior-run"
    assert "run_id" not in payload
    queue.handler = lambda job, progress: {"record_id": "new-record"}
    assert queue.run_one()
    assert queue.get(retried)["status"] == "completed"
    assert queue.get(identifier) == before


@pytest.mark.parametrize("failure", [MemoryError("memory"), PermissionError("access"), OSError(28, "space")])
def test_shared_resource_failure_pauses_future_dispatch(queue, failure):
    identifier = queue.enqueue("extract", {"paper_id": "a"})
    pending = queue.enqueue("extract", {"paper_id": "b"})

    def fail(job, progress):
        raise failure

    queue.handler = fail
    assert queue.run_one()
    assert queue.get(identifier)["status"] == "failed"
    assert queue.paused
    assert not queue.run_one()
    assert queue.get(pending)["status"] == "queued"


def test_pause_stops_dispatch_without_turning_current_job_into_failure(queue):
    identifier = queue.enqueue("extract", {"paper_id": "a"})
    pending = queue.enqueue("extract", {"paper_id": "b"})

    def process(job, progress):
        queue.pause(True)
        progress("当前结果已保存")
        return {"record_id": "one-record"}

    queue.handler = process
    assert queue.run_one()
    assert queue.get(identifier)["status"] == "completed"
    assert not queue.run_one()
    assert queue.get(pending)["status"] == "queued"


def test_cancel_queued_keeps_history_and_running_cannot_be_cancelled(queue):
    identifier = queue.enqueue("scan", {"folders": ["synthetic"]})
    queue.cancel(identifier)
    assert queue.get(identifier)["status"] == "cancelled"
    assert queue.get(identifier)["payload"] == {"folders": ["synthetic"]}
    assert not queue.run_one()
    with pytest.raises(ValueError, match="只能取消"):
        queue.cancel(identifier)
    retried = queue.retry(identifier)
    with queue.db() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (retried,))
    with pytest.raises(ValueError, match="只能取消"):
        queue.cancel(retried)
    assert queue.get(retried)["status"] == "running"


def test_incomplete_extraction_requires_explicit_recovery_choice(queue):
    identifier = queue.enqueue("extract", {"paper_id": "a"})
    queue.handler = lambda job, progress: {"incomplete": True, "run_id": "retained-run"}
    assert queue.run_one()
    assert queue.get(identifier)["status"] == "incomplete"
    assert queue.get(identifier)["result"]["run_id"] == "retained-run"
    with pytest.raises(ValueError, match="恢复方案"):
        queue.retry(identifier)
    assert len(queue.list()) == 1


def test_failed_recovery_cannot_implicitly_restart_from_old_seed(queue):
    identifier = queue.enqueue("recover", {"paper_id": "a", "recovery_action": "continue_candidate"})
    queue.handler = lambda job, progress: {"incomplete": True, "run_id": "new-checkpoint"}
    queue.run_one()
    before = queue.get(identifier)
    with pytest.raises(ValueError, match="恢复方案"):
        queue.retry(identifier)
    assert queue.get(identifier) == before
    assert len(queue.list()) == 1


@pytest.mark.parametrize("failure", [False, True])
def test_authorized_terminal_hook_preserves_outcome_and_links_one_child(queue, failure):
    identifier = queue.enqueue("extract", {"paper_id": "a", "budget_policy": {"max_auto_additions": 1}})
    observed = []

    def process(job, progress):
        progress("原候选已保留", run_id="retained-run")
        if failure:
            raise TimeoutError("original stage deadline")
        return {"incomplete": True, "run_id": "retained-run", "reason": "original outcome"}

    def after(job):
        observed.append(job)
        assert job["status"] == ("failed" if failure else "incomplete")
        assert job["payload"]["run_id"] == "retained-run"
        child = queue.enqueue("recover", {"previous_job_id": job["id"], "paper_id": "a"})
        return {"action": "queued", "queued_job_id": child, "reason": "authenticated stage budget"}

    queue.handler, queue.on_terminal = process, after
    assert queue.run_one()
    original = queue.get(identifier)
    assert len(observed) == 1 and len(queue.list()) == 2
    assert original["status"] == observed[0]["status"]
    assert original["result"] == observed[0]["result"]
    assert original["error"] == observed[0]["error"]
    assert original["payload"]["auto_budget_decision"]["queued_job_id"] != identifier
    queue._after_terminal(identifier)
    assert len(observed) == 1 and len(queue.list()) == 2


@pytest.mark.parametrize("block", ["no_policy", "completed", "paused", "stopping", "other_kind"])
def test_automatic_budget_hook_never_implicitly_authorizes_old_or_paused_work(queue, block):
    payload = {"paper_id": "a"}
    if block != "no_policy":
        payload["budget_policy"] = {"max_auto_additions": 1}
    identifier = queue.enqueue("ask" if block == "other_kind" else "extract", payload)
    queue.on_terminal = lambda *_: pytest.fail("automatic budgeting was not authorized")

    def process(job, progress):
        if block == "paused":
            queue.pause(True)
        if block == "stopping":
            queue.stop_event.set()
        return {"incomplete": block != "completed"}

    queue.handler = process
    assert queue.run_one()
    assert "auto_budget_decision" not in queue.get(identifier)["payload"]
    assert len(queue.list()) == 1


def test_budget_callback_failure_cannot_replace_the_original_failure(queue):
    identifier = queue.enqueue("extract", {"paper_id": "a", "budget_policy": {"max_auto_additions": 1}})
    queue.handler = lambda *_: (_ for _ in ()).throw(TimeoutError("original timeout"))
    queue.on_terminal = lambda *_: (_ for _ in ()).throw(ValueError("unverifiable usage"))
    assert queue.run_one()
    result = queue.get(identifier)
    assert result["status"] == "failed" and result["error"] == "TimeoutError: original timeout"
    assert result["result"] is None
    assert result["payload"]["auto_budget_decision"]["action"] == "blocked"
    assert "unverifiable usage" in result["payload"]["auto_budget_decision"]["reason"]
    assert not queue.paused


def test_completed_or_running_jobs_cannot_be_retried(queue):
    identifier = queue.enqueue("scan", {"folders": []})
    with pytest.raises(ValueError, match="仅失败"):
        queue.retry(identifier)
    queue.handler = lambda job, progress: {}
    queue.run_one()
    with pytest.raises(ValueError, match="仅失败"):
        queue.retry(identifier)


def test_repeated_start_does_not_replace_live_worker_or_handler(queue):
    entered = threading.Event()
    release = threading.Event()

    def original_worker():
        entered.set()
        release.wait(timeout=3)

    queue._work = original_worker
    original_handler = lambda job, progress: {}
    try:
        queue.start(original_handler)
        assert entered.wait(timeout=2)
        thread = queue.thread
        queue.start(lambda *_: pytest.fail("must not replace an active handler"))
        assert queue.thread is thread
        assert queue.handler is original_handler
    finally:
        release.set()
        queue.close()


def test_existing_schema_migrates_without_losing_jobs_or_history(tmp_path):
    root = tmp_path / "data"
    workbench = root / "workbench"
    workbench.mkdir(parents=True)
    with sqlite3.connect(workbench / "jobs.sqlite3") as db:
        db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,kind TEXT,status TEXT,stage TEXT,payload TEXT,result TEXT,error TEXT,created_at TEXT,updated_at TEXT)")
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)", ("old-job", "extract", "queued", "等待处理", '{"paper_id":"a"}', None, None, "before", "before"))
    migrated = JobQueue(root)
    try:
        assert migrated.get("old-job")["created_at"] == "before"
        assert migrated.enqueue("extract", {"paper_id": "a"}) == "old-job"
        reopened = JobQueue(root)
        assert len(reopened.list()) == 1
    finally:
        migrated.close()


def test_invalid_requests_fail_without_adding_jobs(queue):
    with pytest.raises(ValueError):
        queue.enqueue("arbitrary-command", {})
    with pytest.raises(ValueError):
        queue.enqueue("scan", ["not", "an", "object"])
    with pytest.raises(ValueError):
        queue.enqueue("ask", {"value": float("nan")})
    with pytest.raises(ValueError, match="不存在"):
        queue.get("missing")
    assert queue.list() == []
