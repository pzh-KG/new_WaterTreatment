"""Read-ledger correction after authenticated composite scientific completion.

All documents and model protocol traces are synthetic. This exercises a new
receipt type, not scientific accuracy or a real provider/Neo4j connection.
"""
import pytest

from test_web_records import fixture, read
from test_partial_audit_completion_workflow import case
from test_partial_audit_remaining_resume import timed_out, finish
from test_coverage_correction_workflow import SyntheticClient
from watertreatment.coverage_correction_workflow import (
    authenticate_coverage_parent, prepare, execute, verify_coverage_correction_run)
from watertreatment.reading import ReadingAudit
from watertreatment.storage import hash_file


def test_authenticated_composite_can_finish_coverage_without_full_reaudit(timed_out):
    parent_run = finish(timed_out)
    parent_hashes = {p: hash_file(p) for p in parent_run.rglob("*") if p.is_file()}
    parent = authenticate_coverage_parent(timed_out.settings, parent_run)
    assert parent.record == timed_out.case.parent.record
    assert parent.audit.review_scope == "scientific_delta"
    assert "completed_full_audit_call" not in parent.receipt
    assert parent.receipt["dossier_path"].endswith("reading_dossier.effective.json")
    path = prepare(timed_out.settings, parent_run, timeout_seconds=120)
    assert hash_file(path.parent / "reading_dossier.json") == hash_file(parent_run / "reading_dossier.effective.json")
    client = SyntheticClient(read(path))
    run = execute(timed_out.settings, path, client=client)
    assert client.closed and client.calls_started == 1
    result = read(run / "result.json")
    assert result["status"] == "candidate_audited_with_limitations"
    assert result["reading_check_issues"] == [] and result["validation_error_count"] == 0
    assert not result["human_validated"] and not result["graph_published"]
    assert hash_file(run / "candidate.json") == hash_file(parent_run / "candidate.json")
    assert hash_file(run / "model_review.json") == hash_file(parent_run / "model_review.json")
    assert all(hash_file(p) == sha for p, sha in parent_hashes.items())
    assert verify_coverage_correction_run(timed_out.settings, run, parent.record, parent.audit)
    timed_out.case.f.records.refresh()
    detail = timed_out.case.f.records.get(run.name)
    assert detail["summary"]["admissible"] and detail["summary"]["current"]
    assert not detail["summary"]["human_validated"]
    assert detail["summary"]["publish_status"] == "not_published"


def test_composite_scientific_concern_cannot_be_cleared_by_coverage(timed_out):
    parent_run = finish(timed_out, blocker=True)
    assert read(parent_run / "result.json")["stop_reason"] == "partial_audit_completion_unresolved"
    with pytest.raises(ValueError):
        authenticate_coverage_parent(timed_out.settings, parent_run)


@pytest.mark.parametrize("artifact", ["partial_remaining_proof.json", "combined_source_review_proof.json",
    "reading_dossier.effective.json", "candidate.json", "model_review.json"])
def test_composite_parent_tamper_blocks_coverage_before_any_model(timed_out, artifact):
    parent_run = finish(timed_out)
    (parent_run / artifact).write_text("{}", encoding="utf-8")
    with pytest.raises((ValueError, KeyError)):
        authenticate_coverage_parent(timed_out.settings, parent_run)


def test_composite_coverage_replays_ancestry_when_admitting_new_result(timed_out):
    parent_run = finish(timed_out)
    parent = authenticate_coverage_parent(timed_out.settings, parent_run)
    path = prepare(timed_out.settings, parent_run, timeout_seconds=120)
    run = execute(timed_out.settings, path, client=SyntheticClient(read(path)))
    assert verify_coverage_correction_run(timed_out.settings, run, parent.record, parent.audit)
    # An unchanged coverage report cannot bless an altered earlier failed call.
    call = next((timed_out.failed / "calls").glob("*/result.json"))
    call.write_text("{}", encoding="utf-8")
    assert not verify_coverage_correction_run(timed_out.settings, run, parent.record, parent.audit)


def test_new_coverage_scientific_concern_preserves_composite_candidate(timed_out):
    parent_run = finish(timed_out)
    parent = authenticate_coverage_parent(timed_out.settings, parent_run)
    path = prepare(timed_out.settings, parent_run, timeout_seconds=120)
    run = execute(timed_out.settings, path, client=SyntheticClient(read(path), blocker=True))
    result = read(run / "result.json")
    assert result["stop_reason"] == "coverage_correction_unresolved"
    assert hash_file(run / "candidate.json") == parent.receipt["candidate_sha256"]
    assert not verify_coverage_correction_run(timed_out.settings, run, parent.record, parent.audit)
