"""Synthetic local revalidation; no model providers or graph connections."""
from dataclasses import replace
from pathlib import Path

import pytest

from test_coverage_correction_workflow import case, SyntheticClient
from test_web_records import fixture, read
from test_audit_addendum_workflow import events
from watertreatment.codex import source_snapshot
from watertreatment.coverage_correction import (POLICY, EQUATION_CONTEXT_POLICY, CoverageDisposition,
    CoverageScientificConcern, build_coverage_correction_scope, coverage_correction_issues)
from watertreatment.coverage_correction_workflow import prepare, execute, verify_coverage_correction_run
from watertreatment.coverage_context_revalidation import (revalidate_context, verify_context_revalidation_run,
    authenticate_context_parent)
from watertreatment.domain import CoverageEntry
from watertreatment.reading import SourceAnchor
from watertreatment.storage import digest_json, hash_file, write_new_json


@pytest.fixture
def equation_case(case, monkeypatch):
    parent = case.parent
    doc = parent.documents[0]
    doc.add_block("equation", text="k", metadata={"parent_block_id": "paragraph"})
    parent.record.coverage.append(CoverageEntry(source_id="main", block_ids=[doc.blocks[-1].block_id],
        state="read_structured", text_read=True))
    (case.f.run / "candidate.json").write_text(parent.record.model_dump_json(), encoding="utf-8")
    # Parent has already been covered, but is explicitly registered as context
    # for the new equation disposition. Reading it again must be proven.
    parent.dossier.coverage.append(CoverageEntry(source_id="main", block_ids=["paragraph"],
        state="read_structured", text_read=True))
    bundle = {"documents": [d.model_dump(mode="json") for d in parent.documents]}
    (case.f.run / "sources.json").write_text(__import__('json').dumps(bundle), encoding="utf-8")
    (case.f.run / "reading_dossier.json").write_text(parent.dossier.model_dump_json(), encoding="utf-8")
    plan = read(case.f.run / "plan.json")
    for item in plan["input_paths"]:
        item["sha256"] = hash_file(Path(item["path"]))
    (case.f.run / "plan.json").write_text(__import__('json').dumps(plan), encoding="utf-8")
    case.f.store.event(case.f.run.name, "prepared", {"plan_sha256": hash_file(case.f.run / "plan.json")})
    receipt = {**parent.receipt, "candidate_sha256": hash_file(case.f.run / "candidate.json"), "dossier_sha256": hash_file(case.f.run / "reading_dossier.json"),
        "source_bundle_sha256": hash_file(case.f.run / "sources.json")}
    case.parent = replace(parent, bundle=bundle, plan=plan, receipt=receipt,
        scope=build_coverage_correction_scope(parent.dossier, parent.documents))
    monkeypatch.setattr("watertreatment.coverage_correction_workflow.authenticate_coverage_parent", lambda *a: case.parent)
    return case


class EquationClient(SyntheticClient):
    def __init__(self, plan, *, defect=None):
        super().__init__(plan)
        self.defect = defect

    def complete(self, model, **kw):
        run = kw["workdir"]
        call = kw["audit_root"] / "call-01-synthetic"
        call.mkdir(parents=True)
        scope = read(Path(kw["identity"]["citation_review"]["scope_path"]))
        self.tools.begin_query_turn()
        self.tools.begin_source_review(scope, call_id=call.name)
        assert self.tools.handle("wt_query_candidate", {"path": ["paper"]})["success"]
        assert self.tools.handle("wt_read_blocks", {"source_id": "main",
            "start_order": 2 if self.defect == "undelivered_parent" else 1, "end_order": 2})["success"]
        binding = self.tools.candidate_binding
        request = {**kw["identity"], "model": "gpt-6.2-sol", "reasoning_effort": "max",
            "candidate_binding": binding, "prompt": kw["prompt"] + "\nCANDIDATE_SNAPSHOT " + binding["content_hash"],
            "sources": source_snapshot([*kw["input_paths"], Path(binding["path"])]),
            "source_review_call": call.name, "source_review_scope_sha256": scope["scope_sha256"]}
        write_new_json(call / "request.json", {"request": request, "fingerprint": digest_json(request)})
        doc = read(run / "sources.json")["documents"][0]
        equation = doc["blocks"][1]
        evidence = [SourceAnchor(source_id="main", block_id=equation["block_id"], quote="k"),
            SourceAnchor(source_id="main", block_id="paragraph", quote=doc["blocks"][0]["text"][:30])]
        if self.defect == "no_own_quote": evidence.pop(0)
        if self.defect == "wrong_quote": evidence[-1].quote = "not in the actual source"
        entry = CoverageDisposition(source_id="main", block_ids=[equation["block_id"]], state="read_unquantified",
            text_read=True, visual_checked=False, category="equation_fragment",
            note="Equation and its actual registered parent were read in this turn; no new scientific values asserted.", evidence=evidence)
        report = model(scope_sha256=scope["scope_sha256"], dispositions=[entry],
            scientific_concerns=[CoverageScientificConcern(source_id="main", block_ids=[equation["block_id"]],
                explanation="Synthetic scientific discrepancy must remain unresolved.")] if self.defect == "science" else [],
            source_limitations=["Synthetic source limitation retained verbatim."])
        write_new_json(call / "answer.json", report.model_dump(mode="json"))
        write_new_json(call / "result.json", {"status": "schema_valid_candidate", "thread_id": "thread", "turn_id": "turn",
            "server_selected_model": "gpt-6.2-sol", "server_selected_effort": "max", "elapsed_seconds": 1,
            "source_review_proof": self.tools.source_review_proof()})
        events(run, report.model_dump(mode="json"))
        self.calls_started = 1
        return report


def finish(case, *, policy=POLICY, defect=None):
    plan = prepare(case.f.settings, case.f.run, timeout_seconds=120, validation_policy=policy)
    return execute(case.f.settings, plan, client=EquationClient(read(plan), defect=defect))


def test_local_context_revalidation_preserves_legacy_bytes_and_enters_normal_web_records(equation_case):
    case = equation_case
    old = finish(case)
    assert not verify_coverage_correction_run(case.f.settings, old, case.parent.record, case.parent.audit)
    before = {str(p): hash_file(p) for p in old.rglob("*") if p.is_file()}
    run = revalidate_context(case.f.settings, old)
    assert before == {str(p): hash_file(p) for p in old.rglob("*") if p.is_file()}
    assert run != old and not (run / "calls").exists()
    assert read(run / "result.json")["model_calls"] == 0
    assert read(run / "result.json")["inherited_model"]["server_selected_effort"] == "max"
    for name in ("candidate.json", "model_review.json", "coverage_correction.v01.json", "reading_dossier.corrected.v01.json"):
        assert hash_file(run / name) == hash_file(old / name)
    assert verify_context_revalidation_run(case.f.settings, run, case.parent.record, case.parent.audit)
    assert not verify_coverage_correction_run(case.f.settings, old, case.parent.record, case.parent.audit)
    case.f.records.refresh()
    assert case.f.records.get(run.name)["summary"]["admissible"]
    assert case.f.records.get(run.name)["summary"]["current"]
    assert not case.f.records.get(old.name)["summary"]["admissible"]
    assert revalidate_context(case.f.settings, old) == run


def test_new_plan_freezes_v2_and_old_validator_still_rejects_extra_parent(equation_case):
    case = equation_case
    run = finish(case, policy=EQUATION_CONTEXT_POLICY)
    assert read(run / "plan.json")["coverage_validation_policy"] == EQUATION_CONTEXT_POLICY
    assert verify_coverage_correction_run(case.f.settings, run, case.parent.record, case.parent.audit)
    from watertreatment.coverage_correction import CoverageCorrectionReport
    report = CoverageCorrectionReport.model_validate(read(run / "coverage_correction.v01.json"))
    assert coverage_correction_issues(case.parent.scope, report, case.parent.dossier, case.parent.documents)
    assert not coverage_correction_issues(case.parent.scope, report, case.parent.dossier, case.parent.documents,
        validation_policy=EQUATION_CONTEXT_POLICY)


@pytest.mark.parametrize("defect", ["no_own_quote", "wrong_quote", "science"])
def test_no_own_quote_bad_quote_or_scientific_concern_cannot_be_revalidated(equation_case, defect):
    run = finish(equation_case, defect=defect)
    with pytest.raises(ValueError, match="beyond"):
        revalidate_context(equation_case.f.settings, run)


def test_parent_context_without_actual_source_delivery_is_rejected(equation_case):
    with pytest.raises(ValueError, match="delivery"):
        finish(equation_case, defect="undelivered_parent")


@pytest.mark.parametrize("artifact", ["candidate.json", "coverage_correction.v01.json", "reading_dossier.corrected.v01.json",
    "coverage_correction_proof.json", "result.json", "context_revalidation_parent.json", "context_revalidation_proof.json",
    "validation_code/coverage_correction.py", "manifest.json"])
def test_new_version_tamper_fails_revalidation(equation_case, artifact):
    case = equation_case
    run = revalidate_context(case.f.settings, finish(case))
    (run / artifact).write_text("{}", encoding="utf-8")
    assert not verify_context_revalidation_run(case.f.settings, run, case.parent.record, case.parent.audit)


def test_inherited_tool_receipt_tamper_revokes_local_run(equation_case):
    case = equation_case
    old = finish(case)
    run = revalidate_context(case.f.settings, old)
    next((old / "reading_tools").glob("*/result.json")).write_text("{}", encoding="utf-8")
    assert not verify_context_revalidation_run(case.f.settings, run, case.parent.record, case.parent.audit)


@pytest.mark.parametrize("defect", ["other_equation_parent", "cross_source_parent", "no_registered_origin"])
def test_context_contract_refuses_registered_but_unrelated_context(defect):
    from test_coverage_correction import ready
    dossier, docs, scope, report = ready()
    doc = docs[0]
    equation = next(e for e in report.dispositions if e.block_ids == [doc.blocks[4].block_id])
    other_parent = doc.add_block("paragraph", text="Different equation context.")
    doc.add_block("equation", text="y", metadata={"parent_block_id": other_parent.block_id})
    dossier.coverage.append(CoverageEntry(source_id="si", block_ids=[other_parent.block_id],
        state="read_structured", text_read=True))
    scope = build_coverage_correction_scope(dossier, docs)
    from test_coverage_correction import report_for
    report = report_for(scope, docs)
    equation = next(e for e in report.dispositions if e.block_ids == [doc.blocks[4].block_id])
    if defect == "other_equation_parent":
        equation.evidence.append(SourceAnchor(source_id="si", block_id=other_parent.block_id, quote=other_parent.text))
    elif defect == "cross_source_parent":
        equation.evidence.append(SourceAnchor(source_id="other-source", block_id=doc.blocks[0].block_id,
            quote=doc.blocks[0].text))
    else:
        # A forged scope entry must never be accepted as source registration.
        scope["source_anchors"] = [a for a in scope["source_anchors"] if a.get("origin") != "equation_parent_context"]
        equation.evidence.append(SourceAnchor(source_id="si", block_id=doc.blocks[0].block_id, quote=doc.blocks[0].text))
    assert coverage_correction_issues(scope, report, dossier, docs, validation_policy=EQUATION_CONTEXT_POLICY)
