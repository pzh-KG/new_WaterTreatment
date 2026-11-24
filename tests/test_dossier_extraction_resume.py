"""Synthetic completed comprehension / failed extraction, without live services."""
import json
import shutil
from pathlib import Path

import pytest

from test_workflow import workspace, read_json
from test_active_reading import active, FakeReader
from watertreatment.dossier_extraction_resume import select, prepare_selected, execute_selected
from watertreatment.identity_resume import bind_dossier_identity
from watertreatment.domain import CoverageEntry
from watertreatment.reading import ReadingDossier
from watertreatment.storage import DataStore, digest_json, hash_file, write_new_json


def rewrite(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def record_failed_extraction(run, plan, seconds=20.5, *, calls=2):
    call = run / "calls" / "extraction"
    call.mkdir(parents=True)
    request = {"stage": "extraction", "model": "gpt-6.2-sol", "reasoning_effort": "max",
        "sources": plan["input_paths"], "code_identity": plan["code_identity"], "rule_version": plan["rule_version"],
        "prompt": "Synthetic source-bound extraction attempt", "schema": {"type": "object"}}
    write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    write_new_json(call / "result.json", {"status": "failed", "elapsed_seconds": seconds,
        "server_selected_model": "gpt-6.2-sol", "server_selected_effort": "max", "effective_model": None,
        "error": "SyntheticExtractionFailure: no candidate delivered"})
    events = run / "app_server/events.jsonl"
    events.parent.mkdir(exist_ok=True)
    with events.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": "2026-01-01T00:00:00+00:00", "direction": "send",
            "message": {"id": 101, "method": "turn/start", "params": {"threadId": "extract-thread",
                "model": "gpt-6.2-sol", "effort": "max", "input": [{"type": "text", "text": request["prompt"]}],
                "outputSchema": request["schema"]}}}) + "\n")
    write_new_json(run / "result.json", {"status": "reading_incomplete_failure", "model_calls": calls,
        "human_validated": False, "graph_published": False, "last_candidate_version": None})
    write_new_json(run / "failure.json", {"error": "SyntheticExtractionFailure: no candidate delivered", "model_calls": calls})


def make_dossier_parent(active, *, structural=False):
    ws, run, bundle, plan = active
    reader = FakeReader(ws.settings, bundle)
    # A normal controller ID binding must remain acceptable and replayable.
    raw = ReadingDossier(paper_id="doi-is-not-system-id", article_question_and_design="中文 synthetic design",
        material_map=[], experiment_map=[], coverage=reader.coverage, cross_source_conflicts=[],
        unresolved_questions=["Author may not report a condition."], schema_gaps=[])
    if structural:
        raw.coverage.append(CoverageEntry(source_id=bundle["documents"][0]["source"]["source_id"],
            block_ids=["invalid-structural-placeholder"], state="out_of_scope"))
    dossier = bind_dossier_identity(raw, plan["paper_id"], run)
    write_new_json(run / "reading_dossier.json", dossier.model_dump(mode="json"))
    if structural:
        from watertreatment.coverage_correction import apply_structural_correction
        from watertreatment.workflow import _read_bundle
        _, documents = _read_bundle(run / "sources.json")
        apply_structural_correction(run, dossier, documents)
    call = run / "calls" / "comprehension"
    call.mkdir(parents=True)
    request = {"stage": "comprehension", "model": "gpt-6.2-sol", "reasoning_effort": "max",
        "sources": plan["input_paths"], "code_identity": plan["code_identity"], "rule_version": plan["rule_version"]}
    write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    write_new_json(call / "result.json", {"status": "schema_valid_candidate", "elapsed_seconds": 10.25,
        "server_selected_model": "gpt-6.2-sol", "server_selected_effort": "max", "thread_id": "thread", "turn_id": "understood"})
    write_new_json(call / "answer.json", raw.model_dump(mode="json"))
    events = [
        {"direction": "receive", "message": {"method": "turn/started", "params": {"threadId": "thread", "turn": {"id": "understood"}}}},
        {"direction": "receive", "message": {"method": "item/completed", "params": {"threadId": "thread", "turnId": "understood",
            "item": {"type": "agentMessage", "phase": "final_answer", "text": raw.model_dump_json()}}}},
        {"direction": "receive", "message": {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "understood", "status": "completed"}}}},
    ]
    (run / "app_server").mkdir()
    (run / "app_server/events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    record_failed_extraction(run, plan)
    return ws, run, bundle, plan


@pytest.fixture
def dossier_parent(active, request):
    return make_dossier_parent(active, structural=getattr(request, "param", None) == "structural")


def test_ordinary_unassigned_paper_retains_cohort_during_dossier_recovery(workspace):
    from watertreatment.settings import ReadingSettings
    from watertreatment.workflow import import_paper, prepare
    settings = workspace.settings
    settings.codex.model = "gpt-6.2-sol"
    settings.codex.max_calls_per_run = 7
    settings.reading = ReadingSettings(enabled=True)
    for stem in ("understand", "audit-reading", "revise-reading"):
        shutil.copyfile(Path(__file__).resolve().parents[1] / "prompts" / f"{stem}.v0.5.txt",
            workspace.project / "prompts" / f"{stem}.v0.5.txt")
    DataStore(settings.data_root).initialize()
    imported = import_paper(settings, workspace.folder, paper_id="ordinary-unassigned-source", render=True,
        source_roles={"main.pdf": "main_article", "support.docx": "supporting_information"})
    run = prepare(settings, imported / "sources.json", cohort_role="unassigned")
    ws, run, bundle, _ = make_dossier_parent((workspace, run, read_json(run / "sources.json"), read_json(run / "plan.json")))
    bounded, child = prepare_selected(settings, run, select(settings, run))
    assert read_json(child / "plan.json")["cohort_role"] == "unassigned"
    client = FakeReader(bounded, bundle)
    # FakeReader's synthetic candidate defaults to development independently of
    # the bundle; the ordinary input must retain its actual unspecified role.
    client.candidate.paper.id = bundle["paper_id"]
    client.candidate.paper.dataset_role = "unspecified"
    execute_selected(bounded, child / "plan.json", client=client)
    assert [stage for stage, _ in client.stages][:2] == ["extraction", "audit_0"]
    assert read_json(child / "candidate.json")["paper"]["dataset_role"] == "unspecified"


def test_completed_dossier_is_reused_without_repeating_comprehension(dossier_parent):
    ws, run, bundle, plan = dossier_parent
    originals = {p: hash_file(p) for p in run.rglob("*") if p.is_file()}
    selected = select(ws.settings, run)
    assert selected["budget"]["used_calls"] == 2
    assert selected["budget"]["used_seconds"] == 30.75
    assert selected["budget"]["remaining_calls"] == 5
    assert selected["budget"]["remaining_seconds"] == 7169
    bounded, new = prepare_selected(ws.settings, run, selected)
    new_plan = read_json(new / "plan.json")
    assert new_plan["reused_dossier"] and not new_plan.get("reused_candidate")
    assert read_json(new / "reading_dossier.json") == read_json(run / "reading_dossier.json")
    client = FakeReader(bounded, bundle)
    execute_selected(bounded, new / "plan.json", client=client)
    assert [stage for stage, _ in client.stages][:2] == ["extraction", "audit_0"]
    assert "comprehension" not in [stage for stage, _ in client.stages]
    assert all(hash_file(p) == checksum for p, checksum in originals.items())
    with pytest.raises(ValueError, match="已有恢复计划"):
        select(ws.settings, run)


@pytest.mark.parametrize("dossier_parent", ["structural"], indirect=True)
def test_controller_structural_correction_does_not_disqualify_completed_model_notes(dossier_parent):
    ws, run, bundle, _ = dossier_parent
    original = hash_file(run / "reading_dossier.json")
    bounded, new = prepare_selected(ws.settings, run, select(ws.settings, run))
    client = FakeReader(bounded, bundle)
    execute_selected(bounded, new / "plan.json", client=client)
    assert "comprehension" not in [stage for stage, _ in client.stages]
    assert hash_file(run / "reading_dossier.json") == original
    assert (new / "reading_structure_correction.v01.json").is_file()


def test_repeated_failed_extraction_counts_full_lineage_without_repeating_reading(dossier_parent):
    ws, run, bundle, _ = dossier_parent
    bounded, new = prepare_selected(ws.settings, run, select(ws.settings, run))
    new_plan = read_json(new / "plan.json")
    record_failed_extraction(new, new_plan, 21.5, calls=1)
    selected = select(ws.settings, new)
    assert selected["dossier_path"] == str(run / "reading_dossier.json")
    assert selected["budget"]["used_seconds"] == 52.25
    assert selected["budget"]["used_calls"] == 3
    assert selected["budget"]["remaining_seconds"] == 7147
    assert selected["budget"]["remaining_calls"] == 4
    _, second = prepare_selected(ws.settings, new, selected)
    record_failed_extraction(second, read_json(second / "plan.json"), 11.25, calls=1)
    third_selection = select(ws.settings, second)
    assert third_selection["budget"]["used_calls"] == 4
    assert third_selection["budget"]["used_seconds"] == 63.5
    assert third_selection["budget"]["remaining_calls"] == 3
    bounded3, third = prepare_selected(ws.settings, second, third_selection)
    client = FakeReader(bounded3, bundle)
    execute_selected(bounded3, third / "plan.json", client=client)
    assert [s for s, _ in client.stages][:2] == ["extraction", "audit_0"]


@pytest.mark.parametrize("defect", ["source", "public_answer", "public_completion", "dossier", "raw_binding",
    "model", "elapsed", "negative_elapsed", "call_count", "request_fingerprint", "frozen_prompt", "candidate",
    "staged_candidate", "delivered_answer", "unknown_call", "insufficient_budget", "failed_start", "failed_model"])
def test_unproven_reading_unknown_usage_and_later_content_are_never_bypassed(dossier_parent, defect):
    ws, run, bundle, plan = dossier_parent
    if defect == "source":
        path = Path(bundle["documents"][0]["source"]["path"]); path.write_bytes(path.read_bytes() + b"change")
    elif defect in {"public_answer", "public_completion", "failed_start"}:
        path = run / "app_server/events.jsonl"
        value = path.read_text(encoding="utf-8")
        value = value.replace("synthetic design", "changed design") if defect == "public_answer" else value.replace("turn/start", "not-started") if defect == "failed_start" else ""
        path.write_text(value, encoding="utf-8")
    elif defect in {"dossier", "raw_binding"}:
        path = run / ("reading_dossier.json" if defect == "dossier" else "reading_dossier.model.json")
        value = read_json(path); value["article_question_and_design"] = "Changed science"; rewrite(path, value)
    elif defect in {"model", "failed_model", "elapsed", "negative_elapsed"}:
        path = run / ("calls/comprehension/result.json" if defect == "model" else "calls/extraction/result.json")
        value = read_json(path)
        value["server_selected_model" if defect in {"model", "failed_model"} else "elapsed_seconds"] = "other-model" if defect in {"model", "failed_model"} else None if defect == "elapsed" else -1
        rewrite(path, value)
    elif defect == "call_count":
        path = run / "failure.json"; value = read_json(path); value["model_calls"] = 1; rewrite(path, value)
    elif defect == "request_fingerprint":
        path = run / "calls/extraction/request.json"; value = read_json(path); value["fingerprint"] = "different"; rewrite(path, value)
    elif defect == "frozen_prompt":
        path = run / "understand_prompt.txt"; path.write_text("Changed instructions", encoding="utf-8")
    elif defect == "insufficient_budget":
        plan["max_calls"] = 3; rewrite(run / "plan.json", plan)
        DataStore(ws.data).event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    else:
        path = {"candidate": run / "candidate.v00.json", "staged_candidate": run / "reading_tools/tool-0001/submitted_record.json",
            "delivered_answer": run / "calls/extraction/answer.json", "unknown_call": run / "calls/extra/request.json"}[defect]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    with pytest.raises((ValueError, OSError, KeyError)):
        select(ws.settings, run)


def test_prepared_continuation_reauthenticates_before_model_execution(dossier_parent, monkeypatch):
    ws, run, _, _ = dossier_parent
    bounded, new = prepare_selected(ws.settings, run, select(ws.settings, run))
    path = run / "calls/extraction/result.json"
    value = read_json(path); value["elapsed_seconds"] += 1; rewrite(path, value)
    monkeypatch.setattr("watertreatment.workflow.execute_plan", lambda *a, **kw: pytest.fail("provider reached"))
    with pytest.raises(ValueError, match="已变化"):
        execute_selected(bounded, new / "plan.json", client=object())


def test_newer_executed_run_prevents_return_to_older_dossier(dossier_parent):
    ws, run, _, _ = dossier_parent
    store = DataStore(ws.data)
    newer = store.new_run("extraction", {"paper_id": read_json(run / "plan.json")["paper_id"]})
    store.event(newer.name, "execution_started", {})
    with pytest.raises(ValueError, match="更晚"):
        select(ws.settings, run)


def test_stale_selection_never_prepares_or_resets_budget(dossier_parent):
    ws, run, _, _ = dossier_parent
    selected = select(ws.settings, run)
    selected["budget"]["remaining_calls"] += 1
    before = DataStore(ws.data).runs()
    with pytest.raises(ValueError, match="已改变"):
        prepare_selected(ws.settings, run, selected)
    assert DataStore(ws.data).runs() == before


def test_direct_core_execution_still_authenticates_the_original_chain(dossier_parent):
    from watertreatment.workflow import execute_plan
    ws, run, bundle, _ = dossier_parent
    bounded, new = prepare_selected(ws.settings, run, select(ws.settings, run))
    path = run / "calls/extraction/result.json"
    value = read_json(path); value["elapsed_seconds"] += 1; rewrite(path, value)
    client = FakeReader(bounded, bundle)
    with pytest.raises(ValueError, match="已变化"):
        execute_plan(bounded, new / "plan.json", client=client)
    assert client.calls_started == 0
    assert not (new / "execution_started.json").exists()


def test_unreserved_prepared_child_cannot_bypass_budget_via_core(dossier_parent, monkeypatch):
    from watertreatment import dossier_extraction_resume as recovery
    from watertreatment.workflow import execute_plan
    ws, run, bundle, _ = dossier_parent
    selected = select(ws.settings, run)
    original = recovery._reservation
    def lose_reservation(store, parent, current=None, *, reserve=False):
        if reserve:
            raise ValueError("Synthetic concurrent reservation lost")
        return original(store, parent, current)
    monkeypatch.setattr(recovery, "_reservation", lose_reservation)
    previous = {r["run_id"] for r in DataStore(ws.data).runs()}
    with pytest.raises(ValueError, match="reservation lost"):
        prepare_selected(ws.settings, run, selected)
    created = [r for r in DataStore(ws.data).runs() if r["run_id"] not in previous]
    assert len(created) == 1
    new = ws.data / "runs" / created[0]["run_id"]
    plan = read_json(new / "plan.json")
    bounded = ws.settings.model_copy(deep=True)
    bounded.codex = bounded.codex.model_validate(plan["model_settings"])
    bounded.reading = bounded.reading.model_validate(plan["reading_settings"])
    client = FakeReader(bounded, bundle)
    with pytest.raises(ValueError, match="没有实际取得"):
        execute_plan(bounded, new / "plan.json", client=client)
    assert client.calls_started == 0
    assert not (new / "execution_started.json").exists()
