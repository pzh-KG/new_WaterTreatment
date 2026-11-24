"""Synthetic immutable CLI continuation plans and cumulative audit bindings."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_staged_source_resume import staged_cli, rewrite
from test_web_records import fixture, read
from watertreatment.audit_resume import prepare_resumed_review, verify_resumed_review
from watertreatment.cli import parser
from watertreatment.domain import ExtractionRecord
from watertreatment.reading import ReadingDossier
from watertreatment.scope_policy import scientific_prompt
from watertreatment.storage import digest_json, hash_file, write_new_json
from watertreatment.workflow import prepare


@pytest.fixture
def staged_plan_case(staged_cli):
    case = staged_cli
    settings = case.f.settings
    settings.reading.enabled = True
    settings.codex.model = "gpt-6.2-sol"
    settings.codex.max_calls_per_run = 6
    prompts = settings.project_root / "prompts"
    prompts.mkdir(parents=True)
    for name in ("extract", "verify", "understand", "audit-reading", "revise-reading", "quantitative-policy"):
        (prompts / f"{name}.v0.7.txt").write_text("Synthetic immutable " + name + " rules", encoding="utf-8")
    settings.taxonomy_path.parent.mkdir(parents=True)
    write_new_json(settings.taxonomy_path, {"version": "synthetic"})
    shutil.copyfile(settings.taxonomy_path, case.run / "taxonomy.json")
    (case.run / "extract_prompt.txt").write_text(scientific_prompt(settings, "extract"), encoding="utf-8")
    (case.run / "understand_prompt.txt").write_text(scientific_prompt(settings, "understand"), encoding="utf-8")
    bundle = read(case.run / "sources.json")
    bundle["paper_id"] = "synthetic-paper"
    rewrite(case.run / "sources.json", bundle)
    plan = read(case.run / "plan.json")
    plan["prompt_sha256"] = hash_file(case.run / "extract_prompt.txt")
    plan["reading_artifacts"] = [{"path": str(case.run / "understand_prompt.txt"),
                                  "sha256": hash_file(case.run / "understand_prompt.txt")}]
    for item in plan["input_paths"]:
        item["sha256"] = hash_file(Path(item["path"]))
    rewrite(case.run / "plan.json", plan)
    case.f.store.event(case.run.name, "prepared", {"plan_sha256": hash_file(case.run / "plan.json")})
    for path in (case.audit_call / "request.json", case.failed_call / "request.json"):
        envelope = read(path)
        for item in envelope["request"]["sources"]:
            item["sha256"] = hash_file(Path(item["path"]))
        envelope["fingerprint"] = digest_json(envelope["request"])
        rewrite(path, envelope)
    dossier = ReadingDossier(paper_id="synthetic-paper", article_question_and_design="Synthetic design",
        material_map=[], experiment_map=[], coverage=case.baseline.coverage,
        cross_source_conflicts=[], unresolved_questions=[], schema_gaps=[])
    write_new_json(case.run / "reading_dossier.json", dossier.model_dump(mode="json"))
    call = case.run / "calls/comprehension"
    write_new_json(call / "request.json", {"request": {"stage": "comprehension"}})
    write_new_json(call / "result.json", {"status": "schema_valid_candidate",
        "server_selected_model": "gpt-6.1-sol", "server_selected_effort": "max"})
    write_new_json(call / "answer.json", dossier.model_dump(mode="json"))
    imported = case.f.store.new_run("import", {"paper_id": "synthetic-paper"})
    write_new_json(imported / "sources.json", bundle)
    case.f.store.event(imported.name, "import_completed", {"bundle_sha256": hash_file(imported / "sources.json")})
    case.imported = imported
    return case


def make_plan(case, *, repairs=("shared_references", "missingness")):
    return prepare(case.f.settings, case.imported / "sources.json", cohort_role=read(case.run / "plan.json")["cohort_role"],
        reuse_dossier=case.run / "reading_dossier.json", reuse_staged_candidate=case.stage,
        continuation_repairs=repairs)


def prepare_audit(case, run, *, revised=True):
    candidate = ExtractionRecord.model_validate(read(run / "reused_candidate.json"))
    if revised:
        candidate.observations[0].limitations.append("Synthetic targeted repair, uncertainty retained")
    write_new_json(run / "candidate.v00.json", candidate.model_dump(mode="json"))
    prepared = prepare_resumed_review(case.f.settings, read(run / "plan.json"), run, candidate)
    return candidate, prepared


@pytest.mark.parametrize("staged_cli", ["development", "unassigned"], indirect=True)
def test_staged_continuation_plan_freezes_old_checkpoint_and_targeted_contract(staged_plan_case):
    case = staged_plan_case
    original = {p: hash_file(p) for p in case.run.rglob("*") if p.is_file()}
    run = make_plan(case)
    plan = read(run / "plan.json")
    proof = plan["resumed_full_audit"]
    assert plan["continuation_repairs"] == ["shared_references", "missingness"]
    assert proof["policy"] == "bounded-scientific-revision-v1"
    assert proof["original_candidate_path"] == str(case.run / "candidate.v01.json")
    assert read(run / "reused_candidate.json") == read(case.stage)
    assert read(Path(proof["frozen_candidate_path"])) == case.baseline.model_dump(mode="json")
    prompt = run / "continuation_repair_prompt.txt"
    assert {"path": str(prompt), "sha256": hash_file(prompt)} in plan["reading_artifacts"]
    assert {"path": str(prompt), "sha256": hash_file(prompt)} in plan["input_paths"]
    assert plan["model_settings"]["model"] == "gpt-6.2-sol" and plan["model_settings"]["reasoning_effort"] == "max"
    assert {p: hash_file(p) for p in original} == original
    assert not (case.run / "candidate.v02.json").exists()
    assert all(not entry["path"].endswith("events.jsonl") for entry in plan["input_paths"])
    assert plan["cohort_role"] == read(case.run / "plan.json")["cohort_role"]
    retained = ExtractionRecord.model_validate(read(run / "reused_candidate.json"))
    assert retained.paper.dataset_role == case.baseline.paper.dataset_role
    assert retained.paper.role_history == case.baseline.paper.role_history
    assert case.f.store.last_event(run.name, "development_use")["paper_id"] == retained.paper.id
    with pytest.raises(ValueError, match="independent heldout"):
        case.f.store.register_cohort(retained.paper.id,
            [d["source"]["sha256"] for d in read(run / "sources.json")["documents"]], "heldout")


@pytest.mark.parametrize("staged_cli", ["unassigned"], indirect=True)
def test_staged_continuation_cannot_relabel_production_sources_to_development(staged_plan_case):
    from watertreatment.workflow import _reusable_staged_candidate

    case = staged_plan_case
    with pytest.raises(ValueError, match="cohort"):
        _reusable_staged_candidate(case.f.settings, case.stage, read(case.run / "sources.json"), "development")


def test_staged_continuation_recompiles_scope_after_pre_audit_repairs(staged_plan_case):
    case = staged_plan_case
    run = make_plan(case)
    candidate, (receipt, scope, baseline) = prepare_audit(case, run)
    proof = read(run / "plan.json")["resumed_full_audit"]
    assert scope["scope_sha256"] != proof["initial_scope_sha256"]
    assert receipt["current_candidate_path"] == str(run / "candidate.v00.json")
    assert receipt["additional_audits"] == []
    write_new_json(run / "candidate.json", candidate.model_dump(mode="json"))
    write_new_json(run / "result.json", {"history": [{"audit_origin": "scientific_delta_model_audit",
        "candidate_sha256": hash_file(run / "candidate.v00.json"), "inherited_full_audit": proof}]})
    assert verify_resumed_review(case.f.settings, run, candidate, case.baseline, baseline, receipt)


@pytest.mark.parametrize("mutation", ["initial_scope", "baseline", "frozen_stage", "checkpoint", "original_stage", "numeric_change"])
def test_staged_continuation_refuses_tampered_lineage_or_expanded_scientific_changes(staged_plan_case, mutation):
    case = staged_plan_case
    run = make_plan(case)
    candidate = ExtractionRecord.model_validate(read(run / "reused_candidate.json"))
    if mutation == "numeric_change":
        candidate.observations[0].value.value = 999
    else:
        path = {"initial_scope": run / "resumed_baseline_initial_scope.json",
                "baseline": run / "resumed_baseline_candidate.json",
                "frozen_stage": run / "resumed_baseline_stage.json",
                "checkpoint": run / "resumed_baseline_checkpoint.json",
                "original_stage": case.stage}[mutation]
        path.write_bytes(path.read_bytes() + b" ")
    write_new_json(run / "candidate.v00.json", candidate.model_dump(mode="json"))
    with pytest.raises(ValueError):
        prepare_resumed_review(case.f.settings, read(run / "plan.json"), run, candidate)


@pytest.mark.parametrize("repairs", ["missingness", ["missingness", "missingness"], ["anything"], [True]])
def test_staged_continuation_requires_explicit_unique_repair_targets(staged_plan_case, repairs):
    with pytest.raises(ValueError):
        make_plan(staged_plan_case, repairs=repairs)


def test_staged_continuation_candidate_options_are_mutually_exclusive(staged_plan_case):
    case = staged_plan_case
    with pytest.raises(ValueError, match="mutually exclusive"):
        prepare(case.f.settings, case.imported / "sources.json", cohort_role="development",
            reuse_candidate=case.run / "candidate.v01.json", reuse_staged_candidate=case.stage)
    with pytest.raises(SystemExit):
        parser().parse_args(["prepare", "sources.json", "--reuse-candidate", "one.json",
                            "--reuse-staged-candidate", "two.json"])
    options = parser().parse_args(["prepare", "sources.json", "--reuse-staged-candidate", "two.json",
        "--continuation-repair", "missingness", "--continuation-repair", "shared_references"])
    assert options.continuation_repair == ["missingness", "shared_references"]


def test_staged_continuation_cumulative_bindings_verify_each_prior_revision(staged_plan_case):
    case = staged_plan_case
    run = make_plan(case)
    first, (first_receipt, first_scope, baseline) = prepare_audit(case, run)
    first_audit = baseline.model_copy(deep=True)
    first_audit.review_scope, first_audit.review_scope_sha256 = "scientific_delta", first_scope["scope_sha256"]
    write_new_json(run / "audit.v00.json", first_audit.model_dump(mode="json"))
    current = first.model_copy(deep=True)
    current.observations[0].limitations.append("Another synthetic bounded source limitation")
    write_new_json(run / "candidate.v01.json", current.model_dump(mode="json"))
    previous = {"candidate_path": str(run / "candidate.v00.json"),
        "candidate_sha256": hash_file(run / "candidate.v00.json"),
        "audit_path": str(run / "audit.v00.json"), "audit_sha256": hash_file(run / "audit.v00.json")}
    plan = read(run / "plan.json")
    receipt, scope, _ = prepare_resumed_review(case.f.settings, plan, run, current,
        revision=1, additional_audits=[previous])
    assert scope["old_record_hash"] == first_scope["old_record_hash"]
    assert scope["additional_audit_hashes"] == [digest_json(first_audit.model_dump(mode="json"))]
    assert receipt["additional_audits"] == [previous]
    write_new_json(run / "candidate.json", current.model_dump(mode="json"))
    write_new_json(run / "result.json", {"history": [
        {"audit_origin": "scientific_delta_model_audit", "candidate_sha256": hash_file(run / "candidate.v00.json"),
         "inherited_full_audit": plan["resumed_full_audit"]},
        {"audit_origin": "scientific_delta_model_audit", "candidate_sha256": hash_file(run / "candidate.v01.json")} ]})
    assert verify_resumed_review(case.f.settings, run, current, case.baseline, baseline, receipt)
    # Earlier audit validation must not depend on being the final history row.
    assert verify_resumed_review(case.f.settings, run, first, case.baseline, baseline, first_receipt)
    with pytest.raises(ValueError, match="every earlier"):
        prepare_resumed_review(case.f.settings, plan, run, current, revision=1)


def test_staged_continuation_cannot_use_full_or_missing_prior_audit(staged_plan_case):
    case = staged_plan_case
    run = make_plan(case)
    current = ExtractionRecord.model_validate(read(run / "reused_candidate.json"))
    write_new_json(run / "candidate.v00.json", current.model_dump(mode="json"))
    write_new_json(run / "candidate.v01.json", current.model_dump(mode="json"))
    write_new_json(run / "audit.v00.json", case.audit.model_dump(mode="json"))
    previous = {"candidate_path": str(run / "candidate.v00.json"),
        "candidate_sha256": hash_file(run / "candidate.v00.json"),
        "audit_path": str(run / "audit.v00.json"), "audit_sha256": hash_file(run / "audit.v00.json")}
    with pytest.raises(ValueError, match="scientific-delta"):
        prepare_resumed_review(case.f.settings, read(run / "plan.json"), run, current,
            revision=1, additional_audits=[previous])
