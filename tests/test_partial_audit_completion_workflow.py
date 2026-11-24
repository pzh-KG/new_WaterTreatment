"""Bounded append-only workflow contracts; synthetic evidence, no providers."""
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import pytest

from test_web_records import fixture, read
from test_audit_addendum_workflow import events, rewrite
from test_scientific_revision_audit import report_for
from watertreatment.codex import source_snapshot
from watertreatment.partial_audit_completion import build_partial_audit_completion_scope
from watertreatment.partial_audit_completion_workflow import (
    PartialAuditParent, prepare, execute, verify_partial_audit_completion_run)
from watertreatment.reading import AUDIT_DIMENSIONS, ReadingAudit, ReadingDossier, AuditFinding, SourceAnchor
from watertreatment.scientific_revision_audit import build_scientific_revision_scope, NEW_SCOPE_LIMITS
from watertreatment.scope_policy import frozen_scope
from watertreatment.settings import CodexSettings
from watertreatment.source_review import partial_source_review_progress
from watertreatment.sources import SourceDocument
from watertreatment.storage import digest_json, hash_file, write_new_json


@pytest.fixture
def case(fixture, monkeypatch):
    f = fixture
    f.settings.rules_version = "0.7"
    f.settings.codex.model, f.settings.codex.reasoning_effort = "gpt-6.2-sol", "max"
    old = f.record.model_copy(deep=True)
    old.rule_version, old.schema_version, old.extraction_scope = "0.7", "0.3.0", "research_core"
    for obs in old.observations:
        obs.analysis_role = "key_result"
    new = old.model_copy(deep=True)
    new.observations[0].value.status = "inferred"
    new.observations[0].origin = "model_inference"
    new.observations[0].limitations = ["Synthetic retained interpretation restriction."]
    baseline = ReadingAudit(findings=[], potential_omissions=[], checked_dimensions=list(AUDIT_DIMENSIONS),
        coverage_limitations=["Synthetic retained limitation"])
    full = build_scientific_revision_scope(old, new, baseline, baseline_origin="fresh_model_audit", **NEW_SCOPE_LIMITS)
    parent_audit = report_for(full, baseline)
    parent_audit.findings = [f for f in parent_audit.findings if f.record_path != "observations[0]"]
    # Honest missing source delivery, even though a synthetic public answer exists.
    progress = partial_source_review_progress(f.run, full, [], "parent-partial")
    scope = build_partial_audit_completion_scope(full, parent_audit, old_record=old, new_record=new,
        baseline_audit=baseline, source_progress=progress)
    bundle = read(f.run / "sources.json")
    dossier = ReadingDossier(paper_id=new.paper.id, article_question_and_design="Synthetic design",
        material_map=[], experiment_map=[], coverage=[], cross_source_conflicts=[], unresolved_questions=[], schema_gaps=[])
    write_new_json(f.run / "candidate.v00.json", new.model_dump(mode="json"))
    rewrite(f.run / "candidate.json", new.model_dump(mode="json"))
    write_new_json(f.run / "reading_dossier.json", dossier.model_dump(mode="json"))
    call = f.run / "calls" / "parent-partial"
    write_new_json(call / "answer.json", parent_audit.model_dump(mode="json"))
    write_new_json(call / "result.json", {"status": "failed", "elapsed_seconds": 125})
    parent_plan = {"cohort_role": "unassigned", "rule_version": "0.7", "extraction_policy": frozen_scope(baseline=new)}
    receipt = {"policy": "bounded-partial-audit-completion-v1", "parent_run": f.run.name,
        "candidate_sha256": hash_file(f.run / "candidate.v00.json"), "parent_answer_sha256": hash_file(call / "answer.json"),
        "original_total_model_seconds": 600, "original_max_calls": 3, "used_model_seconds": 125,
        "used_model_calls": 1, "remaining_model_seconds": 475, "remaining_model_calls": 2}
    parent = PartialAuditParent(f.run, parent_plan, bundle,
        [SourceDocument.model_validate(d) for d in bundle["documents"]], new, old, baseline, parent_audit,
        dossier, full, progress, scope, call, receipt)
    # Only ancestry admission is isolated. New candidate queries, source receipts,
    # union replay, complete SCI validation, byte preservation and budgets are real.
    monkeypatch.setattr("watertreatment.partial_audit_completion_workflow.authenticate_partial_audit_parent", lambda *a, **k: parent)
    return SimpleNamespace(f=f, parent=parent)


class SyntheticClient:
    def __init__(self, plan, *, skip_query=False, blocker=False, elapsed=3):
        self.settings = CodexSettings.model_validate(plan["model_settings"])
        self.total_timeout_seconds = plan["reading_settings"]["total_timeout_seconds"]
        self.calls_started, self.closed, self.events = 0, False, None
        self.skip_query, self.blocker, self.elapsed = skip_query, blocker, elapsed

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
        paths = [list(_parts(p)) for p in scope["required_query_paths"]]
        if self.skip_query:
            paths = [["paper"]]
        assert self.tools.handle("wt_query_candidate", {"paths": paths})["success"]
        if scope["source_anchors"]:
            assert self.tools.handle("wt_read_blocks", {"source_id": "main", "start_order": 1, "end_order": 1})["success"]
        binding = self.tools.candidate_binding
        request = {**kw["identity"], "model": "gpt-6.2-sol", "reasoning_effort": "max",
            "candidate_binding": binding, "prompt": kw["prompt"] + "\nCANDIDATE_SNAPSHOT " + binding["content_hash"],
            "sources": source_snapshot([*kw["input_paths"], Path(binding["path"])]),
            "source_review_call": call.name, "source_review_scope_sha256": scope["scope_sha256"]}
        write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
        findings = [AuditFinding(record_path=t["record_path"], record_paths=t["covered_paths"], severity="minor",
            verdict="unsupported" if self.blocker else "supported", needs_revision=self.blocker,
            explanation="Synthetic explicit disposition", evidence=[SourceAnchor(source_id="main", block_id="paragraph")])
            for t in scope["targets"]]
        answer = model(**{k: scope[k] for k in ("scope_sha256", "parent_audit_hash", "candidate_hash")}, findings=findings)
        write_new_json(call / "answer.json", answer.model_dump(mode="json"))
        write_new_json(call / "result.json", {"status": "schema_valid_candidate", "thread_id": "thread", "turn_id": "turn",
            "server_selected_model": "gpt-6.2-sol", "server_selected_effort": "max", "elapsed_seconds": self.elapsed,
            "source_review_proof": self.tools.source_review_proof()})
        events(run, answer.model_dump(mode="json"))
        self.calls_started = 1
        return answer


def finish(case, **kw):
    plan = prepare(case.f.settings, case.f.run, timeout_seconds=120)
    client = SyntheticClient(read(plan), **kw)
    run = execute(case.f.settings, plan, client=client)
    assert client.closed
    return run


def test_partial_completion_preserves_failed_parent_and_candidate_but_does_not_clear_dossier(case):
    old = {p: hash_file(p) for p in (case.f.run / "candidate.json", case.parent.call / "answer.json", case.parent.call / "result.json")}
    run = finish(case)
    assert old == {p: hash_file(p) for p in old}
    result = read(run / "result.json")
    assert result["scientific_audit_state"] == "completed_with_inherited_limitations"
    assert result["status"] == "candidate_incomplete_reading_coverage"
    assert result["stop_reason"] == "coverage_correction_required"
    assert result["reading_check_issues"] and all(x.startswith("dossier ") for x in result["reading_check_issues"])
    assert result["budget_lineage"]["used_model_seconds"] == 128
    assert result["budget_lineage"]["remaining_model_seconds"] == 472
    assert result["budget_lineage"]["used_model_calls"] == 2
    assert result["budget_lineage"]["remaining_model_calls"] == 1
    assert hash_file(run / "candidate.json") == case.parent.receipt["candidate_sha256"]
    audit = ReadingAudit.model_validate(read(run / "model_review.json"))
    assert audit.findings[:len(case.parent.audit.findings)] == case.parent.audit.findings
    assert verify_partial_audit_completion_run(case.f.settings, run, case.parent.record, audit)
    assert case.f.records._audit_provenance(run, case.parent.record, result, audit.model_dump(mode="json"))


@pytest.mark.parametrize("timeout", [476, 900, 30, True])
def test_partial_completion_cannot_reset_or_exceed_original_budget(case, timeout):
    with pytest.raises(ValueError, match="remaining model time"):
        prepare(case.f.settings, case.f.run, timeout_seconds=timeout)


def test_partial_completion_failure_is_preserved_and_cannot_retry_with_fresh_budget(case):
    plan = prepare(case.f.settings, case.f.run, timeout_seconds=120)
    client = SyntheticClient(read(plan), skip_query=True)
    with pytest.raises(ValueError, match="every exact required"):
        execute(case.f.settings, plan, client=client)
    assert client.closed and (plan.parent / "failure.json").exists()
    assert not (plan.parent / "candidate.json").exists()
    with pytest.raises(ValueError, match="already has"):
        prepare(case.f.settings, case.f.run, timeout_seconds=120)


def test_partial_completion_new_scientific_problem_stays_unresolved(case):
    run = finish(case, blocker=True)
    result = read(run / "result.json")
    assert result["stop_reason"] == "partial_audit_completion_unresolved"
    audit = ReadingAudit.model_validate(read(run / "model_review.json"))
    assert not verify_partial_audit_completion_run(case.f.settings, run, case.parent.record, audit)
    assert verify_partial_audit_completion_run(case.f.settings, run, case.parent.record, audit, for_repair=True)


def test_report_only_completion_inherits_authentic_delivery_without_repeating_reads(case, monkeypatch):
    from watertreatment.reading_tools import ReadingTools
    from watertreatment.partial_audit_completion_workflow import _source_receipts
    parent = case.parent
    tools = ReadingTools(parent.bundle, parent.run, max_calls=10,
        expected_inputs=read(parent.run / "plan.json")["input_paths"])
    tools.begin_source_review(parent.full_scope, call_id=parent.call.name)
    assert tools.handle("wt_read_blocks", {"source_id": "main", "start_order": 1, "end_order": 1})["success"]
    progress = partial_source_review_progress(parent.run, parent.full_scope,
        _source_receipts(parent.run, parent.call.name), parent.call.name)
    assert progress["complete"]
    scope = build_partial_audit_completion_scope(parent.full_scope, parent.audit, old_record=parent.old_record,
        new_record=parent.record, baseline_audit=parent.baseline, source_progress=progress)
    assert scope["source_anchors"] == [] and scope["inherited_source_anchors"]
    case.parent = replace(parent, progress=progress, scope=scope)
    monkeypatch.setattr("watertreatment.partial_audit_completion_workflow.authenticate_partial_audit_parent", lambda *a, **k: case.parent)
    run = finish(case)
    audit = ReadingAudit.model_validate(read(run / "model_review.json"))
    assert verify_partial_audit_completion_run(case.f.settings, run, case.parent.record, audit)
    requests = [read(p)["tool"] for p in (run / "reading_tools").glob("*/request.json")]
    assert requests == ["wt_query_candidate"]
    union = read(run / "combined_source_review_proof.json")
    assert len(union["parts"]) == 2 and union["parts"][1]["progress"]["tool_receipts"] == []
    assert read(run / "result.json")["stop_reason"] == "coverage_correction_required"


@pytest.mark.parametrize("artifact", ["partial_audit_addendum.json", "partial_parent_receipt.json",
    "candidate.json", "model_review.json", "combined_source_review_proof.json", "partial_audit_completion_proof.json"])
def test_partial_completion_tamper_invalidates_provenance(case, artifact):
    run = finish(case)
    audit = ReadingAudit.model_validate(read(run / "model_review.json"))
    (run / artifact).write_text("{}", encoding="utf-8")
    assert not verify_partial_audit_completion_run(case.f.settings, run, case.parent.record, audit)
