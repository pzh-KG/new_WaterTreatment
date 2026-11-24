"""Offline initial/identity recovery routes; all source data are synthetic."""
import json
from pathlib import Path

import pytest

from test_workflow import workspace, read_json
from test_active_reading import active, FakeReader
from test_identity_resume import identity_parent
from watertreatment.storage import DataStore, digest_json, hash_file, write_new_json
from watertreatment.web_initial_recovery import (
    select_initial, select_identity, prepare_selected, execute_selected)


def rewrite(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def initial_parent(active):
    ws, run, bundle, plan = active
    prompt = (run / "understand_prompt.txt").read_text(encoding="utf-8")
    size = {"characters": len(prompt), "utf8_bytes": len(prompt.encode("utf-8")), "max_utf8_bytes": 1}
    error = (f"ModelCallError: prompt exceeds conservative UTF-8 budget 1 bytes: "
             f"{size['utf8_bytes']} bytes / {size['characters']} characters; input was not truncated")
    request = {"stage": "comprehension", "model": "gpt-6.2-sol", "reasoning_effort": "max",
        "prompt": prompt, "prompt_size": size, "code_identity": plan["code_identity"],
        "rule_version": plan["rule_version"], "sources": [*plan["input_paths"], *plan["reading_artifacts"]]}
    call = run / "calls" / "preflight"
    call.mkdir(parents=True)
    write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    write_new_json(call / "result.json", {"status": "failed", "error": error, "elapsed_seconds": 0,
        "effective_model": None, "usage": [], "prompt_size": size})
    write_new_json(run / "result.json", {"status": "reading_incomplete_failure", "model_calls": 0,
        "last_candidate_version": None, "human_validated": False, "graph_published": False})
    write_new_json(run / "failure.json", {"error": error, "model_calls": 0})
    return ws, run, bundle, plan


def test_initial_selection_is_readonly_and_does_not_claim_model_reading(initial_parent):
    ws, run, _, plan = initial_parent
    store = DataStore(ws.data)
    before = {p: hash_file(p) for p in run.rglob("*") if p.is_file()}
    runs = store.runs()
    selected = select_initial(ws.settings, run)
    assert selected["route"] == "initial_reading"
    assert selected["budget"]["remaining_seconds"] == plan["reading_settings"]["total_timeout_seconds"]
    assert selected["budget"]["remaining_calls"] == plan["max_calls"]
    assert selected["budget"]["used_calls"] == 0
    assert not selected["candidate_reused"] and not selected["scientific_audit_inherited"]
    assert store.runs() == runs
    assert before == {p: hash_file(p) for p in run.rglob("*") if p.is_file()}


@pytest.mark.parametrize("defect", ["elapsed", "provider", "usage", "fingerprint", "prompt_size",
                                    "candidate", "dossier", "answer", "source", "events"])
def test_initial_route_rejects_uncertain_or_changed_evidence(initial_parent, defect):
    ws, run, bundle, _ = initial_parent
    result_path = run / "calls/preflight/result.json"
    result = read_json(result_path)
    if defect == "elapsed": result["elapsed_seconds"] = 1
    elif defect == "provider": result["thread_id"] = "actually-started"
    elif defect == "usage": result["usage"] = [{"input_tokens": 10}]
    elif defect == "prompt_size": result["prompt_size"]["utf8_bytes"] += 1
    elif defect == "fingerprint":
        p = run / "calls/preflight/request.json"; q = read_json(p); q["fingerprint"] = "changed"; rewrite(p, q)
    elif defect == "source":
        p = Path(bundle["documents"][0]["source"]["path"]); p.write_bytes(p.read_bytes() + b"changed")
    else:
        path = {"candidate": run / "candidate.v00.json", "dossier": run / "reading_dossier.json",
                "answer": run / "calls/preflight/answer.json", "events": run / "app_server/events.jsonl"}[defect]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    rewrite(result_path, result)
    with pytest.raises((ValueError, OSError)):
        select_initial(ws.settings, run)


@pytest.mark.parametrize("entry", ["web", "core"])
def test_initial_route_prepares_same_import_and_executes_unstarted_stages(initial_parent, entry):
    ws, run, bundle, _ = initial_parent
    selected = select_initial(ws.settings, run)
    originals = {p: hash_file(p) for p in run.rglob("*") if p.is_file()}
    bounded, new = prepare_selected(ws.settings, run, selected)
    plan = read_json(new / "plan.json")
    assert plan["initial_recovery"] and not plan.get("reused_candidate") and not plan.get("reused_dossier")
    assert read_json(new / "sources.json") == bundle
    assert read_json(new / "manifest.json")["source_bundle"] == selected["bundle_path"]
    client = FakeReader(bounded, bundle)
    from watertreatment.workflow import execute_plan
    executor = execute_selected if entry == "web" else execute_plan
    executor(bounded, new / "plan.json", client=client)
    stages = [stage for stage, _ in client.stages]
    assert stages[:3] == ["comprehension", "extraction", "audit_0"]
    assert stages.count("comprehension") == stages.count("extraction") == 1
    assert all(hash_file(p) == h for p, h in originals.items())
    with pytest.raises(ValueError, match="预算已由"):
        select_initial(ws.settings, run)


def test_initial_route_rechecks_parent_and_never_calls_provider_after_tampering(initial_parent, monkeypatch):
    ws, run, _, _ = initial_parent
    bounded, new = prepare_selected(ws.settings, run, select_initial(ws.settings, run))
    path = run / "failure.json"
    rewrite(path, {"error": "different", "model_calls": 0})
    monkeypatch.setattr("watertreatment.workflow.execute_plan", lambda *a, **k: pytest.fail("provider path reached"))
    with pytest.raises(ValueError):
        execute_selected(bounded, new / "plan.json", client=object())


def test_initial_route_stale_selection_does_not_prepare(initial_parent):
    ws, run, _, _ = initial_parent
    selected = select_initial(ws.settings, run)
    selected["budget"]["remaining_seconds"] += 1
    count = len(DataStore(ws.data).runs())
    with pytest.raises(ValueError, match="恢复点"):
        prepare_selected(ws.settings, run, selected)
    assert len(DataStore(ws.data).runs()) == count


def test_direct_core_initial_recovery_replays_parent_proof(initial_parent):
    from watertreatment.workflow import execute_plan

    ws, run, bundle, _ = initial_parent
    bounded, new = prepare_selected(ws.settings, run, select_initial(ws.settings, run))
    rewrite(run / "failure.json", {"error": "changed original failure", "model_calls": 0})
    client = FakeReader(bounded, bundle)
    with pytest.raises(ValueError):
        execute_plan(bounded, new / "plan.json", client=client)
    assert client.calls_started == 0
    assert not (new / "execution_started.json").exists()


def test_unreserved_initial_plan_cannot_execute_via_core(initial_parent, monkeypatch):
    from watertreatment import web_initial_recovery as recovery
    from watertreatment.workflow import execute_plan

    ws, run, bundle, _ = initial_parent
    selected = select_initial(ws.settings, run)
    original = recovery._reservation

    def lose_reservation(store, parent, current=None, *, reserve=False):
        if reserve:
            raise ValueError("Synthetic preparation reservation lost")
        return original(store, parent, current)

    before = {row["run_id"] for row in DataStore(ws.data).runs()}
    monkeypatch.setattr(recovery, "_reservation", lose_reservation)
    with pytest.raises(ValueError, match="reservation lost"):
        prepare_selected(ws.settings, run, selected)
    monkeypatch.setattr(recovery, "_reservation", original)
    created = [row for row in DataStore(ws.data).runs() if row["run_id"] not in before]
    assert len(created) == 1
    new = ws.data / "runs" / created[0]["run_id"]
    plan = read_json(new / "plan.json")
    bounded = ws.settings.model_copy(deep=True)
    bounded.codex = bounded.codex.model_validate(plan["model_settings"])
    bounded.reading = bounded.reading.model_validate(plan["reading_settings"])
    client = FakeReader(bounded, bundle)
    with pytest.raises(ValueError, match="尚未持有"):
        execute_plan(bounded, new / "plan.json", client=client)
    assert client.calls_started == 0
    assert not (new / "execution_started.json").exists()


def test_competing_initial_reservation_cannot_be_ignored_by_core(initial_parent):
    from watertreatment.workflow import execute_plan

    ws, run, bundle, _ = initial_parent
    bounded, new = prepare_selected(ws.settings, run, select_initial(ws.settings, run))
    store = DataStore(ws.data)
    sibling = store.new_run("extraction", {"paper_id": read_json(run / "plan.json")["paper_id"]})
    store.event(sibling.name, "initial_recovery_prepared", {"parent_run": run.name})
    client = FakeReader(bounded, bundle)
    with pytest.raises(ValueError, match="预算已由"):
        execute_plan(bounded, new / "plan.json", client=client)
    assert client.calls_started == 0
    assert not (new / "execution_started.json").exists()


def test_identity_route_authenticates_then_only_executes_first_audit(identity_parent):
    c = identity_parent
    selected = select_identity(c.workspace.settings, c.run)
    assert selected["route"] == "identity_candidate" and selected["candidate_reused"]
    assert selected["budget"]["remaining_calls"] == 5
    assert selected["budget"]["remaining_seconds"] == 7169
    bounded, new = prepare_selected(c.workspace.settings, c.run, selected)
    old = c.candidate.model_dump(mode="json")
    new_candidate = read_json(new / "reused_candidate.json")
    new_candidate["paper"]["id"] = old["paper"]["id"]
    assert new_candidate == old
    client = FakeReader(bounded, c.bundle)
    client.total_timeout_seconds = bounded.reading.total_timeout_seconds
    execute_selected(bounded, new / "plan.json", client=client)
    assert client.stages == [("audit_0", "audit-0")]
    with pytest.raises(ValueError, match="already belongs"):
        select_identity(c.workspace.settings, c.run)


def test_identity_route_wont_accept_unregistered_import(identity_parent):
    c = identity_parent
    manifest = read_json(c.run / "manifest.json")
    path = Path(manifest["source_bundle"])
    rewrite(path, {**c.bundle, "paper_id": "another-paper"})
    with pytest.raises(ValueError, match="导入快照"):
        select_identity(c.workspace.settings, c.run)
