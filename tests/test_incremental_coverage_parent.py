"""Completed scientific chains can repair only the remaining reading ledger.

All inputs/answers are synthetic; actual tools, ancestry validators and public
protocol replay are used. No provider or graph connection is made.
"""
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_audit_addendum_workflow import events, rewrite
from test_completed_native_resume import completed_native
from test_staged_source_resume import staged_cli
from test_web_records import fixture, read, completed_call
from test_web_scientific_provenance import completed_delta, second_delta, freeze_request
from test_scientific_revision_audit import report_for
from test_partial_audit_completion_workflow import SyntheticClient as PartialClient
from test_coverage_correction_workflow import SyntheticClient as CoverageClient
from watertreatment.audit_execution import citation_paths, prepare_citation_review
from watertreatment.audit_resume import freeze_seed_resume_audit, prepare_resumed_review
from watertreatment.codex import source_snapshot
from watertreatment.completed_native_resume import authenticate_completed_revision
from watertreatment.coverage_correction_workflow import (
    authenticate_coverage_parent, prepare, execute, verify_coverage_correction_run)
from watertreatment.domain import EvidenceRef
from watertreatment.partial_audit_completion_workflow import (
    authenticate_partial_audit_parent, prepare as prepare_partial, execute as execute_partial)
from watertreatment.reading import AuditFinding, ReadingAudit, ReadingDossier, SourceAnchor
from watertreatment.reading_tools import ReadingTools
from watertreatment.reader_workflow import reading_checks
from watertreatment.scope_policy import frozen_scope
from watertreatment.sources import SourceDocument
from watertreatment.storage import digest_json, hash_file, write_new_json


def dossier(record):
    return ReadingDossier(paper_id=record.paper.id, article_question_and_design="Synthetic design",
        material_map=[], experiment_map=[], coverage=[], cross_source_conflicts=[],
        unresolved_questions=[], schema_gaps=[])


@pytest.fixture
def delta_parent(fixture):
    f = fixture
    run = f.store.new_run("extraction", {"paper_id": "synthetic-paper"})
    shutil.copyfile(f.run / "sources.json", run / "sources.json")
    source_paths = [run / "sources.json", *[Path(x["source"]["path"]) for x in read(run / "sources.json")["documents"]]]
    old = f.record.model_copy(deep=True)
    old.rule_version, old.schema_version, old.extraction_scope = "0.7", "0.3.0", "research_core"
    for item in old.observations:
        item.analysis_role = "key_result"
    plan = {**read(f.run / "plan.json"), "model_settings": {"model": "gpt-6.1-sol", "reasoning_effort": "max"},
        "run_id": run.name, "rule_version": "0.7", "schema_version": "0.3.0",
        "input_paths": source_snapshot(source_paths), "extraction_policy": frozen_scope(baseline=old)}
    write_new_json(run / "plan.json", plan)
    completed_call(run, old)
    baseline = ReadingAudit.model_validate(read(run / "model_review.json"))
    baseline.findings = [AuditFinding(record_path="observations[0].value", severity="major",
        verdict="unsupported", needs_revision=True, explanation="Synthetic attribution repair",
        evidence=[SourceAnchor(source_id="main", block_id="paragraph")])]
    rewrite(run / "calls/audit/answer.json", baseline.model_dump(mode="json"))
    write_new_json(run / "candidate.v00.json", old.model_dump(mode="json"))
    write_new_json(run / "audit.v00.json", baseline.model_dump(mode="json"))
    state = SimpleNamespace(f=f, run=run, plan=plan, source_paths=source_paths,
        history=[{"version": 0, "audit_origin": "fresh_model_audit", "candidate_sha256": hash_file(run / "candidate.v00.json"),
            "validation_error_count": 0, "reading_check_issues": []}],
        tools=ReadingTools(read(run / "sources.json"), run, max_calls=20, expected_inputs=plan["input_paths"]))
    new = old.model_copy(deep=True)
    new.observations[0].value.status, new.observations[0].origin = "inferred", "model_inference"
    new.observations[0].limitations = ["Synthetic inferred attribution remains distinct."]
    c = second_delta(completed_delta(state, new, new_blocker=True))
    shutil.copyfile(c.run / "audit.v02.json", c.run / "model_review.json")
    c.f.settings.rules_version = "0.7"
    c.f.settings.codex.model, c.f.settings.codex.reasoning_effort = "gpt-6.2-sol", "max"
    d = dossier(c.candidate)
    write_new_json(c.run / "reading_dossier.json", d.model_dump(mode="json"))
    docs = [SourceDocument.model_validate(x) for x in read(c.run / "sources.json")["documents"]]
    gaps = reading_checks(d, c.candidate, c.report, docs)
    from watertreatment.validation import validate_record
    from watertreatment.workflow import validate_plan_metadata
    assert not [i for i in [*validate_record(c.candidate, source_documents=docs),
        *validate_plan_metadata(c.candidate, c.plan, docs, store=c.f.store)] if i.severity == "error"]
    assert not c.report.potential_omissions
    assert not [f for f in c.report.findings if f.needs_revision or f.verdict == "unsupported"]
    c.history[-1].update(needs_revision=False, reading_check_issues=gaps)
    rewrite(c.run / "result.json", {"status": "candidate_incomplete_reading_coverage",
        "stop_reason": "coverage_correction_required", "history": c.history,
        "human_validated": False, "graph_published": False, "validation_error_count": 0,
        "reading_check_issues": gaps})
    request = read(c.call / "request.json")["request"]
    request.update(model="gpt-6.1-sol", reasoning_effort="max",
        code_identity=c.plan.get("code_identity"), rule_version="0.7")
    freeze_request(c.call, request)
    completed = read(c.call / "result.json")
    completed.update(thread_id="thread", turn_id="turn")
    rewrite(c.call / "result.json", completed)
    events(c.run, c.report.model_dump(mode="json"))
    c.f.store.event(c.run.name, "prepared", {"plan_sha256": hash_file(c.run / "plan.json")})
    return c


def finish_coverage(settings, parent_run):
    parent = authenticate_coverage_parent(settings, parent_run)
    path = prepare(settings, parent_run, timeout_seconds=120)
    run = execute(settings, path, client=CoverageClient(read(path)))
    assert verify_coverage_correction_run(settings, run, parent.record, parent.audit)
    assert read(run / "result.json")["reading_check_issues"] == []
    assert hash_file(run / "candidate.json") == hash_file(parent_run / "candidate.json")
    assert hash_file(run / "model_review.json") == hash_file(parent_run / "model_review.json")
    return run


def test_cumulative_delta_chain_can_complete_only_coverage(delta_parent):
    c = delta_parent
    old = {p: hash_file(p) for p in c.run.rglob("*") if p.is_file()}
    parent = authenticate_coverage_parent(c.f.settings, c.run)
    assert parent.audit.review_scope == "scientific_delta"
    assert "completed_full_audit_call" not in parent.receipt
    assert parent.receipt["completed_incremental_audit_call"] == str(c.call)
    run = finish_coverage(c.f.settings, c.run)
    assert all(hash_file(p) == sha for p, sha in old.items())
    c.f.records.refresh()
    assert c.f.records.get(run.name)["summary"]["admissible"]


def test_citation_only_delta_can_finish_coverage_with_real_parent_authentication(delta_parent):
    f = delta_parent.f
    run = f.store.new_run("extraction", {"paper_id": "synthetic-paper"})
    shutil.copyfile(delta_parent.run / "sources.json", run / "sources.json")
    old = delta_parent.candidate.model_copy(deep=True)
    old.evidence.append(EvidenceRef(id="citation-proof", source_id="main", block_id="paragraph", quote="rate 2.4"))
    paths = [run / "sources.json", *[Path(x["source"]["path"]) for x in read(run / "sources.json")["documents"]]]
    plan = {**delta_parent.plan, "run_id": run.name, "input_paths": source_snapshot(paths)}
    write_new_json(run / "plan.json", plan)
    completed_call(run, old)
    baseline = ReadingAudit.model_validate(read(run / "model_review.json"))
    baseline.findings = [AuditFinding(record_path="observations[1].value.evidence_ids", severity="minor",
        verdict="unsupported", needs_revision=True, explanation="Synthetic citation repair",
        evidence=[SourceAnchor(source_id="main", block_id="paragraph")])]
    rewrite(run / "calls/audit/answer.json", baseline.model_dump(mode="json"))
    write_new_json(run / "candidate.v00.json", old.model_dump(mode="json"))
    write_new_json(run / "audit.v00.json", baseline.model_dump(mode="json"))
    new = old.model_copy(deep=True)
    new.observations[1].value.evidence_ids.append("citation-proof")
    write_new_json(run / "candidate.v01.json", new.model_dump(mode="json"))
    docs = [SourceDocument.model_validate(x) for x in read(run / "sources.json")["documents"]]
    d = dossier(old)
    write_new_json(run / "reading_dossier.json", d.model_dump(mode="json"))
    # The original science review may complete while its separately tracked
    # dossier ledger still needs explicit dispositions. This is not scientific
    # coverage being asserted by the missing ledger.
    gaps = reading_checks(d, old, baseline, docs)
    history = [{"version": 0, "audit_origin": "fresh_model_audit", "validation_errors": 0,
        "reading_check_issues": gaps, "candidate_sha256": hash_file(run / "candidate.v00.json")}]
    prepared = prepare_citation_review(run, 1, new, history, plan)
    assert prepared and prepared[1]["policy"] == "citation-only-audit-v1"
    receipt, scope, _ = prepared
    audit = baseline.model_copy(deep=True)
    audit.review_scope, audit.review_scope_sha256 = "citation_delta", scope["scope_sha256"]
    audit.findings = [AuditFinding(record_path="observations[1].value", severity="minor", verdict="supported",
        needs_revision=False, explanation="Synthetic citation disposition",
        evidence=[SourceAnchor(source_id="main", block_id="paragraph")])]
    tools = ReadingTools(read(run / "sources.json"), run, max_calls=10, expected_inputs=plan["input_paths"])
    tools.bind_candidate(new, run / "candidate.v01.json")
    assert tools.handle("wt_query_candidate", {"path": ["observations", 1]})["success"]
    assert tools.handle("wt_read_blocks", {"source_id": "main", "start_order": 1, "end_order": 1})["success"]
    binding = tools.candidate_binding
    call = run / "calls/citation-1"
    request = {"stage": "audit_1", "model": "gpt-6.1-sol", "reasoning_effort": "max",
        "code_identity": plan.get("code_identity"), "rule_version": "0.7", "candidate_binding": binding,
        "prompt": "CANDIDATE_SNAPSHOT " + binding["content_hash"], "citation_review": receipt,
        "sources": source_snapshot([*paths, Path(binding["path"]), *citation_paths(prepared)])}
    write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    write_new_json(call / "answer.json", audit.model_dump(mode="json"))
    write_new_json(call / "result.json", {"status": "schema_valid_candidate", "thread_id": "thread", "turn_id": "turn",
        "server_selected_model": "gpt-6.1-sol", "server_selected_effort": "max"})
    write_new_json(run / "audit.v01.json", audit.model_dump(mode="json"))
    shutil.copyfile(run / "candidate.v01.json", run / "candidate.json")
    shutil.copyfile(run / "audit.v01.json", run / "model_review.json")
    history.append({"version": 1, "audit_origin": "citation_delta_model_audit", "validation_errors": 0,
        "reading_check_issues": gaps, "needs_revision": False, "candidate_sha256": hash_file(run / "candidate.json")})
    rewrite(run / "result.json", {"status": "candidate_incomplete_reading_coverage", "stop_reason": "coverage_correction_required",
        "history": history, "reading_check_issues": gaps, "validation_error_count": 0,
        "human_validated": False, "graph_published": False})
    events(run, audit.model_dump(mode="json"))
    f.store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    parent = authenticate_coverage_parent(f.settings, run)
    assert parent.audit.review_scope == "citation_delta"
    finish_coverage(f.settings, run)


@pytest.mark.parametrize("tamper", ["baseline", "intermediate", "current_source_receipt", "protocol", "candidate"])
def test_incremental_coverage_rejects_broken_ancestry_or_delivery(delta_parent, tamper):
    c = delta_parent
    assert authenticate_coverage_parent(c.f.settings, c.run)
    if tamper == "baseline":
        path = c.run / "calls/audit/result.json"
        result = read(path); result["status"] = "failed"; rewrite(path, result)
    elif tamper == "intermediate":
        path = c.run / "calls/scientific-1/result.json"
        result = read(path); result.pop("source_review_proof"); rewrite(path, result)
    elif tamper == "current_source_receipt":
        proof = read(c.call / "result.json")["source_review_proof"]
        Path(proof["tool_receipts"][0]["result_path"]).write_text("{}", encoding="utf-8")
    elif tamper == "protocol":
        events(c.run, c.report.model_dump(mode="json"), mutation=True)
    else:
        (c.run / "candidate.json").write_bytes((c.run / "candidate.json").read_bytes() + b" ")
    with pytest.raises((ValueError, KeyError)):
        authenticate_coverage_parent(c.f.settings, c.run)


def test_incremental_protocol_and_candidate_proofs_must_belong_to_same_call(delta_parent):
    c = delta_parent
    assert authenticate_coverage_parent(c.f.settings, c.run)
    request = read(c.call / "request.json")["request"]
    request.pop("model")
    freeze_request(c.call, request)
    # An otherwise complete second envelope cannot lend its model/protocol
    # fields to the first call while borrowing that call's candidate/source read.
    other = c.run / "calls/unbound-copy"
    other.mkdir()
    counterfeit = dict(request, model="gpt-6.1-sol", prompt="No candidate delivered here")
    counterfeit.pop("candidate_binding")
    freeze_request(other, counterfeit)
    shutil.copyfile(c.call / "answer.json", other / "answer.json")
    shutil.copyfile(c.call / "result.json", other / "result.json")
    with pytest.raises(ValueError, match="lacks its authenticated full-baseline and delta audit chain"):
        authenticate_coverage_parent(c.f.settings, c.run)


@pytest.fixture
def partial_parent(completed_native):
    """Complete native patch chain -> genuine partial source-review failure."""
    c = completed_native
    settings = c.f.settings
    settings.codex.model, settings.codex.reasoning_effort = "gpt-6.2-sol", "max"
    authenticated = authenticate_completed_revision(settings, c.candidate_path)
    run = c.f.store.new_run("extraction", {"paper_id": authenticated.record.paper.id})
    shutil.copyfile(c.run / "sources.json", run / "sources.json")
    bundle = read(run / "sources.json")
    record = authenticated.record
    for name in ("candidate.v00.json", "candidate.json"):
        write_new_json(run / name, record.model_dump(mode="json"))
    write_new_json(run / "reading_dossier.json", dossier(record).model_dump(mode="json"))
    ancestry, frozen = freeze_seed_resume_audit(settings, run, authenticated, bundle, seed_kind="completed_revision")
    input_paths = [run / "sources.json", *[Path(d["source"]["path"]) for d in bundle["documents"]]]
    plan = {"run_id": run.name, "paper_id": record.paper.id, "cohort_role": "development",
        "rule_version": "0.7", "schema_version": "0.3.0", "code_identity": "f" * 64,
        "input_paths": source_snapshot(input_paths), "extraction_policy": frozen_scope(baseline=record),
        "resumed_full_audit": ancestry, "reading_artifacts": frozen, "max_calls": 3,
        "reading_settings": {"enabled": True, "total_timeout_seconds": 600},
        "model_settings": settings.codex.model_dump(mode="json")}
    write_new_json(run / "plan.json", plan)
    prepared = prepare_resumed_review(settings, plan, run, record)
    receipt, scope, baseline = prepared
    tools = ReadingTools(bundle, run, max_calls=20, expected_inputs=plan["input_paths"])
    tools.bind_candidate(record, run / "candidate.v00.json")
    call = run / "calls/partial-source-review"
    tools.begin_query_turn(); tools.begin_source_review(scope, call_id=call.name)
    assert tools.handle("wt_query_candidate", {"path": ["observations", 0]})["success"]
    binding = tools.candidate_binding
    request = {"stage": "audit_0", "model": "gpt-6.2-sol", "reasoning_effort": "max",
        "code_identity": plan["code_identity"], "rule_version": "0.7", "candidate_binding": binding,
        "prompt": "CANDIDATE_SNAPSHOT " + binding["content_hash"], "citation_review": receipt,
        "source_review_call": call.name, "source_review_scope_sha256": scope["scope_sha256"],
        "sources": source_snapshot([*input_paths, Path(binding["path"]), *citation_paths(prepared)])}
    write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    audit = report_for(scope, baseline)
    # Model finished its public report, but no required source block was actually
    # delivered. The controller therefore retains a failed partial source audit.
    error = "ValueError: bounded source review delivery incomplete; synthetic missing blocks"
    write_new_json(call / "answer.json", audit.model_dump(mode="json"))
    write_new_json(call / "result.json", {"status": "failed", "error": error, "elapsed_seconds": 125,
        "server_selected_model": "gpt-6.2-sol", "server_selected_effort": "max",
        "thread_id": "thread", "turn_id": "turn"})
    write_new_json(run / "failure.json", {"error": error, "model_calls": 1})
    write_new_json(run / "result.json", {"status": "candidate_incomplete_failure",
        "last_candidate_version": "candidate.v00.json", "model_calls": 1,
        "human_validated": False, "graph_published": False})
    events(run, audit.model_dump(mode="json"))
    c.f.store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    assert authenticate_partial_audit_parent(settings, run).record == record
    return SimpleNamespace(f=c.f, settings=settings, run=run, native=c)


def complete_partial(c, **kwargs):
    path = prepare_partial(c.settings, c.run, timeout_seconds=120)
    return execute_partial(c.settings, path, client=PartialClient(read(path), **kwargs))


def test_real_partial_completion_chain_then_coverage_without_full_reaudit(partial_parent):
    c = partial_parent
    run = complete_partial(c)
    parent = authenticate_coverage_parent(c.settings, run)
    assert parent.receipt["scientific_audit_origin"] == "partial_scientific_audit_composite"
    assert "completed_full_audit_call" not in parent.receipt
    assert parent.receipt["dossier_path"].endswith("reading_dossier.effective.json")
    finish_coverage(c.settings, run)


def test_partial_scientific_blocker_cannot_be_cleared_by_coverage(partial_parent):
    c = partial_parent
    run = complete_partial(c, blocker=True)
    with pytest.raises(ValueError):
        authenticate_coverage_parent(c.settings, run)


@pytest.mark.parametrize("tamper", ["native_patch", "partial_answer", "source_union", "effective_dossier"])
def test_partial_coverage_replays_complete_original_ancestry(partial_parent, tamper):
    c = partial_parent
    run = complete_partial(c)
    assert authenticate_coverage_parent(c.settings, run)
    paths = {"native_patch": c.native.stage,
        "partial_answer": c.run / "calls/partial-source-review/answer.json",
        "source_union": run / "combined_source_review_proof.json",
        "effective_dossier": run / "reading_dossier.effective.json"}
    paths[tamper].write_text("{}", encoding="utf-8")
    with pytest.raises((ValueError, KeyError)):
        authenticate_coverage_parent(c.settings, run)
