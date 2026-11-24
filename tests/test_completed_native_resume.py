"""Synthetic completed native patch delivery; no providers or graph access."""
import json
import shutil

import pytest

from test_staged_source_resume import staged_cli, save_events, load_events, turn, rewrite
from test_web_records import fixture, read
from watertreatment.completed_native_resume import authenticate_completed_revision
from watertreatment.storage import hash_file, write_new_json


@pytest.fixture
def completed_native(staged_cli):
    case = staged_cli
    case.candidate_path = case.run / "candidate.v02.json"
    shutil.copyfile(case.stage, case.candidate_path)
    shutil.copyfile(case.stage, case.run / "candidate.json")
    shutil.copyfile(case.stage, case.failed_call / "record.json")
    data = read(case.stage.parent / "result.json")["data"]
    answer = {"submission_id": data["submission_id"]}
    write_new_json(case.failed_call / "answer.json", answer)
    result = read(case.failed_call / "result.json")
    result.update(status="schema_valid_candidate", submission_id=data["submission_id"])
    rewrite(case.failed_call / "result.json", result)
    terminal = read(case.run / "result.json")
    terminal["last_candidate_version"] = case.candidate_path.name
    rewrite(case.run / "result.json", terminal)
    final = {"direction": "receive", "message": {"method": "item/completed", "params": {
        "threadId": "failed-thread", "turnId": "failed-turn", "item": {
            "type": "agentMessage", "phase": "final_answer", "text": json.dumps(answer)}}}}
    save_events(case, [*load_events(case), final, turn("turn/completed", "failed-thread", "failed-turn", "completed")])
    return case


def test_authenticates_completed_native_patch_without_reextracting(completed_native):
    c = completed_native
    before = {p: hash_file(p) for p in c.run.rglob("*") if p.is_file()}
    authenticated = authenticate_completed_revision(c.f.settings, c.candidate_path)
    assert authenticated.record.model_dump(mode="json") == read(c.stage)
    assert authenticated.baseline_record == c.baseline
    assert authenticated.receipt["requires_new_source_audit"] is True
    assert authenticated.receipt["scientific_validation"] == "not_established"
    assert c.events_path not in authenticated.input_paths
    assert before == {p: hash_file(p) for p in c.run.rglob("*") if p.is_file()}


def test_completed_revision_survives_a_later_crashed_review_log_tail(completed_native):
    c = completed_native
    with c.events_path.open("a", encoding="utf-8") as stream:
        stream.write('{"direction":"receive","message":')
    assert authenticate_completed_revision(c.f.settings, c.candidate_path).record.model_dump(mode="json") == read(c.stage)


def test_successful_audit_answer_cannot_be_bypassed_when_summary_write_failed(completed_native):
    c = completed_native
    call = c.run / "calls" / "new-audit"
    write_new_json(call / "request.json", {"request": {"stage": "audit_2"}})
    write_new_json(call / "result.json", {"status": "schema_valid_candidate"})
    write_new_json(call / "answer.json", {"potential_omissions": ["new scientific concern"]})
    assert not (c.run / "audit.v02.json").exists()
    with pytest.raises(ValueError, match="completed current or later"):
        authenticate_completed_revision(c.f.settings, c.candidate_path)


@pytest.mark.parametrize("change", ["candidate", "public_answer", "call_answer", "failed_turn", "model",
                                    "later_candidate", "current_audit", "patch_receipt", "source"])
def test_rejects_tampering_and_unfinished_delivery(completed_native, change):
    c = completed_native
    if change == "candidate":
        c.candidate_path.write_bytes(c.candidate_path.read_bytes() + b" ")
    elif change == "public_answer":
        events = load_events(c)
        events[-2]["message"]["params"]["item"]["text"] = '{"submission_id":"wrong"}'
        save_events(c, events)
    elif change == "call_answer":
        rewrite(c.failed_call / "answer.json", {"submission_id": "wrong"})
    elif change == "failed_turn":
        events = load_events(c)
        events[-1]["message"]["params"]["turn"]["status"] = "failed"
        save_events(c, events)
    elif change == "model":
        result = read(c.failed_call / "result.json")
        result["server_selected_model"] = "different"
        rewrite(c.failed_call / "result.json", result)
    elif change == "later_candidate":
        shutil.copyfile(c.candidate_path, c.run / "candidate.v03.json")
    elif change == "current_audit":
        shutil.copyfile(c.run / "audit.v01.json", c.run / "audit.v02.json")
    elif change == "patch_receipt":
        result = read(c.stage.parent / "result.json")
        result["data"]["published"] = True
        rewrite(c.stage.parent / "result.json", result)
    else:
        path = c.run / "sources.json"
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises((ValueError, KeyError)):
        authenticate_completed_revision(c.f.settings, c.candidate_path)
