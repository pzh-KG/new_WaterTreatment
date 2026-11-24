"""Offline inherited-audit authenticity and tamper rejection; no model/graph."""
import json
import shutil
from pathlib import Path

import pytest

from test_web_records import fixture, completed_call, read
from watertreatment.audit_resume import (authenticate_baseline, freeze_resume_audit,
    prepare_resumed_review, verify_resumed_review)
from watertreatment.domain import DatasetRole, ObservationEvidenceLink
from watertreatment.reading import AuditFinding, ReadingAudit, SourceAnchor
from watertreatment.storage import hash_file, write_new_json


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def resumed(fixture):
    f = fixture
    f.settings.rules_version = "0.7"
    prior = f.store.new_run("extraction", {"paper_id": "synthetic-paper"})
    shutil.copyfile(f.run / "sources.json", prior / "sources.json")
    plan = {**read(f.run / "plan.json"), "run_id": prior.name,
            "cohort_role": "development", "rule_version": "0.7", "schema_version": "0.3.0"}
    plan["input_paths"] = [{"path": str(prior / "sources.json"), "sha256": hash_file(prior / "sources.json")},
                           *plan["input_paths"][1:]]
    write_new_json(prior / "plan.json", plan)
    f.store.event(prior.name, "prepared", {"plan_sha256": hash_file(prior / "plan.json")})
    old = f.record.model_copy(deep=True)
    old.rule_version, old.schema_version = "0.7", "0.3.0"
    old.extraction_scope = "research_core"
    old.paper.dataset_role = DatasetRole.DEVELOPMENT
    for observation in old.observations:
        observation.analysis_role = "key_result"
    old.observations[0].evidence_links = [ObservationEvidenceLink(evidence_id="source-value", support_type="context",
        supported_fields=["unit"], explanation="Synthetic old method description")]
    completed_call(prior, old)
    baseline = ReadingAudit.model_validate(read(prior / "model_review.json"))
    baseline.findings = [AuditFinding(record_path="observations[0].evidence_links[0].explanation",
        severity="minor", verdict="unsupported", needs_revision=True,
        explanation="Correct the method description against the source.",
        evidence=[SourceAnchor(source_id="main", block_id="paragraph")])]
    write(prior / "calls/audit/answer.json", baseline.model_dump(mode="json"))
    write_new_json(prior / "candidate.v00.json", old.model_dump(mode="json"))
    write_new_json(prior / "audit.v00.json", baseline.model_dump(mode="json"))
    new = old.model_copy(deep=True)
    new.observations[0].evidence_links[0].explanation = "Synthetic corrected method description"
    candidate_path = prior / "candidate.v01.json"
    write_new_json(candidate_path, new.model_dump(mode="json"))
    run = f.store.new_run("extraction", {"paper_id": "synthetic-paper"})
    shutil.copyfile(prior / "sources.json", run / "sources.json")
    proof, artifacts = freeze_resume_audit(f.settings, run, candidate_path, new, read(run / "sources.json"))
    write_new_json(run / "plan.json", {"resumed_full_audit": proof, "reading_artifacts": artifacts})
    return f, prior, run, old, new, baseline, proof


def test_completed_full_audit_can_be_inherited_without_claiming_new_full_read(resumed):
    f, prior, run, old, new, baseline, proof = resumed
    prepared = prepare_resumed_review(f.settings, read(run / "plan.json"), run, new)
    receipt, scope, inherited = prepared
    assert inherited == baseline and scope["new_record_hash"] != scope["old_record_hash"]
    write_new_json(run / "candidate.json", new.model_dump(mode="json"))
    write_new_json(run / "result.json", {"history": [{"audit_origin": "citation_delta_model_audit",
        "candidate_sha256": hash_file(run / "candidate.json"), "inherited_full_audit": proof}]})
    assert verify_resumed_review(f.settings, run, new, old, baseline, receipt)
    assert read(prior / "candidate.v00.json") == old.model_dump(mode="json")


@pytest.mark.parametrize("tamper", ["failed_call", "model", "effort", "candidate", "audit",
                                     "source", "plan", "frozen_copy", "scope", "scientific_value"])
def test_resumed_audit_rejects_tampering_before_model_use(resumed, tamper):
    f, prior, run, old, new, baseline, proof = resumed
    if tamper in {"failed_call", "model", "effort"}:
        path = prior / "calls/audit/result.json"
        data = read(path)
        key = {"failed_call": "status", "model": "server_selected_model", "effort": "server_selected_effort"}[tamper]
        data[key] = "failed" if tamper == "failed_call" else "wrong"
        write(path, data)
    elif tamper == "candidate":
        path = prior / "candidate.v00.json"
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    elif tamper == "audit":
        data = read(prior / "audit.v00.json")
        data["coverage_limitations"] = []
        write(prior / "audit.v00.json", data)
    elif tamper == "source":
        path = prior / "sources.json"
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    elif tamper == "plan":
        path = prior / "plan.json"
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    elif tamper == "frozen_copy":
        write(run / "resumed_baseline_audit.json", {})
    elif tamper == "scope":
        proof["scope_sha256"] = "0" * 64
        write(run / "plan.json", {"resumed_full_audit": proof})
    else:
        new.observations[0].value.value = 99
    with pytest.raises((ValueError, KeyError)):
        prepare_resumed_review(f.settings, read(run / "plan.json"), run, new)


def test_failed_final_call_cannot_supply_a_full_audit_baseline(resumed):
    f, prior, run, old, new, baseline, proof = resumed
    data = read(prior / "calls/audit/result.json")
    data["status"] = "failed"
    write(prior / "calls/audit/result.json", data)
    with pytest.raises(ValueError, match="completed"):
        authenticate_baseline(f.settings, proof, new)


def test_selected_candidate_newer_audit_cannot_be_bypassed(resumed):
    f, prior, run, old, new, baseline, proof = resumed
    write_new_json(prior / "audit.v01.json", baseline.model_dump(mode="json"))
    another = f.store.new_run("extraction", {"paper_id": "synthetic-paper"})
    with pytest.raises(ValueError, match="already has an audit"):
        freeze_resume_audit(f.settings, another, prior / "candidate.v01.json", new, read(run / "sources.json"))


def test_bounded_source_navigation_preserves_required_locators_without_full_text(resumed):
    from watertreatment.audit_context import bounded_source_context
    f, prior, run, old, new, baseline, proof = resumed
    _, scope, _ = prepare_resumed_review(f.settings, read(run / "plan.json"), run, new)
    navigation = bounded_source_context(run, scope)
    assert '"block_id": "paragraph"' in navigation and '"order": 1' in navigation
    assert "Synthetic final material: rate 2.4" not in navigation
    scope["source_anchors"][0]["block_id"] = "unknown"
    with pytest.raises(ValueError, match="unknown"):
        bounded_source_context(run, scope)


def test_resumed_final_admission_requires_current_source_delivery_receipts(resumed):
    from watertreatment.reading_tools import ReadingTools
    from watertreatment.codex import source_snapshot
    from watertreatment.storage import digest_json

    f, prior, run, old, new, baseline, proof = resumed
    receipt, scope, _ = prepare_resumed_review(f.settings, read(run / "plan.json"), run, new)
    plan = read(run / "plan.json")
    source_paths = [run / "sources.json", *[Path(d["source"]["path"]) for d in read(run / "sources.json")["documents"]]]
    plan["input_paths"] = source_snapshot(source_paths)
    write(run / "plan.json", plan)
    f.store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    write_new_json(run / "candidate.v00.json", new.model_dump(mode="json"))
    write_new_json(run / "candidate.json", new.model_dump(mode="json"))
    tools = ReadingTools(read(run / "sources.json"), run, max_calls=10,
                         expected_inputs=plan["input_paths"])
    tools.bind_candidate(new, run / "candidate.v00.json")
    call = run / "calls/call-resumed"
    call.mkdir(parents=True)
    tools.begin_query_turn()
    tools.begin_source_review(scope, call_id=call.name)
    assert tools.handle("wt_query_candidate", {"path": ["observations", 0]})["success"]
    assert tools.handle("wt_read_blocks", {"source_id": "main", "start_order": 1, "end_order": 1})["success"]
    source_proof = tools.source_review_proof()
    binding = tools.candidate_binding
    request = {"stage": "audit_0", "prompt": "CANDIDATE_SNAPSHOT " + binding["content_hash"],
        "candidate_binding": binding, "citation_review": receipt, "source_review_call": call.name,
        "source_review_scope_sha256": scope["scope_sha256"],
        "sources": source_snapshot([*source_paths, Path(binding["path"]),
            *[Path(receipt[k]) for k in ("scope_path", "baseline_candidate_path", "baseline_audit_path")]])}
    write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    audit = baseline.model_copy(deep=True)
    audit.review_scope, audit.review_scope_sha256 = "citation_delta", scope["scope_sha256"]
    audit.findings = [AuditFinding(record_path="observations[0]", severity="minor", verdict="supported",
        needs_revision=False, explanation="Synthetic source recheck supports the bounded metadata repair.",
        evidence=[SourceAnchor(source_id="main", block_id="paragraph")])]
    write_new_json(call / "answer.json", audit.model_dump(mode="json"))
    completed = {"status": "schema_valid_candidate", "server_selected_model": "gpt-6.1-sol",
        "server_selected_effort": "max", "source_review_proof": source_proof}
    write_new_json(call / "result.json", completed)
    result = {"history": [{"audit_origin": "citation_delta_model_audit",
        "candidate_sha256": hash_file(run / "candidate.json"), "inherited_full_audit": proof}]}
    write_new_json(run / "result.json", result)
    assert f.records._audit_provenance(run, new, result, audit.model_dump(mode="json"))
    del completed["source_review_proof"]
    write(call / "result.json", completed)
    assert not f.records._audit_provenance(run, new, result, audit.model_dump(mode="json"))
