"""Synthetic completed repair ancestry and audit-only recovery; no model calls."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_staged_continuation_workflow import staged_plan_case, make_plan
from test_staged_source_resume import staged_cli, add_patch, save_events, load_events, turn, rewrite
from test_web_records import fixture, read
from watertreatment.audit_resume import prepare_resumed_review, authenticate_baseline
from watertreatment.cli import parser
from watertreatment.codex import source_snapshot
from watertreatment.completed_continuation_resume import authenticate_completed_continuation
from watertreatment.domain import ExtractionRecord
from watertreatment.reading_tools import ReadingTools
from watertreatment.scientific_revision_audit import NEW_SCOPE_LIMITS
from watertreatment.storage import digest_json, hash_file, write_new_json
from watertreatment.workflow import prepare


@pytest.fixture
def completed_continuation(staged_plan_case):
    parent = staged_plan_case
    run = make_plan(parent, repairs=("missingness",))
    plan = read(run / "plan.json")
    seed = ExtractionRecord.model_validate(read(run / "reused_candidate.json"))
    tools = ReadingTools(read(run / "sources.json"), run, max_calls=20, expected_inputs=plan["input_paths"])
    tools.prepare_revision(seed)
    tools.begin_submission(True)
    binding = tools.candidate_binding
    case = SimpleNamespace(f=parent.f, parent=parent, run=run, tools=tools,
        events_path=run / "app_server/events.jsonl", seed=seed)
    save_events(case, [turn("turn/started", "failed-thread", "failed-turn")])
    case.first = add_patch(case, [{"operation": "append", "path": ["observations", 0, "limitations"],
        "value": "Synthetic completed repair first supported restriction"}])
    case.stage = add_patch(case, [{"operation": "append", "path": ["observations", 0, "limitations"],
        "value": "Synthetic completed repair second supported restriction"}])
    case.candidate = ExtractionRecord.model_validate(read(case.stage))
    case.candidate_path = run / "candidate.v00.json"
    write_new_json(case.candidate_path, case.candidate.model_dump(mode="json"))
    call = case.call = run / "calls/call-completed"
    contract = (run / "continuation_repair_prompt.txt").read_text(encoding="utf-8")
    request = {"stage": "continuation_repair_0", "code_identity": plan["code_identity"],
        "rule_version": plan["rule_version"], "model": "gpt-6.2-sol", "reasoning_effort": "max",
        "candidate_binding": binding, "prompt": contract + "\nCANDIDATE_SNAPSHOT " + binding["content_hash"],
        "sources": source_snapshot([*[Path(x["path"]) for x in plan["input_paths"]], Path(binding["path"])])}
    write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    data = read(case.stage.parent / "result.json")["data"]
    write_new_json(call / "result.json", {"status": "schema_valid_candidate", "server_selected_model": "gpt-6.2-sol",
        "server_selected_effort": "max", "thread_id": "failed-thread", "turn_id": "failed-turn",
        "submission_id": data["submission_id"]})
    write_new_json(call / "answer.json", {"submission_id": data["submission_id"]})
    write_new_json(call / "record.json", case.candidate.model_dump(mode="json"))
    write_new_json(run / "continuation_repair.json", {"targets": plan["continuation_repairs"],
        "input_path": str(run / "reused_candidate.json"), "input_sha256": hash_file(run / "reused_candidate.json"),
        "input_content_hash": digest_json(seed.model_dump(mode="json")),
        "output_content_hash": digest_json(case.candidate.model_dump(mode="json")),
        "status": "model_repaired_requires_source_audit", "human_validated": False, "graph_published": False})
    write_new_json(run / "result.json", {"status": "candidate_incomplete_failure", "last_candidate_version": "candidate.v00.json",
        "human_validated": False, "graph_published": False, "dataset_role": plan["cohort_role"]})
    write_new_json(run / "failure.json", {"error": "Synthetic scope planner stopped before the first audit"})
    save_events(case, [*load_events(case), turn("turn/completed", "failed-thread", "failed-turn", "completed")])
    return case


def authenticate(case):
    return authenticate_completed_continuation(case.f.settings, case.candidate_path)


@pytest.mark.parametrize("staged_cli", ["development", "unassigned"], indirect=True)
def test_completed_continuation_authenticates_exact_final_delivery_and_original_full_baseline(completed_continuation):
    case = completed_continuation
    before = {p: hash_file(p) for p in case.run.rglob("*") if p.is_file()}
    result = authenticate(case)
    assert result.record == case.candidate and result.baseline_record == case.parent.baseline
    assert result.baseline_candidate_path == case.parent.run / "candidate.v01.json"
    assert result.receipt["turn_status"] == "completed_continuation_awaiting_source_audit"
    assert result.receipt["requires_new_source_audit"] is True
    assert result.receipt["scientific_validation"] == "not_established"
    assert case.events_path not in result.input_paths
    assert len(result.receipt["staged_chain"]) == 2
    assert before == {p: hash_file(p) for p in case.run.rglob("*") if p.is_file()}


@pytest.mark.parametrize("mutation", ["candidate", "call_record", "answer", "status", "model", "effort",
    "completion", "completion_thread", "missing_completion", "wrong_order", "protocol_args", "replay",
    "first_stage", "parent_seed", "inherited_baseline", "summary", "contract", "newer_candidate", "new_audit", "later_call"])
def test_completed_continuation_rejects_changed_delivery_or_ancestry(completed_continuation, mutation):
    case = completed_continuation
    if mutation in {"candidate", "call_record", "first_stage", "parent_seed", "inherited_baseline", "contract"}:
        path = {"candidate": case.candidate_path, "call_record": case.call / "record.json", "first_stage": case.first,
            "parent_seed": case.parent.stage, "inherited_baseline": case.run / "resumed_baseline_candidate.json",
            "contract": case.run / "continuation_repair_prompt.txt"}[mutation]
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation in {"answer", "status", "model", "effort", "summary"}:
        path = case.call / ("answer.json" if mutation == "answer" else "result.json")
        if mutation == "summary":
            path = case.run / "continuation_repair.json"
        data = read(path)
        key = {"answer": "submission_id", "status": "status", "model": "server_selected_model",
            "effort": "server_selected_effort", "summary": "output_content_hash"}[mutation]
        data[key] = "wrong"
        rewrite(path, data)
    elif mutation in {"newer_candidate", "new_audit", "later_call"}:
        path = {"newer_candidate": case.run / "candidate.v01.json", "new_audit": case.run / "audit.v00.json",
                "later_call": case.run / "calls/later/request.json"}[mutation]
        write_new_json(path, {})
    else:
        events = load_events(case)
        if mutation == "completion":
            events[-1]["message"]["params"]["turn"]["status"] = "failed"
        elif mutation == "completion_thread":
            events[-1]["message"]["params"]["threadId"] = "wrong"
        elif mutation == "missing_completion":
            events.pop()
        elif mutation == "wrong_order":
            events[-1], events[-2] = events[-2], events[-1]
        else:
            events[-3]["message"]["params"]["arguments"]["edits"][0]["value"] = "Unretained alteration"
            if mutation == "replay":
                request = read(case.stage.parent / "request.json")
                request["arguments"] = events[-3]["message"]["params"]["arguments"]
                rewrite(case.stage.parent / "request.json", request)
        save_events(case, events)
    with pytest.raises(ValueError):
        authenticate(case)


@pytest.mark.parametrize("staged_cli", ["development", "unassigned"], indirect=True)
def test_completed_continuation_plan_resumes_audit_without_repeating_repair(completed_continuation):
    case = completed_continuation
    role = read(case.run / "plan.json")["cohort_role"]
    run = prepare(case.f.settings, case.parent.imported / "sources.json", cohort_role=role,
        reuse_dossier=case.parent.run / "reading_dossier.json", reuse_completed_continuation_candidate=case.candidate_path)
    plan = read(run / "plan.json")
    assert plan["continuation_repairs"] == [] and not (run / "continuation_repair_prompt.txt").exists()
    assert plan["resumed_full_audit"]["seed_provenance"]["kind"] == "completed_continuation"
    assert "staged_checkpoint" not in plan["resumed_full_audit"]
    assert plan["scientific_scope_limits"] == NEW_SCOPE_LIMITS
    assert read(run / "reused_candidate.json") == case.candidate.model_dump(mode="json")
    write_new_json(run / "candidate.v00.json", case.candidate.model_dump(mode="json"))
    receipt, scope, baseline = prepare_resumed_review(case.f.settings, plan, run, case.candidate)
    assert scope["scope_limits"] == plan["scientific_scope_limits"]
    assert baseline == case.parent.audit and receipt["additional_audits"] == []
    assert authenticate_baseline(case.f.settings, plan["resumed_full_audit"], case.candidate)[0] == case.parent.baseline
    assert plan["cohort_role"] == role
    assert read(run / "reused_candidate.json")["paper"] == case.candidate.model_dump(mode="json")["paper"]
    assert case.f.store.last_event(run.name, "development_use")["paper_id"] == case.candidate.paper.id


@pytest.mark.parametrize("staged_cli", ["unassigned"], indirect=True)
def test_completed_continuation_cannot_relabel_production_sources(completed_continuation):
    from watertreatment.workflow import _reusable_completed_continuation

    case = completed_continuation
    with pytest.raises(ValueError, match="cohort"):
        _reusable_completed_continuation(case.f.settings, case.candidate_path,
            read(case.run / "sources.json"), "development")
    with pytest.raises(ValueError, match="non-heldout"):
        _reusable_completed_continuation(case.f.settings, case.candidate_path,
            read(case.run / "sources.json"), "heldout")


@pytest.mark.parametrize("staged_cli", ["unassigned"], indirect=True)
def test_completed_continuation_rejects_changed_terminal_cohort(completed_continuation):
    case = completed_continuation
    terminal = read(case.run / "result.json")
    terminal["dataset_role"] = "development"
    rewrite(case.run / "result.json", terminal)
    with pytest.raises(ValueError, match="unpublished completed repair"):
        authenticate(case)


def test_completed_continuation_cli_and_explicit_no_repair_contract(completed_continuation):
    case = completed_continuation
    parsed = parser().parse_args(["prepare", "sources.json", "--reuse-completed-continuation-candidate", "candidate.v00.json"])
    assert parsed.reuse_completed_continuation_candidate == Path("candidate.v00.json")
    with pytest.raises(ValueError, match="staged candidate"):
        prepare(case.f.settings, case.parent.imported / "sources.json", cohort_role="development",
            reuse_dossier=case.parent.run / "reading_dossier.json", reuse_completed_continuation_candidate=case.candidate_path,
            continuation_repairs=["missingness"])
