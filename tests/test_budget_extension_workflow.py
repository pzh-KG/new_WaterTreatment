"""Offline stage continuation with genuine archived understanding/protocol inputs."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_dossier_extraction_resume import (dossier_parent, active, workspace, FakeReader, rewrite,
    make_dossier_parent, record_failed_extraction)
from watertreatment.budget_extension_workflow import (BudgetExtensionWorkflow, select_checkpoint,
    prepare_extension, verify_extension_plan)
from watertreatment.budget_grants import preview_grant, reserve_grant, load_grant
from watertreatment.storage import DataStore, digest_json, hash_file
from watertreatment.web_jobs import JobQueue
from watertreatment.web_recovery import RecoveryWorkflow
from watertreatment.workflow import execute_plan


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def policy_for(plan, *, automatic=1):
    seconds, calls = plan["reading_settings"]["total_timeout_seconds"], plan["max_calls"]
    return {"initial_seconds": seconds, "initial_calls": calls, "addition_seconds": 600,
        "addition_calls": 2, "max_auto_additions": automatic,
        "max_total_seconds": seconds + 1200, "max_total_calls": calls + 4}


def exhausted(parent):
    ws, run, bundle, plan = parent
    path = run / "calls/extraction/result.json"
    value = read(path)
    value["elapsed_seconds"] = plan["reading_settings"]["total_timeout_seconds"] - 10.25
    rewrite(path, value)
    return ws, run, bundle, plan


def test_exhausted_dossier_continues_without_repeating_understanding_or_mutating_parent(dossier_parent):
    ws, run, bundle, plan = exhausted(dossier_parent)
    before = {str(p): hash_file(p) for p in run.rglob("*") if p.is_file()}
    policy = policy_for(plan)
    grant = preview_grant(ws.settings, run, policy)
    selected = select_checkpoint(ws.settings, run, grant)
    assert selected["route"] == "dossier" and selected["minimum_calls"] == 2
    descriptor = reserve_grant(ws.settings, run, grant, digest_json(selected))
    settings, new, executor = prepare_extension(ws.settings, descriptor, selected)
    assert verify_extension_plan(settings, new)["used_seconds"] == policy["initial_seconds"]
    client = FakeReader(settings, bundle)
    client.total_timeout_seconds = settings.reading.total_timeout_seconds
    executor(settings, new / "plan.json", client=client, verify=True)
    assert [stage for stage, _ in client.stages][:2] == ["extraction", "audit_0"]
    assert all(stage != "comprehension" for stage, _ in client.stages)
    assert client.calls_started <= grant["allocated_calls"]
    assert read(new / "result.json")["graph_published"] is False
    assert before == {str(p): hash_file(Path(p)) for p in before}


@pytest.mark.parametrize("defect", ["checkpoint", "source", "usage", "unbound"])
def test_direct_core_extension_rejects_changed_authority_before_provider(dossier_parent, defect):
    ws, run, bundle, plan = exhausted(dossier_parent)
    grant = preview_grant(ws.settings, run, policy_for(plan))
    selected = select_checkpoint(ws.settings, run, grant)
    descriptor = reserve_grant(ws.settings, run, grant, digest_json(selected))
    settings, new, _ = prepare_extension(ws.settings, descriptor, selected)
    if defect == "checkpoint":
        p = new / "budget_checkpoint.json"; value = read(p); value["minimum_calls"] = 1; rewrite(p, value)
    elif defect == "source":
        p = Path(bundle["documents"][0]["source"]["path"]); p.write_bytes(p.read_bytes() + b"changed")
    elif defect == "usage":
        p = run / "calls/extraction/result.json"; value = read(p); value["elapsed_seconds"] += 1; rewrite(p, value)
    else:
        from watertreatment.budget_grants import BOUND
        with DataStore(ws.data).connection() as db:
            db.execute("DELETE FROM run_events WHERE run_id=? AND event=?", (new.name, BOUND))
    client = FakeReader(settings, bundle)
    client.total_timeout_seconds = settings.reading.total_timeout_seconds
    with pytest.raises((ValueError, OSError)):
        execute_plan(settings, new / "plan.json", client=client, verify=True)
    assert client.calls_started == 0


@pytest.fixture
def budget_web(dossier_parent):
    ws, run, bundle, plan = exhausted(dossier_parent)
    jobs = JobQueue(ws.settings.data_root)
    paper = {"id": plan["paper_id"], "cohort_role": plan["cohort_role"], "files": [
        {"path": doc["source"]["metadata"]["original_path"], "sha256": doc["source"]["sha256"],
         "role": doc["source"]["role"], "status": "confirmed"} for doc in bundle["documents"]]}
    store = DataStore(ws.settings.data_root)
    records = SimpleNamespace(list_records=lambda: [])
    w = SimpleNamespace(settings=ws.settings, store=store, jobs=jobs, records=records,
        library=SimpleNamespace(get=lambda _: paper, roles_for_import=lambda _: {}))
    flow = RecoveryWorkflow(w)
    job_id = jobs.enqueue("extract", {"paper_id": plan["paper_id"], "run_id": run.name})
    with jobs.db() as db:
        db.execute("UPDATE jobs SET status='failed',error='retained source-bound timeout' WHERE id=?", (job_id,))
    return SimpleNamespace(ws=ws, run=run, plan=plan, bundle=bundle, w=w, flow=flow,
        extension=BudgetExtensionWorkflow(flow), jobs=jobs, job_id=job_id, policy=policy_for(plan))


def test_manual_preview_is_read_only_and_repeated_submit_reserves_once(budget_web):
    f = budget_web
    old_job = f.jobs.get(f.job_id)
    before = f.w.store.runs()
    preview = f.flow.extension_options(f.job_id, f.policy)
    assert preview["available"], preview["reason"]
    assert preview["policy_editable"]
    assert preview["continuation"]["label"] == "从已完成全文理解继续抽取"
    assert f.w.store.runs() == before and len(f.jobs.list()) == 1
    identifier = f.flow.queue_extension(f.job_id, preview["base_fingerprint"], f.policy)
    assert f.flow.queue_extension(f.job_id, preview["base_fingerprint"], f.policy) == identifier
    job = f.jobs.get(identifier)
    assert job["kind"] == "recover" and job["payload"]["recovery_action"] == "extend_budget"
    assert job["payload"]["auto_publish"] is False
    assert f.jobs.get(f.job_id) == old_job
    assert f.w.store.runs() == before  # Preparing the new model run is execution, not diagnosis/queueing.


@pytest.mark.parametrize("status", ["cancelled", "failed"])
def test_unstarted_addition_reuses_the_same_reservation(budget_web, status):
    f = budget_web
    preview = f.flow.extension_options(f.job_id, f.policy)
    first = f.flow.queue_extension(f.job_id, preview["base_fingerprint"], f.policy)
    descriptor = f.jobs.get(first)["payload"]["budget_grant"]
    if status == "cancelled":
        f.jobs.cancel(first)
    else:
        with f.jobs.db() as db:
            db.execute("UPDATE jobs SET status='failed' WHERE id=?", (first,))
    resume = f.flow.extension_options(first)
    assert resume["available"], resume["reason"]
    assert resume["reuse_reserved"] and resume["proposed_addition"] == {"seconds": 0, "calls": 0}
    grant = load_grant(f.ws.settings, descriptor)
    assert resume["budget_summary"]["allocated_seconds"] == grant["authorized_seconds"] + grant["added_seconds"]
    assert resume["budget_summary"]["allocated_calls"] == grant["authorized_calls"] + grant["added_calls"]
    second = f.flow.queue_extension(first, resume["base_fingerprint"])
    assert second != first and f.jobs.get(first)["status"] == status
    assert f.jobs.get(second)["payload"]["budget_grant"] == descriptor
    # Execution rechecks the previous failed/cancelled task while excluding the
    # new active job; an unbound reservation must not block its own continuation.
    _, parent, _ = f.extension._context(first, exclude_job=second)
    assert parent == f.run
    assert len(list((f.ws.settings.data_root / "budget_grants").glob("*.json"))) == 1


def test_old_task_cannot_gain_automatic_authorization_from_new_defaults(budget_web):
    f = budget_web
    result = f.flow.automatic_budget_extension(f.job_id)
    assert result["action"] != "queued"
    assert "预授权" in result["reason"]
    assert len(f.jobs.list()) == 1


@pytest.mark.parametrize("finish_child", [False, True])
def test_automatic_original_allowance_resumes_dossier_and_survives_parent_decision(active, monkeypatch, finish_child):
    from watertreatment.budget_policy import freeze_initial_policy
    ws, run, bundle, plan = active
    policy = policy_for(plan, automatic=0)
    jobs = JobQueue(ws.data)
    job_id = jobs.enqueue("extract", {"paper_id":plan["paper_id"], "run_id":run.name,
                                      "budget_policy":policy, "batch_id":"batch-test"})
    freeze_initial_policy(ws.settings, run, policy, "batch-test", job_id=job_id)
    make_dossier_parent((ws, run, bundle, read(run/"plan.json")))
    paper = {"id":plan["paper_id"],"files":[{"path":d["source"]["metadata"]["original_path"],
        "sha256":d["source"]["sha256"],"role":d["source"]["role"],"status":"confirmed"} for d in bundle["documents"]]}
    flow = RecoveryWorkflow(SimpleNamespace(settings=ws.settings, store=DataStore(ws.data), jobs=jobs,
        records=SimpleNamespace(list_records=lambda:[]),
        library=SimpleNamespace(get=lambda _:paper, roles_for_import=lambda _:{})))
    with jobs.db() as db:
        db.execute("UPDATE jobs SET status='failed',error='TimeoutError' WHERE id=?", (job_id,))
    jobs.on_terminal = flow.automatic_resume
    jobs._after_terminal(job_id)
    decision = jobs.get(job_id)["payload"]["auto_budget_decision"]
    assert decision["action"] == "resumed", decision
    child = jobs.get(decision["queued_job_id"])
    payload = child["payload"]
    assert not payload["auto_publish"] and payload["recovery_action"] == "resume_extraction"
    assert not list((ws.data/"budget_grants").glob("*.json"))
    state, selections = flow._inspect(job_id, exclude_job=child["id"], automatic_resume=True)
    assert state["base_fingerprint"] == payload["base_fingerprint"]
    assert selections["resume_extraction"] == payload["recovery_selection"]
    assert flow._within_original_allowance(run, child, selections["resume_extraction"])
    jobs._after_terminal(job_id)
    assert len(jobs.list()) == 2
    if finish_child:
        client = FakeReader(ws.settings, bundle)
        def offline_client(settings, *, live, total_timeout_seconds):
            client.settings = settings
            client.total_timeout_seconds = total_timeout_seconds
            return client
        monkeypatch.setattr("watertreatment.app_server.AppServerClient", offline_client)
        flow.w.records.refresh = lambda: None
        flow.w.records.get = lambda _: {"summary":{"admissible":False}}
        result = flow.execute(child, lambda stage, **values: jobs.progress(child["id"], stage, **values))
        assert [s for s, _ in client.stages][:2] == ["extraction", "audit_0"]
        assert all(s != "comprehension" for s, _ in client.stages)
        assert result["incomplete"] and not result["graph_published"]
        assert result["run_id"] != run.name and not list((ws.data/"budget_grants").glob("*.json"))
        return
    with jobs.db() as db:
        db.execute("UPDATE jobs SET status='failed' WHERE id=?", (child["id"],))
    jobs.progress(child["id"], "unchanged checkpoint", run_id=run.name)
    assert flow.automatic_resume(jobs.get(child["id"]))["action"] == "no_progress"


def test_frozen_policy_cannot_be_raised_and_stale_usage_cannot_be_submitted(budget_web):
    f = budget_web
    preview = f.flow.extension_options(f.job_id, f.policy)
    path = f.run / "calls/extraction/result.json"
    value = read(path); value["elapsed_seconds"] += 1; rewrite(path, value)
    with pytest.raises(ValueError, match="变化"):
        f.flow.queue_extension(f.job_id, preview["base_fingerprint"], f.policy)
    assert len(f.jobs.list()) == 1


def test_no_new_automatic_reservation_after_pause_during_diagnosis(budget_web, monkeypatch):
    f = budget_web
    state = f.flow.extension_options(f.job_id, f.policy)
    original = f.extension.inspect
    def paused(*args, **kwargs):
        answer = original(*args, **{**kwargs, "automatic": False})
        f.jobs.pause(True)
        return answer
    monkeypatch.setattr(f.extension, "inspect", paused)
    with pytest.raises(ValueError, match="暂停"):
        f.extension.queue(f.job_id, state["base_fingerprint"], f.policy, automatic=True)
    assert len(f.jobs.list()) == 1


def test_batch_authorized_addition_queues_once_and_no_progress_stops_another(active):
    from watertreatment.budget_policy import freeze_initial_policy
    ws, run, bundle, plan = active
    policy = policy_for(plan, automatic=2)
    freeze_initial_policy(ws.settings, run, policy, "synthetic-batch")
    plan = read(run / "plan.json")
    parent = make_dossier_parent((ws, run, bundle, plan))
    exhausted(parent)
    jobs = JobQueue(ws.settings.data_root)
    paper = {"id": plan["paper_id"], "cohort_role": plan["cohort_role"], "files": [
        {"path": d["source"]["metadata"]["original_path"], "sha256": d["source"]["sha256"],
         "role": d["source"]["role"], "status": "confirmed"} for d in bundle["documents"]]}
    w = SimpleNamespace(settings=ws.settings, store=DataStore(ws.data), jobs=jobs,
        records=SimpleNamespace(list_records=lambda: []),
        library=SimpleNamespace(get=lambda _: paper, roles_for_import=lambda _: {}))
    flow = RecoveryWorkflow(w)
    original = jobs.enqueue("extract", {"paper_id": plan["paper_id"], "run_id": run.name,
        "budget_policy": policy, "batch_id": "synthetic-batch"})
    with jobs.db() as db:
        db.execute("UPDATE jobs SET status='failed' WHERE id=?", (original,))
    decision = flow.automatic_budget_extension(original)
    assert decision["action"] == "queued", decision
    child_job = jobs.get(decision["queued_job_id"])
    args = child_job["payload"]
    grant = load_grant(ws.settings, args["budget_grant"])
    assert grant["mode"] == "automatic" and grant["auto_additions_used"] == 0
    bounded, child, _ = prepare_extension(ws.settings, args["budget_grant"], args["recovery_selection"])
    record_failed_extraction(child, read(child / "plan.json"), seconds=grant["allocated_seconds"], calls=1)
    jobs.progress(child_job["id"], "retained synthetic timeout", run_id=child.name)
    with jobs.db() as db:
        db.execute("UPDATE jobs SET status='failed' WHERE id=?", (child_job["id"],))
    after = flow.automatic_budget_extension(child_job["id"])
    assert after["action"] == "no_progress", after
    assert len(jobs.list()) == 2
    assert len(list((ws.data / "budget_grants").glob("*.json"))) == 1


def test_duplicate_audit_files_or_prose_only_changes_do_not_count_as_progress(tmp_path):
    from watertreatment.budget_extension_workflow import progress_signature
    run = tmp_path / "retained"; run.mkdir()
    report = {"findings": [{"record_path": "observations[0].value", "severity": "major", "verdict": "unsupported",
        "needs_revision": True, "explanation": "First wording", "evidence": []}], "potential_omissions": []}
    rewrite(run / "audit.v00.json", report)
    before = progress_signature(run)
    report["findings"][0]["explanation"] = "Same unresolved defect in new words"
    rewrite(run / "audit.v01.json", report)
    assert progress_signature(run) == before
    report["findings"][0]["needs_revision"] = False
    report["findings"][0]["verdict"] = "supported"
    rewrite(run / "audit.v01.json", report)
    assert progress_signature(run) != before
