"""Real prepared synthetic plans and grants with a source-tool fake provider.

These checks exercise authority/provenance and control flow, not scientific truth.
"""
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from test_web_records import fixture, read
from test_audit_guidance_resume import Reader, public_answer, rewrite, save_audit
from watertreatment.budget_extension_workflow import select_checkpoint, prepare_extension, verify_extension_plan
from watertreatment.budget_grants import preview_grant, reserve_grant, lineage_snapshot
from watertreatment.budget_policy import freeze_initial_policy
from watertreatment.codex import source_snapshot
from watertreatment.domain import ExtractionRecord
from watertreatment.reading import ReadingDossier, ReadingAudit, AuditFinding, SourceAnchor, AUDIT_DIMENSIONS
from watertreatment.reading_tools import ReadingTools
from watertreatment.storage import digest_json, hash_file, write_new_json
from watertreatment.workflow import prepare, execute_plan


def completed_model_call(run, plan, stage, answer, *, seconds, record=False, inputs=None):
    call = run / "calls" / stage
    request = {"stage": stage, "model": plan["model_settings"]["model"], "reasoning_effort": "max",
        "code_identity": plan["code_identity"], "rule_version": plan["rule_version"],
        "sources": plan["input_paths"] if inputs is None else source_snapshot(inputs),
        "prompt": "Source-bound synthetic " + stage}
    write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    write_new_json(call / "answer.json", answer.model_dump(mode="json"))
    if record:
        write_new_json(call / "record.json", answer.model_dump(mode="json"))
    thread, turn = public_answer(run, call, answer.model_dump(mode="json"))
    write_new_json(call / "result.json", {"status": "schema_valid_candidate", "elapsed_seconds": seconds,
        "thread_id": thread, "turn_id": turn, "server_selected_model": plan["model_settings"]["model"], "server_selected_effort": "max"})


def prepared_parent(f):
    repo = Path(__file__).resolve().parents[1]
    settings = f.settings
    settings.rules_version = "0.7"
    settings.codex.model, settings.codex.reasoning_effort = "gpt-6.2-sol", "max"
    settings.codex.max_calls_per_run, settings.codex.timeout_seconds = 3, 120
    settings.reading.enabled, settings.reading.max_revisions = True, 1
    settings.reading.total_timeout_seconds = 120
    (settings.project_root / "config").mkdir(parents=True)
    shutil.copytree(repo / "prompts", settings.project_root / "prompts")
    shutil.copyfile(repo / "config/taxonomy.v0.5.json", settings.taxonomy_path)
    imported = f.store.new_run("import", {"paper_id": f.record.paper.id})
    bundle = read(f.run / "sources.json")
    bundle.update(paper_id=f.record.paper.id, renderings=[])
    write_new_json(imported / "sources.json", bundle)
    f.store.event(imported.name, "import_completed", {"bundle_sha256": hash_file(imported / "sources.json")})
    run = prepare(settings, imported / "sources.json", cohort_role="unassigned")
    policy = {"initial_seconds": 120, "initial_calls": 3, "addition_seconds": 120, "addition_calls": 2,
        "max_auto_additions": 2, "max_total_seconds": 360, "max_total_calls": 7}
    freeze_initial_policy(settings, run, policy, "synthetic-guidance-batch")
    plan = read(run / "plan.json")
    record = f.record.model_copy(deep=True)
    record.rule_version, record.schema_version, record.extraction_scope = "0.7", "0.3.0", "research_core"
    for observation in record.observations:
        observation.analysis_role = "key_result"
    dossier = ReadingDossier(paper_id=record.paper.id, article_question_and_design="Synthetic retained source design",
        material_map=[], experiment_map=[], coverage=record.coverage, cross_source_conflicts=[],
        unresolved_questions=[], schema_gaps=[])
    write_new_json(run / "reading_dossier.json", dossier.model_dump(mode="json"))
    completed_model_call(run, plan, "comprehension", dossier, seconds=40)
    completed_model_call(run, plan, "extraction", record, seconds=40, record=True)
    baseline = ReadingAudit(checked_dimensions=list(AUDIT_DIMENSIONS), potential_omissions=[],
        coverage_limitations=["Synthetic retained limitation"], findings=[AuditFinding(
            record_path="observations[0].value", severity="major", verdict="unsupported", needs_revision=True,
            explanation="Source attribution requires a bounded correction",
            evidence=[SourceAnchor(source_id="main", block_id="paragraph")])])
    parent = SimpleNamespace(f=f, run=run, plan=plan, history=[],
        tools=ReadingTools(bundle, run, max_calls=30, expected_inputs=plan["input_paths"]))
    save_audit(parent, record, baseline, 0)
    outcome = read(parent.call / "result.json")
    outcome["elapsed_seconds"] = 40
    rewrite(parent.call / "result.json", outcome)
    return parent


class CoreReader(Reader):
    def __init__(self, parent, settings, *, report_mutator=None):
        super().__init__(parent, report_mutator=report_mutator)
        self.settings, self.live = settings.codex, True
        self.total_timeout_seconds = settings.reading.total_timeout_seconds

    def close(self):
        pass

    def complete(self, model, **kwargs):
        answer = super().complete(model, **kwargs)
        stage, run = kwargs["identity"]["stage"], Path(kwargs["workdir"])
        if stage.startswith("revision_"):
            completed_model_call(run, read(run / "plan.json"), stage, answer,
                seconds=60, record=True, inputs=kwargs["input_paths"])
        else:
            path = run / "calls/audit-1/result.json"
            completed = read(path)
            completed["elapsed_seconds"] = 60
            rewrite(path, completed)
        return answer


def test_root_grant_path_repeated_guidance_keeps_source_audits_and_no_full_restart(fixture):
    root = prepared_parent(fixture)
    root_hashes = {path: hash_file(path) for path in root.run.rglob("*") if path.is_file()}

    def remaining_concern(report):
        report.findings.append(AuditFinding(record_path="observations[1].value.note", severity="major",
            verdict="unsupported", needs_revision=True, explanation="A newly localized source concern remains",
            evidence=[SourceAnchor(source_id="main", block_id="paragraph")]))
        report.coverage_limitations.append("Retained across separate budget grants")

    parent = root
    for iteration in range(2):
        grant = preview_grant(fixture.settings, parent.run, mode="automatic")
        selection = select_checkpoint(fixture.settings, parent.run, grant)
        assert selection["route"] == "candidate" and selection["guidance"]
        assert selection["minimum_calls"] == 2
        descriptor = reserve_grant(fixture.settings, parent.run, grant, digest_json(selection), mode="automatic")
        settings, run, executor = prepare_extension(fixture.settings, descriptor, selection)
        assert executor is execute_plan
        assert verify_extension_plan(settings, run)["parent_run"] == parent.run.name
        client = CoreReader(parent, settings, report_mutator=remaining_concern if iteration == 0 else None)
        executor(settings, run / "plan.json", client=client, verify=True)
        assert client.stages == ["revision_1", "audit_1"]
        result, audit = read(run / "result.json"), read(run / "model_review.json")
        assert result["stop_reason"] == ("revision_budget_exhausted" if iteration == 0 else "audit_completed")
        record = ExtractionRecord.model_validate(read(run / "candidate.json"))
        assert fixture.records._audit_provenance(run, record, result, audit)
        assert result["graph_published"] is False and result["human_validated"] is False
        assert "Retained across separate budget grants" in audit["coverage_limitations"]
        parent = SimpleNamespace(f=fixture, run=run, plan=read(run / "plan.json"), history=result["history"],
            record=record, audit=ReadingAudit.model_validate(audit))
    snapshot = lineage_snapshot(fixture.settings, parent.run)
    assert (snapshot["used_calls"], snapshot["used_seconds"]) == (7, 360)
    assert len(read(parent.run / "audit_guidance.json")["frozen_nodes"]) == 2
    assert all(hash_file(path) == sha for path, sha in root_hashes.items())
