"""Actual source-authenticated budget continuations, without providers or Neo4j."""
import json
from pathlib import Path

import pytest

from test_workflow import workspace, read_json
from test_active_reading import active, FakeReader
from test_identity_resume import identity_parent
from watertreatment.budget_policy import default_policy
from watertreatment.budget_grants import (preview_grant, reserve_grant, authenticate_unfinished_reading,
    verify_grant_plan)
from watertreatment.budget_extension_workflow import select_checkpoint, prepare_extension, verify_extension_plan
from watertreatment.identity_resume import authenticate_identity_parent
from watertreatment.storage import digest_json, hash_file, write_new_json


def rewrite(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_exhausted_identity_recovery_reuses_science_and_only_runs_first_audit(identity_parent):
    c = identity_parent
    settings = c.workspace.settings
    output = c.run / "calls/extraction/result.json"
    value = read_json(output)
    value["elapsed_seconds"] = 7191
    rewrite(output, value)
    with pytest.raises(ValueError, match="budget"):
        authenticate_identity_parent(settings, c.run, c.bundle, "development")
    before = {p: hash_file(p) for p in c.run.rglob("*") if p.is_file()}
    preview = preview_grant(settings, c.run, default_policy(settings))
    selected = select_checkpoint(settings, c.run, preview)
    assert selected["route"] == "identity"
    descriptor = reserve_grant(settings, c.run, preview, digest_json(selected))
    bounded, child, execute = prepare_extension(settings, descriptor, selected)
    assert verify_extension_plan(bounded, child)["added_seconds"] == 1800
    assert read_json(child / "reused_candidate.json")["paper"]["id"] == c.bundle["paper_id"]
    client = FakeReader(bounded, c.bundle)
    client.total_timeout_seconds = bounded.reading.total_timeout_seconds
    execute(bounded, child / "plan.json", client=client)
    assert client.stages == [("audit_0", "audit-0")]
    assert {p: hash_file(p) for p in before} == before
    assert verify_grant_plan(bounded, child)["used_seconds"] == 7201.25


@pytest.fixture
def unfinished_reading(active):
    ws, run, bundle, plan = active
    request = {"stage": "comprehension", "model": "gpt-6.2-sol", "reasoning_effort": "max",
        "code_identity": plan["code_identity"], "rule_version": plan["rule_version"],
        "sources": plan["input_paths"], "prompt": "synthetic authentic incomplete understanding", "schema": {}}
    call = run / "calls/failed-first-understanding"
    write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    write_new_json(call / "result.json", {"status": "failed", "error": "TimeoutError: fixture",
        "elapsed_seconds": 7201, "server_selected_model": "gpt-6.2-sol", "server_selected_effort": "max",
        "thread_id": "thread", "turn_id": "turn"})
    write_new_json(run / "failure.json", {"model_calls": 1, "error": "TimeoutError: fixture"})
    write_new_json(run / "result.json", {"status": "reading_incomplete_failure", "last_candidate_version": None,
        "model_calls": 1, "human_validated": False, "graph_published": False})
    event = {"direction": "send", "message": {"method": "turn/start", "params": {
        "model": request["model"], "effort": request["reasoning_effort"], "threadId": "thread",
        "input": [{"type": "text", "text": request["prompt"]}], "outputSchema": request["schema"]}}}
    events = run / "app_server/events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(json.dumps(event) + "\n", encoding="utf-8")
    return ws, run, bundle


def test_unfinished_first_understanding_requires_explicit_grant_and_keeps_original(unfinished_reading):
    ws, parent, bundle = unfinished_reading
    proof = authenticate_unfinished_reading(ws.settings, parent)
    assert proof["reading_completed"] is False
    before = {p: hash_file(p) for p in parent.rglob("*") if p.is_file()}
    preview = preview_grant(ws.settings, parent, default_policy(ws.settings))
    selected = select_checkpoint(ws.settings, parent, preview)
    assert selected["route"] == "reading"
    descriptor = reserve_grant(ws.settings, parent, preview, digest_json(selected))
    bounded, child, execute = prepare_extension(ws.settings, descriptor, selected)
    client = FakeReader(bounded, bundle)
    client.total_timeout_seconds = bounded.reading.total_timeout_seconds
    execute(bounded, child / "plan.json", client=client)
    assert client.stages[0][0] == "comprehension"
    assert child != parent and {p: hash_file(p) for p in before} == before


@pytest.mark.parametrize("defect", ["missing_elapsed", "different_model", "public_answer", "completed_turn", "retained_dossier"])
def test_unfinished_understanding_does_not_discard_or_invent_proof(unfinished_reading, defect):
    ws, run, _ = unfinished_reading
    path = run / "calls/failed-first-understanding/result.json"
    if defect in {"missing_elapsed", "different_model"}:
        value = read_json(path)
        value["elapsed_seconds" if defect == "missing_elapsed" else "server_selected_model"] = None
        rewrite(path, value)
    elif defect == "retained_dossier":
        write_new_json(run / "reading_dossier.json", {})
    else:
        message = {"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "phase": "final_answer", "text": "retained answer"}}}
        if defect == "completed_turn":
            message = {"method": "turn/completed", "params": {"turn": {"id": "turn", "status": "completed"}}}
        with (run / "app_server/events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"direction": "receive", "message": message}) + "\n")
    with pytest.raises(ValueError):
        authenticate_unfinished_reading(ws.settings, run)
