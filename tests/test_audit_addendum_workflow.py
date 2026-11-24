"""Offline workflow/source receipts; synthetic audits are not accuracy evidence."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_web_records import fixture, read
from test_scientific_revision_audit import report_for
from watertreatment.audit_addendum import build_audit_addendum_scope
from watertreatment.audit_addendum_workflow import (AddendumParent, _completed_call_protocol,
    prepare_audit_addendum, execute_audit_addendum, verify_audit_addendum_run)
from watertreatment.codex import source_snapshot
from watertreatment.reading import AUDIT_DIMENSIONS, ReadingAudit, ReadingDossier, AuditFinding, SourceAnchor
from watertreatment.scientific_revision_audit import build_scientific_revision_scope, NEW_SCOPE_LIMITS
from watertreatment.scope_policy import frozen_scope
from watertreatment.settings import CodexSettings
from watertreatment.sources import SourceDocument
from watertreatment.storage import digest_json, hash_file, write_new_json


def rewrite(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def events(run, answer, *, status="completed", mutation=False):
    items = [{"method": "turn/started", "params": {"threadId": "thread", "turn": {"id": "turn"}}}]
    if mutation:
        items.append({"method": "item/tool/call", "params": {"threadId": "thread", "turnId": "turn", "tool": "wt_patch_record"}})
    items += [{"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn",
        "item": {"type": "agentMessage", "phase": "final_answer", "text": json.dumps(answer)}}},
        {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": status}}}]
    folder = run / "app_server"
    folder.mkdir(exist_ok=True)
    (folder / "events.jsonl").write_text("".join(json.dumps({"direction": "receive", "message": x}) + "\n" for x in items), encoding="utf-8")


@pytest.fixture
def case(fixture, monkeypatch):
    f = fixture
    f.settings.rules_version = "0.7"
    f.settings.codex.model = "gpt-6.2-sol"
    old = f.record.model_copy(deep=True)
    old.rule_version, old.schema_version, old.extraction_scope = "0.7", "0.3.0", "research_core"
    old.paper.dataset_role = "development"
    for observation in old.observations:
        observation.analysis_role = "key_result"
    new = old.model_copy(deep=True)
    new.observations[0].value.status = "inferred"
    new.observations[0].origin = "model_inference"
    new.observations[0].limitations = ["Interpretation uncertainty retained."]
    baseline = ReadingAudit(findings=[], potential_omissions=[], checked_dimensions=list(AUDIT_DIMENSIONS),
        coverage_limitations=["Synthetic retained limitation"])
    full = build_scientific_revision_scope(old, new, baseline, baseline_origin="fresh_model_audit", **NEW_SCOPE_LIMITS)
    assert full
    parent = report_for(full, baseline)
    for finding in parent.findings:
        if finding.record_path == "observations[0]":
            finding.evidence = []
    scope = build_audit_addendum_scope(full, baseline, parent, old_record=old, new_record=new)
    assert scope
    bundle = read(f.run / "sources.json")
    dossier = ReadingDossier(paper_id=new.paper.id, article_question_and_design="Synthetic design",
        material_map=[], experiment_map=[], coverage=new.coverage,
        cross_source_conflicts=[], unresolved_questions=[], schema_gaps=[])
    parent_plan = {"cohort_role": "development", "rule_version": "0.7", "extraction_policy": frozen_scope(baseline=new)}
    receipt = {"policy": "bounded-audit-addendum-v1", "parent_run": f.run.name,
        "candidate_hash": digest_json(new.model_dump(mode="json")), "remaining_model_seconds": 1300,
        "remaining_model_calls": 2, "artifact_hashes": []}
    authenticated = AddendumParent(f.run, parent_plan, bundle,
        [SourceDocument.model_validate(d) for d in bundle["documents"]], new, dossier, old, baseline,
        parent, [], full, scope, receipt, 1300)
    # Isolate ancestry admission here; completed-call/source integrity below is
    # exercised with actual local tool receipts, not a mocked verifier.
    monkeypatch.setattr("watertreatment.audit_addendum_workflow.authenticate_addendum_parent", lambda *a: authenticated)
    return SimpleNamespace(f=f, parent=authenticated)


class SyntheticClient:
    def __init__(self, plan, *, blocker=False):
        self.settings = CodexSettings.model_validate(plan["model_settings"])
        self.total_timeout_seconds = plan["reading_settings"]["total_timeout_seconds"]
        self.calls_started, self.closed, self.blocker = 0, False, blocker
        self.events = None

    def bind_tools(self, tools):
        self.tools = tools

    def close(self):
        self.closed = True

    def complete(self, model, **kw):
        from watertreatment.citation_audit import _parts
        run = kw["workdir"]
        call = kw["audit_root"] / "call-01-synthetic"
        call.mkdir(parents=True)
        scope = read(Path(kw["identity"]["citation_review"]["scope_path"]))
        self.tools.begin_query_turn()
        self.tools.begin_source_review(scope, call_id=call.name)
        target = scope["targets"][0]["record_path"]
        assert self.tools.handle("wt_query_candidate", {"path": list(_parts(target))})["success"]
        assert self.tools.handle("wt_read_blocks", {"source_id": "main", "start_order": 1, "end_order": 1})["success"]
        binding = self.tools.candidate_binding
        request = {**kw["identity"], "model": "gpt-6.2-sol", "reasoning_effort": "max",
            "candidate_binding": binding, "prompt": kw["prompt"] + "\nCANDIDATE_SNAPSHOT " + binding["content_hash"],
            "sources": source_snapshot([*kw["input_paths"], Path(binding["path"])]),
            "source_review_call": call.name, "source_review_scope_sha256": scope["scope_sha256"]}
        write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
        finding = AuditFinding(record_path=target, record_paths=[target], severity="minor",
            verdict="unsupported" if self.blocker else "supported", needs_revision=self.blocker,
            explanation="Synthetic source-based finding", evidence=[SourceAnchor(source_id=a["source_id"], block_id=a["block_id"]) for a in scope["source_anchors"]])
        answer = model(scope_sha256=scope["scope_sha256"], parent_audit_hash=scope["parent_audit_hash"],
            candidate_hash=scope["candidate_hash"], findings=[finding])
        write_new_json(call / "answer.json", answer.model_dump(mode="json"))
        write_new_json(call / "result.json", {"status": "schema_valid_candidate", "thread_id": "thread", "turn_id": "turn",
            "server_selected_model": "gpt-6.2-sol", "server_selected_effort": "max", "elapsed_seconds": 1,
            "source_review_proof": self.tools.source_review_proof()})
        events(run, answer.model_dump(mode="json"))
        self.calls_started = 1
        return answer


def finish(case, *, blocker=False):
    path = prepare_audit_addendum(case.f.settings, case.f.run, timeout_seconds=120)
    client = SyntheticClient(read(path), blocker=blocker)
    run = execute_audit_addendum(case.f.settings, path, client=client)
    assert client.closed
    return run


def test_addendum_retains_parent_and_candidate_and_passes_real_source_provenance(case):
    before = case.parent.audit.model_dump(mode="json")
    run = finish(case)
    audit = ReadingAudit.model_validate(read(run / "model_review.json"))
    assert verify_audit_addendum_run(case.f.settings, run, case.parent.record, audit)
    assert audit.findings[:len(case.parent.audit.findings)] == case.parent.audit.findings
    assert case.parent.audit.model_dump(mode="json") == before
    assert hash_file(run / "candidate.json") == hash_file(run / "candidate.v00.json")
    assert read(run / "result.json")["model_calls"] == 1
    # Exercise the actual web provenance dispatch and normal plan validation.
    outcome = case.f.records._assess(case.parent.record, run.name, read(run / "result.json"), audit.model_dump(mode="json"))[0]
    assert outcome["admissible"], outcome
    prompt = (run / "audit_addendum_prompt.txt").read_text(encoding="utf-8")
    assert "EXACT TARGET VALUES" in prompt and len(prompt) < 20000


@pytest.mark.parametrize("tamper", ["answer", "model", "source", "event", "mutation", "candidate", "merged", "parent", "plan"])
def test_composite_rejects_tampered_raw_or_inherited_provenance(case, tamper):
    run = finish(case)
    audit = ReadingAudit.model_validate(read(run / "model_review.json"))
    call = next((run / "calls").iterdir())
    if tamper == "answer":
        raw = read(call / "answer.json"); raw["findings"][0]["explanation"] = "replacement"; rewrite(call / "answer.json", raw)
    elif tamper == "model":
        raw = read(call / "result.json"); raw["server_selected_model"] = "wrong"; rewrite(call / "result.json", raw)
    elif tamper == "source":
        raw = read(call / "result.json"); raw["source_review_proof"]["tool_receipts"] = []; rewrite(call / "result.json", raw)
    elif tamper in {"event", "mutation"}:
        events(run, read(call / "answer.json"), status="failed" if tamper == "event" else "completed", mutation=tamper == "mutation")
    elif tamper == "candidate":
        raw = read(run / "candidate.json"); raw["observations"][0]["value"]["value"] = 999; rewrite(run / "candidate.json", raw)
    elif tamper == "merged":
        raw = read(run / "audit.v00.json"); raw["coverage_limitations"] = []; rewrite(run / "audit.v00.json", raw)
    elif tamper == "parent":
        case.parent.receipt["remaining_model_seconds"] = 999
    else:
        raw = read(run / "plan.json"); raw["model_settings"]["timeout_seconds"] = 3600; rewrite(run / "plan.json", raw)
    assert not verify_audit_addendum_run(case.f.settings, run, case.parent.record, audit)


def test_web_replays_old_addendum_under_its_frozen_rules_and_checks_raw_receipts(case, monkeypatch):
    from watertreatment import audit_addendum_workflow
    run = finish(case)
    audit = read(run / "model_review.json")
    result = read(run / "result.json")
    expected = read(run / "plan.json")["rule_version"]
    original = audit_addendum_workflow.authenticate_addendum_parent

    def require_parent_rules(settings, *args, **kwargs):
        assert settings.rules_version == expected
        return original(settings, *args, **kwargs)

    monkeypatch.setattr(audit_addendum_workflow, "authenticate_addendum_parent", require_parent_rules)
    case.f.settings.rules_version = "0.9"
    records = case.f.records
    assert records._assess(case.parent.record, run.name, result, audit)[0]["admissible"]
    assert records.settings.rules_version == "0.9"
    assert not records._audit_provenance(run, case.parent.record.model_copy(update={"rule_version": "0.9"}), result, audit)
    call = next((run / "calls").iterdir())
    answer = read(call / "answer.json")
    answer["findings"][0]["explanation"] = "replaced after registration"
    rewrite(call / "answer.json", answer)
    assert not records._audit_provenance(run, case.parent.record, result, audit)


def test_new_source_concern_stays_blocking_and_cannot_be_published(case):
    run = finish(case, blocker=True)
    result = read(run / "result.json")
    audit = ReadingAudit.model_validate(read(run / "model_review.json"))
    assert result["status"] == "candidate_incomplete_budget" and result["reading_check_issues"]
    assert audit.findings[-1].needs_revision
    assert not verify_audit_addendum_run(case.f.settings, run, case.parent.record, audit)


@pytest.mark.parametrize("seconds", [0, 1201, True, 1.5])
def test_prepare_never_extends_budget(case, seconds):
    with pytest.raises(ValueError):
        prepare_audit_addendum(case.f.settings, case.f.run, timeout_seconds=seconds)


def test_original_remaining_budget_is_enforced_even_below_local_cap(case):
    object.__setattr__(case.parent, "remaining_seconds", 70)
    with pytest.raises(ValueError):
        prepare_audit_addendum(case.f.settings, case.f.run, timeout_seconds=71)


def test_repeated_prepare_cannot_regrant_the_same_parent_budget(case):
    prepare_audit_addendum(case.f.settings, case.f.run, timeout_seconds=120)
    with pytest.raises(ValueError, match="cannot be reset"):
        prepare_audit_addendum(case.f.settings, case.f.run, timeout_seconds=120)


@pytest.mark.parametrize("mode", ["failed", "mutation", "changed_answer", "duplicate_completion"])
def test_public_completion_must_match_readonly_final_output(tmp_path, mode):
    answer = {"findings": []}
    events(tmp_path, answer, status="failed" if mode == "failed" else "completed", mutation=mode == "mutation")
    if mode == "duplicate_completion":
        path = tmp_path / "app_server/events.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(path.read_text(encoding="utf-8").splitlines()[-1] + "\n")
    with pytest.raises(ValueError):
        _completed_call_protocol(tmp_path, {"thread_id": "thread", "turn_id": "turn"},
            {"findings": ["changed"]} if mode == "changed_answer" else answer)
