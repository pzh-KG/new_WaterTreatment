"""A stale server must preserve a queued recovery and its existing grant."""
import json

import pytest

from watertreatment.runtime_code import RuntimeDispatchDeferred, RuntimeRestartRequired
from watertreatment.web_jobs import JobQueue


@pytest.mark.parametrize("kind", ["extract", "recover", "recheck", "ask"])
def test_pre_dispatch_restart_keeps_original_job_without_terminal_callback(tmp_path, kind):
    queue = JobQueue(tmp_path / "data")
    payload = {"paper_id": "synthetic-paper", "budget_policy": {"max_auto_additions": 2},
        "budget_grant": {"grant_id": "already-reserved", "parent_run": "retained-parent"},
        "recovery_action": "extend_budget"}
    identifier = queue.enqueue(kind, payload)
    second = queue.enqueue("extract", {"paper_id": "another-study"})
    original = queue.get(identifier)
    def preflight(job, progress):
        raise RuntimeDispatchDeferred("synthetic source edit; no provider call started")
    queue.handler = preflight
    queue.on_terminal = lambda *_: pytest.fail("A deferred dispatch must not reserve another budget")
    assert queue.run_one() is False
    retained = queue.get(identifier)
    assert queue.paused and retained["status"] == "queued"
    assert "重启" in retained["stage"]
    assert retained["payload"] == original["payload"]
    assert retained["result"] == original["result"] and retained["error"] == original["error"]
    assert queue.get(second)["status"] == "queued"
    assert not queue.run_one()
    with queue.db() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE job_id=? AND stage LIKE '派发已暂停%'",
                          (identifier,)).fetchone()[0] == 1


def test_restart_and_explicit_resume_spend_the_same_queued_authorization(tmp_path, monkeypatch):
    root = tmp_path / "data"
    queue = JobQueue(root)
    identifier = queue.enqueue("recover", {"parent_run_id": "immutable-parent",
        "recovery_action": "extend_budget", "budget_grant": {"grant_id": "one-reservation"}})
    queue.handler = lambda *_: (_ for _ in ()).throw(RuntimeDispatchDeferred("edited code"))
    assert not queue.run_one()
    original_payload = queue.get(identifier)["payload"]
    restarted = JobQueue(root)
    calls = []
    monkeypatch.setattr(restarted, "_work", lambda: restarted.run_one())
    try:
        restarted.start(lambda job, progress: calls.append(job["payload"]) or {"incomplete": False})
        restarted.thread.join(timeout=2)
        assert restarted.paused and calls == []
        restarted.pause(False)
        assert restarted.run_one()
        assert calls == [original_payload]
        assert restarted.get(identifier)["status"] == "completed"
        assert len(restarted.list()) == 1
    finally:
        restarted.close()


def test_an_error_after_work_started_is_not_reclassified_as_deferred(tmp_path):
    queue = JobQueue(tmp_path / "data")
    identifier = queue.enqueue("recover", {"parent_run_id": "retained-parent"})
    def actual_failure(job, progress):
        progress("A retained stage began", run_id="retained-child")
        raise RuntimeRestartRequired("not the pre-dispatch exception")
    queue.handler = actual_failure
    assert queue.run_one()
    result = queue.get(identifier)
    assert result["status"] == "failed"
    assert result["payload"]["run_id"] == "retained-child"
    assert result["error"].startswith("RuntimeRestartRequired:")
