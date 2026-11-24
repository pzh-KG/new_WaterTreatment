"""Frozen cumulative scope/tool integration only; synthetic facts are not scientific gold."""
import json
from pathlib import Path

import pytest

from test_bounded_revision_audit import fixture as bounded_fixture
from test_source_review import source_case, scope_for, begin, read_blocks, view
from test_app_server import adapter
from test_active_reading import active
from test_workflow import workspace
from watertreatment.audit_execution import (prepare_citation_review, citation_review_prompt,
    citation_review_issues, citation_paths, ScientificReviewScopeUnavailable)
from watertreatment.domain import Component, Fact, Substance
from watertreatment.reading import AuditFinding, ReadingAudit, SourceAnchor
from watertreatment.scientific_revision_audit import POLICY
from watertreatment.source_review import verify_source_review
from watertreatment.storage import digest_json, hash_file, write_new_json


def records():
    old, _, baseline = bounded_fixture()
    old.rule_version = "0.8"
    old.substances = [Substance(id="synthetic-component")]
    old.materials[0].components = [Component(substance_id="synthetic-component", retention="retained",
        amount=Fact(status="unread", evidence_ids=["text"]))]
    new = old.model_copy(deep=True)
    new.materials[0].components[0].amount = Fact(status="not_reported", evidence_ids=["text"],
        note="Synthetic source-specific reading disposition; no precursor amount substituted.")
    baseline.findings = [AuditFinding(record_path="materials[0].components[0].amount", severity="major",
        verdict="unsupported", needs_revision=True, explanation="Resolve reading state with its original source.",
        evidence=[SourceAnchor(source_id="source", block_id="page1")])]
    return old, new, baseline


def stage(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    old, new, baseline = records()
    write_new_json(run / "candidate.v00.json", old.model_dump(mode="json"))
    write_new_json(run / "audit.v00.json", baseline.model_dump(mode="json"))
    write_new_json(run / "candidate.v01.json", new.model_dump(mode="json"))
    history = [{"version": 0, "audit_origin": "fresh_model_audit", "validation_errors": 0,
        "reading_check_issues": [], "candidate_sha256": hash_file(run / "candidate.v00.json")}]
    return run, old, new, baseline, history


def report(prepared):
    _, scope, baseline = prepared
    result = baseline.model_copy(deep=True)
    result.review_scope, result.review_scope_sha256 = "scientific_delta", scope["scope_sha256"]
    paths = set(scope["changed_paths"])
    paths.update(ref["record_path"] for ref in scope["entity_refs"] if ref["role"] == "affected")
    paths.update(path for item in scope["blocking_obligations"] for path in item["paths"])
    result.findings = [AuditFinding(record_path="Explicit synthetic scope dispositions", record_paths=sorted(paths),
        severity="minor", verdict="supported", needs_revision=False,
        explanation="Synthetic recheck covers the changed reading state and every affected consumer; unchanged facts inherit coverage.",
        evidence=[SourceAnchor(source_id=a["source_id"], block_id=a["block_id"]) for a in scope["source_anchors"]])]
    return result


def test_scientific_preparation_keeps_frozen_candidates_and_all_source_obligations(tmp_path):
    run, old, new, baseline, history = stage(tmp_path)
    frozen = (old.model_dump(mode="json"), new.model_dump(mode="json"))
    prepared = prepare_citation_review(run, 1, new, history)
    assert prepared and prepared[1]["policy"] == POLICY
    receipt = prepared[0]
    assert receipt["additional_audits"] == []
    assert receipt["current_candidate_sha256"] == hash_file(run / "candidate.v01.json")
    assert run / "candidate.v01.json" in citation_paths(prepared)
    text = citation_review_prompt(prepared, new)
    assert "record_paths" in text and "EVERY" in text and "all NINE" in text
    assert "review_scope=scientific_delta" in text and "WHOLE RECORD" not in text
    assert citation_review_issues(prepared, report(prepared)) == []
    assert frozen == (old.model_dump(mode="json"), new.model_dump(mode="json"))


def test_unavailable_scope_reports_recoverable_reason_without_creating_full_review(tmp_path):
    run, old, new, baseline, history = stage(tmp_path)
    new.observations[0].value.value = 123
    (run / "candidate.v01.json").write_text(new.model_dump_json(), encoding="utf-8")
    diagnostics = []
    assert prepare_citation_review(run, 1, new, history, diagnostics=diagnostics) is None
    assert diagnostics[0]["code"] == "protected_quantity_or_identity_changed"
    assert diagnostics[0]["path"] == "observations[0].value"
    assert not diagnostics[0]["automatic_full_review_allowed"]
    assert not (run / "citation_scope.v01.json").exists()
    error = ScientificReviewScopeUnavailable(diagnostics)
    assert "recoverably blocked" in str(error) and error.diagnostics == diagnostics


@pytest.mark.parametrize("gap", [
    "dossier lacks explicit coverage disposition for 1 blocks in source: equation-context",
    "dossier coverage references unknown source/blocks.",
])
def test_dossier_dispositions_can_remain_pending_during_local_scientific_repair(tmp_path, gap):
    run, old, new, baseline, history = stage(tmp_path)
    history[0]["reading_check_issues"] = [gap]
    before = json.loads(json.dumps(history))
    prepared = prepare_citation_review(run, 1, new, history)
    assert prepared and prepared[1]["policy"] == POLICY
    assert prepared[0]["baseline_candidate_path"] == str(run / "candidate.v00.json")
    assert citation_review_issues(prepared, report(prepared)) == []
    assert history == before  # Pending coverage is not erased or declared read.


@pytest.mark.parametrize("checks", [None, "", {}, [None],
    ["dossier lacks the explicit three-layer research-scope outline required by rule 0.8."],
    ["Reading dossier paper identity differs from candidate."],
    ["candidate coverage references unknown source/blocks."],
    ["Unknown reading/audit anchor: source/page9"],
    ["Audit dimensions missing: uncertainty"],
    ["dossier lacks explicit coverage disposition for 1 blocks in source: page2", "unknown defect"],
])
def test_pending_coverage_exception_does_not_hide_other_consistency_defects(tmp_path, checks):
    run, old, new, baseline, history = stage(tmp_path)
    history[0]["reading_check_issues"] = checks
    diagnostics = []
    assert prepare_citation_review(run, 1, new, history, diagnostics=diagnostics) is None
    assert diagnostics[0]["code"] == "unclean_prior_history"
    assert not (run / "citation_scope.v01.json").exists()


def second_stage(tmp_path):
    run, old, first, baseline, history = stage(tmp_path)
    prepared = prepare_citation_review(run, 1, first, history)
    first_audit = report(prepared)
    first_audit.findings.append(AuditFinding(record_path="observations[1].value", severity="major",
        verdict="unsupported", needs_revision=True, explanation="A source-located intermediate blocker remains.",
        evidence=[SourceAnchor(source_id="source", block_id="page2")]))
    write_new_json(run / "audit.v01.json", first_audit.model_dump(mode="json"))
    history.append({"version": 1, "audit_origin": "scientific_delta_model_audit", "validation_errors": 0,
        "reading_check_issues": [], "candidate_sha256": hash_file(run / "candidate.v01.json")})
    second = first.model_copy(deep=True)
    second.observations[0].limitations.append("An explicit additional source restriction.")
    write_new_json(run / "candidate.v02.json", second.model_dump(mode="json"))
    return run, second, history


def test_cumulative_scope_retains_nearest_full_baseline_and_intermediate_blockers(tmp_path):
    run, second, history = second_stage(tmp_path)
    prepared = prepare_citation_review(run, 2, second, history)
    assert prepared and len(prepared[0]["additional_audits"]) == 1
    assert prepared[0]["baseline_candidate_path"] == str(run / "candidate.v00.json")
    assert any(item["audit_index"] == 1 and "observations[1].value" in item["paths"]
               for item in prepared[1]["blocking_obligations"])
    assert {run / "audit.v01.json", run / "candidate.v01.json"}.issubset(citation_paths(prepared))
    assert citation_review_issues(prepared, report(prepared)) == []


@pytest.mark.parametrize("defect", ["missing_audit", "candidate_hash", "history_gap", "old_delta", "bad_scope", "new_candidate", "additional"])
def test_missing_or_changed_intermediate_binding_never_silently_disappears(tmp_path, defect):
    run, second, history = second_stage(tmp_path)
    if defect == "missing_audit": (run / "audit.v01.json").unlink()
    elif defect == "candidate_hash": history[1]["candidate_sha256"] = "0" * 64
    elif defect == "history_gap": history.pop(1)
    elif defect == "old_delta":
        payload = json.loads((run / "audit.v01.json").read_bytes())
        payload["review_scope"] = "citation_delta"
        (run / "audit.v01.json").write_text(json.dumps(payload), encoding="utf-8")
    if defect in {"missing_audit", "candidate_hash", "history_gap", "old_delta"}:
        assert prepare_citation_review(run, 2, second, history) is None
        return
    prepared = prepare_citation_review(run, 2, second, history)
    audit = report(prepared)
    if defect == "bad_scope": prepared[1]["changed_paths"].clear()
    elif defect == "new_candidate": (run / "candidate.v02.json").write_text("{}", encoding="utf-8")
    else: prepared[0]["additional_audits"][0]["audit_sha256"] = "0" * 64
    assert citation_review_issues(prepared, audit)


def test_resumed_full_baseline_remains_original_across_local_versions(tmp_path):
    run, old, first, baseline, history = stage(tmp_path)
    # A separate run begins from the already repaired candidate and copied full baseline.
    resumed = tmp_path / "resumed"
    resumed.mkdir()
    write_new_json(resumed / "resumed_baseline_candidate.json", old.model_dump(mode="json"))
    write_new_json(resumed / "resumed_baseline_audit.json", baseline.model_dump(mode="json"))
    write_new_json(resumed / "candidate.v00.json", first.model_dump(mode="json"))
    proof = {f"frozen_{kind}_{suffix}": str(resumed / f"resumed_baseline_{kind}.json") if suffix == "path"
             else hash_file(resumed / f"resumed_baseline_{kind}.json")
             for kind in ("candidate", "audit") for suffix in ("path", "sha256")}
    prepared = prepare_citation_review(resumed, 0, first, [], plan={"resumed_full_audit": proof})
    assert prepared and citation_review_issues(prepared, report(prepared)) == []
    write_new_json(resumed / "audit.v00.json", report(prepared).model_dump(mode="json"))
    history = [{"version": 0, "audit_origin": "scientific_delta_model_audit", "validation_errors": 0,
        "reading_check_issues": [], "candidate_sha256": hash_file(resumed / "candidate.v00.json"),
        "inherited_full_audit": proof}]
    next_record = first.model_copy(deep=True)
    next_record.observations[0].limitations.append("Synthetic extra qualification.")
    write_new_json(resumed / "candidate.v01.json", next_record.model_dump(mode="json"))
    follow = prepare_citation_review(resumed, 1, next_record, history, plan={"resumed_full_audit": proof})
    assert follow and len(follow[0]["additional_audits"]) == 1
    assert follow[0]["baseline_candidate_path"] == proof["frozen_candidate_path"]


def scientific_scope(documents, visual=False):
    scope = scope_for(documents, visual=visual)
    scope["policy"] = POLICY
    scope["scope_sha256"] = digest_json({k: v for k, v in scope.items() if k != "scope_sha256"})
    return scope


def test_scientific_scope_requires_same_call_full_views_and_frozen_source_receipts(source_case):
    c = source_case
    scope = scientific_scope(c.docs[:1], visual=True)
    begin(c, scope=scope)
    read_blocks(c, "main")
    view(c, rectangle=[0, 0, 0.5, 0.5])
    with pytest.raises(ValueError, match="missing full-page"):
        c.tools.source_review_proof()
    view(c)
    proof = c.tools.source_review_proof()
    assert verify_source_review(c.run, scope, proof, c.call_id)
    assert not verify_source_review(c.run, scope, proof, "different-call")
    image_path = Path(proof["tool_receipts"][0]["result_path"])
    payload = json.loads(image_path.read_bytes())
    payload["data"]["source_delivery"]["source_sha256"] = "0" * 64
    image_path.write_text(json.dumps(payload), encoding="utf-8")
    assert not verify_source_review(c.run, scope, proof, c.call_id)


def test_app_server_enforces_scientific_source_proof_and_uses_small_revision_navigation(active):
    from test_app_server import Answer
    from watertreatment.sources import SourceDocument
    client, transport, kwargs = adapter(active)
    bundle = json.loads((active[1] / "sources.json").read_bytes())
    docs = [SourceDocument.model_validate(d) for d in bundle["documents"]]
    scope = scientific_scope(docs[:1])
    scope_path = active[1] / "scientific_scope.json"
    write_new_json(scope_path, scope)
    kwargs["identity"] = {"stage": "continuation_repair_0", "citation_review": {
        "scope_path": str(scope_path), "scope_file_sha256": hash_file(scope_path)}}
    try:
        # ReplayTransport reads inventory only; a syntactically valid answer cannot satisfy source proof.
        with pytest.raises(ValueError, match="did not re-read"):
            client.complete(Answer, **kwargs)
        request = json.loads(next((active[1] / "calls").glob("*/request.json")).read_bytes())["request"]
        assert request["prompt_size"]["candidate_view_cap_characters"] == 20000
        assert "required_current_source_deliveries" in request["prompt"]
    finally:
        client.close()


@pytest.mark.parametrize("stage,cap", [("extraction", 200000), ("revision_1", 20000), ("continuation_repair_0", 20000)])
def test_stage_navigation_limit_keeps_actual_record_submission_and_general_scientific_contract(active, stage, cap):
    from test_app_server import CompactReplay
    from test_active_reading import toolset
    from watertreatment.app_server import AppServerClient
    from watertreatment.domain import ExtractionRecord
    workspace, run, _, plan = active
    transport = CompactReplay()
    client = AppServerClient(workspace.settings.codex, live=True,
        transport_factory=lambda *a: transport, config_factory=lambda: {})
    client.bind_tools(toolset(active))
    try:
        result = client.complete(ExtractionRecord, prompt="Synthetic stage-independent delivery contract.",
            input_paths=[Path(i["path"]) for i in plan["input_paths"]], workdir=run,
            audit_root=run / "calls", identity={"stage": stage})
    finally:
        client.close()
    assert result.paper.id == plan["paper_id"]
    request = json.loads(next((run / "calls").glob("*/request.json")).read_bytes())["request"]
    assert request["prompt_size"]["candidate_view_cap_characters"] == cap
    assert "reference_associations" in request["prompt"] and "BOTH sides" in request["prompt"]
    assert "not automatic conclusions" in request["prompt"] and "retained composition" in request["prompt"]
    assert request["model"] == "gpt-6.2-sol" and request["reasoning_effort"] == "max"
