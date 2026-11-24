"""Remaining-stage budgets use real offline prepare/reader state transitions."""
import json
import shutil
from pathlib import Path

import pytest

from test_active_reading import active, workspace, FakeReader
from test_staged_continuation_workflow import staged_plan_case, staged_cli, fixture, make_plan
from watertreatment.domain import ExtractionRecord
from watertreatment.reading import ReadingAudit, AuditFinding
from watertreatment.storage import hash_file
from watertreatment.workflow import execute_plan, prepare


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


class CorrectReader(FakeReader):
    def __init__(self, settings, bundle, *, repair=False):
        super().__init__(settings, bundle)
        self.repair = repair

    def complete(self, model, **kwargs):
        output = super().complete(model, **kwargs)
        if model is ExtractionRecord:
            output.paper.id = "paper-test"
        if model is ReadingAudit and self.repair and kwargs["identity"]["stage"] == "audit_0":
            output.findings = [AuditFinding(record_path="paper.title", severity="minor",
                verdict="unsupported", needs_revision=True,
                explanation="Synthetic fixture needs a bounded correction", evidence=[])]
        return output


def seed(active, *, repair=False):
    workspace, run, bundle, _ = active
    execute_plan(workspace.settings, run / "plan.json", client=CorrectReader(workspace.settings, bundle, repair=repair))
    checkpoint = run / "candidate.v00.json"
    candidate = ExtractionRecord.model_validate(read(checkpoint))
    # Synthetic provider receipts exercise the existing lineage verifier, rather
    # than replacing it with a stub. They authorize no real provider or evidence.
    for stage, answer, record in [("comprehension", "reading_dossier.json", None),
                                   ("extraction", None, "candidate.v00.json"),
                                   ("audit_0", "audit.v00.json", None)]:
        call = run / "calls" / ("offline-proof-" + stage)
        call.mkdir(parents=True)
        (call / "request.json").write_text(json.dumps({"request": {"stage": stage, "model": workspace.settings.codex.model, "reasoning_effort": "max",
            "prompt": candidate.model_dump_json(exclude_defaults=True)}}), encoding="utf-8")
        (call / "result.json").write_text(json.dumps({"status": "schema_valid_candidate",
            "server_selected_model": "gpt-6.2-sol", "server_selected_effort": "max"}), encoding="utf-8")
        if answer:
            shutil.copyfile(run / answer, call / "answer.json")
        if record:
            shutil.copyfile(run / record, call / "record.json")
    bundle_path = Path(read(run / "manifest.json")["source_bundle"])
    return workspace, run, bundle, checkpoint, bundle_path


@pytest.mark.parametrize("repair,budget,expected", [
    (False, 1, ["audit_0"]),
    (True, 2, ["revision_1", "audit_1"]),
])
def test_resume_minimum_budget_runs_only_remaining_stages(active, repair, budget, expected):
    workspace, parent, bundle, checkpoint, source = seed(active, repair=repair)
    old_hashes = {p: hash_file(p) for p in parent.rglob("*") if p.is_file()}
    workspace.settings.codex.max_calls_per_run = budget
    run = prepare(workspace.settings, source, cohort_role="development",
        reuse_dossier=parent / "reading_dossier.json", reuse_candidate=checkpoint)
    plan = read(run / "plan.json")
    assert plan["max_calls"] == plan["model_settings"]["max_calls_per_run"] == budget
    client = CorrectReader(workspace.settings, bundle)
    execute_plan(workspace.settings, run / "plan.json", client=client)
    assert [stage for stage, _ in client.stages] == expected
    result = read(run / "result.json")
    assert result["stop_reason"] == "audit_completed"
    assert result["model_calls"] == budget
    assert not result["human_validated"] and not result["graph_published"]
    assert {p: hash_file(p) for p in old_hashes} == old_hashes


def test_reused_dossier_needs_extraction_and_audit_only(active):
    workspace, parent, bundle, _, source = seed(active)
    workspace.settings.codex.max_calls_per_run = 2
    run = prepare(workspace.settings, source, cohort_role="development",
        reuse_dossier=parent / "reading_dossier.json")
    client = CorrectReader(workspace.settings, bundle)
    execute_plan(workspace.settings, run / "plan.json", client=client)
    assert [stage for stage, _ in client.stages] == ["extraction", "audit_0"]
    assert read(run / "result.json")["stop_reason"] == "audit_completed"


@pytest.mark.parametrize("reuse_dossier,repair,budget,required", [
    (False, False, 2, 3),
    (True, False, 1, 2),
    (True, True, 1, 2),
])
def test_short_budget_is_rejected_before_a_new_run(active, reuse_dossier, repair, budget, required):
    workspace, parent, _, checkpoint, source = seed(active, repair=repair)
    workspace.settings.codex.max_calls_per_run = budget
    before = set((workspace.data / "runs").iterdir())
    kwargs = {"reuse_dossier": parent / "reading_dossier.json"} if reuse_dossier else {}
    if repair:
        kwargs["reuse_candidate"] = checkpoint
    with pytest.raises(ValueError, match=f"at least {required} calls"):
        prepare(workspace.settings, source, cohort_role="development", **kwargs)
    assert set((workspace.data / "runs").iterdir()) == before


def test_execute_checks_remaining_client_budget_even_with_a_reused_seed(active):
    workspace, parent, bundle, checkpoint, source = seed(active)
    workspace.settings.codex.max_calls_per_run = 1
    run = prepare(workspace.settings, source, cohort_role="development",
        reuse_dossier=parent / "reading_dossier.json", reuse_candidate=checkpoint)
    client = CorrectReader(workspace.settings, bundle)
    client.calls_started = 1
    with pytest.raises(ValueError, match="call budget cannot cover"):
        execute_plan(workspace.settings, run / "plan.json", client=client)
    assert client.stages == []
    assert not (run / "execution_started.json").exists()


def test_low_call_budget_does_not_skip_candidate_lineage_checks(active):
    workspace, parent, _, checkpoint, source = seed(active)
    workspace.settings.codex.max_calls_per_run = 1
    record = read(checkpoint)
    record["paper"]["title"]["value"] = "untrusted replacement"
    checkpoint.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="completed Sol MAX record"):
        prepare(workspace.settings, source, cohort_role="development",
            reuse_dossier=parent / "reading_dossier.json", reuse_candidate=checkpoint)


def test_staged_pre_repair_requires_an_audit_after_the_repair(staged_plan_case):
    case = staged_plan_case
    case.f.settings.codex.max_calls_per_run = 1
    before = set((case.f.settings.data_root / "runs").iterdir())
    with pytest.raises(ValueError, match="at least 2 calls"):
        make_plan(case)
    assert set((case.f.settings.data_root / "runs").iterdir()) == before
