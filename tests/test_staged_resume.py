"""Failed-turn checkpoints require bound local receipts and a new source audit.

All events and calls below are synthetic protocol artifacts; no provider or
database service is contacted and these checks do not verify scientific facts.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from test_repair_guidance import failed_repair, rewrite, timeout
from test_web_model import ReplayClient, audit, model_fixture
from test_web_records import fixture, read
from watertreatment.domain import RoleHistory
from watertreatment.reading import ReadingAudit
from watertreatment.storage import digest_json, hash_file, write_new_json
from watertreatment.web_model import reaudit
from watertreatment.web_resume import staged_revision


def save_events(case, events):
    case.events_path.parent.mkdir(parents=True, exist_ok=True)
    case.events_path.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
                                encoding="utf-8")


def load_events(case):
    return [json.loads(line) for line in case.events_path.read_text(encoding="utf-8").splitlines()]


def add_stage(case, number, value, *, tool="wt_submit_record", base=None):
    """Match ReadingTools.handle artifacts and the transport's tool envelopes."""
    candidate = case.f.record.model_copy(deep=True)
    candidate.observations[0].value.value = value
    scientific = read(case.prior / "plan.json")["scientific_plan"]
    candidate.rule_version = scientific["rule_version"]
    candidate.schema_version = scientific["schema_version"]
    folder = case.prior / "reading_tools" / f"tool-{number:04d}"
    write_new_json(folder / "submitted_record.json", candidate.model_dump(mode="json"))
    if tool == "wt_submit_record":
        arguments = {"record": candidate.model_dump(mode="json", exclude_defaults=True)}
    else:
        arguments = {"base_hash": digest_json(base.model_dump(mode="json")), "edits": [
            {"operation": "set", "path": ["observations", 0, "value", "value"], "value": value}]}
    write_new_json(folder / "request.json", {"tool": tool, "arguments": arguments})
    sha = hash_file(folder / "submitted_record.json")
    data = {"submission_id": folder.name + "-" + sha[:16], "canonical_record_sha256": sha,
            "base_hash": digest_json(candidate.model_dump(mode="json")), "schema_valid": True,
            "issues": [], "scientific_accuracy": "not_established", "published": False,
            "tool_call_number": number, "remaining_tool_calls": 160 - number,
            "remaining_turn_seconds": 100, "evidence_warning": "Synthetic source evidence is untrusted."}
    write_new_json(folder / "result.json", {"success": True, "data": data})
    events = load_events(case) if case.events_path.exists() else []
    events.extend([
        {"direction": "receive", "message": {"id": number, "method": "item/tool/call", "params": {
            "threadId": "synthetic-thread", "turnId": "synthetic-failed-turn", "tool": tool,
            "namespace": None, "arguments": arguments}}},
        {"direction": "send", "message": {"id": number, "result": {"success": True,
            "contentItems": [{"type": "inputText", "text": json.dumps(data, ensure_ascii=False)}]}}},
    ])
    save_events(case, events)
    return folder, candidate


@pytest.fixture
def staged_case(failed_repair):
    case = failed_repair
    case.events_path = case.prior / "app_server" / "events.jsonl"
    case.revision_call = case.prior / "calls" / "call-02"
    plan = read(case.prior / "plan.json")
    request = {"model": plan["model"], "reasoning_effort": plan["effort"], "stage": "revision_web_0", "sources": plan["input_paths"],
               "prompt": "Synthetic source-based revision: " + case.f.record.model_dump_json(exclude_defaults=True),
               "code_identity": plan["code_identity"]}
    write_new_json(case.revision_call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    write_new_json(case.revision_call / "result.json", {"status": "failed", "error": "Synthetic timeout",
        "server_selected_model": "gpt-6.2-sol", "server_selected_effort": "max",
        "thread_id": "synthetic-thread", "turn_id": "synthetic-failed-turn"})
    case.first_stage, first = add_stage(case, 1, 2.45)
    case.stage, case.candidate = add_stage(case, 2, 2.5, tool="wt_patch_record", base=first)
    return case


def recover(case, detail=None):
    return staged_revision(case.f.settings, case.f.records, detail or case.detail, case.prior.name)


def test_staged_resume_selects_latest_successful_draft_without_registering_it(staged_case):
    case = staged_case
    candidate, paths, receipt = recover(case)
    assert candidate == case.candidate and candidate.observations[0].value.value == 2.5
    assert receipt["stage"] == case.stage.name
    assert receipt["candidate_sha256"] == hash_file(case.stage / "submitted_record.json")
    assert receipt["candidate_base_hash"] == digest_json(candidate.model_dump(mode="json"))
    assert receipt["turn_status"] == "failed_with_verified_staged_candidate"
    assert receipt["requires_fresh_audit"] is True and receipt["scientific_validation"] == "not_established"
    assert receipt["events_sha256"] == hash_file(case.events_path)
    assert len(paths) == 8 and all(path.is_file() for path in paths)
    assert case.stage / "submitted_record.json" in paths
    assert case.first_stage / "submitted_record.json" not in paths
    assert case.events_path not in paths  # Reasoning/event text must not enter the next prompt.
    assert len(case.f.records.list_records()) == case.original_records
    assert hash_file(case.f.run / "candidate.json") == case.original_hash


def test_later_failed_tool_without_candidate_does_not_hide_latest_successful_stage(staged_case):
    case = staged_case
    folder = case.prior / "reading_tools" / "tool-0003"
    write_new_json(folder / "request.json", {"tool": "wt_patch_record", "arguments": {"base_hash": "stale", "edits": []}})
    write_new_json(folder / "result.json", {"success": False, "contentItems": [{"type": "inputText", "text": "stale base_hash"}]})
    assert recover(case)[2]["stage"] == case.stage.name


def test_latest_complete_submission_can_also_supply_a_checkpoint(staged_case):
    case = staged_case
    folder, candidate = add_stage(case, 3, 2.6)
    recovered, _, receipt = recover(case)
    assert recovered == candidate and receipt["stage"] == folder.name


def test_corrupt_latest_stage_is_rejected_instead_of_silently_falling_back(staged_case):
    case = staged_case
    latest = read(case.stage / "result.json")
    latest["success"] = False
    rewrite(case.stage / "result.json", latest)
    with pytest.raises(ValueError, match="successful local tool result"):
        recover(case)


@pytest.mark.parametrize("mutation", ["record_bytes", "record_content", "receipt_hash", "base_hash", "schema_valid",
    "published", "tool_success", "request_tool", "request_args", "event_args", "event_receipt", "event_success",
    "event_turn", "event_thread", "event_namespace", "event_id", "event_tool", "missing_send"])
def test_staged_resume_rejects_unmatched_content_tool_receipt_or_protocol(staged_case, mutation):
    case = staged_case
    path = case.stage / "submitted_record.json"
    if mutation == "record_bytes":
        path.write_bytes(path.read_bytes() + b"\n ")
    elif mutation == "record_content":
        record = read(path)
        record["observations"][0]["value"]["value"] = 99
        rewrite(path, record)
    elif mutation in {"receipt_hash", "base_hash", "schema_valid", "published", "tool_success"}:
        receipt = read(case.stage / "result.json")
        if mutation == "tool_success":
            receipt["success"] = False
        else:
            key, value = {"receipt_hash": ("canonical_record_sha256", "0" * 64), "base_hash": ("base_hash", "0" * 64),
                          "schema_valid": ("schema_valid", False), "published": ("published", True)}[mutation]
            receipt["data"][key] = value
        rewrite(case.stage / "result.json", receipt)
    elif mutation in {"request_tool", "request_args"}:
        request = read(case.stage / "request.json")
        if mutation == "request_tool":
            request["tool"] = "wt_page_text"
        else:
            request["arguments"]["edits"][0]["value"] = 99
        rewrite(case.stage / "request.json", request)
    else:
        events = load_events(case)
        request, response = events[-2]["message"], events[-1]["message"]
        if mutation == "event_args":
            request["params"]["arguments"]["edits"][0]["value"] = 99
        elif mutation == "event_receipt":
            receipt = json.loads(response["result"]["contentItems"][0]["text"])
            receipt["base_hash"] = "0" * 64
            response["result"]["contentItems"][0]["text"] = json.dumps(receipt)
        elif mutation == "event_success":
            response["result"]["success"] = False
        elif mutation == "event_id":
            response["id"] = "not-the-request"
        elif mutation == "missing_send":
            events.pop()
        else:
            key, value = {"event_turn": ("turnId", "other-turn"), "event_thread": ("threadId", "other-thread"),
                          "event_namespace": ("namespace", "unauthorized"), "event_tool": ("tool", "wt_submit_record")}[mutation]
            request["params"][key] = value
        save_events(case, events)
    with pytest.raises(ValueError):
        recover(case)


@pytest.mark.parametrize("mutation", ["fingerprint", "stage", "source_binding", "status", "model", "effort",
    "thread", "turn"])
def test_staged_resume_requires_source_bound_failed_sol_max_revision(staged_case, mutation):
    case = staged_case
    if mutation in {"fingerprint", "stage", "source_binding"}:
        envelope = read(case.revision_call / "request.json")
        if mutation == "stage":
            envelope["request"]["stage"] = "audit_web_0"
        elif mutation == "source_binding":
            envelope["request"]["sources"] = []
        envelope["fingerprint"] = "0" * 64 if mutation == "fingerprint" else digest_json(envelope["request"])
        rewrite(case.revision_call / "request.json", envelope)
    else:
        result = read(case.revision_call / "result.json")
        key, value = {"status": ("status", "schema_valid_candidate"), "model": ("server_selected_model", "other"),
            "effort": ("server_selected_effort", "low"), "thread": ("thread_id", "other-thread"),
            "turn": ("turn_id", "other-turn")}[mutation]
        result[key] = value
        rewrite(case.revision_call / "result.json", result)
    with pytest.raises(ValueError):
        recover(case)


@pytest.mark.parametrize("mutation", ["record_id", "record_hash", "sources_bundle", "candidate"])
def test_staged_resume_is_bound_to_current_parent_record_and_source(staged_case, mutation):
    case = staged_case
    detail = copy.deepcopy(case.detail)
    if mutation == "record_id":
        detail["id"] = "different-record"
    elif mutation == "record_hash":
        detail["summary"]["record_hash"] = "0" * 64
    elif mutation == "sources_bundle":
        other = case.f.settings.data_root / "changed-source-bundle.json"
        write_new_json(other, {**read(Path(detail["sources_bundle"])), "different": True})
        detail["sources_bundle"] = str(other)
    else:
        detail["record"]["observations"][0]["value"]["value"] = 99
    with pytest.raises(ValueError):
        recover(case, detail)


def rewrite_staged_candidate_and_receipts(case, candidate):
    """Refresh byte receipts so semantic identity checks, not stale hashes, run."""
    path = case.stage / "submitted_record.json"
    rewrite(path, candidate.model_dump(mode="json"))
    receipt = read(case.stage / "result.json")
    receipt["data"]["canonical_record_sha256"] = hash_file(path)
    receipt["data"]["base_hash"] = digest_json(candidate.model_dump(mode="json"))
    receipt["data"]["submission_id"] = case.stage.name + "-" + hash_file(path)[:16]
    rewrite(case.stage / "result.json", receipt)
    events = load_events(case)
    events[-1]["message"]["result"]["contentItems"][0]["text"] = json.dumps(receipt["data"])
    save_events(case, events)


@pytest.mark.parametrize("mutation", ["paper_id", "cohort", "source_ids", "role_history", "rule", "schema"])
def test_even_hash_matched_draft_cannot_change_identity_or_target_version(staged_case, mutation):
    case = staged_case
    candidate = case.candidate.model_copy(deep=True)
    if mutation == "paper_id":
        candidate.paper.id = "other-paper"
    elif mutation == "cohort":
        candidate.paper.dataset_role = "development"
    elif mutation == "source_ids":
        candidate.paper.source_ids = ["different-source"]
    elif mutation == "role_history":
        candidate.paper.role_history = [RoleHistory(role="development", date="2026-01-01",
            reason="Changed synthetic cohort provenance")]
    elif mutation == "rule":
        candidate.rule_version = "0.6"
    else:
        candidate.schema_version = "0.2.0"
    rewrite_staged_candidate_and_receipts(case, candidate)
    with pytest.raises(ValueError):
        recover(case)


@pytest.mark.parametrize("filename", ["plan.json", "input_candidate.json", "sources.json"])
def test_changed_frozen_checkpoint_input_stops_before_client_factory(staged_case, filename):
    case = staged_case
    path = case.prior / filename
    path.write_bytes(path.read_bytes() + b"\n ")
    runs_before = len(case.f.store.runs())
    with pytest.raises(ValueError):
        reaudit(case.f.settings, case.f.records, case.f.run.name, "Invalid checkpoint",
            resume_staged_run_id=case.prior.name, client_factory=lambda *a, **k: pytest.fail("client factory reached"))
    assert len(case.f.store.runs()) == runs_before


@pytest.mark.parametrize("field", ["parent_run", "record_id", "record_hash", "source_bundle_sha256", "model", "effort",
                                   "target_rule_version", "target_schema_version"])
def test_staged_resume_rejects_manifest_parent_or_target_mismatch(staged_case, field):
    case = staged_case
    manifest = read(case.prior / "manifest.json")
    manifest[field] = "wrong-parent-or-target"
    rewrite(case.prior / "manifest.json", manifest)
    with pytest.raises(ValueError):
        recover(case)


def test_nonfailed_or_unregistered_run_cannot_supply_checkpoint(staged_case):
    case = staged_case
    for identifier in (case.f.run.name, "unregistered-checkpoint"):
        with pytest.raises(ValueError, match="registered failed re-audit"):
            staged_revision(case.f.settings, case.f.records, case.detail, identifier)
    (case.prior / "failure.json").rename(case.prior / "retained-failure.json")
    with pytest.raises(ValueError, match="registered failed re-audit"):
        recover(case)


def test_successful_run_cannot_be_misclassified_by_a_leftover_failure_file(staged_case):
    write_new_json(staged_case.prior / "result.json", {"status": "candidate_audited_with_limitations", "stop_reason": "audit_completed"})
    with pytest.raises(ValueError):
        recover(staged_case)


def test_resume_and_reused_guidance_are_mutually_exclusive_before_new_run(staged_case):
    case = staged_case
    runs_before = len(case.f.store.runs())
    with pytest.raises(ValueError, match="fresh audit"):
        reaudit(case.f.settings, case.f.records, case.f.run.name, "Do not reuse approval",
            resume_staged_run_id=case.prior.name, repair_audit_run_id=case.prior.name,
            client_factory=lambda *a, **k: pytest.fail("client factory reached"))
    assert len(case.f.store.runs()) == runs_before


def test_resumed_draft_is_registered_only_after_new_audit_of_the_exact_candidate(staged_case):
    case = staged_case
    def before_fresh_audit(model, prompt):
        assert model is ReadingAudit
        assert case.candidate.model_dump_json(exclude_defaults=True) in prompt
        assert len(case.f.records.list_records()) == case.original_records
        assert case.f.records.get(case.f.run.name)["summary"]["current"]
        return audit()
    replay = ReplayClient([before_fresh_audit])
    result = reaudit(case.f.settings, case.f.records, case.f.run.name, "Audit the resumed scientific draft",
        resume_staged_run_id=case.prior.name, client_factory=replay.factory)
    assert replay.closed and replay.calls_started == 1
    assert [item["stage"] for item in replay.calls] == ["audit_web_0"]
    assert result["revision"]["accepted"] and result["revision"]["changed"]
    assert result["result"]["history"][0]["audit_origin"] == "fresh_model_audit"
    assert result["result"]["resumed_staged_candidate"]["requires_fresh_audit"] is True
    resumed = case.f.settings.data_root / "runs" / result["run_id"]
    plan = read(resumed / "plan.json")
    copies = list((resumed / "resumed_candidate").iterdir())
    frozen = {Path(item["path"]): item["sha256"] for item in plan["input_paths"]}
    assert len(copies) == 9 and all(frozen.get(path) == hash_file(path) for path in copies)
    assert not any("events.jsonl" in path.name for path in copies)
    assert read(resumed / "input_candidate.json") == case.detail["record"]
    assert read(resumed / "candidate.v00.json") == case.candidate.model_dump(mode="json")
    assert hash_file(case.f.run / "candidate.json") == case.original_hash


def test_fresh_audit_failure_preserves_checkpoint_without_registering(staged_case):
    case = staged_case
    staged_hash = hash_file(case.stage / "submitted_record.json")
    replay = ReplayClient([timeout])
    with pytest.raises(TimeoutError):
        reaudit(case.f.settings, case.f.records, case.f.run.name, "Fresh audit is required",
            resume_staged_run_id=case.prior.name, client_factory=replay.factory)
    assert replay.closed and replay.calls_started == 1
    assert len(case.f.records.list_records()) == case.original_records
    assert case.f.records.get(case.f.run.name)["summary"]["current"]
    assert hash_file(case.stage / "submitted_record.json") == staged_hash
    assert hash_file(case.f.run / "candidate.json") == case.original_hash
    row = next(row for row in case.f.store.runs() if row["kind"] == "web_reaudit" and row["run_id"] != case.prior.name)
    resumed = Path(row["manifest_path"]).parent
    assert (resumed / "failure.json").is_file()
    assert read(resumed / "candidate.v00.json") == case.candidate.model_dump(mode="json")
    assert not (resumed / "model_review.json").exists()


def test_resumed_copy_cannot_change_during_fresh_audit_and_then_be_registered(staged_case):
    case = staged_case
    def change_copy(run):
        path = next((run / "resumed_candidate").glob("*-submitted_record.json"))
        path.write_bytes(path.read_bytes() + b"\n ")
    replay = ReplayClient([audit()], change_after=change_copy)
    with pytest.raises(ValueError, match="冻结输入发生变化"):
        reaudit(case.f.settings, case.f.records, case.f.run.name, "Check the frozen resumed candidate",
            resume_staged_run_id=case.prior.name, client_factory=replay.factory)
    assert replay.closed and len(case.f.records.list_records()) == case.original_records
    assert case.f.records.get(case.f.run.name)["summary"]["current"]
