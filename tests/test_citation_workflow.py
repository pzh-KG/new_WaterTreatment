"""Synthetic full -> citation correction -> scoped audit, without a provider.

These are provenance and controller tests; successful replay proves no scientific
claim and never contacts a model, database, credentials or external process.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from test_web_model import ReplayClient
from test_web_records import fixture, completed_call, read
from watertreatment.audit_execution import prepare_citation_review
from watertreatment.domain import EvidenceRef, ExtractionRecord, Observation
from watertreatment.reading import AUDIT_DIMENSIONS, AuditFinding, ReadingAudit, SourceAnchor
from watertreatment.scope_policy import frozen_scope
from watertreatment.settings import CodexSettings
from watertreatment.storage import digest_json, hash_file, write_new_json
from watertreatment.validation import validate_record
from watertreatment.web_model import reaudit


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def citation_fixture(fixture):
    fixture.settings.rules_version = "0.7"
    fixture.settings.codex = CodexSettings(model="gpt-6.2-sol", reasoning_effort="max", max_calls_per_run=7)
    prompts = fixture.settings.project_root / "prompts"
    prompts.mkdir(parents=True)
    repository = Path(__file__).resolve().parents[1]
    for path in (repository / "prompts").glob("*.v0.7.txt"):
        shutil.copyfile(path, prompts / path.name)
    record = fixture.record.model_copy(deep=True)
    record.rule_version, record.schema_version = "0.7", "0.3.0"
    record.extraction_scope = "research_core"
    record.evidence.append(EvidenceRef(id="unit-source", source_id="main", block_id="paragraph", quote="kg m-2 h-1"))
    record.observations.append(Observation(id="independent", experiment_id="separate", material_id="final"))
    for observation in record.observations:
        observation.analysis_role = "key_result"
    run = fixture.store.new_run("extraction", {"paper_id": "synthetic-paper"})
    shutil.copyfile(fixture.run / "sources.json", run / "sources.json")
    plan = {**read(fixture.run / "plan.json"), "run_id": run.name,
            "rule_version": "0.7", "schema_version": "0.3.0", "extraction_policy": frozen_scope()}
    plan["input_paths"] = [{"path": str(run / "sources.json"), "sha256": hash_file(run / "sources.json")},
                           *plan["input_paths"][1:]]
    write_new_json(run / "plan.json", plan)
    fixture.store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    completed_call(run, record)
    fixture.run, fixture.record = run, record
    assert not [i for i in validate_record(record) if i.severity == "error"]
    fixture.records.refresh()
    detail = fixture.records.get(run.name)
    assert detail["summary"]["admissible"], detail["summary"]["admission_reasons"]
    return fixture


def full_audit():
    return ReadingAudit(findings=[
        AuditFinding(record_path="observations[0].unit.evidence_ids", severity="minor", verdict="supported",
                     needs_revision=True, explanation="Attach the explicit unit source.",
                     evidence=[SourceAnchor(source_id="main", block_id="paragraph")]),
        AuditFinding(record_path="observations[2].value", severity="minor", verdict="unresolved",
                     needs_revision=False, explanation="Independent source limitation remains unresolved.", evidence=[])],
        potential_omissions=[], coverage_limitations=["Synthetic source limitation must remain."],
        checked_dimensions=list(AUDIT_DIMENSIONS))


class CitationClient(ReplayClient):
    """Use real candidate query logging, with synthetic model response artifacts."""
    def complete(self, model, *, prompt, input_paths, workdir, audit_root, identity,
                 image_paths=None, conversation_key=None):
        self.active_workdir = workdir
        binding = None
        if model is ReadingAudit:
            binding = self.tools.candidate_binding
            assert self.tools.handle("wt_query_candidate", {"section": "observations", "limit": 20})["success"]
            prompt += "\nCANDIDATE_SNAPSHOT " + binding["content_hash"]
            input_paths = [*input_paths, Path(binding["path"])]
        response = super().complete(model, prompt=prompt, input_paths=input_paths, workdir=workdir,
            audit_root=audit_root, identity=identity, image_paths=image_paths, conversation_key=conversation_key)
        if binding:
            request_path = audit_root / f"call-{self.calls_started:02d}" / "request.json"
            request = read(request_path)["request"]
            request["candidate_binding"] = binding
            write(request_path, {"request": request, "fingerprint": digest_json(request)})
        return response


def complete_replay(fixture):
    changed = fixture.record.model_copy(deep=True)
    changed.observations[0].unit.evidence_ids.append("unit-source")
    client = CitationClient([])
    def final_audit(model, prompt):
        assert model is ReadingAudit and "CITATION-ONLY SOURCE RECHECK" in prompt
        scope = read(client.active_workdir / "citation_scope.v01.json")
        report = full_audit()
        report.findings[0] = AuditFinding(record_path="observations[0].unit", severity="minor", verdict="supported",
            needs_revision=False, explanation="The unit now cites the explicit source.",
            evidence=[SourceAnchor(source_id="main", block_id="paragraph")])
        report.review_scope, report.review_scope_sha256 = "citation_delta", scope["scope_sha256"]
        report.dimension_notes = ["Unchanged full-audit coverage is inherited; only declared citation scope was rechecked."]
        return report
    client.responses = [full_audit(), changed, final_audit]
    response = reaudit(fixture.settings, fixture.records, fixture.run.name,
        "Synthetic explicit unit citation correction only", client_factory=client.factory)
    run = fixture.settings.data_root / "runs" / response["run_id"]
    return response, run, changed, client


def verifies(fixture, run, record):
    return fixture.records._audit_provenance(run, record, read(run / "result.json"), read(run / "model_review.json"))


def test_citation_workflow_full_then_delta_registers_exact_changed_facts(citation_fixture):
    original_hash = hash_file(citation_fixture.run / "candidate.json")
    result, run, record, client = complete_replay(citation_fixture)
    assert client.closed and client.calls_started == 3
    assert [h["audit_origin"] for h in result["result"]["history"]] == ["fresh_model_audit", "citation_delta_model_audit"]
    assert result["result"]["validation_error_count"] == 0 and result["result"]["reading_check_issues"] == []
    assert result["result"]["status"] == "candidate_audited_with_limitations"
    assert not result["result"]["human_validated"] and verifies(citation_fixture, run, record)
    assert hash_file(citation_fixture.run / "candidate.json") == original_hash
    request = read(run / "calls" / "call-03" / "request.json")["request"]
    receipt = request["citation_review"]
    sources = {item["path"]: item["sha256"] for item in request["sources"]}
    for path_key, hash_key in (("scope_path", "scope_file_sha256"),
                               ("baseline_candidate_path", "baseline_candidate_sha256"),
                               ("baseline_audit_path", "baseline_audit_sha256")):
        assert sources[receipt[path_key]] == receipt[hash_key] == hash_file(Path(receipt[path_key]))
    assert client.calls[-1]["images"] == []
    report = ReadingAudit.model_validate(read(run / "model_review.json"))
    assert report.findings[1] == full_audit().findings[1]
    assert report.coverage_limitations == full_audit().coverage_limitations
    assert citation_fixture.records.get(result["revision"]["record_id"])["summary"]["admissible"]


@pytest.mark.parametrize("tamper", ["scope", "baseline_audit", "baseline_candidate", "scope_rehashed",
                                    "baseline_audit_rehashed", "baseline_candidate_rehashed", "baseline_failed",
                                    "baseline_effort", "baseline_history_error", "baseline_history_missing",
                                    "baseline_history_hash", "baseline_history_delta", "current_history_origin",
                                    "missing_scope_source", "scope_hash_in_answer", "query_proof"])
def test_citation_workflow_rejects_broken_provenance(citation_fixture, tamper):
    _, run, record, _ = complete_replay(citation_fixture)
    assert verifies(citation_fixture, run, record)
    request_path = run / "calls" / "call-03" / "request.json"
    request = read(request_path)["request"]
    receipt = request["citation_review"]
    if tamper.removesuffix("_rehashed") in {"scope", "baseline_audit", "baseline_candidate"}:
        kind = tamper.removesuffix("_rehashed")
        key = {"scope": "scope_path", "baseline_audit": "baseline_audit_path", "baseline_candidate": "baseline_candidate_path"}[kind]
        path = Path(receipt[key])
        data = read(path)
        if kind == "scope":
            data["entity_refs"] = data["entity_refs"][:-1]
            data["scope_sha256"] = digest_json({k: v for k, v in data.items() if k != "scope_sha256"})
        elif kind == "baseline_audit":
            data["coverage_limitations"] = []
        else:
            data["observations"][0]["value"]["value"] = 999
        write(path, data)
        if tamper.endswith("_rehashed"):
            hash_key = {"scope": "scope_file_sha256", "baseline_audit": "baseline_audit_sha256",
                        "baseline_candidate": "baseline_candidate_sha256"}[kind]
            receipt[hash_key] = hash_file(path)
            for source in request["sources"]:
                if source["path"] == str(path):
                    source["sha256"] = receipt[hash_key]
            write(request_path, {"request": request, "fingerprint": digest_json(request)})
            if kind == "scope":
                for report_path in (run / "model_review.json", run / "calls" / "call-03" / "answer.json"):
                    report = read(report_path)
                    report["review_scope_sha256"] = data["scope_sha256"]
                    write(report_path, report)
            elif kind == "baseline_candidate":
                history_path = run / "result.json"
                history_result = read(history_path)
                history_result["history"][-2]["candidate_sha256"] = receipt[hash_key]
                write(history_path, history_result)
    elif tamper in {"baseline_failed", "baseline_effort"}:
        path = run / "calls" / "call-01" / "result.json"
        data = read(path)
        data["status" if tamper == "baseline_failed" else "server_selected_effort"] = "failed" if tamper == "baseline_failed" else "low"
        write(path, data)
    elif tamper.startswith("baseline_history") or tamper == "current_history_origin":
        path = run / "result.json"
        data = read(path)
        if tamper == "baseline_history_error":
            data["history"][-2]["validation_error_count"] = 1
        elif tamper == "baseline_history_missing":
            data["history"][-2].pop("validation_error_count")
        elif tamper == "baseline_history_hash":
            data["history"][-2]["candidate_sha256"] = "0" * 64
        elif tamper == "baseline_history_delta":
            data["history"][-2]["audit_origin"] = "citation_delta_model_audit"
        else:
            data["history"][-1]["audit_origin"] = "fresh_model_audit"
        write(path, data)
    elif tamper == "missing_scope_source":
        request["sources"] = [s for s in request["sources"] if s["path"] != receipt["scope_path"]]
        write(request_path, {"request": request, "fingerprint": digest_json(request)})
    elif tamper == "scope_hash_in_answer":
        for path in (run / "model_review.json", run / "calls" / "call-03" / "answer.json"):
            data = read(path)
            data["review_scope_sha256"] = "0" * 64
            write(path, data)
    else:
        for path in (run / "reading_tools").glob("*/result.json"):
            data = read(path)
            if data.get("data", {}).get("candidate_binding") == request["candidate_binding"]:
                data["success"] = False
                write(path, data)
    assert not verifies(citation_fixture, run, record)


@pytest.mark.parametrize("defect", ["missing_hash", "mismatched_hash", "missing_error_count", "machine_error",
                                    "reading_issue", "missing_reading_issues", "not_fresh"])
def test_citation_preparation_requires_exact_clean_previous_history(citation_fixture, defect):
    run = citation_fixture.store.new_run("synthetic_scope_preparation", {})
    old, new = citation_fixture.record, citation_fixture.record.model_copy(deep=True)
    new.observations[0].unit.evidence_ids.append("unit-source")
    write_new_json(run / "candidate.v00.json", old.model_dump(mode="json"))
    write_new_json(run / "audit.v00.json", full_audit().model_dump(mode="json"))
    history = [{"audit_origin": "fresh_model_audit", "validation_error_count": 0,
                "reading_check_issues": [], "candidate_sha256": hash_file(run / "candidate.v00.json")}]
    if defect == "missing_hash":
        history[0].pop("candidate_sha256")
    elif defect == "mismatched_hash":
        history[0]["candidate_sha256"] = "0" * 64
    elif defect == "missing_error_count":
        history[0].pop("validation_error_count")
    elif defect == "machine_error":
        history[0]["validation_error_count"] = 1
    elif defect == "reading_issue":
        history[0]["reading_check_issues"] = ["unread source"]
    elif defect == "missing_reading_issues":
        history[0].pop("reading_check_issues")
    else:
        history[0]["audit_origin"] = "citation_delta_model_audit"
    assert prepare_citation_review(run, 1, new, history) is None
    assert not (run / "citation_scope.v01.json").exists()
