"""Unaccepted terminal candidates may seed fresh audits, never self-approve."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from test_completed_revision_resume import delivered_case
from test_repair_guidance import failed_repair, rewrite, timeout
from test_staged_resume import staged_case, add_stage, load_events, save_events, recover
from test_web_model import ReplayClient, audit, model_fixture
from test_web_records import fixture, read
from test_web_retry_recovery import service, reaudit as routed_reaudit, saved_run, terminal_job, retry_job, execute
from watertreatment.reading import ReadingAudit
from watertreatment.storage import digest_json, hash_file, write_new_json
from watertreatment.web_model import reaudit


@pytest.fixture
def terminal_case(delivered_case):
    case = delivered_case
    (case.prior / "failure.json").unlink()
    result = read(case.failed_audit_call / "result.json")
    result.update(status="schema_valid_candidate")
    result.pop("error")
    rewrite(case.failed_audit_call / "result.json", result)
    review = audit(revision=True).model_dump(mode="json")
    for path in [case.failed_audit_call / "answer.json", case.prior / "audit.v01.json", case.prior / "model_review.json"]:
        write_new_json(path, review)
    for filename in ["candidate.v01.json", "candidate.json"]:
        write_new_json(case.prior / filename, case.candidate.model_dump(mode="json"))
    write_new_json(case.prior / "validation.v01.json", {"issues": [], "semantic_accuracy": "not_established"})
    history = [{"candidate_sha256": hash_file(case.prior / "candidate.v00.json"), "needs_revision": True,
                "audit_origin": "fresh_model_audit", "validation_error_count": 0, "reading_check_issues": []},
               {"candidate_sha256": hash_file(case.prior / "candidate.json"), "needs_revision": True,
                "audit_origin": "fresh_model_audit", "validation_error_count": 0, "reading_check_issues": []}]
    write_new_json(case.prior / "result.json", {"status": "needs_revision", "stop_reason": "revision_budget_exhausted",
        "history": history, "validation_error_count": 0, "reading_check_issues": [], "revisions": 1,
        "human_validated": False, "graph_published": False, "scientific_accuracy": "not_established"})
    case.query = case.prior / "reading_tools" / "tool-0003"
    binding = read(case.failed_audit_call / "request.json")["request"]["candidate_binding"]
    write_new_json(case.query / "request.json", {"tool": "wt_query_candidate", "arguments": {"path": ["observations", 0]}})
    write_new_json(case.query / "result.json", {"success": True, "data": {
        "candidate_binding": copy.deepcopy(binding), "mode": "path", "returned_count": 1}})
    events = load_events(case)
    events.append({"direction": "receive", "message": {"method": "turn/completed", "params": {
        "threadId": "synthetic-audit-thread", "turn": {"id": "synthetic-audit-turn", "status": "completed"}}}})
    save_events(case, events)
    return case


def test_terminal_resume_is_an_immutable_unresolved_candidate_not_approval(terminal_case):
    case = terminal_case
    before = {p: hash_file(p) for p in case.prior.rglob("*") if p.is_file()}
    candidate, paths, receipt = recover(case)
    assert candidate == case.candidate
    assert receipt["turn_status"] == "completed_revision_with_unresolved_final_audit"
    assert receipt["terminal_run_status"] == "needs_revision"
    assert receipt["terminal_stop_reason"] == "revision_budget_exhausted"
    assert receipt["following_audit_status"] == "schema_valid_candidate"
    assert receipt["requires_fresh_audit"] is True and receipt["scientific_validation"] == "not_established"
    assert case.prior / "result.json" in paths and case.prior / "model_review.json" in paths
    assert case.events_path not in paths and all(p.is_file() for p in paths)
    assert not (case.prior / "failure.json").exists()
    assert {p: hash_file(p) for p in before} == before
    assert len(case.f.records.list_records()) == case.original_records


@pytest.mark.parametrize("field,value", [("status", "candidate_audited_with_limitations"), ("status", "failed"),
    ("stop_reason", "audit_completed"), ("human_validated", True), ("graph_published", True), ("revisions", 0),
    ("revisions", True), ("revisions", 2), ("validation_error_count", 1)])
def test_terminal_resume_rejects_other_or_inconsistent_terminal_states(terminal_case, field, value):
    case = terminal_case
    result = read(case.prior / "result.json")
    result[field] = value
    rewrite(case.prior / "result.json", result)
    with pytest.raises(ValueError):
        recover(case)


def test_terminal_resume_does_not_relabel_a_failed_run_as_terminal(terminal_case):
    case = terminal_case
    write_new_json(case.prior / "failure.json", {"error": "synthetic incompatible outcome"})
    with pytest.raises(ValueError, match="unresolved revision-budget result"):
        recover(case)


@pytest.mark.parametrize("filename", ["candidate.json", "candidate.v01.json", "model_review.json", "audit.v01.json",
                                      "validation.v01.json", "calls/call-03/answer.json"])
def test_terminal_resume_rejects_missing_proof(terminal_case, filename):
    (terminal_case.prior / filename).unlink()
    with pytest.raises(ValueError):
        recover(terminal_case)


@pytest.mark.parametrize("filename", ["candidate.json", "candidate.v01.json", "calls/call-02/record.json"])
def test_terminal_resume_requires_latest_candidate_everywhere(terminal_case, filename):
    case = terminal_case
    path = case.prior / filename
    record = read(path)
    record["observations"][0]["value"]["value"] = 99
    rewrite(path, record)
    with pytest.raises(ValueError):
        recover(case)


@pytest.mark.parametrize("change", ["history_hash", "history_size", "history_revision_flag", "audit_origin", "checks",
                                    "missing_dimensions", "different_answer", "summary_query", "empty_query", "failed_query"])
def test_terminal_resume_rejects_unverified_last_audit_or_history(terminal_case, change):
    case = terminal_case
    result = read(case.prior / "result.json")
    if change == "history_hash":
        result["history"][-1]["candidate_sha256"] = "0" * 64
    elif change == "history_size":
        result["history"].pop(0)
    elif change == "history_revision_flag":
        result["history"][-1]["needs_revision"] = False
    elif change == "audit_origin":
        result["history"][-1]["audit_origin"] = "reused_repair_guidance"
    elif change == "checks":
        result["reading_check_issues"] = ["not in retained history"]
    elif change in {"missing_dimensions", "different_answer"}:
        review = read(case.failed_audit_call / "answer.json")
        if change == "missing_dimensions":
            review["checked_dimensions"] = []
            for path in [case.prior / "model_review.json", case.prior / "audit.v01.json"]:
                rewrite(path, review)
        else:
            review["findings"] = []
        rewrite(case.failed_audit_call / "answer.json", review)
    else:
        query = read(case.query / "result.json")
        if change == "summary_query":
            query["data"]["mode"] = "summary"
        elif change == "empty_query":
            query["data"]["returned_count"] = 0
        else:
            query["success"] = False
        rewrite(case.query / "result.json", query)
    rewrite(case.prior / "result.json", result)
    with pytest.raises(ValueError):
        recover(case)


def test_terminal_resume_cannot_turn_an_actually_clear_audit_into_needs_revision(terminal_case):
    case = terminal_case
    review = audit().model_dump(mode="json")
    for path in [case.failed_audit_call / "answer.json", case.prior / "model_review.json", case.prior / "audit.v01.json"]:
        rewrite(path, review)
    with pytest.raises(ValueError, match="not supported"):
        recover(case)


@pytest.mark.parametrize("mutation", ["missing", "failed", "wrong_thread", "wrong_order"])
def test_terminal_resume_requires_completed_final_audit_protocol(terminal_case, mutation):
    case = terminal_case
    events = load_events(case)
    if mutation == "missing":
        events.pop()
    elif mutation == "failed":
        events[-1]["message"]["params"]["turn"]["status"] = "failed"
    elif mutation == "wrong_thread":
        events[-1]["message"]["params"]["threadId"] = "other-thread"
    else:
        events[-2], events[-1] = events[-1], events[-2]
    save_events(case, events)
    with pytest.raises(ValueError, match="completed protocol"):
        recover(case)


def test_terminal_resume_never_falls_back_from_a_newer_stage(terminal_case):
    case = terminal_case
    add_stage(case, 4, 2.7)
    with pytest.raises(ValueError, match="latest staged"):
        recover(case)


def test_terminal_resume_retains_current_parent_source_and_rule_guards(terminal_case):
    case = terminal_case
    different = copy.deepcopy(case.detail)
    different["summary"]["record_hash"] = "0" * 64
    with pytest.raises(ValueError, match="current record/source"):
        recover(case, different)
    case.f.settings.rules_version = "0.6" if case.f.settings.rules_version != "0.6" else "0.5"
    with pytest.raises(ValueError, match="target rule/schema"):
        recover(case)


def test_terminal_resume_always_starts_new_fresh_audit_before_acceptance(terminal_case):
    case = terminal_case
    replay = ReplayClient([audit()])
    result = reaudit(case.f.settings, case.f.records, case.f.run.name, "Fresh audit of an unresolved terminal candidate",
        resume_staged_run_id=case.prior.name, client_factory=replay.factory)
    assert replay.calls_started == 1 and replay.calls[0]["model"] is ReadingAudit
    assert replay.calls[0]["stage"] == "audit_web_0"
    assert result["revision"]["accepted"]
    assert result["result"]["resumed_staged_candidate"]["terminal_run_status"] == "needs_revision"
    assert read(case.prior / "result.json")["status"] == "needs_revision"
    assert not (case.prior / "failure.json").exists()
    assert hash_file(case.f.run / "candidate.json") == case.original_hash


def test_terminal_resume_failed_new_audit_leaves_candidate_unregistered(terminal_case, monkeypatch):
    case = terminal_case
    monkeypatch.setattr(case.f.records, "publish", lambda *a, **k: pytest.fail("no publication authorized"))
    replay = ReplayClient([timeout])
    with pytest.raises(TimeoutError):
        reaudit(case.f.settings, case.f.records, case.f.run.name, "Never replace the required new audit",
            resume_staged_run_id=case.prior.name, client_factory=replay.factory)
    assert replay.calls_started == 1 and len(case.f.records.list_records()) == case.original_records
    assert read(case.prior / "result.json")["status"] == "needs_revision"
    assert case.f.records.get(case.f.run.name)["summary"]["current"]


def terminal_route_run(service):
    run = saved_run(service, staged=True, failure=False)
    write_new_json(run / "result.json", {"status": "needs_revision", "stop_reason": "revision_budget_exhausted"})
    return run


def test_terminal_resume_web_routes_only_same_incomplete_task(service, routed_reaudit):
    run = terminal_route_run(service)
    job = retry_job(service, terminal_job(service, run.name, status="incomplete"))
    execute(service, job)
    assert routed_reaudit.call_args.kwargs["resume_staged_run_id"] == run.name


@pytest.mark.parametrize("status", ["failed", "interrupted", "cancelled"])
def test_terminal_resume_web_rejects_other_previous_job_statuses(service, routed_reaudit, status):
    run = terminal_route_run(service)
    job = retry_job(service, terminal_job(service, run.name, status=status))
    with pytest.raises(ValueError, match="同一未完成"):
        execute(service, job)
    routed_reaudit.assert_not_called()


@pytest.mark.parametrize("field,value", [("record_id", "other-record"), ("question", "other question"),
                                       ("observation_id", "other-observation"), ("previous_run_id", "other-run")])
def test_terminal_resume_web_rejects_other_task_scope(service, routed_reaudit, field, value):
    run = terminal_route_run(service)
    job = retry_job(service, terminal_job(service, run.name, status="incomplete"))
    job["payload"][field] = value
    with pytest.raises(ValueError):
        execute(service, job)
    routed_reaudit.assert_not_called()


def test_terminal_resume_web_new_request_does_not_pick_terminal_history(service, routed_reaudit):
    run = terminal_route_run(service)
    terminal_job(service, run.name, status="incomplete")
    job_id = service.jobs.enqueue("recheck", {"record_id": "record-a", "question": "new independent request"})
    execute(service, service.jobs.get(job_id))
    assert set(routed_reaudit.call_args.kwargs) == {"progress"}


def test_terminal_resume_web_validation_failure_never_falls_back(service, routed_reaudit):
    run = terminal_route_run(service)
    job = retry_job(service, terminal_job(service, run.name, status="incomplete"))
    routed_reaudit.side_effect = ValueError("terminal source verification failed")
    with pytest.raises(ValueError, match="terminal source"):
        execute(service, job)
    routed_reaudit.assert_called_once()


def test_terminal_resume_tests_import_the_isolated_draft_only():
    import watertreatment.web_resume as module
    assert Path(module.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])
