"""Synthetic failed CLI checkpoint integrity, not scientific validation."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_web_records import fixture, read
from watertreatment.codex import source_snapshot
from watertreatment.domain import DatasetRole, ExtractionRecord, ObservationEvidenceLink, RoleHistory
from watertreatment.reading import AUDIT_DIMENSIONS, AuditFinding, ReadingAudit, SourceAnchor
from watertreatment.reading_tools import ReadingTools
from watertreatment.scope_policy import frozen_scope
from watertreatment.staged_source_resume import authenticate_staged_candidate
from watertreatment.storage import digest_json, hash_file, write_new_json


def rewrite(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def save_events(case, events):
    case.events_path.parent.mkdir(parents=True, exist_ok=True)
    case.events_path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def load_events(case):
    return [json.loads(line) for line in case.events_path.read_text(encoding="utf-8").splitlines()]


def turn(method, thread, identifier, status="inProgress"):
    return {"direction": "receive", "message": {"method": method, "params": {
        "threadId": thread, "turn": {"id": identifier, "status": status}}}}


def add_patch(case, edits):
    arguments = {"base_hash": case.tools.candidate_binding["content_hash"], "edits": edits}
    response = case.tools.handle("wt_patch_record", arguments)
    assert response["success"], response
    number = case.tools.calls_started
    path = case.run / "reading_tools" / f"tool-{number:04d}" / "submitted_record.json"
    data = read(path.parent / "result.json")["data"]
    events = load_events(case)
    events.extend([
        {"direction": "receive", "message": {"id": number, "method": "item/tool/call", "params": {
            "threadId": "failed-thread", "turnId": "failed-turn", "namespace": None,
            "tool": "wt_patch_record", "arguments": arguments}}},
        {"direction": "send", "message": {"id": number, "result": {"success": True,
            "contentItems": [{"type": "inputText", "text": json.dumps(data)}]}}},
    ])
    save_events(case, events)
    return path


@pytest.fixture
def staged_cli(fixture, request):
    f = fixture
    cohort = getattr(request, "param", "development")
    f.settings.rules_version = "0.7"
    run = f.store.new_run("extraction", {"paper_id": "synthetic-paper"})
    shutil.copyfile(f.run / "sources.json", run / "sources.json")
    sources = [run / "sources.json", Path(read(run / "sources.json")["documents"][0]["source"]["path"])]
    plan = {"run_id": run.name, "paper_id": "synthetic-paper", "cohort_role": cohort,
        "model_settings": {"model": "gpt-6.1-sol", "reasoning_effort": "max"},
        "rule_version": "0.7", "schema_version": "0.3.0", "code_identity": "f" * 64,
        "extraction_policy": frozen_scope(), "input_paths": source_snapshot(sources)}
    write_new_json(run / "plan.json", plan)
    f.store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    baseline = f.record.model_copy(deep=True)
    baseline.rule_version, baseline.schema_version = "0.7", "0.3.0"
    baseline.extraction_scope = "research_core"
    baseline.paper.dataset_role = {"development": DatasetRole.DEVELOPMENT,
        "unassigned": DatasetRole.UNSPECIFIED, "heldout": DatasetRole.VALIDATION_RESERVED}[cohort]
    baseline.paper.role_history = [RoleHistory(role=baseline.paper.dataset_role,
        date="2000-01-01", reason="Synthetic original cohort assignment")]
    for item in baseline.observations:
        item.analysis_role = "key_result"
    baseline.observations[0].evidence_links = [ObservationEvidenceLink(
        evidence_id="source-value", support_type="context", supported_fields=["unit"],
        explanation="Synthetic original citation description")]
    for name in ("candidate.v01.json", "candidate.json"):
        write_new_json(run / name, baseline.model_dump(mode="json"))
    audit = ReadingAudit(checked_dimensions=list(AUDIT_DIMENSIONS), potential_omissions=[], findings=[AuditFinding(
        record_path="observations[0].evidence_links[0].explanation", severity="minor",
        verdict="unsupported", needs_revision=True, explanation="Synthetic citation repair required",
        evidence=[SourceAnchor(source_id="main", block_id="paragraph")])],
        coverage_limitations=["Synthetic source evidence; accuracy not established"])
    write_new_json(run / "audit.v01.json", audit.model_dump(mode="json"))
    tools = ReadingTools(read(run / "sources.json"), run, max_calls=20, expected_inputs=plan["input_paths"])
    tools.bind_candidate(baseline, run / "candidate.v01.json")
    assert tools.handle("wt_query_candidate", {"path": ["observations", 0]})["success"]
    audit_binding = tools.candidate_binding
    audit_call, failed_call = run / "calls" / "audit", run / "calls" / "revision"

    def call(path, stage, binding, status, thread, identifier):
        request = {"stage": stage, "code_identity": plan["code_identity"], "rule_version": "0.7",
            "model": "gpt-6.1-sol", "reasoning_effort": "max", "candidate_binding": binding,
            "prompt": "CANDIDATE_SNAPSHOT " + binding["content_hash"],
            "sources": source_snapshot([*sources, Path(binding["path"])])}
        write_new_json(path / "request.json", {"request": request, "fingerprint": digest_json(request)})
        write_new_json(path / "result.json", {"status": status, "server_selected_model": "gpt-6.1-sol",
            "server_selected_effort": "max", "thread_id": thread, "turn_id": identifier})

    call(audit_call, "audit_1", audit_binding, "schema_valid_candidate", "audit-thread", "audit-turn")
    write_new_json(audit_call / "answer.json", audit.model_dump(mode="json"))
    tools.prepare_revision(baseline)
    tools.begin_submission(True)
    call(failed_call, "revision_2", tools.candidate_binding, "failed", "failed-thread", "failed-turn")
    write_new_json(run / "result.json", {"status": "candidate_incomplete_failure", "human_validated": False,
        "graph_published": False, "dataset_role": cohort, "last_candidate_version": "candidate.v01.json"})
    write_new_json(run / "failure.json", {"error": "Synthetic interrupted turn"})
    case = SimpleNamespace(f=f, run=run, tools=tools, baseline=baseline, audit=audit,
        audit_call=audit_call, failed_call=failed_call, events_path=run / "app_server/events.jsonl")
    save_events(case, [turn("turn/started", "audit-thread", "audit-turn"),
        turn("turn/completed", "audit-thread", "audit-turn", "completed"),
        turn("turn/started", "failed-thread", "failed-turn")])
    case.first = add_patch(case, [{"operation": "set", "path": ["observations", 0, "evidence_links", 0, "explanation"],
        "value": "Synthetic corrected citation description"}])
    case.stage = add_patch(case, [{"operation": "append", "path": ["observations", 0, "limitations"],
        "value": "Synthetic unverified source limitation retained"}])
    return case


def recover(case):
    return authenticate_staged_candidate(case.f.settings, case.stage)


@pytest.mark.parametrize("staged_cli", ["development", "unassigned"], indirect=True)
def test_staged_cli_authenticates_literal_chain_without_promoting_or_publishing(staged_cli):
    case = staged_cli
    before = {p: hash_file(p) for p in case.run.rglob("*") if p.is_file()}
    result = recover(case)
    assert result.record == ExtractionRecord.model_validate(read(case.stage))
    assert result.baseline_record == case.baseline and result.baseline_audit == case.audit
    assert result.receipt["requires_new_source_audit"] and result.receipt["scientific_validation"] == "not_established"
    assert result.receipt["events_sha256"] == hash_file(case.events_path)
    assert len(result.receipt["staged_chain"]) == 2 and case.first in result.input_paths
    assert case.events_path not in result.input_paths
    assert before == {p: hash_file(p) for p in case.run.rglob("*") if p.is_file()}
    assert not (case.run / "candidate.v02.json").exists()


@pytest.mark.parametrize("staged_cli", ["unassigned"], indirect=True)
@pytest.mark.parametrize("mutation", ["terminal_cohort", "dataset_role", "role_history"])
def test_staged_cli_production_recovery_cannot_change_research_identity(staged_cli, mutation):
    case = staged_cli
    if mutation == "terminal_cohort":
        terminal = read(case.run / "result.json")
        terminal["dataset_role"] = "development"
        rewrite(case.run / "result.json", terminal)
    else:
        value = "development" if mutation == "dataset_role" else []
        response = case.tools.handle("wt_patch_record", {
            "base_hash": case.tools.candidate_binding["content_hash"],
            "edits": [{"operation": "set", "path": ["paper", mutation], "value": value}]})
        assert response["success"] is False
        assert "cannot change paper/cohort/source identity" in str(response)
        # An out-of-band edit must not get farther than the model's rejected
        # patch. The original public submission remains the only authority.
        saved = read(case.stage)
        saved["paper"][mutation] = value
        rewrite(case.stage, saved)
    with pytest.raises(ValueError):
        recover(case)


@pytest.mark.parametrize("staged_cli", ["heldout"], indirect=True)
def test_staged_cli_keeps_heldout_recovery_separate_from_production(staged_cli):
    with pytest.raises(ValueError, match="non-heldout"):
        recover(staged_cli)


@pytest.mark.parametrize("mutation", ["bytes", "receipt", "candidate_hash", "tool_args", "protocol_args",
    "protocol_turn", "protocol_namespace", "missing_response", "base_hash", "replayed_value", "prior_stage"])
def test_staged_cli_rejects_mutated_stage_chain(staged_cli, mutation):
    case = staged_cli
    if mutation in {"bytes", "prior_stage"}:
        path = case.first if mutation == "prior_stage" else case.stage
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation in {"receipt", "candidate_hash"}:
        path = case.stage.parent / "result.json"
        data = read(path)
        data["data"]["published" if mutation == "receipt" else "base_hash"] = True if mutation == "receipt" else "0" * 64
        rewrite(path, data)
    else:
        events = load_events(case)
        if mutation == "missing_response":
            events.pop()
        elif mutation in {"protocol_turn", "protocol_namespace"}:
            events[-2]["message"]["params"]["turnId" if mutation == "protocol_turn" else "namespace"] = "wrong"
        else:
            path = case.stage.parent / "request.json"
            request = read(path)
            if mutation == "base_hash":
                request["arguments"]["base_hash"] = "0" * 64
            else:
                request["arguments"]["edits"][0]["value"] = "A different, unretained literal"
            if mutation != "protocol_args":
                rewrite(path, request)
            if mutation != "tool_args":
                events[-2]["message"]["params"]["arguments"] = request["arguments"]
        save_events(case, events)
    with pytest.raises(ValueError):
        recover(case)


@pytest.mark.parametrize("mutation", ["model", "effort", "status", "thread", "stage", "fingerprint",
    "sources", "code", "rule", "binding", "completed_turn", "audit_after_revision", "duplicate_start"])
def test_staged_cli_requires_exact_completed_audit_then_failed_sol_max_revision(staged_cli, mutation):
    case = staged_cli
    if mutation in {"completed_turn", "audit_after_revision", "duplicate_start"}:
        events = load_events(case)
        if mutation == "completed_turn":
            events.append(turn("turn/completed", "failed-thread", "failed-turn", "completed"))
        elif mutation == "audit_after_revision":
            events[1], events[2] = events[2], events[1]
        else:
            events.append(events[2])
        save_events(case, events)
    elif mutation in {"model", "effort", "status", "thread"}:
        path = case.failed_call / "result.json"
        data = read(path)
        key = {"model": "server_selected_model", "effort": "server_selected_effort",
               "status": "status", "thread": "thread_id"}[mutation]
        data[key] = "wrong"
        rewrite(path, data)
    else:
        path = case.failed_call / "request.json"
        envelope = read(path)
        request = envelope["request"]
        if mutation == "sources":
            request["sources"] = []
        elif mutation == "binding":
            request["candidate_binding"]["content_hash"] = "0" * 64
        elif mutation != "fingerprint":
            request[{"stage": "stage", "code": "code_identity", "rule": "rule_version"}[mutation]] = "wrong"
        envelope["fingerprint"] = "0" * 64 if mutation == "fingerprint" else digest_json(request)
        rewrite(path, envelope)
    with pytest.raises(ValueError):
        recover(case)


@pytest.mark.parametrize("mutation", ["source", "plan", "formal", "audit_answer", "newer_candidate", "newer_audit", "newer_stage", "older_selected"])
def test_staged_cli_cannot_bypass_changed_or_newer_artifacts(staged_cli, mutation):
    case = staged_cli
    if mutation == "older_selected":
        case.stage = case.first
    elif mutation.startswith("newer_"):
        path = {"newer_candidate": case.run / "candidate.v02.json", "newer_audit": case.run / "audit.v02.json",
                "newer_stage": case.run / "reading_tools/tool-0999/submitted_record.json"}[mutation]
        write_new_json(path, {})
    else:
        path = {"source": case.run / "sources.json", "plan": case.run / "plan.json",
                "formal": case.run / "candidate.json", "audit_answer": case.audit_call / "answer.json"}[mutation]
        if mutation == "audit_answer":
            data = read(path)
            data["coverage_limitations"] = []
            rewrite(path, data)
        else:
            path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        recover(case)


def test_staged_cli_later_failed_tool_does_not_discard_a_valid_checkpoint(staged_cli):
    case = staged_cli
    write_new_json(case.run / "reading_tools/tool-0999/request.json", {"tool": "wt_patch_record", "arguments": {}})
    write_new_json(case.run / "reading_tools/tool-0999/result.json", {"success": False})
    assert recover(case).receipt["stage_path"] == str(case.stage)


@pytest.mark.parametrize("mutation", ["response_after_failure", "duplicate_request", "malformed_interior", "later_call"])
def test_staged_cli_rejects_ambiguous_protocol_or_later_work(staged_cli, mutation):
    case = staged_cli
    events = load_events(case)
    if mutation == "response_after_failure":
        events.insert(-1, turn("turn/completed", "failed-thread", "failed-turn", "failed"))
    elif mutation == "duplicate_request":
        events[-2]["message"]["id"] = events[-4]["message"]["id"]
        events[-1]["message"]["id"] = events[-4]["message"]["id"]
    elif mutation == "later_call":
        write_new_json(case.run / "calls/later/request.json", {"request": {"stage": "audit_2"}})
    save_events(case, events)
    if mutation == "malformed_interior":
        case.events_path.write_text("not-json\n" + case.events_path.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError):
        recover(case)


def test_staged_cli_accepts_only_partial_final_crash_line(staged_cli):
    case = staged_cli
    with case.events_path.open("a", encoding="utf-8") as stream:
        stream.write('{"incomplete')
    assert recover(case).receipt["requires_new_source_audit"] is True


def test_staged_cli_retains_unreviewed_omissions_without_certifying_them(staged_cli):
    case = staged_cli
    audit = read(case.run / "audit.v01.json")
    audit["potential_omissions"] = ["Synthetic unresolved omission, never silently waived"]
    rewrite(case.run / "audit.v01.json", audit)
    rewrite(case.audit_call / "answer.json", audit)
    result = recover(case)
    assert result.baseline_audit.potential_omissions == audit["potential_omissions"]
    assert result.baseline_audit.coverage_limitations == case.audit.coverage_limitations
    assert result.receipt["scientific_validation"] == "not_established"
