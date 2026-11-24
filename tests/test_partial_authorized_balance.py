"""Previously granted resources remain spendable after a smaller local timeout.

Only the older scientific ancestor fixture is isolated. New grants, call
protocols, candidate queries, source scopes, accounting and admission are real.
"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_web_records import fixture, read
from test_partial_audit_completion_workflow import case, SyntheticClient
from test_audit_addendum_workflow import rewrite
from watertreatment import partial_audit_completion_workflow as original
from watertreatment import partial_audit_remaining_resume as remaining
from watertreatment.budget_extension_workflow import select_checkpoint, prepare_extension
from watertreatment.budget_grants import (preview_grant, reserve_grant, remaining_authority,
    _events, RESERVED, BOUND)
from watertreatment.partial_audit_completion import build_partial_audit_completion_scope
from watertreatment.reading import ReadingAudit
from watertreatment.reading_tools import ReadingTools
from watertreatment.source_review import partial_source_review_progress
from watertreatment.storage import digest_json, hash_file, write_new_json
from watertreatment.web_recovery import RecoveryWorkflow


def timeout_client(path, seconds):
    client = SyntheticClient(read(path))
    def complete(model, **kw):
        SyntheticClient.complete(client, model, **kw)
        call = path.parent / "calls/call-01-synthetic"
        (call / "answer.json").unlink()
        result = read(call / "result.json")
        result.update(status="failed", error="TimeoutError: synthetic deadline", elapsed_seconds=seconds)
        rewrite(call / "result.json", result)
        event = {"direction": "receive", "message": {"method": "turn/started", "params": {
            "threadId": "thread", "turn": {"id": "turn", "status": "inProgress"}}}}
        (path.parent / "app_server/events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
        raise TimeoutError("synthetic deadline")
    client.complete = complete
    return client


@pytest.fixture
def granted_timeout(case, monkeypatch):
    f, parent = case.f, case.parent
    plan = read(f.run / "plan.json")
    plan.update(rule_version="0.7", max_calls=3,
        model_settings={**f.settings.codex.model_dump(mode="json"), "max_calls_per_run": 3},
        reading_settings={"enabled": True, "total_timeout_seconds": 600, "max_revisions": 0})
    rewrite(f.run / "plan.json", plan)
    f.store.event(f.run.name, "prepared", {"plan_sha256": hash_file(f.run / "plan.json")})
    # Retained synthetic root calls account for 479.5s and 2 calls. The first
    # 120.5s timeout then consumes its full 600s / 3-call original authority.
    for call, elapsed in ((f.run / "calls/audit", 354.5), (parent.call, 125)):
        request = read(call / "request.json")["request"] if (call / "request.json").exists() else {"stage": "audit_0"}
        request.update(model="gpt-6.2-sol", reasoning_effort="max")
        rewrite(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
        result = read(call / "result.json"); result["elapsed_seconds"] = elapsed
        rewrite(call / "result.json", result)
    tools = ReadingTools(parent.bundle, parent.run, max_calls=10, expected_inputs=plan["input_paths"])
    tools.begin_source_review(parent.full_scope, call_id=parent.call.name)
    assert tools.handle("wt_read_blocks", {"source_id": "main", "start_order": 1, "end_order": 1})["success"]
    progress = partial_source_review_progress(parent.run, parent.full_scope,
        original._source_receipts(parent.run, parent.call.name), parent.call.name)
    scope = build_partial_audit_completion_scope(parent.full_scope, parent.audit, old_record=parent.old_record,
        new_record=parent.record, baseline_audit=parent.baseline, source_progress=progress)
    receipt = {**parent.receipt, "used_model_seconds": 479.5, "used_model_calls": 2,
        "remaining_model_seconds": 120.5, "remaining_model_calls": 1}
    parent = replace(parent, progress=progress, scope=scope, receipt=receipt)
    monkeypatch.setattr(original, "authenticate_partial_audit_parent", lambda *a, **kw: parent)
    initial = original.prepare(f.settings, parent.run, timeout_seconds=120)
    with pytest.raises(TimeoutError):
        original.execute(f.settings, initial, client=timeout_client(initial, 120.5))
    policy = {"initial_seconds": 600, "initial_calls": 3, "addition_seconds": 1800, "addition_calls": 2,
        "max_auto_additions": 1, "max_total_seconds": 2400, "max_total_calls": 5}
    preview = preview_grant(f.settings, initial.parent, policy)
    assert preview["allocated_seconds"] == 1800 and preview["allocated_calls"] == 2
    selection = select_checkpoint(f.settings, initial.parent, preview)
    grant = reserve_grant(f.settings, initial.parent, preview, digest_json(selection))
    bounded, child, execute = prepare_extension(f.settings, grant, selection)
    assert read(child / "plan.json")["reading_settings"]["total_timeout_seconds"] == 900
    with pytest.raises(TimeoutError):
        execute(bounded, child / "plan.json", client=timeout_client(child / "plan.json", 900.5))
    return SimpleNamespace(f=f, settings=f.settings, failed=child, parent=parent,
        before_grants=_events(f.store, RESERVED), before_bindings=_events(f.store, BOUND))


def test_granted_local_timeout_uses_remaining_authority_without_new_grant(granted_timeout):
    c = granted_timeout
    source = {p: hash_file(p) for p in c.failed.rglob("*") if p.is_file()}
    flow = RecoveryWorkflow(SimpleNamespace(settings=c.settings, store=c.f.store))
    plan = read(c.failed / "plan.json")
    assert "resume_audit" in flow._stage_actions(c.failed, plan)
    choice = remaining.select(c.settings, c.failed)
    assert choice["route"] == "partial_resume"
    assert choice["receipt"]["budget"]["remaining_calls"] < 0
    assert choice["budget"]["seconds"] == 899 and choice["budget"]["remaining_calls"] == 1
    path = remaining.prepare(c.settings, c.failed)
    retained = read(path)
    assert "budget_continuation" not in retained and "budget_grant_preparation" not in retained
    assert read(path.parent / "partial_resume_receipt.json") == choice["receipt"]
    run = remaining.execute(c.settings, path, client=SyntheticClient(retained))
    result = read(run / "result.json")
    audit = ReadingAudit.model_validate(read(run / "model_review.json"))
    assert remaining.verify_remaining_completion_run(c.settings, run, c.parent.record, audit)
    assert c.f.records._audit_provenance(run, c.parent.record, result, audit.model_dump(mode="json"))
    assert result["authorized_balance_usage"]["new_grants"] == 0
    assert result["authorized_balance_usage"]["cumulative_used_seconds"] == 1503.5
    assert result["authorized_balance_usage"]["cumulative_used_calls"] == 5
    assert remaining_authority(c.settings, run)["remaining_calls"] == 0
    assert _events(c.f.store, RESERVED) == c.before_grants and _events(c.f.store, BOUND) == c.before_bindings
    assert all(hash_file(p) == sha for p, sha in source.items())


def test_remaining_authority_cap_and_frozen_balance_cannot_be_expanded(granted_timeout):
    c = granted_timeout
    with pytest.raises(ValueError, match="lineage budget"):
        remaining.prepare(c.settings, c.failed, timeout_seconds=900)
    path = remaining.prepare(c.settings, c.failed)
    balance = path.parent / "partial_remaining_balance.json"
    changed = read(balance)
    changed["allowance"]["remaining_seconds"] += 1
    rewrite(balance, changed)
    client = SyntheticClient(read(path))
    with pytest.raises(ValueError):
        remaining.execute(c.settings, path, client=client)
    assert client.calls_started == 0
    assert _events(c.f.store, RESERVED) == c.before_grants
