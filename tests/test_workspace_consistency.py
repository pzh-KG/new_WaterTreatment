"""One version-aware paper set drives cards, drilldowns and recovery labels."""
from pathlib import Path

from fastapi.testclient import TestClient

from test_web_service import workbench, add_paper, candidate_from_plan
from test_web_activity import run_for, indexed
from test_web_records import completed_call
from watertreatment.storage import hash_file, write_new_json
from watertreatment.web_api import create_app
from watertreatment.workflow import import_paper, prepare


def fail_job(w, paper, run, created="2026-01-01T00:00:00+00:00"):
    job = w.service.jobs.enqueue("extract", {"paper_id": paper["id"], "run_id": run.name})
    with w.service.jobs.db() as db:
        db.execute("UPDATE jobs SET status='failed',created_at=?,error='TimeoutError' WHERE id=?", (created, job))
    return job


def test_unexecuted_plan_does_not_replace_a_failed_attempt(workbench):
    w = workbench; paper = add_paper(w)
    failed = run_for(w, paper, failure="Input budget", created="2026-01-01T00:00:00Z")
    pending = run_for(w, paper, created="2026-01-02T00:00:00Z")
    shown = w.service.papers()[0]["extraction"]
    assert shown["state"] == "failed" and shown["latest_run_id"] == failed.name
    assert shown["latest_preparation"]["run_id"] == pending.name


def test_same_run_recovery_requires_its_exact_completion_receipt_after_the_failed_job(workbench, monkeypatch):
    w=workbench
    paper=add_paper(w)
    status="candidate_audited_with_limitations"
    run=run_for(w,paper,count=3,state=status,failure="Delivery interrupted")
    job=fail_job(w,paper,run)
    with w.service.jobs.db() as db:
        db.execute("UPDATE jobs SET updated_at='2026-01-02T00:00:00Z' WHERE id=?",(job,))
    monkeypatch.setattr(w.service.records,"list_records",lambda:[indexed(paper,run)])
    store=w.service.store
    store.event(run.name,"candidate_completed",{"status":status,"result_sha256":"unrelated"})
    assert not w.service.workspace_snapshot()["jobs"][0]["recovered"]
    receipt={"status":status,"result_sha256":hash_file(run/"result.json")}
    store.event(run.name,"candidate_completed",receipt)
    with store.connection() as db:
        db.execute("UPDATE run_events SET created_at='2026-01-03T00:00:00Z' WHERE run_id=?",(run.name,))
    view=w.service.workspace_snapshot()
    assert view["jobs"][0]["display_status"]=="recovered" and view["jobs"][0]["status"]=="failed"
    assert view["jobs"][0]["recovery"]["created_at"]=="2026-01-03T00:00:00Z"
    assert view["stats"]["failed_unresolved"]==0 and (run/"failure.json").exists()
    with w.service.jobs.db() as db:
        db.execute("UPDATE jobs SET updated_at='2026-01-04T00:00:00Z' WHERE id=?",(job,))
    store.event(run.name,"candidate_completed",receipt)
    assert not w.service.workspace_snapshot()["jobs"][0]["recovered"]


def test_recovery_updates_display_but_preserves_failed_job_and_own_detail(workbench, monkeypatch):
    w = workbench; paper = add_paper(w)
    old = run_for(w, paper, failure="TimeoutError", count=3, created="2026-01-01T08:00:00+08:00")
    job = fail_job(w, paper, old)
    new = run_for(w, paper, count=3, state="candidate_audited_with_limitations", created="2026-01-01T01:00:00Z")
    record = {**indexed(paper, new), "created_at": "2026-01-01T01:00:00Z"}
    monkeypatch.setattr(w.service.records, "list_records", lambda: [record])
    view = w.service.workspace_snapshot()
    shown = view["jobs"][0]
    assert shown["status"] == "failed" and shown["display_status"] == "recovered"
    assert shown["current_result_id"] == new.name and shown["recovery"]["record_id"] == new.name
    assert w.service.jobs.get(job)["status"] == "failed"
    detail = w.service.job_detail(job)
    assert detail["outcome"]["state"] == "failed" and detail["recovery"]["record_id"] == new.name
    assert view["stats"]["failed_historical"] == 1 and view["stats"]["failed_unresolved"] == 0


def test_older_success_does_not_resolve_a_later_failed_attempt(workbench, monkeypatch):
    w = workbench; paper = add_paper(w)
    old = run_for(w, paper, count=3, state="candidate_audited_with_limitations", created="2026-01-01T00:00:00Z")
    new = run_for(w, paper, failure="TimeoutError", count=3, created="2026-01-02T00:00:00Z")
    fail_job(w, paper, new, created="2026-01-02T00:00:00Z")
    monkeypatch.setattr(w.service.records, "list_records", lambda: [indexed(paper, old)])
    view = w.service.workspace_snapshot()
    assert view["jobs"][0]["recovered"] is False
    assert view["papers"][0]["extraction"]["state"] == "completed"
    assert view["papers"][0]["extraction"]["latest_attempt"]["state"] == "failed"


def test_counts_and_memberships_use_one_registered_current_result_per_source_paper(workbench, monkeypatch):
    w = workbench; p1, p2, p3 = (add_paper(w, i) for i in (1, 2, 3))
    legacy = run_for(w, p1, count=3, wrong_id=True)
    good = run_for(w, p1, count=3, state="candidate_audited_with_limitations")
    incomplete = run_for(w, p2, count=3)
    stale = run_for(w, p3, count=3, state="candidate_audited_with_limitations")
    records = [{**indexed(p1, legacy, admitted=False), "paper_id": "10.1000/alias", "published_current": True},
        {**indexed(p1, good), "created_at": "2026-02-01T00:00:00Z", "review_status": "partial_reviewed", "published_current": True},
        indexed(p2, incomplete, admitted=False),
        {**indexed(p3, stale), "source_change": "changed", "published_current": True}]
    monkeypatch.setattr(w.service.records, "list_records", lambda: records)
    view = w.service.workspace_snapshot()
    stats, members = view["stats"], view["metric_membership"]
    assert (stats["papers"], stats["records"], stats["completed"], stats["incomplete"]) == (3, 3, 1, 2)
    assert members["completed"] == members["reviewed"] == members["published"] == [p1["id"]]
    assert {p["id"] for p in view["papers"] if p["extraction"]["state"] == "completed"} == set(members["completed"])
    current = [r for r in view["records"] if r["current"]]
    assert len(current) == 3 and sum(r["display_status"] == "completed" for r in current) == 1
    old = next(r for r in view["records"] if r["id"] == legacy.name)
    assert old["registry_current"] and not old["workspace_current"]
    assert w.service.status()["metric_membership"] == members


def test_historical_publication_and_review_do_not_count_for_a_new_current_version(workbench, monkeypatch):
    w = workbench; paper = add_paper(w)
    old = run_for(w, paper, count=3, state="candidate_audited_with_limitations")
    new = run_for(w, paper, count=3, state="candidate_audited_with_limitations")
    records = [{**indexed(paper, old, current=False), "review_status": "partial_reviewed", "published_current": True},
               {**indexed(paper, new), "review_status": "not_reviewed", "published_current": False}]
    monkeypatch.setattr(w.service.records, "list_records", lambda: records)
    view = w.service.workspace_snapshot()
    assert view["stats"]["records"] == view["stats"]["completed"] == 1
    assert view["stats"]["reviewed"] == view["stats"]["published"] == 0
    assert view["metric_membership"]["reviewed"] == view["metric_membership"]["published"] == []


def test_registered_external_completion_is_indexed_once_and_revision_changes(workbench, monkeypatch):
    w = workbench; paper = add_paper(w)
    before = w.service.workspace_snapshot()["activity_revision"]
    imported = import_paper(w.settings, Path(paper["folder"]), paper_id=paper["id"], render=True,
                            source_roles=w.service.library.roles_for_import(paper["id"]))
    run = prepare(w.settings, imported / "sources.json", cohort_role="unassigned")
    completed_call(run, candidate_from_plan(run / "plan.json"))
    refresh = w.service.records.refresh
    calls = []
    def counted():
        calls.append(True)
        return refresh()
    monkeypatch.setattr(w.service.records, "refresh", counted)
    w.service.workspace_snapshot()
    w.service.activity._sync_thread.join(timeout=15)
    assert not w.service.activity._sync_thread.is_alive()
    view = w.service.workspace_snapshot()
    assert len(calls) == 1 and view["stats"]["completed"] == 1
    assert view["activity_revision"] != before and not w.clients
    with TestClient(create_app(w.settings, start_worker=False, service=w.service)) as client:
        assert client.get("/api/workspace").json()["metric_membership"]["completed"] == [paper["id"]]
        assert client.get("/api/jobs").json()["activity_revision"] == client.get("/api/workspace").json()["activity_revision"]
    assert len(calls) == 1


def test_failed_background_index_is_not_retried_on_every_poll(workbench, monkeypatch):
    w = workbench; paper = add_paper(w)
    run = run_for(w, paper)
    write_new_json(run / "candidate.json", {"paper": {"id": paper["id"]}, "observations": []})
    write_new_json(run / "result.json", {"status": "candidate_unverified"})
    calls = []
    def failure():
        calls.append(True)
        raise ValueError("synthetic index failure")
    monkeypatch.setattr(w.service.records, "refresh", failure)
    w.service.workspace_snapshot()
    w.service.activity._sync_thread.join(timeout=5)
    for _ in range(3):
        assert w.service.workspace_snapshot()["sync_state"]["state"] == "error"
    assert len(calls) == 1


def test_changed_indexed_artifact_cannot_be_misread_as_a_completed_repair(workbench, monkeypatch):
    w = workbench; paper = add_paper(w)
    run = run_for(w, paper, count=3, state="candidate_audited_with_limitations")
    write_new_json(run / "candidate.json", {"paper": {"id": paper["id"]}, "observations": []})
    monkeypatch.setattr(w.service.records, "list_records", lambda: [indexed(paper, run)])
    monkeypatch.setattr(w.service.records, "refresh", lambda: None)
    w.service.workspace_snapshot()
    w.service.activity._sync_thread.join(timeout=5)
    assert w.service.workspace_snapshot()["stats"]["completed"] == 1
    (run / "candidate.json").write_text('{"changed": true}', encoding="utf-8")
    view = w.service.workspace_snapshot()
    w.service.activity._sync_thread.join(timeout=5)
    assert view["stats"]["completed"] == 0
    assert view["papers"][0]["extraction"]["state"] != "completed"
    assert w.service.workspace_snapshot()["sync_state"]["state"] == "error"
    assert (run / "candidate.json").read_text(encoding="utf-8") == '{"changed": true}'
