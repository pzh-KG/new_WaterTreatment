"""Synthetic source/protocol chains; no model or graph connection is made."""
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_web_records import fixture, read
from test_scientific_revision_audit import report_for
from watertreatment.audit_execution import prepare_citation_review, citation_paths
from watertreatment.audit_guidance_resume import authenticate_guidance_parent, freeze_guidance, verify_guidance
from watertreatment.codex import source_snapshot
from watertreatment.domain import ExtractionRecord, EvidenceRef
from watertreatment.reading import AUDIT_DIMENSIONS, AuditFinding, ReadingAudit, ReadingDossier, SourceAnchor
from watertreatment.reading_tools import ReadingTools
from watertreatment.reader_workflow import execute_reader
from watertreatment.scope_policy import frozen_scope
from watertreatment.scientific_revision_audit import NEW_SCOPE_LIMITS
from watertreatment.sources import SourceDocument
from watertreatment.storage import digest_json, hash_file, write_new_json


def rewrite(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def public_answer(run, call, answer):
    folder = run / "app_server"
    folder.mkdir(exist_ok=True)
    thread, turn = "synthetic-guidance-thread", call.name
    messages = [
        {"method": "turn/started", "params": {"threadId": thread, "turn": {"id": turn}}},
        {"method": "item/completed", "params": {"threadId": thread, "turnId": turn,
            "item": {"type": "agentMessage", "phase": "final_answer", "text": json.dumps(answer)}}},
        {"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "completed"}}}]
    with (folder / "events.jsonl").open("a", encoding="utf-8") as stream:
        for message in messages:
            stream.write(json.dumps({"direction": "receive", "message": message}) + "\n")
    return thread, turn


def save_audit(state, record, audit, version, prepared=None):
    run, plan = state.run, state.plan
    path = run / f"candidate.v{version:02d}.json"
    if not path.exists():
        write_new_json(path, record.model_dump(mode="json"))
    call = run / "calls" / f"audit-{version}"
    inputs = [Path(x["path"]) for x in plan["input_paths"]]
    request = {"stage": f"audit_{version}", "model": plan["model_settings"]["model"], "reasoning_effort": "max",
        "code_identity": plan["code_identity"], "rule_version": plan["rule_version"],
        "prompt": "Inspect this candidate: " + record.model_dump_json(exclude_defaults=True)}
    proof = {}
    if prepared:
        state.tools.bind_candidate(record, path)
        state.tools.begin_query_turn()
        if prepared[1]["policy"] in {"bounded-scientific-revision-v1", "bounded-revision-audit-v1"}:
            state.tools.begin_source_review(prepared[1], call_id=call.name)
        assert state.tools.handle("wt_query_candidate", {"path": ["observations", 0]})["success"]
        assert state.tools.handle("wt_read_blocks", {"source_id": "main", "start_order": 1, "end_order": 1})["success"]
        binding = state.tools.candidate_binding
        request.update(prompt="CANDIDATE_SNAPSHOT " + binding["content_hash"], candidate_binding=binding,
            citation_review=prepared[0], source_review_call=call.name,
            source_review_scope_sha256=prepared[1]["scope_sha256"])
        inputs += [Path(binding["path"]), *citation_paths(prepared)]
        if prepared[1]["policy"] in {"bounded-scientific-revision-v1", "bounded-revision-audit-v1"}:
            proof["source_review_proof"] = state.tools.source_review_proof()
    request["sources"] = source_snapshot(inputs)
    write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    write_new_json(call / "answer.json", audit.model_dump(mode="json"))
    thread, turn = public_answer(run, call, audit.model_dump(mode="json"))
    write_new_json(call / "result.json", {"status": "schema_valid_candidate", "thread_id": thread, "turn_id": turn,
        "server_selected_model": plan["model_settings"]["model"], "server_selected_effort": "max", "elapsed_seconds": 1.0, **proof})
    write_new_json(run / f"audit.v{version:02d}.json", audit.model_dump(mode="json"))
    state.history.append({"version": version, "audit_origin": "fresh_model_audit" if not prepared else
        "scientific_delta_model_audit" if audit.review_scope == "scientific_delta" else "citation_delta_model_audit",
        "candidate_sha256": hash_file(path), "validation_errors": 0, "reading_check_issues": [], "needs_revision": True})
    shutil.copyfile(path, run / "candidate.json")
    shutil.copyfile(run / f"audit.v{version:02d}.json", run / "model_review.json")
    rewrite(run / "result.json", {"status": "candidate_incomplete_budget", "stop_reason": "revision_budget_exhausted",
        "history": state.history, "human_validated": False, "graph_published": False,
        "validation_error_count": 0, "reading_check_issues": []})
    state.record, state.audit, state.call = record, audit, call
    return state


def make_parent(f, kind):
    f.settings.rules_version = "0.7"
    f.settings.codex.model, f.settings.codex.reasoning_effort = "gpt-6.2-sol", "max"
    f.settings.reading.enabled = True
    run = f.store.new_run("extraction", {"paper_id": "synthetic-paper"})
    shutil.copyfile(f.run / "sources.json", run / "sources.json")
    record = f.record.model_copy(deep=True)
    record.rule_version, record.schema_version, record.extraction_scope = "0.7", "0.3.0", "research_core"
    for item in record.observations:
        item.analysis_role = "key_result"
    record.evidence.append(EvidenceRef(id="citation-other", source_id="main", block_id="paragraph", quote="rate 2.4"))
    paths = [run / "sources.json", *[Path(x["source"]["path"]) for x in read(run / "sources.json")["documents"]]]
    plan = {**read(f.run / "plan.json"), "run_id": run.name, "rule_version": "0.7", "schema_version": "0.3.0",
        "input_paths": source_snapshot(paths), "extraction_policy": frozen_scope(baseline=record),
        "code_identity": {}, "runtime": {}, "scientific_scope_limits": dict(NEW_SCOPE_LIMITS)}
    write_new_json(run / "plan.json", plan)
    dossier = ReadingDossier(paper_id=record.paper.id, article_question_and_design="Synthetic guidance experiment",
        material_map=[], experiment_map=[], coverage=record.coverage, cross_source_conflicts=[], unresolved_questions=[], schema_gaps=[])
    write_new_json(run / "reading_dossier.json", dossier.model_dump(mode="json"))
    f.store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    baseline = ReadingAudit(checked_dimensions=list(AUDIT_DIMENSIONS), potential_omissions=[],
        coverage_limitations=["Synthetic limitation remains"], findings=[AuditFinding(
            record_path="observations[0].value.evidence_ids" if kind == "citation" else "observations[0].value",
            severity="major", verdict="unsupported", needs_revision=True, explanation="Original source-attribution concern",
            evidence=[SourceAnchor(source_id="main", block_id="paragraph")])])
    state = SimpleNamespace(f=f, run=run, plan=plan, history=[],
        tools=ReadingTools(read(run / "sources.json"), run, max_calls=20, expected_inputs=plan["input_paths"]))
    save_audit(state, record, baseline, 0)
    if kind != "full":
        newer = record.model_copy(deep=True)
        if kind == "citation":
            newer.observations[0].value.evidence_ids.append("citation-other")
        else:
            newer.observations[0].value.status, newer.observations[0].origin = "inferred", "model_inference"
            newer.observations[0].limitations = ["Synthetic attribution uncertainty"]
        write_new_json(run / "candidate.v01.json", newer.model_dump(mode="json"))
        prepared = prepare_citation_review(run, 1, newer, state.history, plan)
        assert prepared
        if kind == "citation":
            report = baseline.model_copy(deep=True)
            report.review_scope, report.review_scope_sha256 = "citation_delta", prepared[1]["scope_sha256"]
            report.findings = [AuditFinding(record_path="observations[0].value", severity="minor", verdict="supported",
                needs_revision=False, explanation="Synthetic source disposition", evidence=[SourceAnchor(source_id="main", block_id="paragraph")])]
        else:
            report = report_for(prepared[1], baseline)
        report.findings.append(AuditFinding(record_path="observations[1].value.note", severity="major", verdict="unsupported",
            needs_revision=True, explanation="Retain and source-check this later concern", evidence=[SourceAnchor(source_id="main", block_id="paragraph")]))
        report.coverage_limitations.append("Later incremental limitation remains")
        save_audit(state, newer, report, 1, prepared)
    return state


def new_continuation(parent):
    f = parent.f
    run = f.store.new_run("extraction", {"paper_id": parent.record.paper.id})
    shutil.copyfile(parent.run / "sources.json", run / "sources.json")
    shutil.copyfile(parent.run / "reading_dossier.json", run / "reading_dossier.json")
    write_new_json(run / "reused_candidate.json", parent.record.model_dump(mode="json"))
    plan = {**parent.plan, "run_id": run.name, "workflow": "active_reading", "max_calls": 2,
        "reading_settings": {"enabled": True, "max_revisions": 1, "max_tool_calls": 30, "total_timeout_seconds": 120},
        "model_settings": f.settings.codex.model_dump(mode="json"), "reading_artifacts": [],
        "reused_dossier": {"path": str(parent.run / "reading_dossier.json")},
        "reused_candidate": {"parent_run": parent.run.name, "path": str(parent.run / f"candidate.v{len(parent.history)-1:02d}.json")},
        "image_paths": [], "input_paths": source_snapshot([run / "sources.json",
            *[Path(x["source"]["path"]) for x in read(run / "sources.json")["documents"]]])}
    plan.pop("audit_guidance", None)
    for name in ("extract_prompt.txt", "revise_reading_prompt.txt", "audit_reading_prompt.txt", "reading_context.txt"):
        (run / name).write_text("Synthetic source-bound scientific instructions", encoding="utf-8")
    write_new_json(run / "plan.json", plan)
    f.store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    path = parent.run / f"candidate.v{len(parent.history)-1:02d}.json"
    freeze_guidance(f.settings, run, parent.run, path)
    return run


class Reader:
    def __init__(self, parent, *, report_mutator=None, revise=True):
        self.parent, self.calls_started, self.stages = parent, 0, []
        self.report_mutator, self.revise = report_mutator, revise

    def bind_tools(self, tools):
        self.tools = tools

    def complete(self, model, *, prompt, input_paths, workdir, audit_root, identity, image_paths, conversation_key):
        self.calls_started += 1
        stage = identity["stage"]
        self.stages.append(stage)
        run = Path(workdir)
        if stage == "revision_1":
            record = self.parent.record.model_copy(deep=True)
            if not self.revise:
                return record
            if self.parent.audit.review_scope == "full":
                record.observations[0].value.status, record.observations[0].origin = "inferred", "model_inference"
                record.observations[0].limitations = ["Synthetic interpretation corrected with retained uncertainty"]
            else:
                record.observations[1].value.note = "Synthetic source-specific absence interpretation after rereading"
                record.observations[1].value.evidence_ids = ["source-value"]
            return record
        assert stage == "audit_1", "No new comprehension, extraction or full-paper audit is permitted"
        receipt = identity["citation_review"]
        scope = read(Path(receipt["scope_path"]))
        baseline = ReadingAudit.model_validate(read(Path(receipt["baseline_audit_path"])))
        report = report_for(scope, baseline)
        for item in receipt["additional_audits"]:
            original = ReadingAudit.model_validate(read(Path(item["audit_path"])))
            report.coverage_limitations += [x for x in original.coverage_limitations if x not in report.coverage_limitations]
        if self.report_mutator:
            self.report_mutator(report)
        record = ExtractionRecord.model_validate(read(run / "candidate.v01.json"))
        # Source receipts and completion envelopes are real local tool records.
        # The workflow itself owns history and final files, so retain only the
        # call artifacts generated by this synthetic model interaction here.
        call = run / "calls" / "audit-1"
        self.tools.begin_query_turn()
        self.tools.begin_source_review(scope, call_id=call.name)
        assert self.tools.handle("wt_query_candidate", {"path": ["observations", 0]})["success"]
        assert self.tools.handle("wt_read_blocks", {"source_id": "main", "start_order": 1, "end_order": 1})["success"]
        request = {**identity, "model": "gpt-6.2-sol", "reasoning_effort": "max", "prompt": prompt +
            " CANDIDATE_SNAPSHOT " + self.tools.candidate_binding["content_hash"],
            "candidate_binding": self.tools.candidate_binding,
            "source_review_call": call.name, "source_review_scope_sha256": scope["scope_sha256"],
            "sources": source_snapshot([*input_paths, Path(self.tools.candidate_binding["path"])])}
        write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
        write_new_json(call / "answer.json", report.model_dump(mode="json"))
        thread, turn = public_answer(run, call, report.model_dump(mode="json"))
        write_new_json(call / "result.json", {"status": "schema_valid_candidate", "thread_id": thread, "turn_id": turn,
            "server_selected_model": "gpt-6.2-sol", "server_selected_effort": "max", "source_review_proof": self.tools.source_review_proof()})
        return report


@pytest.mark.parametrize("kind", ["full", "scientific", "citation"])
def test_existing_audit_guidance_repairs_then_locally_reviews_without_full_restart(fixture, kind):
    parent = make_parent(fixture, kind)
    original = {p: hash_file(p) for p in parent.run.rglob("*") if p.is_file()}
    run = new_continuation(parent)
    plan = read(run / "plan.json")
    assert verify_guidance(fixture.settings, run)
    client = Reader(parent)
    docs = [SourceDocument.model_validate(x) for x in read(run / "sources.json")["documents"]]
    execute_reader(fixture.settings, plan, run, [Path(x["path"]) for x in plan["input_paths"]], docs, fixture.store, client)
    assert client.stages == ["revision_1", "audit_1"]
    result = read(run / "result.json")
    assert result["stop_reason"] == "audit_completed"
    assert result["history"][0]["audit_origin"] == "reused_repair_guidance"
    assert result["history"][1]["audit_origin"] == "scientific_delta_model_audit"
    current = ExtractionRecord.model_validate(read(run / "candidate.json"))
    assert fixture.records._audit_provenance(run, current, result, read(run / "model_review.json"))
    if kind != "full":
        assert "Later incremental limitation remains" in read(run / "model_review.json")["coverage_limitations"]
    assert all(hash_file(path) == sha for path, sha in original.items())


@pytest.mark.parametrize("defect", ["old_answer", "public_completion", "candidate", "relabel_guidance", "frozen_copy"])
def test_guidance_cannot_hide_changed_originals_or_relabel_inherited_reports(fixture, defect):
    parent = make_parent(fixture, "scientific")
    run = new_continuation(parent)
    if defect == "old_answer":
        path = parent.run / "calls/audit-0/answer.json"
        data = read(path); data["coverage_limitations"].append("altered"); rewrite(path, data)
    elif defect == "public_completion":
        path = parent.run / "app_server/events.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace('"completed"', '"failed"'), encoding="utf-8")
    elif defect == "candidate":
        path = parent.run / "candidate.v01.json"
        data = read(path); data["observations"][0]["value"]["value"] = 999; rewrite(path, data)
    elif defect == "relabel_guidance":
        path = run / "plan.json"
        data = read(path); data["reused_candidate"].pop("repair_audit"); rewrite(path, data)
    else:
        path = run / "guidance_audit.00.json"
        data = read(path); data["findings"] = []; rewrite(path, data)
    with pytest.raises((ValueError, OSError, KeyError)):
        verify_guidance(fixture.settings, run)


def execute_continuation(parent, run, client):
    plan = read(run / "plan.json")
    docs = [SourceDocument.model_validate(x) for x in read(run / "sources.json")["documents"]]
    execute_reader(parent.f.settings, plan, run, [Path(x["path"]) for x in plan["input_paths"]],
        docs, parent.f.store, client)
    return read(run / "result.json")


def test_second_continuation_retains_original_full_and_new_incremental_findings(fixture):
    original = make_parent(fixture, "full")
    first = new_continuation(original)

    def outstanding(report):
        report.findings.append(AuditFinding(record_path="observations[1].value.note", severity="major",
            verdict="unsupported", needs_revision=True, explanation="New source concern remains actionable",
            evidence=[SourceAnchor(source_id="main", block_id="paragraph")]))
        report.coverage_limitations.append("Inherited through a second continuation")

    result = execute_continuation(original, first, Reader(original, report_mutator=outstanding))
    assert result["stop_reason"] == "revision_budget_exhausted"
    parent = SimpleNamespace(f=fixture, run=first, plan=read(first / "plan.json"), history=result["history"],
        record=ExtractionRecord.model_validate(read(first / "candidate.json")),
        audit=ReadingAudit.model_validate(read(first / "model_review.json")))
    second = new_continuation(parent)
    proof = read(second / "audit_guidance.json")
    assert len(proof["frozen_nodes"]) == 2
    assert proof["frozen_nodes"][0]["original_audit_path"] == str(original.run / "audit.v00.json")
    reader = Reader(parent)
    finished = execute_continuation(parent, second, reader)
    assert reader.stages == ["revision_1", "audit_1"]
    assert finished["stop_reason"] == "audit_completed"
    report = read(second / "model_review.json")
    assert "Inherited through a second continuation" in report["coverage_limitations"]
    assert fixture.records._audit_provenance(second,
        ExtractionRecord.model_validate(read(second / "candidate.json")), finished, report)


@pytest.mark.parametrize("drop", ["latest_finding", "incremental_limitation"])
def test_new_audit_cannot_omit_inherited_delta_obligations(fixture, drop):
    parent = make_parent(fixture, "scientific")
    run = new_continuation(parent)

    def omit(report):
        if drop == "latest_finding":
            report.findings = [finding for finding in report.findings
                if not any(path.startswith("observations[1]")
                           for path in [finding.record_path, *finding.record_paths] if path)]
        else:
            report.coverage_limitations.remove("Later incremental limitation remains")

    result = execute_continuation(parent, run, Reader(parent, report_mutator=omit))
    assert result["stop_reason"] == "audit_report_incomplete"
    assert result["reading_check_issues"]
    assert not fixture.records._audit_provenance(run,
        ExtractionRecord.model_validate(read(run / "candidate.json")), result, read(run / "model_review.json"))


def test_no_progress_repair_stops_with_retained_candidate_without_full_audit(fixture):
    from watertreatment.audit_execution import ScientificReviewScopeUnavailable
    parent = make_parent(fixture, "full")
    run = new_continuation(parent)
    reader = Reader(parent, revise=False)
    with pytest.raises(ScientificReviewScopeUnavailable):
        execute_continuation(parent, run, reader)
    assert reader.stages == ["revision_1"]
    assert ExtractionRecord.model_validate(read(run / "candidate.v01.json")) == parent.record
    assert read(run / "scope_rejection.v01.json")["automatic_full_audit"] is False


def test_budget_admission_requires_real_extension_checkpoint_not_only_scientific_audit(fixture):
    parent = make_parent(fixture, "full")
    run = new_continuation(parent)
    result = execute_continuation(parent, run, Reader(parent))
    plan = read(run / "plan.json")
    plan["budget_continuation"] = {"path": str(run / "missing-grant.json"), "sha256": "a" * 64}
    rewrite(run / "plan.json", plan)
    assert not fixture.records._audit_provenance(run,
        ExtractionRecord.model_validate(read(run / "candidate.json")), result, read(run / "model_review.json"))
