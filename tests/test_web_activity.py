"""Human workflow summaries preserve failure, chronology and result identity."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from test_web_service import workbench, add_paper
from watertreatment.storage import hash_file, write_new_json
from watertreatment.web_api import create_app


def run_for(w, paper, *, count=None, state=None, failure=None, created=None, wrong_id=False):
    run = w.service.store.new_run("extraction", {"paper_id": paper["id"]})
    write_new_json(run / "plan.json", {"paper_id": paper["id"]})
    if count is not None:
        write_new_json(run / "candidate.v01.json", {"paper": {"id": "10.1000/wrong-internal-id" if wrong_id else paper["id"]},
                                                   "observations": [{"id": str(i)} for i in range(count)]})
    if state:
        write_new_json(run / "result.json", {"status": state, "stop_reason": "audit_completed", "reading_check_issues": []})
    if failure:
        write_new_json(run / "failure.json", {"error": failure})
    if created:
        with w.service.store.connection() as db:
            db.execute("UPDATE runs SET created_at=? WHERE run_id=?", (created, run.name))
    return run


def indexed(paper, run, *, admitted=True, current=True):
    return {"id": run.name, "run_id": run.name, "paper_id": paper["id"], "current": current,
        "admissible": admitted, "status": "candidate_audited_with_limitations", "title": paper["title"],
        "doi": paper.get("doi"), "observation_count": 3, "created_at": "2026-01-01T00:00:00+00:00"}


def test_library_distinguishes_registered_queued_and_failed_without_candidate(workbench):
    w = workbench
    paper = add_paper(w, 7)
    assert w.service.papers()[0]["extraction"]["state"] == "not_started"
    job = w.service.jobs.enqueue("extract", {"paper_id": paper["id"]})
    assert w.service.papers()[0]["extraction"]["state"] == "queued"
    with w.service.jobs.db() as db:
        db.execute("UPDATE jobs SET status='failed',error='TimeoutError' WHERE id=?", (job,))
    extracted = w.service.papers()[0]["extraction"]
    assert extracted["state"] == "failed" and not extracted["has_candidate"]
    assert len(w.service.jobs.list()) == 1


def test_explicit_recovery_is_visible_as_active_without_hiding_candidate(workbench, monkeypatch):
    w = workbench
    paper = add_paper(w, 17)
    run = run_for(w, paper, count=3, state="candidate_audited_with_limitations", created="2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(w.service.records, "list_records", lambda: [indexed(paper, run)])
    identifier = w.service.jobs.enqueue("recover", {"paper_id": paper["id"],
        "parent_run_id": run.name, "recovery_action": "correct_reading"})
    extraction = w.service.papers()[0]["extraction"]
    assert extraction["active"]
    assert extraction["state"] == "completed"
    assert extraction["latest_job_id"] == identifier
    assert extraction["available_result"]["record_id"] == run.name
    job = w.service.job_summaries()[0]
    assert job["folder_number"] == 17
    assert job["kind_label"] == "补正阅读记录"


def test_dossier_recovery_job_displays_the_actual_unfinished_stage(workbench):
    w = workbench
    paper = add_paper(w, 19)
    job_id = w.service.jobs.enqueue("recover", {"paper_id": paper["id"],
        "recovery_action": "resume_extraction"})
    job = next(j for j in w.service.job_summaries() if j["id"] == job_id)
    assert job["folder_number"] == 19
    assert job["kind_label"] == "从已完成全文理解继续抽取"
    assert job["status"] == "queued"


def test_cli_failure_with_versioned_candidate_is_visible_without_canonical_outputs(workbench):
    w = workbench
    paper = add_paper(w, 8)
    run = run_for(w, paper, count=9, wrong_id=True, failure="TimeoutError: exceeded time budget")
    before = {p: hash_file(p) for p in run.iterdir()}
    row = w.service.papers()[0]
    assert row["folder_number"] == 8
    assert row["extraction"]["state"] == "failed"
    assert row["extraction"]["has_candidate"] and row["extraction"]["observation_count"] == 9
    assert row["extraction"]["latest_run_id"] == run.name and row["extraction"]["available_result"] is None
    assert before == {p: hash_file(p) for p in run.iterdir()}
    assert w.clients == []


def test_newer_failed_attempt_does_not_hide_available_older_result(workbench, monkeypatch):
    w = workbench; paper = add_paper(w, 9)
    good = run_for(w, paper, count=3, state="candidate_audited_with_limitations", created="2026-01-01T09:00:00+08:00")
    bad = run_for(w, paper, count=4, failure="TimeoutError", created="2026-01-01T02:00:00+00:00")
    monkeypatch.setattr(w.service.records, "list_records", lambda: [indexed(paper, good)])
    extraction = w.service.papers()[0]["extraction"]
    assert extraction["latest_run_id"] == bad.name and extraction["state"] == "completed"
    assert extraction["latest_attempt"]["state"] == "failed"
    assert extraction["available_result"]["record_id"] == good.name
    assert extraction["available_result"]["admissible"]


def test_later_cli_completion_outweighs_older_web_failure_across_timezones(workbench, monkeypatch):
    w = workbench; paper = add_paper(w, 10)
    failed = run_for(w, paper, count=3, failure="TimeoutError", created="2026-01-01T08:00:00+08:00")
    job = w.service.jobs.enqueue("extract", {"paper_id": paper["id"], "run_id": failed.name})
    with w.service.jobs.db() as db:
        db.execute("UPDATE jobs SET status='incomplete',created_at='2026-01-01T07:00:00+08:00' WHERE id=?", (job,))
    complete = run_for(w, paper, count=3, state="candidate_audited_with_limitations", created="2026-01-01T01:00:00+00:00")
    monkeypatch.setattr(w.service.records, "list_records", lambda: [indexed(paper, complete)])
    assert w.service.papers()[0]["extraction"]["state"] == "completed"
    detail = w.service.job_detail(job)
    assert detail["job"]["status"] == "incomplete" and detail["outcome"]["state"] != "completed"


def test_audited_label_does_not_override_failed_admission(workbench, monkeypatch):
    w = workbench; paper = add_paper(w, 11)
    run = run_for(w, paper, count=3, state="candidate_audited_with_limitations")
    monkeypatch.setattr(w.service.records, "list_records", lambda: [indexed(paper, run, admitted=False)])
    assert w.service.papers()[0]["extraction"]["state"] == "incomplete"
    record = w.service.record_summaries()[0]
    assert record["display_status"] == "incomplete" and record["folder_number"] == 11


def test_task_detail_is_human_readable_and_does_not_export_prompts(workbench):
    w = workbench; paper = add_paper(w, 12)
    run = run_for(w, paper, count=3, failure="TimeoutError")
    call = run / "calls" / "call-01-demo"
    write_new_json(call / "request.json", {"request": {"stage": "audit_1", "prompt": "PRIVATE_PROMPT_SENTINEL",
        "model": "gpt-6.1-sol", "reasoning_effort": "max", "citation_review": {"scope_path": "local-only"}}})
    write_new_json(call / "result.json", {"status": "failed", "error": "TimeoutError", "elapsed_seconds": 12.5})
    write_new_json(run / "reading_tools" / "tool-0001" / "request.json", {"tool": "wt_view", "source_review_call": call.name,
        "arguments": {"untrusted": "PRIVATE_ARGUMENT_SENTINEL"}})
    job = w.service.jobs.enqueue("extract", {"paper_id": paper["id"], "run_id": run.name})
    w.service.jobs.progress(job, "正在核对修订后的实验条件")
    with w.service.jobs.db() as db:
        db.execute("UPDATE jobs SET status='failed',error='TimeoutError' WHERE id=?", (job,))
    detail = w.service.job_detail(job)
    assert detail["job"]["folder_number"] == 12 and detail["job"]["job_number"] == 1
    assert detail["calls"][0]["label"] == "局部原文复核"
    assert detail["calls"][0]["tool_counts"] == {"wt_view": 1}
    assert detail["timings"]["model_seconds"] == 12.5
    assert detail["outcome"]["has_candidate"] and detail["artifacts"]
    assert "PRIVATE_" not in json.dumps(detail)
    assert all(p.name != "request.json" for p in w.service.activity._cache)
    second = w.service.jobs.enqueue("scan", {})
    assert w.service.job_detail(second)["job"]["job_number"] == 2
    assert w.service.job_detail(job)["job"]["job_number"] == 1
    app = create_app(w.settings, start_worker=False, service=w.service)
    with TestClient(app) as client:
        assert client.get('/api/jobs/' + job).json()["job"]["folder_number"] == 12
        assert client.get('/api/jobs/missing').status_code == 404


def test_candidate_summary_cache_observes_changed_version_and_does_not_retain_full_payload(workbench):
    w = workbench; paper = add_paper(w, 13)
    run = run_for(w, paper, count=3, failure="TimeoutError")
    assert w.service.papers()[0]["extraction"]["observation_count"] == 3
    write_new_json(run / 'candidate.v02.json', {"paper": {"id": paper["id"]}, "observations": [{"id": str(i)} for i in range(4)]})
    assert w.service.papers()[0]["extraction"]["observation_count"] == 4
    assert all(not p.name.startswith('candidate.') for p in w.service.activity._cache)


def test_new_queue_preserves_previous_retained_candidate_summary(workbench):
    w = workbench; paper = add_paper(w, 14)
    run_for(w, paper, count=5, failure="TimeoutError", created="2020-01-01T00:00:00+00:00")
    w.service.jobs.enqueue("extract", {"paper_id": paper["id"]})
    extraction = w.service.papers()[0]["extraction"]
    assert extraction["state"] == "queued" and extraction["has_candidate"]
    assert extraction["observation_count"] == 5 and not extraction["latest_attempt"]["has_candidate"]


def test_record_number_uses_registered_run_when_candidate_identity_is_wrong(workbench, monkeypatch):
    w = workbench; paper = add_paper(w, 15)
    run = run_for(w, paper, count=3, wrong_id=True)
    record = {**indexed(paper, run, admitted=False), "paper_id": "10.1000/wrong-internal-id"}
    monkeypatch.setattr(w.service.records, "list_records", lambda: [record])
    shown = w.service.record_summaries()[0]
    assert shown["folder_number"] == 15 and shown["paper_id"] == record["paper_id"]
    assert shown["source_paper_id"] == paper["id"]


def test_stale_source_is_not_displayed_completed_even_if_cached_admission_was_true(workbench, monkeypatch):
    w = workbench; paper = add_paper(w, 16)
    run = run_for(w, paper, count=3, state="candidate_audited_with_limitations")
    record = {**indexed(paper, run), "source_change": "Synthetic changed source"}
    monkeypatch.setattr(w.service.records, "list_records", lambda: [record])
    assert w.service.record_summaries()[0]["display_status"] == "incomplete"
    assert w.service.papers()[0]["extraction"]["state"] == "incomplete"


def test_unknown_timestamps_are_not_invented_execution_durations(workbench):
    w = workbench; paper = add_paper(w, 17)
    job = w.service.jobs.enqueue("extract", {"paper_id": paper["id"]})
    w.service.jobs.progress(job, "开始识别附件")
    with w.service.jobs.db() as db:
        db.execute("UPDATE jobs SET status='failed',created_at='unknown',updated_at='unknown' WHERE id=?", (job,))
    assert w.service.job_detail(job)["timings"]["queued_seconds"] is None
    assert w.service.job_detail(job)["timings"]["execution_seconds"] is None
