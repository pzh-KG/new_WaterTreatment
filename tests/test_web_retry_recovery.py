"""Explicit retry routing only; model and database access are never exercised."""
from unittest.mock import Mock

import pytest

from watertreatment.settings import CodexSettings, Settings
from watertreatment.web_service import Workbench


@pytest.fixture
def service(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path / "code", data_root=tmp_path / "data", source_roots=[],
                        codex=CodexSettings(model="gpt-6.2-sol", reasoning_effort="max"))
    workbench = Workbench(settings)
    monkeypatch.setattr(workbench.records, "publish", Mock(side_effect=AssertionError("retry must not publish")))
    yield workbench
    workbench.close()


@pytest.fixture
def reaudit(monkeypatch):
    # Deliberately synthetic artifacts below cannot pass scientific validation.
    # This test verifies routing to that validator, never bypasses it in production.
    fake = Mock(return_value={"run_id": "new-run", "revision": {"accepted": True}})
    monkeypatch.setattr("watertreatment.web_model.reaudit", fake)
    return fake


def saved_run(service, *, staged=False, audit=False, failure=True, kind="web_reaudit"):
    run = service.store.new_run(kind, {"record_id": "record-a"})
    if failure:
        (run / "failure.json").write_text('{"error":"synthetic timeout"}', encoding="utf-8")
    if staged:
        stage = run / "reading_tools" / "001"
        stage.mkdir(parents=True)
        (stage / "submitted_record.json").write_text('{"synthetic":"draft"}', encoding="utf-8")
    if audit:
        (run / "audit.v00.json").write_text('{"synthetic":"audit"}', encoding="utf-8")
    return run


def terminal_job(service, run_id=None, *, kind="recheck", status="failed"):
    identifier = service.jobs.enqueue(kind, {"record_id": "record-a", "question": "Check the plotted series",
                                           "observation_id": "observation-a"})
    if run_id is not None:
        service.jobs.progress(identifier, "saved source-bound run", run_id=run_id)
    with service.jobs.db() as db:
        db.execute("UPDATE jobs SET status=? WHERE id=?", (status, identifier))
    return identifier


def retry_job(service, previous_id):
    return service.jobs.get(service.jobs.retry(previous_id))


def execute(service, job):
    return service.handle_job(job, lambda *args, **kwargs: None)


@pytest.mark.parametrize("staged,audit,expected", [
    (True, True, "resume_staged_run_id"),
    (True, False, "resume_staged_run_id"),
    (False, True, "repair_audit_run_id"),
    (False, False, None),
])
def test_web_retry_recovery_selects_only_bound_saved_stage(service, reaudit, staged, audit, expected):
    run = saved_run(service, staged=staged, audit=audit)
    previous = terminal_job(service, run.name)
    job = retry_job(service, previous)
    before = {p.relative_to(run): p.read_bytes() for p in run.rglob("*") if p.is_file()}
    output = execute(service, job)
    assert output["run_id"] == "new-run"
    reaudit.assert_called_once()
    assert reaudit.call_args.args[2:] == ("record-a", "Check the plotted series\n关注 observation_id: observation-a")
    options = {key: value for key, value in reaudit.call_args.kwargs.items() if key != "progress"}
    assert options == ({expected: run.name} if expected else {})
    assert {p.relative_to(run): p.read_bytes() for p in run.rglob("*") if p.is_file()} == before
    service.records.publish.assert_not_called()


def test_web_retry_recovery_new_request_never_picks_a_historical_run(service, reaudit, monkeypatch):
    run = saved_run(service, staged=True, audit=True)
    terminal_job(service, run.name)
    identifier = service.jobs.enqueue("recheck", {"record_id": "record-a", "question": "Check the plotted series"})
    monkeypatch.setattr(service.store, "runs", Mock(side_effect=AssertionError("new requests must not search history")))
    execute(service, service.jobs.get(identifier))
    assert set(reaudit.call_args.kwargs) == {"progress"}


def test_web_retry_recovery_requires_failure_artifact(service, reaudit):
    run = saved_run(service, staged=True, audit=True, failure=False)
    execute(service, retry_job(service, terminal_job(service, run.name)))
    assert set(reaudit.call_args.kwargs) == {"progress"}


def test_web_retry_recovery_before_any_run_starts_is_a_fresh_request(service, reaudit):
    execute(service, retry_job(service, terminal_job(service)))
    assert set(reaudit.call_args.kwargs) == {"progress"}


def test_web_retry_recovery_validation_failure_propagates_without_fallback(service, reaudit):
    run = saved_run(service, staged=True, audit=True)
    job = retry_job(service, terminal_job(service, run.name))
    reaudit.side_effect = ValueError("checkpoint source fingerprint mismatch")
    with pytest.raises(ValueError, match="source fingerprint mismatch"):
        execute(service, job)
    reaudit.assert_called_once()
    assert reaudit.call_args.kwargs["resume_staged_run_id"] == run.name
    assert "repair_audit_run_id" not in reaudit.call_args.kwargs
    service.records.publish.assert_not_called()


def test_web_retry_recovery_queue_records_validation_failure(service, reaudit):
    run = saved_run(service, audit=True)
    job = retry_job(service, terminal_job(service, run.name))
    reaudit.side_effect = ValueError("audit source version changed")
    service.jobs.handler = service.handle_job
    assert service.jobs.run_one() is True
    result = service.jobs.get(job["id"])
    assert result["status"] == "failed"
    assert "audit source version changed" in result["error"]
    reaudit.assert_called_once()


@pytest.mark.parametrize("run_id", ["../outside", "..\\outside", "C:\\outside", "a/b", "a\\b"])
def test_web_retry_recovery_rejects_path_traversal(service, reaudit, run_id):
    job = retry_job(service, terminal_job(service, run_id))
    with pytest.raises(ValueError, match="identifier"):
        execute(service, job)
    reaudit.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("record_id", "another-record"),
    ("question", "A different question"),
    ("observation_id", "another-observation"),
    ("previous_run_id", "another-run"),
])
def test_web_retry_recovery_cannot_reuse_another_task_scope(service, reaudit, field, value):
    run = saved_run(service, staged=True)
    job = retry_job(service, terminal_job(service, run.name))
    job["payload"][field] = value
    with pytest.raises(ValueError, match="不一致|不属于"):
        execute(service, job)
    reaudit.assert_not_called()


def test_web_retry_recovery_cannot_reuse_another_registered_run(service, reaudit):
    bound = saved_run(service, staged=True)
    unrelated = saved_run(service, staged=True)
    job = retry_job(service, terminal_job(service, bound.name))
    job["payload"]["previous_run_id"] = unrelated.name
    with pytest.raises(ValueError, match="不属于"):
        execute(service, job)
    reaudit.assert_not_called()


@pytest.mark.parametrize("kind,status", [("ask", "failed"), ("recheck", "completed")])
def test_web_retry_recovery_requires_failed_recheck_predecessor(service, reaudit, kind, status):
    run = saved_run(service, staged=True)
    previous = terminal_job(service, run.name, kind=kind, status=status)
    job = {"kind": "recheck", "payload": {**service.jobs.get(previous)["payload"],
                                           "previous_job_id": previous, "previous_run_id": run.name}}
    with pytest.raises(ValueError, match="回查恢复只能"):
        execute(service, job)
    reaudit.assert_not_called()


def test_web_retry_recovery_requires_registered_predecessor(service, reaudit):
    run = saved_run(service, staged=True)
    job = {"kind": "recheck", "payload": {"record_id": "record-a", "question": "Check",
                                           "previous_run_id": run.name}}
    with pytest.raises(ValueError, match="原重试任务"):
        execute(service, job)
    reaudit.assert_not_called()


@pytest.mark.parametrize("kind", [None, "extraction"])
def test_web_retry_recovery_requires_registered_reaudit_run(service, reaudit, kind):
    run_id = saved_run(service, staged=True, kind=kind).name if kind else "unregistered-run"
    job = retry_job(service, terminal_job(service, run_id))
    with pytest.raises(ValueError, match="已登记的网页回查"):
        execute(service, job)
    reaudit.assert_not_called()


def test_web_retry_recovery_preserves_binding_through_cancelled_retry(service, reaudit):
    run = saved_run(service, staged=True)
    first_retry = retry_job(service, terminal_job(service, run.name))
    service.jobs.cancel(first_retry["id"])
    second_retry = retry_job(service, first_retry["id"])
    execute(service, second_retry)
    assert reaudit.call_args.kwargs["resume_staged_run_id"] == run.name


def test_web_retry_recovery_preserves_incomplete_revision_status(service, reaudit):
    run = saved_run(service, audit=True)
    reaudit.return_value = {"run_id": "new-run", "revision": {"accepted": False}}
    output = execute(service, retry_job(service, terminal_job(service, run.name)))
    assert output["incomplete"] is True
    service.records.publish.assert_not_called()
