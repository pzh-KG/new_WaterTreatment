"""Offline recovery routing; synthetic artifacts, no provider or Neo4j."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_web_records import fixture, read
from watertreatment.storage import hash_file, write_new_json
from watertreatment.web_jobs import JobQueue
from watertreatment.web_recovery import RecoveryWorkflow


def rewrite(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def fail(jobs, identifier, *, status="failed"):
    with jobs.db() as db:
        db.execute("UPDATE jobs SET status=? WHERE id=?", (status, identifier))


@pytest.fixture
def recovery(fixture, monkeypatch):
    f = fixture
    f.settings.codex.model = "gpt-6.2-sol"
    f.settings.codex.reasoning_effort = "max"
    f.settings.codex.max_calls_per_run = 5
    f.settings.reading.enabled = True
    f.settings.reading.total_timeout_seconds = 600
    bundle = read(f.run / "sources.json")
    bundle["paper_id"] = f.record.paper.id
    bundle["documents"][0]["source"]["metadata"] = {"original_path": str(f.source)}
    rewrite(f.run / "sources.json", bundle)
    imported = f.store.new_run("import", {"paper_id": f.record.paper.id})
    write_new_json(imported / "sources.json", bundle)
    f.store.event(imported.name, "import_completed", {"bundle_sha256": hash_file(imported / "sources.json")})
    manifest = read(f.run / "manifest.json")
    manifest["source_bundle"] = str(imported / "sources.json")
    rewrite(f.run / "manifest.json", manifest)
    plan = read(f.run / "plan.json")
    plan.update(max_calls=3, model_settings=f.settings.codex.model_dump(mode="json"),
                reading_settings={"total_timeout_seconds": 600, "max_revisions": 2})
    rewrite(f.run / "plan.json", plan)
    terminal = {"status": "candidate_incomplete_failure", "last_candidate_version": "candidate.v00.json",
                "model_calls": 1, "human_validated": False, "graph_published": False}
    rewrite(f.run / "result.json", terminal)
    write_new_json(f.run / "candidate.v00.json", f.record.model_dump(mode="json"))
    write_new_json(f.run / "reading_dossier.json", {"paper_id": f.record.paper.id})
    output = read(f.run / "calls" / "audit" / "result.json")
    output["elapsed_seconds"] = 100
    rewrite(f.run / "calls" / "audit" / "result.json", output)
    jobs = JobQueue(f.settings.data_root)
    paper = {"id": f.record.paper.id, "cohort_role": "unassigned", "source_fingerprint": "synthetic",
        "files": [{"name": f.source.name, "path": str(f.source), "sha256": hash_file(f.source),
                   "role": "main_article", "status": "confirmed"}]}
    def roles(_):
        if hash_file(f.source) != paper["files"][0]["sha256"]:
            raise ValueError("附件内容已变化，请重新扫描")
        return {f.source.name: paper["files"][0]["role"]}
    rows = []
    records = SimpleNamespace(list_records=lambda: rows, refresh=lambda: None,
                              get=lambda _: {"summary": {"admissible": True}})
    workbench = SimpleNamespace(settings=f.settings, store=f.store, jobs=jobs, records=records,
        library=SimpleNamespace(get=lambda _: paper, roles_for_import=roles))
    workflow = RecoveryWorkflow(workbench)
    # Route tests isolate legacy call authentication while retaining real source
    # matching, budgets, identities, hashes, queueing and execution dispatch.
    monkeypatch.setattr("watertreatment.workflow._reusable_dossier", lambda *a: (None, {}))
    monkeypatch.setattr("watertreatment.workflow._reusable_candidate", lambda *a: (f.record, None, {}))
    monkeypatch.setattr(workflow, "_special", lambda *a: (_ for _ in ()).throw(ValueError("not the matching specialized route")))
    identifier = jobs.enqueue("extract", {"paper_id": paper["id"], "run_id": f.run.name, "auto_publish": True})
    fail(jobs, identifier)
    return SimpleNamespace(f=f, w=workbench, flow=workflow, jobs=jobs, job_id=identifier, paper=paper, rows=rows)


def action(state, key):
    return next(a for a in state["actions"] if a["id"] == key)


def test_pre_read_failure_offers_first_read_without_claiming_a_candidate(recovery, monkeypatch):
    r = recovery
    (r.f.run / "candidate.v00.json").unlink()
    (r.f.run / "candidate.json").unlink(missing_ok=True)
    (r.f.run / "reading_dossier.json").unlink()
    seen = []
    def initial(which, run):
        seen.append(which)
        return {"route": "initial_reading", "budget": {"remaining_calls": 3, "remaining_seconds": 600}}
    monkeypatch.setattr(r.flow, "_stage_recovery", initial)
    state = r.flow.inspect(r.job_id)
    assert seen == ["resume_initial"]
    assert state["recommended_action"] == "resume_initial"
    assert not state["candidate_summary"]["has_candidate"]
    assert action(state, "continue_candidate")["applicable"] is False
    assert "首次阅读" in state["recovery_explanation"]
    assert r.jobs.list()[0]["status"] == "failed"


def test_identity_recovery_does_not_enable_ordinary_candidate_bypass(recovery, monkeypatch):
    r = recovery
    candidate = read(r.f.run / "candidate.v00.json")
    candidate["paper"]["id"] = "synthetic-mistaken-doi"
    rewrite(r.f.run / "candidate.v00.json", candidate)
    monkeypatch.setattr(r.flow, "_stage_recovery", lambda which, run: {
        "route": "identity_candidate", "budget": {"remaining_calls": 2, "remaining_seconds": 500}})
    state = r.flow.inspect(r.job_id)
    assert state["recommended_action"] == "repair_identity"
    assert not action(state, "continue_candidate")["available"]
    assert state["candidate_summary"]["observation_count"] == 2
    assert state["budget_summary"]["remaining_calls"] == 2


def test_completed_dossier_without_candidate_offers_extraction_stage(recovery, monkeypatch):
    r = recovery
    (r.f.run / "candidate.v00.json").unlink()
    (r.f.run / "candidate.json").unlink(missing_ok=True)
    seen = []
    def completed_dossier(which, run):
        seen.append(which)
        return {"route": "dossier_extraction", "budget": {"remaining_calls": 2, "remaining_seconds": 300}}
    monkeypatch.setattr(r.flow, "_stage_recovery", completed_dossier)
    state = r.flow.inspect(r.job_id)
    assert seen == ["resume_extraction"]
    assert state["recommended_action"] == "resume_extraction"
    assert not state["candidate_summary"]["has_candidate"]
    assert action(state, "continue_candidate")["applicable"] is False
    assert "已完成全文理解" in state["recovery_explanation"]
    assert state["budget_summary"]["remaining_calls"] == 2


def test_partial_resume_uses_authenticated_chain_balance_and_binds_it_to_preview(recovery, monkeypatch):
    r = recovery
    plan = read(r.f.run / "plan.json")
    plan.update(workflow="partial_scientific_audit_completion", max_calls=1)
    plan["reading_settings"]["total_timeout_seconds"] = 100
    rewrite(r.f.run / "plan.json", plan)
    balance = {"policy": "lineage_remaining", "original_seconds": 500, "used_seconds": 350,
               "remaining_seconds": 150, "remaining_calls": 1, "seconds": 150, "calls": 1}
    monkeypatch.setattr(r.flow, "_stage_recovery", lambda which, run: {
        "route": "partial_resume", "budget": dict(balance)})
    state = r.flow.inspect(r.job_id)
    assert not action(state, "continue_candidate")["available"]
    assert state["recommended_action"] == "resume_audit"
    assert state["budget_summary"] == balance
    balance["remaining_seconds"] = balance["seconds"] = 140
    with pytest.raises(ValueError, match="已变化"):
        r.flow.queue(r.job_id, "resume_audit", state["base_fingerprint"])
    assert len(r.jobs.list()) == 1


def test_stage_auth_failure_is_not_a_full_extraction_fallback(recovery, monkeypatch):
    r = recovery
    plan = read(r.f.run / "plan.json")
    plan["workflow"] = "partial_scientific_audit_completion"
    rewrite(r.f.run / "plan.json", plan)
    def rejected(*args):
        raise ValueError("lineage delivery proof changed")
    monkeypatch.setattr(r.flow, "_stage_recovery", rejected)
    state = r.flow.inspect(r.job_id)
    assert not action(state, "resume_audit")["available"]
    assert "lineage delivery proof changed" == action(state, "resume_audit")["technical_reason"]
    with pytest.raises(ValueError):
        r.flow.queue(r.job_id, "resume_audit", state["base_fingerprint"])
    assert len(r.jobs.list()) == 1


@pytest.mark.parametrize("selected_action,route", [
    ("resume_initial", "initial_reading"), ("repair_identity", "identity_candidate"),
    ("resume_audit", "partial_resume"), ("resume_extraction", "dossier_extraction")])
def test_stage_recovery_execution_uses_only_selected_checkpoint(recovery, monkeypatch, selected_action, route):
    r = recovery
    budget = {"remaining_seconds": 120, "remaining_calls": 1, "seconds": 120, "calls": 1}
    choice = {"route": route, "budget": budget}
    monkeypatch.setattr(r.flow, "_stage_actions", lambda *a: [selected_action])
    monkeypatch.setattr(r.flow, "_stage_recovery", lambda *a: choice)
    seen = {}
    r.w._extract = lambda *a, **k: pytest.fail("stage recovery must not enter re-extraction")
    def prepared(settings, parent, selection=None, *, timeout_seconds=None):
        assert parent == r.f.run
        if route == "partial_resume":
            assert timeout_seconds == 120
        else:
            assert selection == choice
        bounded = settings.model_copy(deep=True)
        bounded.codex.max_calls_per_run = 1
        bounded.codex.timeout_seconds = bounded.reading.total_timeout_seconds = 120
        run = r.f.store.new_run("extraction", {"paper_id": r.paper["id"]})
        write_new_json(run / "plan.json", {"model_settings": bounded.codex.model_dump(mode="json"),
            "reading_settings": bounded.reading.model_dump(mode="json"), "max_calls": 1})
        seen["prepared"] = True
        return run / "plan.json" if route == "partial_resume" else (bounded, run)
    def executed(settings, plan, *, client, **kwargs):
        assert seen["prepared"]
        assert kwargs == ({} if route == "partial_resume" else {"verify": True})
        seen["executed"] = True
        write_new_json(plan.parent / "result.json", {"status": "candidate_audited_with_limitations"})
    class Client:
        def __init__(self, config, **kwargs):
            assert config.model == "gpt-6.2-sol" and config.reasoning_effort == "max"
            assert config.max_calls_per_run == 1 and kwargs["total_timeout_seconds"] == 120
        def close(self):
            seen["closed"] = True
    if route == "partial_resume":
        monkeypatch.setattr("watertreatment.partial_audit_remaining_resume.prepare", prepared)
        monkeypatch.setattr("watertreatment.partial_audit_remaining_resume.execute", executed)
    elif route == "dossier_extraction":
        monkeypatch.setattr("watertreatment.dossier_extraction_resume.prepare_selected", prepared)
        monkeypatch.setattr("watertreatment.dossier_extraction_resume.execute_selected", executed)
    else:
        monkeypatch.setattr("watertreatment.web_initial_recovery.prepare_selected", prepared)
        monkeypatch.setattr("watertreatment.web_initial_recovery.execute_selected", executed)
    monkeypatch.setattr("watertreatment.app_server.AppServerClient", Client)
    state = r.flow.inspect(r.job_id)
    identifier = r.flow.queue(r.job_id, selected_action, state["base_fingerprint"])
    outcome = r.flow.execute(r.jobs.get(identifier), lambda *a, **k: None)
    assert seen == {"prepared": True, "executed": True, "closed": True}
    assert outcome["graph_published"] is False
    assert r.jobs.get(r.job_id)["status"] == "failed"


def test_discovery_is_read_only_and_uses_parent_remaining_budget(recovery):
    r = recovery
    before = {str(p): hash_file(p) for p in r.f.run.rglob("*") if p.is_file()}
    runs = r.f.store.runs()
    jobs = r.jobs.list()
    state = r.flow.inspect(r.job_id)
    assert state["recommended_action"] == "continue_candidate"
    assert action(state, "continue_candidate")["budget"]["remaining_calls"] == 2
    assert action(state, "continue_candidate")["budget"]["remaining_seconds"] == 500
    assert state["candidate_summary"]["observation_count"] == 2
    assert r.f.store.runs() == runs and r.jobs.list() == jobs
    assert before == {str(p): hash_file(p) for p in r.f.run.rglob("*") if p.is_file()}


def test_exhausted_budget_disables_continuation_and_never_recommends_reextract(recovery):
    r = recovery
    path = r.f.run / "calls" / "audit" / "result.json"
    output = read(path)
    output["elapsed_seconds"] = 600
    rewrite(path, output)
    state = r.flow.inspect(r.job_id)
    assert not action(state, "continue_candidate")["available"]
    assert state["budget_summary"]["exhausted"]
    assert state["recommended_action"] is None
    assert action(state, "reextract")["available"]


def test_unknown_usage_is_not_a_new_budget(recovery):
    r = recovery
    path = r.f.run / "calls" / "audit" / "result.json"
    rewrite(path, {"status": "failed"})
    state = r.flow.inspect(r.job_id)
    assert not action(state, "continue_candidate")["available"]
    assert state["budget_summary"]["unknown"]


def test_changed_current_source_blocks_every_model_operation(recovery):
    r = recovery
    r.f.source.write_text("changed source", encoding="utf-8")
    state = r.flow.inspect(r.job_id)
    assert not any(a["available"] for a in state["actions"])


def test_changed_role_only_blocks_inheritance_but_allows_explicit_new_extraction(recovery):
    r = recovery
    r.paper["files"][0]["role"] = "supporting_information"
    state = r.flow.inspect(r.job_id)
    assert not action(state, "continue_candidate")["available"]
    assert action(state, "reextract")["available"]


def test_stale_preview_and_unrecognized_actions_cannot_enqueue(recovery):
    r = recovery
    state = r.flow.inspect(r.job_id)
    rewrite(r.f.run / "failure.json", {"error": "new failure context"})
    with pytest.raises(ValueError, match="已变化"):
        r.flow.queue(r.job_id, "continue_candidate", state["base_fingerprint"])
    with pytest.raises(ValueError, match="未知"):
        r.flow.queue(r.job_id, "arbitrary_command", state["base_fingerprint"])
    assert len(r.jobs.list()) == 1


def test_double_click_is_idempotent_and_disables_inherited_autopublish(recovery):
    r = recovery
    state = r.flow.inspect(r.job_id)
    identifier = r.flow.queue(r.job_id, "continue_candidate", state["base_fingerprint"])
    assert identifier == r.flow.queue(r.job_id, "continue_candidate", state["base_fingerprint"])
    job = r.jobs.get(identifier)
    assert job["kind"] == "recover" and job["payload"]["auto_publish"] is False
    assert job["payload"]["parent_run_id"] == r.f.run.name
    assert r.jobs.get(r.job_id)["status"] == "failed"


def test_failed_recovery_cannot_repeat_same_seed_or_budget(recovery):
    r = recovery
    state = r.flow.inspect(r.job_id)
    identifier = r.flow.queue(r.job_id, "continue_candidate", state["base_fingerprint"])
    fail(r.jobs, identifier)
    new_state = r.flow.inspect(r.job_id)
    assert not action(new_state, "continue_candidate")["available"]
    with pytest.raises(ValueError, match="已有保留"):
        r.flow.queue(r.job_id, "continue_candidate", new_state["base_fingerprint"])


def test_other_active_work_on_same_paper_blocks_recovery(recovery):
    r = recovery
    r.jobs.enqueue("extract", {"paper_id": r.paper["id"]})
    assert not any(a["available"] for a in r.flow.inspect(r.job_id)["actions"])


def test_newer_desktop_failed_run_selected_without_falling_back(recovery):
    r = recovery
    newer = r.f.store.new_run("extraction", {"paper_id": r.paper["id"]})
    for name in ("plan.json", "sources.json"):
        write_new_json(newer / name, read(r.f.run / name))
    write_new_json(newer / "result.json", {"status": "reading_incomplete_failure"})
    state = r.flow.inspect(r.job_id)
    assert state["original_run_id"] == r.f.run.name and state["parent_run_id"] == newer.name
    assert not state["candidate_summary"]["has_candidate"]
    assert not action(state, "continue_candidate")["available"]


def test_prepared_only_desktop_run_is_not_newest_candidate_but_reserves_seed(recovery):
    r = recovery
    newer = r.f.store.new_run("extraction", {"paper_id": r.paper["id"]})
    plan = read(r.f.run / "plan.json")
    plan["reused_candidate"] = {"parent_run": r.f.run.name}
    write_new_json(newer / "plan.json", plan)
    state = r.flow.inspect(r.job_id)
    assert state["parent_run_id"] == r.f.run.name
    assert not action(state, "continue_candidate")["available"]


def test_already_admissible_current_result_blocks_recovery(recovery):
    r = recovery
    r.rows.append({"id": r.f.run.name, "run_id": r.f.run.name, "paper_id": r.paper["id"],
        "current": True, "admissible": True, "created_at": r.f.store.runs()[0]["created_at"]})
    state = r.flow.inspect(r.job_id)
    assert state["current_result_id"] == r.f.run.name
    assert not any(a["available"] for a in state["actions"])


def test_tampered_queued_recovery_stops_before_preparation(recovery):
    r = recovery
    state = r.flow.inspect(r.job_id)
    identifier = r.flow.queue(r.job_id, "continue_candidate", state["base_fingerprint"])
    job = r.jobs.get(identifier)
    job["payload"]["recovery_selection"]["budget"]["remaining_seconds"] = 9999
    with pytest.raises(ValueError, match="预算已改变"):
        r.flow.execute(job, lambda *a, **k: None)


def test_explicit_reextract_passes_no_autopublish_and_retains_failed_job(recovery):
    r = recovery
    seen = []
    r.w._extract = lambda args, progress: seen.append(args) or {"run_id": "new", "incomplete": True}
    state = r.flow.inspect(r.job_id)
    identifier = r.flow.queue(r.job_id, "reextract", state["base_fingerprint"])
    output = r.flow.execute(r.jobs.get(identifier), lambda *a, **k: None)
    from watertreatment.budget_policy import default_policy
    policy = default_policy(r.w.settings)
    policy["max_auto_additions"] = 0
    assert seen == [{"paper_id": r.paper["id"], "auto_publish": False, "cohort_role": "unassigned",
        "budget_policy": policy, "batch_id": None, "_job_id": identifier}]
    assert output["incomplete"] and r.jobs.get(r.job_id)["status"] == "failed"


def test_general_continuation_executes_only_reused_candidate_with_clipped_budget(recovery, monkeypatch):
    r = recovery
    seen = {}
    def prepare(settings, bundle, **kw):
        seen.update(settings=settings, bundle=bundle, kwargs=kw)
        run = r.f.store.new_run("extraction", {"paper_id": r.paper["id"]})
        write_new_json(run / "plan.json", {"model_settings": settings.codex.model_dump(mode="json"),
            "reading_settings": settings.reading.model_dump(mode="json"), "max_calls": settings.codex.max_calls_per_run})
        return run
    def execute(settings, plan, **kw):
        seen["execute_kwargs"] = kw
        write_new_json(plan.parent / "result.json", {"status": "candidate_model_audited"})
    class Client:
        def __init__(self, config, **kw):
            seen["client_config"], seen["client_kwargs"] = config, kw
        def close(self):
            seen["closed"] = True
    monkeypatch.setattr("watertreatment.workflow.prepare", prepare)
    monkeypatch.setattr("watertreatment.workflow.execute_plan", execute)
    monkeypatch.setattr("watertreatment.app_server.AppServerClient", Client)
    state = r.flow.inspect(r.job_id)
    identifier = r.flow.queue(r.job_id, "continue_candidate", state["base_fingerprint"])
    output = r.flow.execute(r.jobs.get(identifier), lambda *a, **k: None)
    assert seen["settings"].codex.max_calls_per_run == 2
    assert seen["settings"].reading.total_timeout_seconds == 500
    assert seen["kwargs"]["reuse_candidate"] == r.f.run / "candidate.v00.json"
    assert seen["kwargs"]["reuse_dossier"] == r.f.run / "reading_dossier.json"
    assert seen["execute_kwargs"]["verify"] and seen["closed"]
    assert output["graph_published"] is False and output["incomplete"] is False
    assert r.f.settings.codex.max_calls_per_run == 5


def test_failed_authentication_never_falls_back_to_full_extract(recovery, monkeypatch):
    r = recovery
    r.w._extract = lambda *a, **k: pytest.fail("unexpected full extraction")
    def rejected(*a):
        raise ValueError("candidate provenance does not match")
    monkeypatch.setattr("watertreatment.workflow._reusable_candidate", rejected)
    state = r.flow.inspect(r.job_id)
    assert not action(state, "continue_candidate")["available"]
    assert "provenance" in action(state, "continue_candidate")["technical_reason"]


def test_completed_science_with_coverage_gaps_only_offers_coverage(recovery, monkeypatch):
    r = recovery
    def special(action, *a):
        if action != "correct_reading":
            raise ValueError("not applicable")
        return {"route": "coverage", "budget": {"seconds": 900, "calls": 1}}
    monkeypatch.setattr(r.flow, "_special", special)
    state = r.flow.inspect(r.job_id)
    assert state["recommended_action"] == "correct_reading"
    assert not action(state, "continue_candidate")["available"]


def test_newer_unfinished_desktop_run_is_not_treated_as_stopped(recovery):
    r = recovery
    run = r.f.store.new_run("extraction", {"paper_id": r.paper["id"]})
    for name in ("plan.json", "sources.json"):
        write_new_json(run / name, read(r.f.run / name))
    write_new_json(run / "execution_started.json", {"model": "gpt-6.2-sol"})
    state = r.flow.inspect(r.job_id)
    assert state["parent_run_id"] == run.name
    assert not any(a["available"] for a in state["actions"])


def test_newer_unverified_staged_candidate_cannot_be_silently_ignored(recovery, monkeypatch):
    r = recovery
    staged = r.f.record.model_copy(deep=True)
    staged.observations[0].value.value = 3
    path = r.f.run / "reading_tools" / "tool-0099" / "submitted_record.json"
    write_new_json(path, staged.model_dump(mode="json"))
    seen = []
    def authenticate(settings, candidate, *a):
        seen.append(candidate)
        raise ValueError("staged provenance is missing")
    monkeypatch.setattr("watertreatment.workflow._reusable_staged_candidate", authenticate)
    state = r.flow.inspect(r.job_id)
    assert seen == [path]
    assert not action(state, "continue_candidate")["available"]
    assert "staged provenance" in action(state, "continue_candidate")["technical_reason"]


def test_exhausted_revision_count_blocks_candidate_requiring_repairs(recovery, monkeypatch):
    r = recovery
    plan = read(r.f.run / "plan.json")
    plan["reading_settings"]["max_revisions"] = 0
    rewrite(r.f.run / "plan.json", plan)
    monkeypatch.setattr("watertreatment.workflow._reusable_candidate", lambda *a: (r.f.record, object(), {}))
    state = r.flow.inspect(r.job_id)
    assert not action(state, "continue_candidate")["available"]
    assert "修订轮次" in action(state, "continue_candidate")["reason"]


@pytest.mark.parametrize("route,calls", [("coverage", 1), ("coverage", 0), ("partial", 1), ("addendum", 1)])
def test_specialized_execution_dispatch_preserves_exact_helper_and_frozen_model(recovery, monkeypatch, route, calls):
    r = recovery
    selected_action = "correct_reading" if route == "coverage" else "complete_audit"
    def special(action, *a):
        if action != selected_action:
            raise ValueError("not applicable")
        return {"route": route, "budget": {"seconds": 120, "calls": calls}}
    monkeypatch.setattr(r.flow, "_special", special)
    seen = {}
    def prepare(settings, parent, *, timeout_seconds):
        seen.update(parent=parent, timeout=timeout_seconds)
        run = r.f.store.new_run("extraction", {"paper_id": r.paper["id"]})
        config = settings.codex.model_copy(update={"max_calls_per_run": 1, "timeout_seconds": 120})
        write_new_json(run / "plan.json", {"model_settings": config.model_dump(mode="json"),
            "reading_settings": {"total_timeout_seconds": 120}, "max_calls": calls})
        return run / "plan.json"
    def execute(settings, plan, *, client):
        seen["client"] = client
        write_new_json(plan.parent / "result.json", {"status": "candidate_model_audited"})
    class Client:
        def __init__(self, config, *, live, total_timeout_seconds):
            assert calls and live
            assert config.model == "gpt-6.2-sol" and config.reasoning_effort == "max"
            assert config.max_calls_per_run == 1 and total_timeout_seconds == 120
        def close(self):
            seen["closed"] = True
    module = {"coverage": "coverage_correction_workflow", "partial": "partial_audit_completion_workflow",
              "addendum": "audit_addendum_workflow"}[route]
    monkeypatch.setattr(f"watertreatment.{module}." + ("prepare_audit_addendum" if route == "addendum" else "prepare"), prepare)
    monkeypatch.setattr(f"watertreatment.{module}." + ("execute_audit_addendum" if route == "addendum" else "execute"), execute)
    monkeypatch.setattr("watertreatment.app_server.AppServerClient", Client)
    state = r.flow.inspect(r.job_id)
    identifier = r.flow.queue(r.job_id, selected_action, state["base_fingerprint"])
    output = r.flow.execute(r.jobs.get(identifier), lambda *a, **k: None)
    assert seen["parent"] == r.f.run and seen["timeout"] == 120
    assert (seen["client"] is None) == (calls == 0)
    assert output["graph_published"] is False


def test_prepare_authentication_failure_cannot_fall_back_or_start_model(recovery, monkeypatch):
    r = recovery
    r.w._extract = lambda *a, **k: pytest.fail("must not full-extract")
    monkeypatch.setattr("watertreatment.app_server.AppServerClient", lambda *a, **k: pytest.fail("must not call model"))
    def prepare(*a, **k):
        raise ValueError("changed provenance")
    monkeypatch.setattr("watertreatment.workflow.prepare", prepare)
    state = r.flow.inspect(r.job_id)
    identifier = r.flow.queue(r.job_id, "continue_candidate", state["base_fingerprint"])
    with pytest.raises(ValueError, match="changed provenance"):
        r.flow.execute(r.jobs.get(identifier), lambda *a, **k: None)


def test_failed_recheck_retains_question_and_can_use_its_existing_retry_route(recovery):
    r = recovery
    r.w.records.get = lambda _: {"summary": {"paper_id": r.paper["id"], "current": True, "source_change": False}}
    identifier = r.jobs.enqueue("recheck", {"record_id": "synthetic-current", "question": "Inspect specific concentration"})
    fail(r.jobs, identifier)
    state = r.flow.inspect(identifier)
    assert state["recommended_action"] == "retry_recheck"
    queued = r.flow.queue(identifier, "retry_recheck", state["base_fingerprint"])
    job = r.jobs.get(queued)
    assert job["kind"] == "recheck" and job["payload"]["question"] == "Inspect specific concentration"
    assert job["payload"]["previous_job_id"] == identifier

def test_unresolved_local_review_shows_paper_allowance_after_its_one_call(recovery, monkeypatch):
    r = recovery
    plan = read(r.f.run / "plan.json")
    plan.update(workflow="retained-issue-source-review-v1", max_calls=1)
    rewrite(r.f.run / "plan.json", plan)
    terminal = read(r.f.run / "result.json")
    terminal.update(status="candidate_incomplete_budget", stop_reason="local_review_unresolved")
    rewrite(r.f.run / "result.json", terminal)
    balance = {"policy": "lineage_remaining", "original_calls": 5, "used_calls": 3,
               "original_seconds": 750, "used_seconds": 520, "remaining_seconds": 230, "remaining_calls": 2}
    monkeypatch.setattr("watertreatment.budget_grants.retained_stage_budget", lambda settings, run: dict(balance))
    def multiple_open_fields(*args):
        raise ValueError("Two outstanding fields need independent review")
    monkeypatch.setattr("watertreatment.reviewer_text.authenticate", multiple_open_fields)
    state = r.flow.inspect(r.job_id)
    assert not action(state, "complete_audit")["available"]
    assert not action(state, "continue_candidate")["available"]
    assert state["budget_summary"] == {**balance, "exhausted": False}
    assert "整个处理过程" in state["recovery_explanation"]
    assert len(r.jobs.list()) == 1


def test_failed_receipt_only_finalize_can_retry_without_reopening_model_budget(tmp_path):
    jobs = JobQueue(tmp_path)
    parent = "retained-review"
    payload = {"parent_run_id":parent, "recovery_action":"complete_audit",
               "recovery_selection":{"route":"retained_finalize","budget":{"calls":0,"seconds":0}}}
    job = jobs.enqueue("recover",payload)
    workflow = RecoveryWorkflow(SimpleNamespace(settings=SimpleNamespace(data_root=tmp_path),
                                                jobs=jobs,store=SimpleNamespace(runs=lambda: [])))
    assert workflow._prior_attempt(parent, allow_failed_finalize=True)
    fail(jobs,job)
    assert workflow._prior_attempt(parent)
    assert workflow._prior_attempt(parent, allow_failed_finalize=True) is None
    other = jobs.enqueue("recover",{**payload,"recovery_selection":{"route":"retained_guided","budget":{"calls":1,"seconds":60}}})
    fail(jobs,other)
    assert workflow._prior_attempt(parent, allow_failed_finalize=True)
