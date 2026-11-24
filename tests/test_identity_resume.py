"""Offline source-identity recovery; scientific fields are never silently fixed."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_workflow import workspace, read_json
from test_active_reading import active, FakeReader, toolset
from watertreatment.domain import ExtractionRecord
from watertreatment.identity_resume import (authenticate_identity_parent, bind_dossier_identity,
    check_identity_reservation, verify_dossier_binding, verify_identity_resume)
from watertreatment.reading import ReadingDossier
from watertreatment.storage import DataStore, digest_json, hash_file, write_new_json
from watertreatment.workflow import prepare, execute_plan


@pytest.fixture
def identity_parent(active):
    workspace, run, bundle, plan = active
    model = FakeReader(workspace.settings, bundle)
    candidate = model.candidate.model_copy(deep=True)
    candidate.paper.id = "doi-is-not-internal-id"
    dossier = ReadingDossier(paper_id=candidate.paper.id, article_question_and_design="Preserve these scientific notes",
        material_map=[], experiment_map=[], coverage=model.coverage, cross_source_conflicts=[],
        unresolved_questions=[], schema_gaps=[])
    write_new_json(run / "reading_dossier.json", dossier.model_dump(mode="json"))
    write_new_json(run / "candidate.v00.json", candidate.model_dump(mode="json"))
    tool = run / "reading_tools" / "tool-0001"
    tool.mkdir(parents=True)
    request = {"tool": "wt_submit_record", "arguments": {"record": candidate.model_dump(mode="json")}}
    write_new_json(tool / "request.json", request)
    write_new_json(tool / "submitted_record.json", candidate.model_dump(mode="json"))
    sha = hash_file(tool / "submitted_record.json")
    receipt = {"schema_valid": True, "published": False, "canonical_record_sha256": sha,
        "base_hash": digest_json(candidate.model_dump(mode="json")), "submission_id": "tool-0001-" + sha[:16]}
    write_new_json(tool / "result.json", {"success": True, "data": receipt})
    events = []
    def event(direction, method=None, params=None, **message):
        if method:
            message.update(method=method, params=params)
        events.append({"direction": direction, "message": message})
    for stage, answer, seconds in (("comprehension", dossier.model_dump(mode="json"), 10.25),
                                    ("extraction", {"submission_id": receipt["submission_id"]}, 20.5)):
        call = run / "calls" / stage
        call.mkdir(parents=True)
        req = {"stage": stage, "sources": plan["input_paths"], "model": "gpt-6.2-sol", "reasoning_effort": "max",
            "code_identity": plan["code_identity"], "rule_version": plan["rule_version"]}
        write_new_json(call / "request.json", {"request": req, "fingerprint": digest_json(req)})
        result = {"status": "schema_valid_candidate", "server_selected_model": "gpt-6.2-sol",
            "server_selected_effort": "max", "elapsed_seconds": seconds, "thread_id": "thread", "turn_id": stage}
        if stage == "extraction":
            result["submission_id"] = receipt["submission_id"]
            write_new_json(call / "record.json", candidate.model_dump(mode="json"))
        write_new_json(call / "result.json", result)
        write_new_json(call / "answer.json", answer)
        event("receive", "turn/started", {"threadId": "thread", "turn": {"id": stage}})
        if stage == "extraction":
            event("receive", "item/tool/call", {"threadId": "thread", "turnId": stage,
                "tool": request["tool"], "arguments": request["arguments"]}, id=1)
            event("send", id=1, result={"success": True, "contentItems": [{"type": "inputText", "text": json.dumps(receipt)}]})
        event("receive", "item/completed", {"threadId": "thread", "turnId": stage,
            "item": {"type": "agentMessage", "phase": "final_answer", "text": json.dumps(answer)}})
        event("receive", "turn/completed", {"threadId": "thread", "turn": {"id": stage, "status": "completed"}})
    (run / "app_server").mkdir()
    (run / "app_server" / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return SimpleNamespace(workspace=workspace, run=run, bundle=bundle, plan=plan, dossier=dossier, candidate=candidate)


def authenticate(case):
    return authenticate_identity_parent(case.workspace.settings, case.run, case.bundle, "development")


def test_identity_recovery_preserves_science_and_originals_with_remaining_budget(identity_parent):
    c = identity_parent
    originals = {p: hash_file(p) for p in c.run.rglob("*") if p.is_file()}
    dossier, candidate, proof = authenticate(c)
    assert dossier.paper_id == candidate.paper.id == "paper-test"
    candidate.paper.id = c.candidate.paper.id
    dossier.paper_id = c.dossier.paper_id
    assert candidate == c.candidate and dossier == c.dossier
    assert proof["budget"]["remaining_seconds"] == 7169
    assert proof["budget"]["remaining_calls"] == 5
    assert proof["scientific_audit_inherited"] is False
    assert all(hash_file(p) == sha for p, sha in originals.items())


def test_identity_recovery_prepare_and_execute_only_first_audit(identity_parent):
    c = identity_parent
    bundle = Path(read_json(c.run / "manifest.json")["source_bundle"])
    run = prepare(c.workspace.settings, bundle, cohort_role="development", reuse_identity_corrected_run=c.run)
    plan = read_json(run / "plan.json")
    assert plan["max_calls"] == 5 and plan["continuation_timeout_seconds"] == 7169
    assert not plan.get("resumed_full_audit")
    verify_identity_resume(c.workspace.settings, run)
    client = FakeReader(c.workspace.settings, c.bundle)
    client.total_timeout_seconds = 7200
    execute_plan(c.workspace.settings, run / "plan.json", client=client)
    assert client.stages == [("audit_0", "audit-0")]
    assert client.total_timeout_seconds == 7169
    with pytest.raises(ValueError, match="already belongs"):
        prepare(c.workspace.settings, bundle, cohort_role="development", reuse_identity_corrected_run=c.run)


@pytest.mark.parametrize("target", ["candidate", "dossier", "source", "public_answer", "submission", "model", "elapsed"])
def test_identity_recovery_rejects_unproven_or_changed_authorities(identity_parent, target):
    c = identity_parent
    if target == "source":
        path = Path(c.bundle["documents"][0]["source"]["path"])
        path.write_bytes(path.read_bytes() + b"changed")
    elif target == "public_answer":
        path = c.run / "app_server/events.jsonl"
        path.write_text(path.read_text().replace("doi-is-not-internal-id", "changed-public-answer"), encoding="utf-8")
    else:
        path = {"candidate": c.run / "candidate.v00.json", "dossier": c.run / "reading_dossier.json",
            "submission": c.run / "reading_tools/tool-0001/request.json",
            "model": c.run / "calls/extraction/result.json", "elapsed": c.run / "calls/extraction/result.json"}[target]
        data = read_json(path)
        if target == "candidate": data["paper"]["id"] = "other"
        elif target == "dossier": data["article_question_and_design"] = "tampered science"
        elif target == "submission": data["arguments"]["record"]["paper"]["id"] = "other"
        elif target == "model": data["server_selected_model"] = "different-model"
        else: data["elapsed_seconds"] = None
        path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises((ValueError, KeyError)):
        authenticate(c)


def test_frozen_identity_proof_cannot_change_science_or_extend_budget(identity_parent):
    c = identity_parent
    run = prepare(c.workspace.settings, Path(read_json(c.run / "manifest.json")["source_bundle"]),
                  cohort_role="development", reuse_identity_corrected_run=c.run)
    plan = read_json(run / "plan.json")
    plan["continuation_timeout_seconds"] = 7200
    with pytest.raises(ValueError, match="budget differs"):
        verify_identity_resume(c.workspace.settings, run, plan)
    data = read_json(run / "reading_dossier.json")
    data["article_question_and_design"] = "changed scientific notes"
    (run / "reading_dossier.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="preserved science"):
        verify_identity_resume(c.workspace.settings, run)


def test_future_dossier_binding_is_explicit_and_replayable(active):
    workspace, run, bundle, plan = active
    raw = ReadingDossier(paper_id="DOI-instead-of-id", article_question_and_design="Keep exactly", material_map=[],
        experiment_map=[], coverage=[], cross_source_conflicts=[], unresolved_questions=[], schema_gaps=[])
    bound = bind_dossier_identity(raw, bundle["paper_id"], run)
    write_new_json(run / "reading_dossier.json", bound.model_dump(mode="json"))
    assert verify_dossier_binding(run, plan) == raw
    assert read_json(run / "dossier_identity_binding.json")["scientific_fields_changed"] is False
    bound.article_question_and_design = "changed"
    (run / "reading_dossier.json").write_text(bound.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="scientific notes changed"):
        verify_dossier_binding(run, plan)


def test_future_submission_binds_system_id_and_keeps_raw_science(active):
    tools = toolset(active)
    tools.begin_submission(True)
    raw = {"paper": {"id": "DOI-instead-of-id", "title": {"value": "Keep this title", "status": "reported"}}}
    response = tools.handle("wt_submit_record", {"record": raw})
    assert response["success"]
    data = json.loads(response["contentItems"][0]["text"])
    proof = data["transport"]["system_identity_binding"]
    original = ExtractionRecord.model_validate(read_json(Path(proof["raw_record_path"])))
    bound = tools.revision_base.model_copy(deep=True)
    assert bound.paper.id == "paper-test"
    bound.paper.id = original.paper.id
    assert bound == original and proof["scientific_fields_changed"] is False


def test_recovery_replays_future_initial_binding_and_preserves_later_seed(identity_parent):
    c = identity_parent
    tool = c.run / "reading_tools/tool-0001"
    raw = c.candidate
    bound = raw.model_copy(deep=True)
    bound.paper.id = c.bundle["paper_id"]
    write_new_json(tool / "unbound_submitted_record.json", raw.model_dump(mode="json"))
    for path in (tool / "submitted_record.json", c.run / "candidate.v00.json", c.run / "calls/extraction/record.json"):
        path.write_text(bound.model_dump_json(indent=2), encoding="utf-8")
    sha = hash_file(tool / "submitted_record.json")
    binding = {"policy": "controller-paper-id-binding-v1", "path": "paper.id", "before": raw.paper.id,
        "after": bound.paper.id, "raw_record_path": str(tool / "unbound_submitted_record.json"),
        "raw_record_sha256": hash_file(tool / "unbound_submitted_record.json"),
        "corrected_content_hash": digest_json(bound.model_dump(mode="json")), "scientific_fields_changed": False}
    data = read_json(tool / "result.json")["data"]
    data.update(canonical_record_sha256=sha, base_hash=digest_json(bound.model_dump(mode="json")),
                submission_id="tool-0001-" + sha[:16], transport={"system_identity_binding": binding})
    (tool / "result.json").write_text(json.dumps({"success": True, "data": data}), encoding="utf-8")
    answer = {"submission_id": data["submission_id"]}
    (c.run / "calls/extraction/answer.json").write_text(json.dumps(answer), encoding="utf-8")
    result = read_json(c.run / "calls/extraction/result.json")
    result.update(answer)
    (c.run / "calls/extraction/result.json").write_text(json.dumps(result), encoding="utf-8")
    events_path = c.run / "app_server/events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    for event in events:
        msg = event["message"]
        if event["direction"] == "send":
            msg["result"]["contentItems"][0]["text"] = json.dumps(data)
        elif msg.get("method") == "item/completed" and msg["params"].get("turnId") == "extraction":
            msg["params"]["item"]["text"] = json.dumps(answer)
    events_path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    dossier, candidate, proof = authenticate(c)
    assert candidate == bound and dossier.paper_id == bound.paper.id
    assert proof["scientific_fields_changed"] is False
    # Native revision replay starts at the already-bound seed; it does not
    # replay the initial extraction turn a second time.
    from watertreatment.staged_source_resume import _replay
    request = {"tool": "wt_patch_record", "arguments": {"base_hash": digest_json(bound.model_dump(mode="json")),
        "edits": [{"operation": "set", "path": ["paper", "title", "note"], "value": "Synthetic source clarification"}]}}
    revised = _replay(bound, request)
    assert revised.paper.id == bound.paper.id
    revised.paper.title.note = bound.paper.title.note
    assert revised == bound
