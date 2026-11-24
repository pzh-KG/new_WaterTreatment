"""Scope-bound recovery uses synthetic protocol artifacts, never a provider/Neo4j."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from test_repair_guidance import rewrite, timeout
from test_scope_workflow import register_new_scope_case
from test_staged_resume import add_stage, load_events, save_events, rewrite_staged_candidate_and_receipts
from test_web_model import ReplayClient, audit, model_fixture
from test_web_records import fixture, read
from watertreatment.storage import digest_json, hash_file, write_new_json
from watertreatment.web_model import reaudit
from watertreatment.web_resume import staged_revision


def make_case(data, version="0.7"):
    run, baseline = register_new_scope_case(data, version=version)
    data.run, data.record = run, baseline
    targets = {"digitization_targets": ["rate-course"]} if version == "0.7" else {}
    client = ReplayClient([timeout] if targets else [audit(revision=True), timeout])
    with pytest.raises(TimeoutError):
        reaudit(data.settings, data.records, run.name, "Synthetic bounded scope recovery",
                client_factory=client.factory, **targets)
    prior = Path(next(r["manifest_path"] for r in data.store.runs() if r["kind"] == "web_reaudit")).parent
    case = SimpleNamespace(f=data, prior=prior, detail=data.records.get(run.name),
                           events_path=prior / "app_server" / "events.jsonl",
                           revision_call=prior / "calls" / ("call-01" if targets else "call-02"))
    plan = read(prior / "plan.json")
    request = {"model": plan["model"], "reasoning_effort": plan["effort"], "stage": "revision_digitization_0" if targets else "revision_web_0",
               "sources": plan["input_paths"], "prompt": "Synthetic source-bound revision",
               "code_identity": plan["code_identity"]}
    write_new_json(case.revision_call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    write_new_json(case.revision_call / "result.json", {"status": "failed", "error": "Synthetic timeout",
        "server_selected_model": "gpt-6.2-sol", "server_selected_effort": "max",
        "thread_id": "synthetic-thread", "turn_id": "synthetic-failed-turn"})
    staged_base = baseline.model_copy(deep=True)
    if targets:
        staged_base.extraction_scope, staged_base.digitization_targets = "series_digitization", ["rate-course"]
    data.record = staged_base
    case.stage, case.candidate = add_stage(case, 1, 2.5)
    data.record = baseline
    return case


@pytest.fixture
def scope_case(model_fixture):
    return make_case(model_fixture)


def recover(case):
    version = read(case.prior / "plan.json")["scientific_plan"]["rule_version"]
    settings = case.f.settings.model_copy(update={"rules_version": version})
    return staged_revision(settings, case.f.records, case.detail, case.prior.name)


def deliver_and_fail_audit(case, *, audit_stage="audit_web_0"):
    data = read(case.stage / "result.json")["data"]
    result = read(case.revision_call / "result.json")
    result.update(status="schema_valid_candidate", submission_id=data["submission_id"])
    result.pop("error")
    rewrite(case.revision_call / "result.json", result)
    write_new_json(case.revision_call / "answer.json", {"submission_id": data["submission_id"]})
    write_new_json(case.revision_call / "record.json", case.candidate.model_dump(mode="json"))
    case.snapshot = case.prior / "candidate_snapshots" / "digitization-review.json"
    write_new_json(case.snapshot, case.candidate.model_dump(mode="json"))
    binding = {"path": str(case.snapshot), "sha256": hash_file(case.snapshot), "scope": "digitization-review",
               "content_hash": digest_json(case.candidate.model_dump(mode="json"))}
    plan = read(case.prior / "plan.json")
    request = {"model": plan["model"], "reasoning_effort": plan["effort"], "stage": audit_stage, "sources": [*plan["input_paths"],
               {"path": str(case.snapshot), "sha256": binding["sha256"]}], "candidate_binding": binding,
               "prompt": "Read the exact snapshot. CANDIDATE_SNAPSHOT " + binding["content_hash"],
               "code_identity": plan["code_identity"]}
    case.audit_call = case.prior / "calls" / "call-03"
    write_new_json(case.audit_call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    write_new_json(case.audit_call / "result.json", {"status": "failed", "error": "Synthetic audit timeout",
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


def test_scope_resume_new_default_can_register_legacy_06_checkpoint_after_fresh_audit(model_fixture):
    case = make_case(model_fixture, "0.6")
    assert case.f.settings.rules_version == "0.7"
    before = {p: hash_file(p) for p in case.prior.rglob("*") if p.is_file()}
    client = ReplayClient([audit()])
    result = reaudit(case.f.settings, case.f.records, case.f.run.name, "Resume original rule 0.6",
                    resume_staged_run_id=case.prior.name, client_factory=client.factory)
    assert result["revision"]["accepted"]
    assert [call["stage"] for call in client.calls] == ["audit_web_0"]
    assert case.f.records.get(result["revision"]["record_id"])["record"]["rule_version"] == "0.6"
    assert {p: hash_file(p) for p in before} == before


def test_scope_resume_initial_digitization_keeps_frozen_policy_and_needs_fresh_audit(scope_case):
    case = scope_case
    before = {p: hash_file(p) for p in case.prior.rglob("*") if p.is_file()}
    candidate, paths, receipt = recover(case)
    assert candidate == case.candidate
    assert receipt["extraction_policy"] == read(case.prior / "plan.json")["extraction_policy"]
    assert receipt["requires_fresh_audit"] and receipt["scientific_validation"] == "not_established"
    assert case.events_path not in paths
    assert {p: hash_file(p) for p in before} == before


def test_scope_resume_completed_initial_digitization_maps_to_audit_zero(scope_case):
    deliver_and_fail_audit(scope_case)
    candidate, _, receipt = recover(scope_case)
    assert candidate == scope_case.candidate
    assert receipt["following_audit_stage"] == "audit_web_0"
    assert receipt["turn_status"] == "completed_revision_awaiting_fresh_audit"
    assert receipt["extraction_policy"]["digitization_targets"] == ["rate-course"]


def test_scope_resume_initial_digitization_unresolved_terminal_audit_zero_is_not_approval(scope_case):
    case = scope_case
    deliver_and_fail_audit(case)
    (case.prior / "failure.json").unlink()  # Temporary synthetic fixture only.
    completed = read(case.audit_call / "result.json")
    completed.update(status="schema_valid_candidate")
    completed.pop("error")
    rewrite(case.audit_call / "result.json", completed)
    review = audit(revision=True).model_dump(mode="json")
    for path in (case.audit_call / "answer.json", case.prior / "audit.v00.json", case.prior / "model_review.json"):
        write_new_json(path, review)
    for name in ("candidate.v00.json", "candidate.json"):
        write_new_json(case.prior / name, case.candidate.model_dump(mode="json"))
    write_new_json(case.prior / "validation.v00.json", {"issues": [], "semantic_accuracy": "not_established"})
    history = [{"candidate_sha256": hash_file(case.prior / "candidate.json"), "needs_revision": True,
                "audit_origin": "fresh_model_audit", "validation_error_count": 0, "reading_check_issues": []}]
    write_new_json(case.prior / "result.json", {"status": "needs_revision", "stop_reason": "revision_budget_exhausted",
        "history": history, "validation_error_count": 0, "reading_check_issues": [], "revisions": 0,
        "human_validated": False, "graph_published": False, "scientific_accuracy": "not_established",
        "extraction_policy": read(case.prior / "plan.json")["extraction_policy"]})
    query = case.prior / "reading_tools" / "tool-0003"
    binding = read(case.audit_call / "request.json")["request"]["candidate_binding"]
    write_new_json(query / "request.json", {"tool": "wt_query_candidate", "arguments": {"path": ["observations", 0]}})
    write_new_json(query / "result.json", {"success": True, "data": {
        "candidate_binding": binding, "mode": "path", "returned_count": 1}})
    events = load_events(case)
    events.append({"direction": "receive", "message": {"method": "turn/completed", "params": {
        "threadId": "synthetic-audit-thread", "turn": {"id": "synthetic-audit-turn", "status": "completed"}}}})
    save_events(case, events)
    before = {p: hash_file(p) for p in case.prior.rglob("*") if p.is_file()}
    candidate, _, receipt = recover(case)
    assert candidate == case.candidate
    assert receipt["following_audit_stage"] == "audit_web_0"
    assert receipt["turn_status"] == "completed_revision_with_unresolved_final_audit"
    assert receipt["requires_fresh_audit"] and receipt["scientific_validation"] == "not_established"
    assert {p: hash_file(p) for p in before} == before


def test_scope_resume_initial_digitization_cannot_claim_audit_one(scope_case):
    deliver_and_fail_audit(scope_case, audit_stage="audit_web_1")
    with pytest.raises(ValueError, match="following failed audit"):
        recover(scope_case)


@pytest.mark.parametrize("stage", ["revision_digitization_1", "revision_unknown_0", "revision_web_bad"])
def test_scope_resume_does_not_widen_accepted_revision_stages(scope_case, stage):
    path = scope_case.revision_call / "request.json"
    envelope = read(path)
    envelope["request"]["stage"] = stage
    envelope["fingerprint"] = digest_json(envelope["request"])
    rewrite(path, envelope)
    with pytest.raises(ValueError, match="source-bound Sol MAX revision"):
        recover(scope_case)


@pytest.mark.parametrize("mode,targets", [("research_core", []), ("series_digitization", ["other-curve"])])
def test_scope_resume_staged_output_cannot_drop_or_change_authorized_targets(scope_case, mode, targets):
    candidate = scope_case.candidate.model_copy(update={"extraction_scope": mode, "digitization_targets": targets})
    rewrite_staged_candidate_and_receipts(scope_case, candidate)
    with pytest.raises(ValueError, match="frozen extraction scope"):
        recover(scope_case)


def test_scope_resume_only_fresh_audit_registers_new_scope_without_redigitization(scope_case):
    case = scope_case
    deliver_and_fail_audit(case)
    client = ReplayClient([audit()])
    result = reaudit(case.f.settings, case.f.records, case.f.run.name, "Audit the selected recovered curve",
                    digitization_targets=["rate-course"], resume_staged_run_id=case.prior.name,
                    client_factory=client.factory)
    assert result["revision"]["accepted"]
    assert [call["stage"] for call in client.calls] == ["audit_web_0"]
    assert result["result"]["graph_published"] is False
    resumed = case.f.settings.data_root / "runs" / result["run_id"]
    plan = read(resumed / "plan.json")
    assert plan["extraction_policy"] == read(case.prior / "plan.json")["extraction_policy"]
    assert plan["resumed_staged_candidate"]["extraction_policy"] == plan["extraction_policy"]


def test_scope_resume_request_cannot_silently_drop_digitization_policy(scope_case):
    case = scope_case
    with pytest.raises(ValueError, match="scope|范围|policy"):
        reaudit(case.f.settings, case.f.records, case.f.run.name, "Attempt recovery without the authorized scope",
                resume_staged_run_id=case.prior.name,
                client_factory=lambda *a, **k: pytest.fail("scope mismatch must stop before a provider"))


@pytest.mark.parametrize("fault", [None, "new_turn", "wrong_scope", "different_obligation"])
def test_delivered_revision_stopped_before_source_review_requires_fresh_audit(scope_case, monkeypatch, fault):
    case = scope_case
    request_path = case.revision_call / "request.json"
    envelope = read(request_path)
    envelope["request"]["stage"] = "revision_web_0"
    envelope["fingerprint"] = digest_json(envelope["request"])
    rewrite(request_path, envelope)
    deliver_and_fail_audit(case, audit_stage="audit_web_1")
    # Replace the synthetic failed provider call with the actual prestart shape.
    (case.audit_call / "request.json").unlink()
    (case.audit_call / "result.json").unlink()
    events = load_events(case)
    if fault != "new_turn":
        events.pop()
    save_events(case, events)
    value = case.candidate.model_dump(mode="json")
    write_new_json(case.prior / "candidate.v01.json", value)
    scope = {"policy": "bounded-scientific-revision-v1", "new_record_hash": digest_json(value)}
    scope["scope_sha256"] = digest_json(scope)
    write_new_json(case.prior / "citation_scope.v01.json", scope)
    expected = [{"source_id": "registered-source", "block_id": "caption", "unresolved_word_visual": "No registered occurrence"}]
    marker = {"new_turn_started": False, "stage": "audit_web_1", "scope_sha256": scope["scope_sha256"],
              "unresolved": expected}
    if fault == "wrong_scope":
        marker["scope_sha256"] = "changed"
    if fault == "different_obligation":
        marker["unresolved"] = []
    write_new_json(case.prior / "source_review_preflight-test.json", marker)
    monkeypatch.setattr("watertreatment.source_review.review_requirements", lambda *args: expected)
    if fault:
        with pytest.raises(ValueError, match="source preflight"):
            recover(case)
    else:
        candidate, paths, receipt = recover(case)
        assert candidate == case.candidate
        assert receipt["following_audit_stage"] == "audit_web_1"
        assert receipt["following_audit_status"] == "source_preflight_no_model_turn"
        assert receipt["requires_fresh_audit"] and receipt["scientific_validation"] == "not_established"
        assert case.prior / "source_review_preflight-test.json" in paths
