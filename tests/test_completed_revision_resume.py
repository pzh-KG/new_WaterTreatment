"""Completed delivery plus failed follow-up audit remains an unaudited candidate."""
from __future__ import annotations

import copy

import pytest

from test_repair_guidance import failed_repair, rewrite, timeout
from test_staged_resume import staged_case, add_stage, load_events, save_events, recover
from test_web_model import ReplayClient, audit, model_fixture
from test_web_records import fixture, read
from watertreatment.reading import ReadingAudit
from watertreatment.storage import digest_json, hash_file, write_new_json
from watertreatment.web_model import reaudit


@pytest.fixture
def delivered_case(staged_case):
    case = staged_case
    data = read(case.stage / "result.json")["data"]
    result = read(case.revision_call / "result.json")
    result.update(status="schema_valid_candidate", submission_id=data["submission_id"])
    result.pop("error")
    rewrite(case.revision_call / "result.json", result)
    write_new_json(case.revision_call / "answer.json", {"submission_id": data["submission_id"]})
    write_new_json(case.revision_call / "record.json", case.candidate.model_dump(mode="json"))
    case.snapshot = case.prior / "candidate_snapshots" / "fresh-audit-scope.json"
    write_new_json(case.snapshot, case.candidate.model_dump(mode="json"))
    binding = {"path": str(case.snapshot), "sha256": hash_file(case.snapshot), "scope": "fresh-audit-scope",
               "content_hash": digest_json(case.candidate.model_dump(mode="json"))}
    plan = read(case.prior / "plan.json")
    request = {"model": plan["model"], "reasoning_effort": plan["effort"], "stage": "audit_web_1", "sources": [*plan["input_paths"],
               {"path": str(case.snapshot), "sha256": binding["sha256"]}], "candidate_binding": binding,
               "prompt": "Read the exact snapshot. CANDIDATE_SNAPSHOT " + binding["content_hash"],
               "code_identity": plan["code_identity"]}
    case.failed_audit_call = case.prior / "calls" / "call-03"
    write_new_json(case.failed_audit_call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    write_new_json(case.failed_audit_call / "result.json", {"status": "failed", "error": "Synthetic tool budget exhausted",
        "server_selected_model": "gpt-6.2-sol", "server_selected_effort": "max",
        "thread_id": "synthetic-audit-thread", "turn_id": "synthetic-audit-turn"})
    events = load_events(case)
    events.extend([
        {"direction": "receive", "message": {"method": "turn/completed", "params": {
            "threadId": "synthetic-thread", "turn": {"id": "synthetic-failed-turn", "status": "completed"}}}},
        {"direction": "receive", "message": {"method": "turn/started", "params": {
            "threadId": "synthetic-audit-thread", "turn": {"id": "synthetic-audit-turn", "status": "inProgress"}}}},
    ])
    save_events(case, events)
    return case


def test_completed_revision_resume_preserves_failure_and_requires_fresh_audit(delivered_case):
    case = delivered_case
    before = {p: hash_file(p) for p in case.prior.rglob("*") if p.is_file()}
    candidate, paths, receipt = recover(case)
    assert candidate == case.candidate
    assert receipt["turn_status"] == "completed_revision_awaiting_fresh_audit"
    assert receipt["revision_result_status"] == "schema_valid_candidate"
    assert receipt["following_audit_stage"] == "audit_web_1"
    assert receipt["following_audit_status"] == "failed"
    assert receipt["requires_fresh_audit"] and receipt["scientific_validation"] == "not_established"
    assert receipt["submission_id"] == read(case.revision_call / "answer.json")["submission_id"]
    assert case.revision_call / "record.json" in paths and case.failed_audit_call / "result.json" in paths
    assert case.snapshot in paths and case.events_path not in paths
    assert all(path.is_file() for path in paths)
    assert {p: hash_file(p) for p in before} == before
    assert not (case.prior / "result.json").exists()
    assert len(case.f.records.list_records()) == case.original_records


@pytest.mark.parametrize("mutation", ["answer_id", "result_id", "record", "missing_answer", "missing_record"])
def test_completed_revision_resume_requires_exact_final_delivery(delivered_case, mutation):
    case = delivered_case
    if mutation.startswith("missing_"):
        (case.revision_call / (mutation.removeprefix("missing_") + ".json")).unlink()
    elif mutation in {"answer_id", "result_id"}:
        path = case.revision_call / ("answer.json" if mutation == "answer_id" else "result.json")
        changed = read(path)
        changed["submission_id"] = "an-older-or-unrelated-submission"
        rewrite(path, changed)
    else:
        changed = read(case.revision_call / "record.json")
        changed["observations"][0]["value"]["value"] = 99
        rewrite(case.revision_call / "record.json", changed)
    with pytest.raises(ValueError):
        recover(case)


@pytest.mark.parametrize("mutation", ["missing_completion", "failed_completion", "wrong_completed_thread",
    "wrong_completed_turn", "missing_audit_start", "wrong_audit_thread", "wrong_audit_turn", "wrong_order"])
def test_completed_revision_resume_requires_ordered_real_protocol_events(delivered_case, mutation):
    case = delivered_case
    events = load_events(case)
    completion, audit_start = events[-2]["message"]["params"], events[-1]["message"]["params"]
    if mutation == "missing_completion":
        events.pop(-2)
    elif mutation == "missing_audit_start":
        events.pop()
    elif mutation == "failed_completion":
        completion["turn"]["status"] = "failed"
    elif mutation == "wrong_order":
        events[-2], events[-1] = events[-1], events[-2]
    elif mutation == "wrong_completed_thread":
        completion["threadId"] = "wrong-thread"
    elif mutation == "wrong_completed_turn":
        completion["turn"]["id"] = "wrong-turn"
    elif mutation == "wrong_audit_thread":
        audit_start["threadId"] = "wrong-thread"
    else:
        audit_start["turn"]["id"] = "wrong-turn"
    save_events(case, events)
    with pytest.raises(ValueError, match="protocol events"):
        recover(case)


@pytest.mark.parametrize("mutation", ["round", "fingerprint", "source_binding", "scope", "file_hash", "content_hash",
    "snapshot_source", "marker", "missing_result", "completed_status", "model", "effort", "missing_binding"])
def test_completed_revision_resume_requires_bound_failed_followup_audit(delivered_case, mutation):
    case = delivered_case
    request_path, result_path = case.failed_audit_call / "request.json", case.failed_audit_call / "result.json"
    if mutation == "missing_result":
        result_path.unlink()
    elif mutation in {"completed_status", "model", "effort"}:
        result = read(result_path)
        key, value = {"completed_status": ("status", "schema_valid_candidate"),
                      "model": ("server_selected_model", "other"), "effort": ("server_selected_effort", "low")}[mutation]
        result[key] = value
        rewrite(result_path, result)
    else:
        envelope = read(request_path)
        request = envelope["request"]
        binding = request["candidate_binding"]
        if mutation == "round":
            request["stage"] = "audit_web_0"
        elif mutation == "source_binding":
            request["sources"][0]["sha256"] = "0" * 64
        elif mutation == "scope":
            binding["scope"] = "unrelated-scope"
        elif mutation == "file_hash":
            binding["sha256"] = "0" * 64
            request["sources"][-1]["sha256"] = binding["sha256"]
        elif mutation == "content_hash":
            binding["content_hash"] = "0" * 64
            request["prompt"] = "CANDIDATE_SNAPSHOT " + binding["content_hash"]
        elif mutation == "snapshot_source":
            request["sources"].pop()
        elif mutation == "marker":
            request["prompt"] = "Another candidate"
        elif mutation == "missing_binding":
            request.pop("candidate_binding")
        envelope["fingerprint"] = "0" * 64 if mutation == "fingerprint" else digest_json(request)
        rewrite(request_path, envelope)
    with pytest.raises(ValueError):
        recover(case)


@pytest.mark.parametrize("outside_store", [False, True])
def test_completed_revision_resume_rejects_snapshot_path_outside_run(delivered_case, outside_store):
    case = delivered_case
    other = ((case.f.settings.data_root.parent / "outside.json") if outside_store
             else (case.f.settings.data_root / "runs" / "another-run" / case.snapshot.name))
    write_new_json(other, case.candidate.model_dump(mode="json"))
    envelope = read(case.failed_audit_call / "request.json")
    request = envelope["request"]
    request["candidate_binding"].update(path=str(other), sha256=hash_file(other))
    request["sources"][-1] = {"path": str(other), "sha256": hash_file(other)}
    envelope["fingerprint"] = digest_json(request)
    rewrite(case.failed_audit_call / "request.json", envelope)
    with pytest.raises(ValueError):
        recover(case)


def test_completed_revision_resume_rejects_followup_bound_to_older_candidate(delivered_case):
    case = delivered_case
    older = case.f.record.model_dump(mode="json")
    rewrite(case.snapshot, older)
    envelope = read(case.failed_audit_call / "request.json")
    request = envelope["request"]
    request["candidate_binding"].update(sha256=hash_file(case.snapshot), content_hash=digest_json(older))
    request["sources"][-1]["sha256"] = hash_file(case.snapshot)
    request["prompt"] = "CANDIDATE_SNAPSHOT " + digest_json(older)
    envelope["fingerprint"] = digest_json(request)
    rewrite(case.failed_audit_call / "request.json", envelope)
    with pytest.raises(ValueError, match="differs from the delivered"):
        recover(case)


def test_completed_revision_resume_never_falls_back_past_a_later_stage(delivered_case):
    case = delivered_case
    add_stage(case, 3, 2.6)
    with pytest.raises(ValueError, match="latest staged"):
        recover(case)


def test_completed_revision_does_not_hide_later_failed_revision_checkpoint(delivered_case):
    case = delivered_case
    folder, candidate = add_stage(case, 3, 2.6)
    events = load_events(case)
    events[-2]["message"]["params"]["turnId"] = "later-failed-revision"
    save_events(case, events)
    call = case.prior / "calls" / "call-04"
    request = copy.deepcopy(read(case.revision_call / "request.json")["request"])
    request["stage"] = "revision_web_1"
    write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    write_new_json(call / "result.json", {"status": "failed", "server_selected_model": "gpt-6.2-sol",
        "server_selected_effort": "max", "thread_id": "synthetic-thread", "turn_id": "later-failed-revision"})
    recovered, _, receipt = recover(case)
    assert recovered == candidate and receipt["stage"] == folder.name
    assert receipt["turn_status"] == "failed_with_verified_staged_candidate"


def test_completed_revision_resume_calls_fresh_audit_before_registration(delivered_case):
    case = delivered_case
    replay = ReplayClient([audit()])
    outcome = reaudit(case.f.settings, case.f.records, case.f.run.name, "Audit the delivered but unaudited candidate",
        resume_staged_run_id=case.prior.name, client_factory=replay.factory)
    assert replay.calls_started == 1 and replay.calls[0]["model"] is ReadingAudit
    assert replay.calls[0]["stage"] == "audit_web_0"
    assert case.candidate.model_dump_json(exclude_defaults=True) in replay.calls[0]["prompt"]
    assert outcome["revision"]["accepted"]
    assert read(case.prior / "failure.json") and not (case.prior / "result.json").exists()
    assert outcome["result"]["resumed_staged_candidate"]["turn_status"] == "completed_revision_awaiting_fresh_audit"
    assert hash_file(case.f.run / "candidate.json") == case.original_hash


def test_completed_revision_resume_new_audit_failure_never_registers_or_publishes(delivered_case, monkeypatch):
    case = delivered_case
    monkeypatch.setattr(case.f.records, "publish", lambda *a, **k: pytest.fail("recovery cannot publish"))
    replay = ReplayClient([timeout])
    with pytest.raises(TimeoutError):
        reaudit(case.f.settings, case.f.records, case.f.run.name, "Fresh audit must not be skipped",
            resume_staged_run_id=case.prior.name, client_factory=replay.factory)
    assert replay.calls_started == 1
    assert len(case.f.records.list_records()) == case.original_records
    assert case.f.records.get(case.f.run.name)["summary"]["current"]
    newest = next(r for r in case.f.store.runs() if r["kind"] == "web_reaudit" and r["run_id"] != case.prior.name)
    new_run = case.f.settings.data_root / "runs" / newest["run_id"]
    assert (new_run / "failure.json").exists() and not (new_run / "result.json").exists()
    assert read(new_run / "candidate.v00.json") == case.candidate.model_dump(mode="json")
    assert hash_file(case.f.run / "candidate.json") == case.original_hash
