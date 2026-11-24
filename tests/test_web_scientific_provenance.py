"""Offline authentication of cumulative scientific reviews using real tool receipts."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_web_records import fixture, completed_call, read
from test_scientific_revision_audit import report_for
from watertreatment.audit_execution import prepare_citation_review, citation_paths
from watertreatment.codex import source_snapshot
from watertreatment.reading import AuditFinding, ReadingAudit, SourceAnchor
from watertreatment.reading_tools import ReadingTools
from watertreatment.storage import digest_json, hash_file, write_new_json


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def freeze_request(call, request):
    write(call / "request.json", {"request": request, "fingerprint": digest_json(request)})


def completed_delta(state, candidate, *, new_blocker=False):
    """Persist a synthetic completed call with genuine query/source-delivery logs."""
    version = len(state.history)
    candidate_path = state.run / f"candidate.v{version:02d}.json"
    write_new_json(candidate_path, candidate.model_dump(mode="json"))
    prepared = prepare_citation_review(state.run, version, candidate, state.history, state.plan)
    assert prepared and prepared[1]["policy"] == "bounded-scientific-revision-v1"
    receipt, scope, baseline = prepared
    call = state.run / "calls" / f"scientific-{version}"
    call.mkdir(parents=True)
    state.tools.bind_candidate(candidate, candidate_path)
    state.tools.begin_query_turn()
    state.tools.begin_source_review(scope, call_id=call.name)
    assert state.tools.handle("wt_query_candidate", {"path": ["observations", 0]})["success"]
    assert state.tools.handle("wt_read_blocks", {"source_id": "main", "start_order": 1, "end_order": 1})["success"]
    binding = state.tools.candidate_binding
    request = {"stage": f"audit_{version}", "prompt": "CANDIDATE_SNAPSHOT " + binding["content_hash"],
        "candidate_binding": binding, "citation_review": receipt, "source_review_call": call.name,
        "source_review_scope_sha256": scope["scope_sha256"],
        "sources": source_snapshot([*state.source_paths, Path(binding["path"]), *citation_paths(prepared)])}
    freeze_request(call, request)
    report = report_for(scope, baseline)
    if new_blocker:
        report.findings.append(AuditFinding(record_path="observations[1].value", severity="major",
            verdict="unsupported", needs_revision=True, explanation="Source-check the missing-value attribution.",
            evidence=[SourceAnchor(source_id="main", block_id="paragraph")]))
    write_new_json(call / "answer.json", report.model_dump(mode="json"))
    write_new_json(call / "result.json", {"status": "schema_valid_candidate",
        "server_selected_model": "gpt-6.1-sol", "server_selected_effort": "max",
        "source_review_proof": state.tools.source_review_proof()})
    write_new_json(state.run / f"audit.v{version:02d}.json", report.model_dump(mode="json"))
    state.history.append({"version": version, "audit_origin": "scientific_delta_model_audit",
        "candidate_sha256": hash_file(candidate_path), "validation_error_count": 0, "reading_check_issues": []})
    shutil.copyfile(candidate_path, state.run / "candidate.json")
    write(state.run / "model_review.json", report.model_dump(mode="json"))
    write(state.run / "result.json", {"history": state.history})
    state.candidate, state.report, state.call, state.scope = candidate, report, call, scope
    return state


@pytest.fixture
def chain(fixture):
    f = fixture
    run = f.store.new_run("extraction", {"paper_id": "synthetic-paper"})
    shutil.copyfile(f.run / "sources.json", run / "sources.json")
    source_paths = [run / "sources.json", *[Path(x["source"]["path"]) for x in read(run / "sources.json")["documents"]]]
    plan = {**read(f.run / "plan.json"), "run_id": run.name, "rule_version": "0.7", "schema_version": "0.3.0",
            "input_paths": source_snapshot(source_paths)}
    write_new_json(run / "plan.json", plan)
    old = f.record.model_copy(deep=True)
    old.rule_version, old.schema_version, old.extraction_scope = "0.7", "0.3.0", "research_core"
    completed_call(run, old)
    baseline = ReadingAudit.model_validate(read(run / "model_review.json"))
    baseline.findings = [AuditFinding(record_path="observations[0].value", severity="major",
        verdict="unsupported", needs_revision=True, explanation="Printed result attribution needs interpretation repair.",
        evidence=[SourceAnchor(source_id="main", block_id="paragraph")])]
    write(run / "calls/audit/answer.json", baseline.model_dump(mode="json"))
    write_new_json(run / "candidate.v00.json", old.model_dump(mode="json"))
    write_new_json(run / "audit.v00.json", baseline.model_dump(mode="json"))
    state = SimpleNamespace(f=f, run=run, plan=plan, source_paths=source_paths,
        history=[{"version": 0, "audit_origin": "fresh_model_audit", "candidate_sha256": hash_file(run / "candidate.v00.json"),
                  "validation_error_count": 0, "reading_check_issues": []}],
        tools=ReadingTools(read(run / "sources.json"), run, max_calls=20, expected_inputs=plan["input_paths"]))
    new = old.model_copy(deep=True)
    new.observations[0].value.status = "inferred"
    new.observations[0].limitations = ["An unchanged numeric value has inferred attribution."]
    return completed_delta(state, new, new_blocker=True)


def second_delta(state):
    new = state.candidate.model_copy(deep=True)
    new.observations[1].value.evidence_ids = ["source-value"]
    new.observations[1].value.note = "Source reread supports the retained not-reported state."
    return completed_delta(state, new)


def admitted(state):
    return state.f.records._audit_provenance(state.run, state.candidate,
        read(state.run / "result.json"), state.report.model_dump(mode="json"))


def test_current_scientific_delta_authenticates_with_real_candidate_and_source_delivery(chain):
    assert admitted(chain)  # Provenance only; the blocker still prevents scientific acceptance elsewhere.
    assert read(chain.call / "result.json")["source_review_proof"]["tool_receipts"]
    assert second_delta(chain) and admitted(chain)
    assert len(read(chain.call / "request.json")["request"]["citation_review"]["additional_audits"]) == 1


def test_local_chain_retains_pending_dossier_dispositions_without_repeating_full_audit(chain):
    gap = "dossier lacks explicit coverage disposition for 1 blocks in main: paragraph"
    for item in chain.history:
        item["reading_check_issues"] = [gap]
    write(chain.run / "result.json", {"history": chain.history})
    assert admitted(chain)
    second_delta(chain)
    assert admitted(chain)
    result = read(chain.run / "result.json")
    assert result["history"][0]["reading_check_issues"] == [gap]
    assert result["history"][1]["reading_check_issues"] == [gap]
    assert read(chain.call / "answer.json")["review_scope"] == "scientific_delta"
    baseline_call = read(chain.run / "calls/audit/result.json")
    baseline_call["status"] = "failed"
    write(chain.run / "calls/audit/result.json", baseline_call)
    assert not admitted(chain)  # Coverage gaps never replace full-audit provenance.


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("defect", [
    "candidate coverage references unknown source/blocks.",
    "dossier lacks the explicit three-layer research-scope outline required by rule 0.8.",
    "Reading dossier paper identity differs from candidate.",
])
def test_local_provenance_rejects_non_disposition_defects_in_every_ancestor(chain, index, defect):
    second_delta(chain)
    assert admitted(chain)
    result = read(chain.run / "result.json")
    result["history"][index]["reading_check_issues"] = [defect]
    write(chain.run / "result.json", result)
    assert not admitted(chain)


def test_complete_path_batch_provenance_replays_values_and_rejects_modified_receipts(chain):
    run = chain.run / "batch-only"
    run.mkdir()
    snapshot = run / "candidate.json"
    write_new_json(snapshot, chain.candidate.model_dump(mode="json"))
    query_tools = ReadingTools(read(chain.run / "sources.json"), run, max_calls=3,
        expected_inputs=source_snapshot(chain.source_paths))
    query_tools.bind_candidate(chain.candidate, snapshot)
    query_tools.begin_query_turn()
    response = query_tools.handle("wt_query_candidate", {"paths": [["observations", 0], ["observations", 1]]})
    assert response["success"]
    binding = query_tools.candidate_binding
    request = {"candidate_binding": binding, "prompt": "CANDIDATE_SNAPSHOT " + binding["content_hash"]}
    sources = {binding["path"]: binding["sha256"]}
    verify = lambda: chain.f.records._queried_candidate_snapshot(run, chain.candidate, request, sources)
    assert verify()
    log = next((run / "reading_tools").glob("*/result.json"))
    receipt = read(log)
    receipt["data"]["items"][0]["value"]["value"]["value"] = 987654
    write(log, receipt)
    assert not verify()


@pytest.mark.parametrize("tamper", ["missing_proof", "scope_binding", "call_binding", "wrong_model", "wrong_effort", "candidate_binding"])
def test_current_completed_call_cannot_claim_an_unbound_source_review(chain, tamper):
    assert admitted(chain)
    completed = read(chain.call / "result.json")
    request = read(chain.call / "request.json")["request"]
    if tamper == "missing_proof":
        completed.pop("source_review_proof")
    elif tamper == "scope_binding":
        request["source_review_scope_sha256"] = "0" * 64
    elif tamper == "call_binding":
        request["source_review_call"] = "not-this-call"
    elif tamper == "wrong_model":
        completed["server_selected_model"] = "other-model"
    elif tamper == "wrong_effort":
        completed["server_selected_effort"] = "low"
    else:
        request["candidate_binding"]["content_hash"] = "0" * 64
    write(chain.call / "result.json", completed)
    freeze_request(chain.call, request)
    assert not admitted(chain)


@pytest.mark.parametrize("tamper", ["drop_receipt", "failed_prior", "prior_source_proof", "fake_full_origin", "wrong_prior_hash"])
def test_every_intermediate_delta_must_authenticate_instead_of_being_skipped(chain, tamper):
    second_delta(chain)
    assert admitted(chain)
    request = read(chain.call / "request.json")["request"]
    prior_call = chain.run / "calls/scientific-1"
    if tamper == "drop_receipt":
        request["citation_review"]["additional_audits"] = []
    elif tamper == "failed_prior":
        completed = read(prior_call / "result.json")
        completed["status"] = "failed"
        write(prior_call / "result.json", completed)
    elif tamper == "prior_source_proof":
        completed = read(prior_call / "result.json")
        completed.pop("source_review_proof")
        write(prior_call / "result.json", completed)
    elif tamper == "fake_full_origin":
        result = read(chain.run / "result.json")
        result["history"][1]["audit_origin"] = "fresh_model_audit"
        write(chain.run / "result.json", result)
    else:
        request["citation_review"]["additional_audits"][0]["audit_sha256"] = "0" * 64
    freeze_request(chain.call, request)
    assert not admitted(chain)


def test_full_baseline_requires_its_completed_original_call_even_with_valid_current_review(chain):
    assert admitted(chain)
    completed = read(chain.run / "calls/audit/result.json")
    completed["status"] = "failed"
    write(chain.run / "calls/audit/result.json", completed)
    assert not admitted(chain)


def test_new_blocker_from_intermediate_review_cannot_disappear_from_current_source_dispositions(chain):
    second_delta(chain)
    assert admitted(chain)
    chain.report.findings = [f for f in chain.report.findings if not f.record_path.startswith("observations[1]")]
    write(chain.call / "answer.json", chain.report.model_dump(mode="json"))
    write(chain.run / "audit.v02.json", chain.report.model_dump(mode="json"))
    write(chain.run / "model_review.json", chain.report.model_dump(mode="json"))
    assert not admitted(chain)


def test_prior_source_delivery_receipt_cannot_be_replayed_as_current_call_proof(chain):
    proof = read(chain.call / "result.json")["source_review_proof"]
    second_delta(chain)
    completed = read(chain.call / "result.json")
    completed["source_review_proof"] = proof
    write(chain.call / "result.json", completed)
    assert not admitted(chain)


def test_same_candidate_later_review_with_broken_prior_proof_fails_without_recursive_inheritance(chain):
    # A renewed disposition may retain every candidate byte. The later model
    # answer must never authenticate an earlier audit merely by sharing facts.
    unchanged = chain.candidate.model_copy(deep=True)
    completed_delta(chain, unchanged)
    assert hash_file(chain.run / "candidate.v01.json") == hash_file(chain.run / "candidate.v02.json")
    assert admitted(chain)
    prior = chain.run / "calls/scientific-1/result.json"
    completed = read(prior)
    completed.pop("source_review_proof")
    write(prior, completed)
    assert not admitted(chain)


def test_rehashed_source_delivery_payload_is_replayed_against_actual_original_text(chain):
    completed = read(chain.call / "result.json")
    proof = completed["source_review_proof"]
    receipt = proof["tool_receipts"][0]
    result_path = Path(receipt["result_path"])
    delivered = read(result_path)
    delivered["data"]["blocks"][0]["text"] = "Fabricated replacement text"
    write(result_path, delivered)
    receipt["result_sha256"] = hash_file(result_path)
    proof["proof_sha256"] = digest_json({k: v for k, v in proof.items() if k != "proof_sha256"})
    write(chain.call / "result.json", completed)
    assert not admitted(chain)
