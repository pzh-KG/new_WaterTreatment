"""Controller sequencing only; fake provider, no database/network."""
import json

import pytest

from test_workflow import workspace, read_json
from test_active_reading import active, FakeReader
from watertreatment.continuation_repair import repair_contract, source_navigation
from watertreatment.domain import ExtractionRecord
from watertreatment.reading import ReadingDossier
from watertreatment.reader_workflow import execute_reader
from watertreatment.storage import DataStore, hash_file, write_new_json
from watertreatment.sources import SourceDocument


@pytest.mark.parametrize("targets", [[], ["unknown"], ["missingness", "missingness"], "missingness"])
def test_unknown_or_duplicated_repair_target_rejected(targets):
    with pytest.raises(ValueError):
        repair_contract(targets)


def test_repair_navigation_is_not_source_text(active):
    _, _, bundle, _ = active
    text = source_navigation(bundle)
    assert "Synthetic final composite" not in text
    for document in bundle["documents"]:
        assert document["source"]["source_id"] in text
        assert document["source"]["sha256"] in text


def test_opted_in_existing_candidate_is_repaired_then_audited_without_reextract(active, monkeypatch):
    workspace, run, bundle, frozen = active
    client = FakeReader(workspace.settings, bundle)
    candidate = client.candidate
    dossier = ReadingDossier(paper_id="paper-test", article_question_and_design="Synthetic fixture",
        material_map=[], experiment_map=[], coverage=client.coverage,
        cross_source_conflicts=[], unresolved_questions=[], schema_gaps=[])
    write_new_json(run / "reused_candidate.json", candidate.model_dump(mode="json"))
    write_new_json(run / "reading_dossier.json", dossier.model_dump(mode="json"))
    before = hash_file(run / "reused_candidate.json")
    targets = ["shared_references", "missingness"]
    (run / "continuation_repair_prompt.txt").write_text(repair_contract(targets), encoding="utf-8")
    plan = {**frozen, "reused_candidate": {"path": str(run / "reused_candidate.json")},
            "reused_dossier": {"fixture": True}, "continuation_repairs": targets,
            "resumed_full_audit": {"frozen_candidate_path": "synthetic baseline"}}
    # This test isolates controller sequencing; staged authentication and source
    # audit proof are tested with immutable fixtures in their own suites.
    monkeypatch.setattr("watertreatment.audit_resume.prepare_resumed_review", lambda *a: None)
    execute_reader(workspace.settings, plan, run, [], [SourceDocument.model_validate(d) for d in bundle["documents"]],
                   DataStore(workspace.settings.data_root), client)
    assert client.stages == [("continuation_repair_0", "continuation-repair-0"), ("audit_0", "audit-0")]
    assert hash_file(run / "reused_candidate.json") == before
    assert ExtractionRecord.model_validate(read_json(run / "candidate.json")) == candidate
    assert read_json(run / "continuation_repair.json")["status"] == "model_repaired_requires_source_audit"
    result = read_json(run / "result.json")
    assert not result["human_validated"] and not result["graph_published"]


@pytest.mark.parametrize("corrected", [True, False])
def test_report_only_defect_never_spends_a_candidate_revision(active, monkeypatch, corrected):
    workspace, run, bundle, frozen = active
    client = FakeReader(workspace.settings, bundle)
    dossier = ReadingDossier(paper_id="paper-test", article_question_and_design="Synthetic fixture",
        material_map=[], experiment_map=[], coverage=client.coverage,
        cross_source_conflicts=[], unresolved_questions=[], schema_gaps=[])
    write_new_json(run / "reused_candidate.json", client.candidate.model_dump(mode="json"))
    write_new_json(run / "reading_dossier.json", dossier.model_dump(mode="json"))
    plan = {**frozen, "reused_candidate": {"path": str(run / "reused_candidate.json")},
            "reused_dossier": {"fixture": True}}
    # Source/provenance tests exercise the real scoped contract separately;
    # inject only the report-completeness outcome to verify failure sequencing.
    monkeypatch.setattr("watertreatment.reader_workflow.prepare_citation_review", lambda *a, **k: ({}, {}, None))
    monkeypatch.setattr("watertreatment.reader_workflow.citation_review_prompt", lambda *a: "synthetic scope")
    monkeypatch.setattr("watertreatment.reader_workflow.citation_paths", lambda *a: [])
    monkeypatch.setattr("watertreatment.reader_workflow.citation_review_issues",
        lambda *a: [] if corrected and client.calls_started >= 2 else ["Affected entity lacks source disposition"])
    execute_reader(workspace.settings, plan, run, [], [SourceDocument.model_validate(d) for d in bundle["documents"]],
                   DataStore(workspace.settings.data_root), client)
    assert client.stages == [("audit_0", "audit-0"), ("audit_0", "audit-0-protocol-1")]
    assert (run / "audit_protocol_incomplete.v00.json").is_file()
    assert not (run / "candidate.v01.json").exists()
    result = read_json(run / "result.json")
    assert result["stop_reason"] == ("audit_completed" if corrected else "audit_report_incomplete")
