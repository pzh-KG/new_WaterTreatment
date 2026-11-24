"""Offline bounded correction with real local tool receipts, no provider/graph."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_web_records import fixture, read
from test_audit_addendum_workflow import events
from watertreatment.codex import source_snapshot
from watertreatment.coverage_correction import (CoverageDisposition, CoverageScientificConcern,
    build_coverage_correction_scope)
from watertreatment.coverage_correction_workflow import (CoverageParent, prepare, execute,
    verify_coverage_correction_run)
from watertreatment.reading import ReadingAudit, ReadingDossier, SourceAnchor
from watertreatment.settings import CodexSettings
from watertreatment.sources import SourceDocument
from watertreatment.storage import digest_json, hash_file, write_new_json


@pytest.fixture
def case(fixture, monkeypatch):
    f = fixture
    f.settings.codex.model = "gpt-6.2-sol"
    f.settings.codex.reasoning_effort = "max"
    bundle = read(f.run / "sources.json")
    docs = [SourceDocument.model_validate(d) for d in bundle["documents"]]
    dossier = ReadingDossier(paper_id=f.record.paper.id, article_question_and_design="Synthetic material",
        material_map=[], experiment_map=[], coverage=[], cross_source_conflicts=[], unresolved_questions=[], schema_gaps=[])
    write_new_json(f.run / "reading_dossier.json", dossier.model_dump(mode="json"))
    candidate = f.run / "candidate.json"
    audit_path = f.run / "model_review.json"
    receipt = {"policy": "reading-coverage-correction-v1", "parent_run": f.run.name,
        "candidate_path": str(candidate), "candidate_sha256": hash_file(candidate),
        "audit_path": str(audit_path), "audit_sha256": hash_file(audit_path),
        "dossier_sha256": hash_file(f.run / "reading_dossier.json"),
        "source_bundle_sha256": hash_file(f.run / "sources.json")}
    parent = CoverageParent(f.run, read(f.run / "plan.json"), bundle, docs, f.record, dossier,
        ReadingAudit.model_validate(read(audit_path)), build_coverage_correction_scope(dossier, docs), receipt)
    # The source/protocol/candidate preservation checks remain real; ancestry is
    # isolated here because this test's parent audit is intentionally synthetic.
    monkeypatch.setattr("watertreatment.coverage_correction_workflow.authenticate_coverage_parent", lambda *a: parent)
    return SimpleNamespace(f=f, parent=parent)


class SyntheticClient:
    def __init__(self, plan, *, blocker=False, missing_delivery=False):
        self.settings = CodexSettings.model_validate(plan["model_settings"])
        self.total_timeout_seconds = plan["reading_settings"]["total_timeout_seconds"]
        self.calls_started, self.closed = 0, False
        self.events = None
        self.blocker, self.missing_delivery = blocker, missing_delivery

    def bind_tools(self, tools):
        self.tools = tools

    def close(self):
        self.closed = True

    def complete(self, model, **kw):
        run = kw["workdir"]
        call = kw["audit_root"] / "call-01-synthetic"
        call.mkdir(parents=True)
        scope = read(Path(kw["identity"]["citation_review"]["scope_path"]))
        self.tools.begin_query_turn()
        self.tools.begin_source_review(scope, call_id=call.name)
        assert self.tools.handle("wt_query_candidate", {"path": ["paper"]})["success"]
        if not self.missing_delivery:
            assert self.tools.handle("wt_read_blocks", {"source_id": "main", "start_order": 1, "end_order": 1})["success"]
        binding = self.tools.candidate_binding
        request = {**kw["identity"], "model": "gpt-6.2-sol", "reasoning_effort": "max",
            "candidate_binding": binding,
            "prompt": kw["prompt"] + "\nCANDIDATE_SNAPSHOT " + binding["content_hash"],
            "sources": source_snapshot([*kw["input_paths"], Path(binding["path"])]),
            "source_review_call": call.name, "source_review_scope_sha256": scope["scope_sha256"]}
        write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
        text = read(run / "sources.json")["documents"][0]["blocks"][0]["text"]
        disposition = CoverageDisposition(source_id="main", block_ids=["paragraph"], state="read_unquantified",
            text_read=True, visual_checked=False, category="substantive", note="The final-material source paragraph was read; no new numeric extraction was performed.",
            evidence=[SourceAnchor(source_id="main", block_id="paragraph", quote=text[:30])])
        report = model(scope_sha256=scope["scope_sha256"], dispositions=[disposition],
            scientific_concerns=[CoverageScientificConcern(source_id="main", block_ids=["paragraph"],
                explanation="Synthetic new source discrepancy needs a separate scientific check.")] if self.blocker else [])
        write_new_json(call / "answer.json", report.model_dump(mode="json"))
        proof = self.tools.source_review_proof() if not self.missing_delivery else {}
        write_new_json(call / "result.json", {"status": "schema_valid_candidate", "thread_id": "thread", "turn_id": "turn",
            "server_selected_model": "gpt-6.2-sol", "server_selected_effort": "max", "elapsed_seconds": 1,
            "source_review_proof": proof})
        events(run, report.model_dump(mode="json"))
        self.calls_started = 1
        return report


def finish(case, **kw):
    path = prepare(case.f.settings, case.f.run, timeout_seconds=120)
    client = SyntheticClient(read(path), **kw)
    run = execute(case.f.settings, path, client=client)
    assert client.closed
    return run


def test_source_bound_correction_preserves_candidate_audit_and_original_dossier_bytes(case):
    before = {name: hash_file(case.f.run / name) for name in
        ("candidate.json", "model_review.json", "reading_dossier.json", "result.json")}
    run = finish(case)
    assert before == {name: hash_file(case.f.run / name) for name in before}
    assert hash_file(run / "candidate.json") == before["candidate.json"]
    assert hash_file(run / "model_review.json") == before["model_review.json"]
    assert hash_file(run / "reading_dossier.json") == before["reading_dossier.json"]
    result = read(run / "result.json")
    assert result["reading_check_issues"] == [] and result["stop_reason"] == "audit_completed"
    assert not result["human_validated"] and not result["graph_published"]
    assert result["model_calls"] == 1 and result["revisions"] == 0
    assert read(run / "plan.json")["cohort_role"] == "unassigned"
    assert verify_coverage_correction_run(case.f.settings, run, case.parent.record, case.parent.audit)
    assert case.f.records._audit_provenance(run, case.parent.record, result, case.parent.audit.model_dump(mode="json"))


def test_scientific_concern_remains_incomplete_without_modifying_scientific_values(case):
    run = finish(case, blocker=True)
    result = read(run / "result.json")
    assert result["stop_reason"] == "coverage_correction_unresolved"
    assert any("scientific concern" in issue for issue in result["reading_check_issues"])
    assert hash_file(run / "candidate.json") == case.parent.receipt["candidate_sha256"]
    assert not verify_coverage_correction_run(case.f.settings, run, case.parent.record, case.parent.audit)


def test_declared_reading_without_current_source_receipt_is_retained_failure(case):
    path = prepare(case.f.settings, case.f.run, timeout_seconds=120)
    client = SyntheticClient(read(path), missing_delivery=True)
    with pytest.raises(ValueError, match="source delivery"):
        execute(case.f.settings, path, client=client)
    assert client.closed and (path.parent / "failure.json").exists()
    assert not (path.parent / "candidate.json").exists()
    with pytest.raises(ValueError, match="already has"):
        prepare(case.f.settings, case.f.run, timeout_seconds=120)


@pytest.mark.parametrize("artifact", ["candidate.json", "model_review.json", "reading_dossier.corrected.v01.json",
    "coverage_correction.v01.json", "coverage_correction_proof.json"])
def test_new_version_tamper_fails_provenance(case, artifact):
    run = finish(case)
    (run / artifact).write_text("{}", encoding="utf-8")
    assert not verify_coverage_correction_run(case.f.settings, run, case.parent.record, case.parent.audit)


def test_prepare_has_no_provider_and_execute_requires_explicit_client(case):
    path = prepare(case.f.settings, case.f.run, timeout_seconds=120)
    assert not (path.parent / "calls").exists()
    with pytest.raises(ValueError, match="explicitly enabled"):
        execute(case.f.settings, path)
