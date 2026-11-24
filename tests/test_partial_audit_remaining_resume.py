"""Source-bound report continuation must charge every prior failed call."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_web_records import fixture, read
from test_partial_audit_completion_workflow import case, SyntheticClient
from test_audit_addendum_workflow import rewrite
from watertreatment import partial_audit_completion_workflow as original
from watertreatment.partial_audit_completion import build_partial_audit_completion_scope
from watertreatment.partial_audit_remaining_resume import (
    select, prepare, execute, authenticate_failed_completion, verify_remaining_completion_run)
from watertreatment.reading import ReadingAudit
from watertreatment.reading_tools import ReadingTools
from watertreatment.source_review import partial_source_review_progress
from watertreatment.storage import hash_file, write_new_json


@pytest.fixture
def timed_out(case, monkeypatch):
    parent = case.parent
    tools = ReadingTools(parent.bundle, parent.run, max_calls=10,
        expected_inputs=read(parent.run / "plan.json")["input_paths"])
    tools.begin_source_review(parent.full_scope, call_id=parent.call.name)
    assert tools.handle("wt_read_blocks", {"source_id": "main", "start_order": 1, "end_order": 1})["success"]
    progress = partial_source_review_progress(parent.run, parent.full_scope,
        original._source_receipts(parent.run, parent.call.name), parent.call.name)
    scope = build_partial_audit_completion_scope(parent.full_scope, parent.audit, old_record=parent.old_record,
        new_record=parent.record, baseline_audit=parent.baseline, source_progress=progress)
    assert not scope["source_anchors"]
    case.parent = replace(parent, progress=progress, scope=scope)
    monkeypatch.setattr(original, "authenticate_partial_audit_parent", lambda *a, **kw: case.parent)
    # Construct a fully frozen source-bound attempted call, then simulate its
    # terminal timeout rather than using a provider. All files here are fixtures.
    path = original.prepare(case.f.settings, parent.run, timeout_seconds=120)
    client = SyntheticClient(read(path))
    with pytest.raises(RuntimeError, match="synthetic timeout"):
        def timeout_complete(model, **kw):
            answer = SyntheticClient.complete(client, model, **kw)
            call = path.parent / "calls" / "call-01-synthetic"
            (call / "answer.json").unlink()
            completed = read(call / "result.json")
            completed.update(status="failed", error="TimeoutError: synthetic timeout", elapsed_seconds=120.5)
            rewrite(call / "result.json", completed)
            events = {"direction": "receive", "message": {"method": "turn/started",
                "params": {"threadId": "thread", "turn": {"id": "turn", "status": "inProgress"}}}}
            (path.parent / "app_server" / "events.jsonl").write_text(json.dumps(events) + "\n", encoding="utf-8")
            raise RuntimeError("synthetic timeout")
        client.complete = timeout_complete
        original.execute(case.f.settings, path, client=client)
    # Terminal type is a timeout in the real client and must match its call.
    rewrite(path.parent / "failure.json", {"error": "TimeoutError: synthetic timeout", "model_calls": 1})
    return SimpleNamespace(case=case, settings=case.f.settings, failed=path.parent)


def finish(timed_out, **kw):
    path = prepare(timed_out.settings, timed_out.failed, timeout_seconds=120)
    client = SyntheticClient(read(path), **kw)
    run = execute(timed_out.settings, path, client=client)
    assert client.closed
    return run


def test_remaining_resume_charges_failed_seconds_and_preserves_candidate(timed_out):
    before = {p: hash_file(p) for p in timed_out.failed.rglob("*") if p.is_file()}
    choice = select(timed_out.settings, timed_out.failed)
    budget = choice["budget"]
    assert budget["original_seconds"] == 600
    assert budget["used_seconds"] == 245.5
    assert budget["remaining_seconds"] == 354
    assert budget["used_calls"] == 2 and budget["remaining_calls"] == 1
    run = finish(timed_out)
    result = read(run / "result.json")
    assert result["budget_lineage"]["used_model_seconds"] == 248.5
    assert result["budget_lineage"]["used_model_calls"] == 3
    assert result["budget_lineage"]["remaining_model_calls"] == 0
    assert result["status"] == "candidate_incomplete_reading_coverage"
    assert result["stop_reason"] == "coverage_correction_required"
    assert result["human_validated"] is False and result["graph_published"] is False
    assert all(hash_file(p) == old for p, old in before.items())
    assert hash_file(run / "candidate.json") == hash_file(timed_out.failed / "candidate.v00.json")
    audit = ReadingAudit.model_validate(read(run / "model_review.json"))
    assert verify_remaining_completion_run(timed_out.settings, run, timed_out.case.parent.record, audit)
    assert timed_out.case.f.records._audit_provenance(run, timed_out.case.parent.record, result, audit.model_dump(mode="json"))


def test_remaining_resume_keeps_scientific_blockers(timed_out):
    run = finish(timed_out, blocker=True)
    result = read(run / "result.json")
    audit = ReadingAudit.model_validate(read(run / "model_review.json"))
    assert result["status"] == "candidate_incomplete_budget"
    assert not verify_remaining_completion_run(timed_out.settings, run, timed_out.case.parent.record, audit)


@pytest.mark.parametrize("timeout", [355, 900, 30, True])
def test_remaining_resume_cannot_expand_ancestral_budget(timed_out, timeout):
    with pytest.raises(ValueError, match="original scientific lineage"):
        prepare(timed_out.settings, timed_out.failed, timeout_seconds=timeout)


def test_remaining_resume_reserves_once_even_before_execution(timed_out):
    prepare(timed_out.settings, timed_out.failed)
    with pytest.raises(ValueError, match="retained reservation"):
        select(timed_out.settings, timed_out.failed)
    with pytest.raises(ValueError, match="retained reservation"):
        prepare(timed_out.settings, timed_out.failed)


def test_remaining_resume_rejects_sibling_completion(timed_out):
    parent = timed_out.case.parent
    timed_out.case.f.store.new_run("extraction", {"operation": original.POLICY, "parent_run": parent.run.name})
    with pytest.raises(ValueError, match="another reserved"):
        select(timed_out.settings, timed_out.failed)


def test_remaining_resume_rejects_newer_unrelated_run_instead_of_falling_back(timed_out):
    timed_out.case.f.store.new_run("extraction", {"paper_id": timed_out.case.parent.record.paper.id})
    with pytest.raises(ValueError, match="newer registered"):
        select(timed_out.settings, timed_out.failed)


def test_remaining_resume_requires_report_only_source_scope(timed_out, monkeypatch):
    # All frozen-input authentication remains required; this isolated boundary
    # tests that uncompleted source-reading work cannot masquerade as report-only.
    real_auth = original._authenticate_plan
    def with_missing_source(*a):
        records, run, plan, parent = real_auth(*a)
        changed = {**parent.scope, "source_anchors": [{"source_id": "main", "block_id": "paragraph"}]}
        return records, run, plan, replace(parent, scope=changed)
    monkeypatch.setattr(original, "_authenticate_plan", with_missing_source)
    with pytest.raises(ValueError, match="report-only"):
        select(timed_out.settings, timed_out.failed)


def test_remaining_resume_rechecks_frozen_failure_before_spending_budget(timed_out):
    path = prepare(timed_out.settings, timed_out.failed, timeout_seconds=120)
    result_path = next((timed_out.failed / "calls").glob("*/result.json"))
    changed = read(result_path); changed["elapsed_seconds"] += 1; rewrite(result_path, changed)
    client = SyntheticClient(read(path))
    with pytest.raises(ValueError, match="frozen lineage"):
        execute(timed_out.settings, path, client=client)
    assert client.calls_started == 0


@pytest.mark.parametrize("elapsed", [None, -1, True, float('nan'), 10, 500])
def test_remaining_resume_rejects_unusable_elapsed(timed_out, elapsed):
    call = next((timed_out.failed / "calls").glob("*/result.json"))
    data = read(call); data["elapsed_seconds"] = elapsed; rewrite(call, data)
    with pytest.raises(ValueError, match="budget|remaining"):
        select(timed_out.settings, timed_out.failed)


def test_remaining_resume_rejects_retained_answer_instead_of_discarding_it(timed_out):
    call = next((timed_out.failed / "calls").glob("*/result.json")).parent
    write_new_json(call / "answer.json", {"findings": []})
    with pytest.raises(ValueError, match="authenticated timeout"):
        select(timed_out.settings, timed_out.failed)


def test_remaining_resume_rejects_completed_public_turn(timed_out):
    events = timed_out.failed / "app_server" / "events.jsonl"
    with events.open('a', encoding='utf-8') as f:
        f.write(json.dumps({"direction": "receive", "message": {"method": "turn/completed", "params": {
            "threadId": "thread", "turn": {"id": "turn", "status": "completed"}}}}) + '\n')
    with pytest.raises(ValueError, match="completed public turn"):
        select(timed_out.settings, timed_out.failed)


@pytest.mark.parametrize("name", ["candidate.v00.json", "partial_completion_scope.json", "reading_dossier.json"])
def test_remaining_resume_rejects_changed_frozen_inputs(timed_out, name):
    (timed_out.failed / name).write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError):
        select(timed_out.settings, timed_out.failed)


@pytest.mark.parametrize("name", ["result.json", "partial_remaining_proof.json", "partial_resume_receipt.json",
    "combined_source_review_proof.json", "candidate.json", "model_review.json"])
def test_remaining_resume_admission_rejects_artifact_tamper(timed_out, name):
    run = finish(timed_out)
    audit = ReadingAudit.model_validate(read(run / "model_review.json"))
    (run / name).write_text('{}', encoding='utf-8')
    assert not verify_remaining_completion_run(timed_out.settings, run, timed_out.case.parent.record, audit)


def test_remaining_resume_failed_query_retained_and_not_retryable(timed_out):
    path = prepare(timed_out.settings, timed_out.failed, timeout_seconds=120)
    with pytest.raises(ValueError, match="every exact required"):
        execute(timed_out.settings, path, client=SyntheticClient(read(path), skip_query=True))
    assert (path.parent / "failure.json").is_file()
    with pytest.raises(ValueError, match="retained reservation"):
        prepare(timed_out.settings, timed_out.failed)


def test_repeated_report_timeout_replays_each_ancestor_without_restoring_spent_calls(timed_out):
    path = prepare(timed_out.settings, timed_out.failed, timeout_seconds=120)
    client = SyntheticClient(read(path))
    def timeout_complete(model, **kwargs):
        SyntheticClient.complete(client, model, **kwargs)
        call = path.parent / "calls/call-01-synthetic"
        (call / "answer.json").unlink()
        completed = read(call / "result.json")
        completed.update(status="failed", error="TimeoutError: second synthetic timeout", elapsed_seconds=120.5)
        rewrite(call / "result.json", completed)
        events = {"direction": "receive", "message": {"method": "turn/started", "params": {
            "threadId": "thread", "turn": {"id": "turn", "status": "inProgress"}}}}
        (path.parent / "app_server/events.jsonl").write_text(json.dumps(events) + "\n", encoding="utf-8")
        raise TimeoutError("second synthetic timeout")
    client.complete = timeout_complete
    with pytest.raises(TimeoutError):
        execute(timed_out.settings, path, client=client)
    _, failed, _, parent, receipt = authenticate_failed_completion(timed_out.settings, path.parent, allow_exhausted=True)
    assert failed == path.parent and parent.run == timed_out.case.parent.run
    assert receipt["budget"]["used_seconds"] == 366
    assert receipt["budget"]["used_calls"] == 3
    assert receipt["budget"]["remaining_calls"] == 0
    # Source authentication can be replayed for a future explicit grant; it is
    # not by itself authority to issue another model call.
    with pytest.raises(ValueError, match="remaining"):
        prepare(timed_out.settings, path.parent)
