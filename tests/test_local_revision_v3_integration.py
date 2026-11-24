"""Synthetic native repair -> new v3 plan -> source-bound delta admission.

The original v2 run, native submissions and audit findings remain immutable.
No provider or graph connection is used; these are protocol tests, not accuracy
claims about real literature.
"""
import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_web_records import fixture, read
from test_audit_guidance_resume import public_answer, rewrite, save_audit
from test_budget_guidance_integration import completed_model_call
from test_staged_source_resume import add_patch, load_events, save_events, turn
from test_scientific_revision_audit import report_for
from test_source_review import source_case, scope_for
from watertreatment.codex import source_snapshot
from watertreatment.completed_native_resume import authenticate_completed_revision
from watertreatment.domain import (ExtractionRecord, CoverageEntry, Fact, TimeWindow, ResearchDesignScope,
    ResearchEligibility, CoreResearchCheck, ResearchObservationAssignment, ResearchSourceIndex)
from watertreatment.design_scope import CORE_AREAS, RESEARCH_USES
from watertreatment.reading import ReadingDossier, ReadingAudit, ReadingScopeEntry, AuditFinding, SourceAnchor, AUDIT_DIMENSIONS
from watertreatment.reading_tools import ReadingTools
from watertreatment.scientific_revision_audit import NEW_SCOPE_LIMITS, build_scientific_revision_scope
from watertreatment.storage import digest_json, hash_file, write_new_json
from watertreatment.workflow import prepare, execute_plan


V2 = {**NEW_SCOPE_LIMITS, "compiler_version": 2}
LIMITED_ROW = f"design_scope.eligibility[{len(RESEARCH_USES)}]"


def native_parent(f, *, missing_dossier_disposition=False, defect=None, with_eligibility_restriction=False):
    repo, settings = Path(__file__).resolve().parents[1], f.settings
    settings.rules_version = "0.8" if with_eligibility_restriction else "0.7"
    settings.codex.model, settings.codex.reasoning_effort = "gpt-6.2-sol", "max"
    settings.codex.max_calls_per_run, settings.codex.timeout_seconds = 7, 300
    settings.reading.enabled, settings.reading.max_revisions = True, 2
    settings.reading.total_timeout_seconds = 1200
    (settings.project_root / "config").mkdir(parents=True)
    shutil.copytree(repo / "prompts", settings.project_root / "prompts")
    shutil.copyfile(repo / "config/taxonomy.v0.5.json", settings.taxonomy_path)
    definition = "The 0-60 min axis describes the associated test; no independent calculation interval is stated."
    f.source.write_text(f.source.read_text(encoding="utf-8") + "\n" + definition +
        "\nAn additional synthetic note has no quantitative outcome.", encoding="utf-8")
    original = f.store.import_file(f.source)
    bundle = read(f.run / "sources.json")
    bundle.update(paper_id=f.record.paper.id, renderings=[])
    source = bundle["documents"][0]["source"]
    source.update(path=original["stored_path"], sha256=original["sha256"], size_bytes=original["size_bytes"])
    bundle["documents"][0]["blocks"].extend([
        {"block_id": "definition", "source_id": "main", "kind": "paragraph", "order": 2, "text": definition},
        {"block_id": "missing-note", "source_id": "main", "kind": "paragraph", "order": 3,
         "text": "An additional synthetic note has no quantitative outcome."}])
    imported = f.store.new_run("import", {"paper_id": f.record.paper.id})
    write_new_json(imported / "sources.json", bundle)
    f.store.event(imported.name, "import_completed", {"bundle_sha256": hash_file(imported / "sources.json")})
    run = prepare(settings, imported / "sources.json", cohort_role="unassigned")
    # Before any synthetic model work, freeze the historical v2 policy which
    # produced the retained scope rejection. The new continuation must be v3.
    plan = read(run / "plan.json")
    plan["scientific_scope_limits"] = dict(V2)
    plan["model_settings"]["model"] = "gpt-6.1-sol"
    rewrite(run / "plan.json", plan)
    manifest = read(run / "manifest.json")
    manifest["model_requested"] = plan["model_settings"]["model"]
    rewrite(run / "manifest.json", manifest)
    f.store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    record = f.record.model_copy(deep=True)
    record.rule_version, record.schema_version, record.extraction_scope = "0.7", "0.3.0", "research_core"
    if with_eligibility_restriction:
        record.rule_version, record.schema_version = "0.8", "0.4.0"
    for observation in record.observations:
        observation.analysis_role = "key_result"
    record.coverage = [CoverageEntry(source_id="main", block_ids=["paragraph", "definition", "missing-note"],
        state="read_structured", text_read=True)]
    record.observations[0].time_window = TimeWindow(kind="treatment",
        start=Fact(value=0, status="reported", evidence_ids=["source-value"]),
        end=Fact(value=60, status="reported", evidence_ids=["source-value"]),
        unit=Fact(value="min", status="reported", evidence_ids=["source-value"]),
        reference=Fact(value="Treatment interval", status="reported", evidence_ids=["source-value"]))
    if with_eligibility_restriction:
        record.design_scope = ResearchDesignScope(
            core_checks=[CoreResearchCheck(area=area, state="checked", reason="Synthetic source checked; missing values retained.")
                for area in sorted(CORE_AREAS)],
            observation_assignments=[ResearchObservationAssignment(observation_id=item.id, layer="core",
                purpose="performance", reason="Synthetic final-system result or explicit missing result.") for item in record.observations],
            source_index=[ResearchSourceIndex(id="index-" + block_id, source_id="main", block_id=block_id,
                content_types=["text"], reading_state="read", summary="Synthetic text and its limitations were read.",
                material_relation="The final sample and its bounded experiment.", reason="Relevant source context retained.")
                for block_id in ("paragraph", "definition", "missing-note")],
            eligibility=[ResearchEligibility(use=use, target_kind="paper", target_id=record.paper.id,
                status="not_assessed", reason="Downstream use remains pending explicit review.") for use in sorted(RESEARCH_USES)])
    dossier = ReadingDossier(paper_id=record.paper.id, article_question_and_design="Synthetic source-bound design",
        material_map=[], experiment_map=[], coverage=copy.deepcopy(record.coverage), cross_source_conflicts=[],
        unresolved_questions=[], schema_gaps=[])
    if with_eligibility_restriction:
        dossier.scope_outline = [ReadingScopeEntry(layer="core", subject="Synthetic final-system performance",
            purpose="Retain key results, exact time semantics, source evidence and downstream-use limits.",
            planned_action="Read each available source block before extracting the bounded research core.",
            evidence=[SourceAnchor(source_id="main", block_id="definition")])]
    if missing_dossier_disposition:
        dossier.coverage[0].block_ids.remove("missing-note")
    write_new_json(run / "reading_dossier.json", dossier.model_dump(mode="json"))
    completed_model_call(run, plan, "comprehension", dossier, seconds=1)
    completed_model_call(run, plan, "extraction", record, seconds=1, record=True)
    baseline = ReadingAudit(checked_dimensions=list(AUDIT_DIMENSIONS), potential_omissions=[],
        coverage_limitations=["Synthetic missing calculation interval remains"], findings=[AuditFinding(
            record_path="observations[0].time_window", severity="major", verdict="unsupported", needs_revision=True,
            explanation="Use the original definition and qualify the associated interval, preserving its numbers.",
            evidence=[SourceAnchor(source_id="main", block_id="definition")])])
    parent = SimpleNamespace(f=f, run=run, plan=plan, history=[],
        tools=ReadingTools(bundle, run, max_calls=30, expected_inputs=plan["input_paths"]),
        events_path=run / "app_server/events.jsonl", bundle=bundle, imported=imported)
    save_audit(parent, record, baseline, 0)
    parent.tools.prepare_revision(record)
    parent.tools.begin_submission(True)
    binding = copy.deepcopy(parent.tools.candidate_binding)
    save_events(parent, [*load_events(parent), turn("turn/started", "failed-thread", "failed-turn")])
    new_evidence = {"id": "new-definition", "source_id": "main", "block_id": "definition", "quote": definition}
    if defect == "unknown_source":
        new_evidence["source_id"] = "outside-paper"
    elif defect == "bad_page":
        new_evidence["locator"] = {"physical_page": 999}
    edits = [{"operation": "append", "path": ["evidence"], "value": new_evidence},
        {"operation": "set", "path": ["observations", 0, "time_window", "kind"], "value": "associated_test"}]
    for name in ("start", "end", "unit", "reference"):
        edits.extend([
            {"operation": "set", "path": ["observations", 0, "time_window", name, "status"], "value": "inferred"},
            {"operation": "set", "path": ["observations", 0, "time_window", name, "evidence_ids"], "value": ["new-definition"]}])
    if defect == "old_evidence":
        edits.append({"operation": "set", "path": ["evidence", 0, "quote"], "value": "Changed old source attribution"})
    if with_eligibility_restriction:
        edits.append({"operation": "append", "path": ["design_scope", "eligibility"], "value": {
            "use": "performance_pairing", "target_kind": "experiment", "target_id": "joint", "status": "limited",
            "reason": "The plotted test interval does not identify an independent calculation interval.",
            "evidence_ids": ["new-definition"],
            "limitations": ["The associated interval cannot establish an independently timed performance pair."]}})
    parent.stage = add_patch(parent, edits)
    parent.candidate_path = run / "candidate.v01.json"
    call = run / "calls/revision_1"
    for destination in (parent.candidate_path, run / "candidate.json", call / "record.json"):
        destination.parent.mkdir(exist_ok=True, parents=True)
        shutil.copyfile(parent.stage, destination)
    data = read(parent.stage.parent / "result.json")["data"]
    answer = {"submission_id": data["submission_id"]}
    request = {"stage": "revision_1", "model": plan["model_settings"]["model"], "reasoning_effort": "max",
        "code_identity": plan["code_identity"], "rule_version": plan["rule_version"], "candidate_binding": binding,
        "prompt": "CANDIDATE_SNAPSHOT " + binding["content_hash"],
        "sources": source_snapshot([*[Path(x["path"]) for x in plan["input_paths"]], Path(binding["path"])])}
    write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    write_new_json(call / "answer.json", answer)
    write_new_json(call / "result.json", {"status": "schema_valid_candidate", "elapsed_seconds": 1,
        "thread_id": "failed-thread", "turn_id": "failed-turn", "submission_id": data["submission_id"],
        "server_selected_model": plan["model_settings"]["model"], "server_selected_effort": "max"})
    events = load_events(parent)
    events.extend([
        {"direction": "receive", "message": {"method": "item/completed", "params": {"threadId": "failed-thread",
            "turnId": "failed-turn", "item": {"type": "agentMessage", "phase": "final_answer", "text": json.dumps(answer)}}}},
        turn("turn/completed", "failed-thread", "failed-turn", "completed")])
    save_events(parent, events)
    parent.revised = ExtractionRecord.model_validate(read(parent.candidate_path))
    diagnostics = []
    assert build_scientific_revision_scope(record, parent.revised, baseline,
        baseline_origin="fresh_model_audit", diagnostics=diagnostics, **V2) is None
    write_new_json(run / "scope_rejection.v01.json", {"diagnostics": diagnostics, "automatic_full_audit": False})
    rewrite(run / "result.json", {"status": "candidate_incomplete_failure", "last_candidate_version": "candidate.v01.json",
        "model_calls": 4, "human_validated": False, "graph_published": False, "dataset_role": "unassigned"})
    write_new_json(run / "failure.json", {"error": "Synthetic retained v2 scope rejection", "model_calls": 4})
    return parent


def continue_native(parent):
    settings = parent.f.settings.model_copy(deep=True)
    settings.codex.max_calls_per_run = 1
    settings.reading.max_revisions = 0
    authenticate_completed_revision(settings, parent.candidate_path)
    run = prepare(settings, parent.imported / "sources.json", cohort_role="unassigned",
        reuse_dossier=parent.run / "reading_dossier.json", reuse_candidate=parent.candidate_path, reuse_source_audit=True)
    return settings, run


class DeltaReader:
    def __init__(self, settings, *, omit_new_source=False, keep_blocker=False, omit_evidence_disposition=False,
                 omit_eligibility_disposition=False):
        self.settings, self.live, self.calls_started, self.stages = settings.codex, True, 0, []
        self.total_timeout_seconds = settings.reading.total_timeout_seconds
        self.omit_new_source, self.keep_blocker = omit_new_source, keep_blocker
        self.omit_evidence_disposition = omit_evidence_disposition
        self.omit_eligibility_disposition = omit_eligibility_disposition

    def bind_tools(self, tools): self.tools = tools
    def close(self): pass

    def complete(self, model, *, prompt, input_paths, workdir, audit_root, identity, image_paths, conversation_key):
        self.calls_started += 1; self.stages.append(identity["stage"])
        assert identity["stage"] == "audit_0"
        run, call = Path(workdir), Path(audit_root) / "delta"
        receipt = identity["citation_review"]
        scope = read(Path(receipt["scope_path"]))
        assert scope["scope_limits"]["compiler_version"] == NEW_SCOPE_LIMITS["compiler_version"]
        baseline = ReadingAudit.model_validate(read(Path(receipt["baseline_audit_path"])))
        report = report_for(scope, baseline)
        if self.omit_evidence_disposition:
            report.findings = [finding for finding in report.findings
                if not (finding.record_path or "").startswith("evidence[")]
        if self.omit_eligibility_disposition:
            report.findings = [finding for finding in report.findings
                if finding.record_path != LIMITED_ROW]
        if self.keep_blocker:
            report.findings.append(baseline.findings[0].model_copy(deep=True))
        self.tools.begin_query_turn(); self.tools.begin_source_review(scope, call_id=call.name)
        assert self.tools.handle("wt_query_candidate", {"path": ["observations", 0]})["success"]
        assert self.tools.handle("wt_query_candidate", {"path": ["evidence", 1]})["success"]
        if any(change["kind"] == "eligibility_restriction" for change in scope["changes"]):
            assert self.tools.handle("wt_query_candidate", {"path": ["design_scope", "eligibility", len(RESEARCH_USES)]})["success"]
            assert self.tools.handle("wt_query_candidate", {"path": ["experiments", 0]})["success"]
        assert self.tools.handle("wt_read_blocks", {"source_id": "main", "start_order": 1, "end_order": 1})["success"]
        if not self.omit_new_source:
            assert self.tools.handle("wt_read_blocks", {"source_id": "main", "start_order": 2, "end_order": 2})["success"]
        proof = self.tools.source_review_proof()
        request = {**identity, "model": "gpt-6.2-sol", "reasoning_effort": "max",
            "prompt": prompt + " CANDIDATE_SNAPSHOT " + self.tools.candidate_binding["content_hash"],
            "candidate_binding": self.tools.candidate_binding, "source_review_call": call.name,
            "source_review_scope_sha256": scope["scope_sha256"],
            "sources": source_snapshot([*input_paths, Path(self.tools.candidate_binding["path"])])}
        write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
        write_new_json(call / "answer.json", report.model_dump(mode="json"))
        thread, turn_id = public_answer(run, call, report.model_dump(mode="json"))
        write_new_json(call / "result.json", {"status": "schema_valid_candidate", "elapsed_seconds": 1,
            "thread_id": thread, "turn_id": turn_id, "server_selected_model": "gpt-6.2-sol",
            "server_selected_effort": "max", "source_review_proof": proof})
        return report


def test_real_completed_native_v2_repair_resumes_as_v3_delta_and_admits_sources(fixture):
    parent = native_parent(fixture)
    before = {p: hash_file(p) for p in parent.run.rglob("*") if p.is_file()}
    settings, run = continue_native(parent)
    client = DeltaReader(settings)
    execute_plan(settings, run / "plan.json", client=client)
    result, audit = read(run / "result.json"), read(run / "model_review.json")
    assert client.stages == ["audit_0"] and result["stop_reason"] == "audit_completed"
    assert read(parent.run / "plan.json")["model_settings"]["model"] == "gpt-6.1-sol"
    assert read(run / "plan.json")["model_settings"]["model"] == "gpt-6.2-sol"
    assert read(run / "calls" / "delta" / "result.json")["server_selected_model"] == "gpt-6.2-sol"
    assert fixture.records._audit_provenance(run, parent.revised, result, audit)
    assert read(run / "plan.json")["scientific_scope_limits"] == NEW_SCOPE_LIMITS
    assert read(parent.run / "plan.json")["scientific_scope_limits"]["compiler_version"] == 2
    assert read(parent.run / "scope_rejection.v01.json")["diagnostics"]
    assert all(hash_file(path) == sha for path, sha in before.items())
    assert result["human_validated"] is False and result["graph_published"] is False


def test_new_anchor_requires_current_source_read_even_with_earlier_coverage(fixture):
    settings, run = continue_native(native_parent(fixture))
    client = DeltaReader(settings, omit_new_source=True)
    with pytest.raises(ValueError, match="source review delivery incomplete"):
        execute_plan(settings, run / "plan.json", client=client)
    assert client.stages == ["audit_0"]
    assert read(run / "result.json")["status"] == "candidate_incomplete_failure"
    assert not (run / "model_review.json").exists()


def test_delta_never_claims_unrelated_missing_dossier_disposition_was_read(fixture):
    settings, run = continue_native(native_parent(fixture, missing_dossier_disposition=True))
    execute_plan(settings, run / "plan.json", client=DeltaReader(settings))
    result = read(run / "result.json")
    assert result["stop_reason"] == "coverage_correction_required"
    assert result["status"] == "candidate_incomplete_reading_coverage"
    assert any("missing-note" in str(item) for item in result["reading_check_issues"])


def test_original_scientific_blocker_cannot_be_promoted_to_completion(fixture):
    settings, run = continue_native(native_parent(fixture))
    execute_plan(settings, run / "plan.json", client=DeltaReader(settings, keep_blocker=True))
    result = read(run / "result.json")
    assert result["stop_reason"] != "audit_completed"
    assert result["history"][-1]["needs_revision"] is True


@pytest.mark.parametrize("defect", ["unknown_source", "old_evidence"])
def test_new_plan_rejects_out_of_source_references_and_modified_old_evidence(fixture, defect):
    parent = native_parent(fixture, defect=defect)
    with pytest.raises(ValueError):
        continue_native(parent)


def test_existing_block_with_invented_physical_page_cannot_be_source_admitted(fixture):
    settings, run = continue_native(native_parent(fixture, defect="bad_page"))
    with pytest.raises(ValueError, match="visual locator disagrees"):
        execute_plan(settings, run / "plan.json", client=DeltaReader(settings))
    assert read(run / "result.json")["status"] == "candidate_incomplete_failure"


def test_new_evidence_requires_its_own_scientific_disposition(fixture):
    settings, run = continue_native(native_parent(fixture))
    execute_plan(settings, run / "plan.json", client=DeltaReader(settings, omit_evidence_disposition=True))
    result = read(run / "result.json")
    assert result["stop_reason"] == "audit_report_incomplete"
    assert not fixture.records._audit_provenance(run,
        ExtractionRecord.model_validate(read(run / "candidate.json")), result, read(run / "model_review.json"))


def test_source_bound_eligibility_restriction_enters_delta_closure_and_admission(fixture):
    parent = native_parent(fixture, with_eligibility_restriction=True)
    before = {p: hash_file(p) for p in parent.run.rglob("*") if p.is_file()}
    settings, run = continue_native(parent)
    client = DeltaReader(settings)
    execute_plan(settings, run / "plan.json", client=client)
    scope = read(run / "citation_scope.v00.json")
    affected = {item["record_path"] for item in scope["entity_refs"]}
    assert {LIMITED_ROW, "experiments[0]", "observations[0]", "observations[1]", "evidence[1]"} <= affected
    restriction = next(change for change in scope["changes"] if change["kind"] == "eligibility_restriction")
    assert restriction["changed_path"] == LIMITED_ROW
    assert restriction["required_evidence_ids"] == ["new-definition"]
    result, audit = read(run / "result.json"), read(run / "model_review.json")
    record = ExtractionRecord.model_validate(read(run / "candidate.json"))
    assert client.stages == ["audit_0"] and result["stop_reason"] == "audit_completed"
    assert fixture.records._audit_provenance(run, record, result, audit)
    assert record.design_scope.eligibility == parent.revised.design_scope.eligibility
    assert record.design_scope.eligibility[-1].status == "limited"
    assert all(hash_file(path) == sha for path, sha in before.items())
    assert result["human_validated"] is False and result["graph_published"] is False


def test_new_eligibility_restriction_requires_its_own_source_disposition(fixture):
    parent = native_parent(fixture, with_eligibility_restriction=True)
    settings, run = continue_native(parent)
    client = DeltaReader(settings, omit_eligibility_disposition=True)
    execute_plan(settings, run / "plan.json", client=client)
    result = read(run / "result.json")
    assert client.stages == ["audit_0"] and result["stop_reason"] == "audit_report_incomplete"
    assert not fixture.records._audit_provenance(run,
        ExtractionRecord.model_validate(read(run / "candidate.json")), result, read(run / "model_review.json"))


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_context_navigation_boundary_changes_only_explicit_v3_prompts(version):
    from watertreatment.audit_execution import scientific_review_prompt
    scope = {"scope_limits": {"compiler_version": version}, "changes": [], "entity_refs": [], "source_anchors": []}
    baseline = ReadingAudit(checked_dimensions=list(AUDIT_DIMENSIONS), findings=[], potential_omissions=[], coverage_limitations=[])
    prompt = scientific_review_prompt(scope, baseline)
    inherited_context = "Context entries support navigation and inherited baseline understanding"
    assert (inherited_context in prompt) is (version >= 3)
    assert ("exact cumulative changes and their direct consumers, not a new extraction." in prompt) is (version < 3)


@pytest.mark.parametrize("version,requires_visual", [(1, False), (2, True), (3, True), (4, True)])
def test_pdf_blocker_full_page_requirement_keeps_each_frozen_version(source_case, version, requires_visual):
    from watertreatment.source_review import review_requirements
    scope = scope_for(source_case.docs)
    scope["scope_limits"] = {"compiler_version": version}
    for anchor in scope["source_anchors"]:
        anchor["origin"] = "prior_audit_blocker"
    scope["scope_sha256"] = digest_json({key: value for key, value in scope.items() if key != "scope_sha256"})
    requirements = review_requirements(source_case.run, scope, source_case.call_id)
    assert all(item["requires_full_view"] is requires_visual for item in requirements)


def test_original_v1_scope_remains_admissible_when_old_plan_omits_limits(fixture, monkeypatch):
    import test_audit_guidance_resume as helpers
    monkeypatch.setattr(helpers, "NEW_SCOPE_LIMITS", {"max_entities": 200, "max_changes": 180})
    parent = helpers.make_parent(fixture, "scientific")
    scope_before = read(parent.run / "citation_scope.v01.json")
    assert "compiler_version" not in scope_before["scope_limits"]
    parent.plan.pop("scientific_scope_limits")
    rewrite(parent.run / "plan.json", parent.plan)
    fixture.store.event(parent.run.name, "prepared", {"plan_sha256": hash_file(parent.run / "plan.json")})
    assert fixture.records._audit_provenance(parent.run, parent.record,
        read(parent.run / "result.json"), parent.audit.model_dump(mode="json"))
    assert read(parent.run / "citation_scope.v01.json") == scope_before


def test_new_guidance_plan_can_choose_current_compiler_while_parent_report_remains_v2(fixture, monkeypatch):
    import test_audit_guidance_resume as helpers
    from watertreatment.audit_guidance_resume import freeze_guidance, verify_guidance
    monkeypatch.setattr(helpers, "NEW_SCOPE_LIMITS", dict(V2))
    parent = helpers.make_parent(fixture, "scientific")
    before = {p: hash_file(p) for p in parent.run.rglob("*") if p.is_file()}
    original_freeze = freeze_guidance

    def explicit_new_policy(settings, run, old_run, candidate):
        plan = read(run / "plan.json")
        plan["scientific_scope_limits"] = dict(NEW_SCOPE_LIMITS)
        rewrite(run / "plan.json", plan)
        fixture.store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
        return original_freeze(settings, run, old_run, candidate)

    monkeypatch.setattr(helpers, "freeze_guidance", explicit_new_policy)
    new_run = helpers.new_continuation(parent)
    proof = read(new_run / "audit_guidance.json")
    assert proof["parent_scope_limits"]["compiler_version"] == 2
    assert proof["scope_limits"] == NEW_SCOPE_LIMITS
    assert verify_guidance(fixture.settings, new_run)
    assert all(hash_file(path) == sha for path, sha in before.items())
