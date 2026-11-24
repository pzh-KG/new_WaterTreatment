"""Local versioned revalidation of authenticated equation-parent quote reports.

No source reading, model answer, audit or scientific candidate is rewritten.
This controller-only run inherits the completed source-bound Sol MAX call and
records zero new model/tool calls. Legacy v1 attempts keep their original status.
"""
from __future__ import annotations

from pathlib import Path

from .coverage_correction import (POLICY as LEGACY_POLICY, EQUATION_CONTEXT_POLICY,
    CoverageCorrectionReport, corrected_dossier, coverage_correction_issues)
from .coverage_correction_workflow import _authenticate_plan, _call_auth, _copy_new, _json
from .reading import ReadingDossier
from .storage import hash_file, write_new_json

POLICY = "coverage-equation-context-local-revalidation-v1"
ORIGIN = "inherited_scientific_audit_with_context_revalidation"
INTERPRETATION = ("Local equation-parent quote contract revalidation only. Scientific candidate, scientific audit, "
    "source dispositions and source limitations are inherited byte-for-byte from completed calls; "
    "zero new model calls and no new scientific validation.")
COPIES = ("sources.json", "candidate.v00.json", "candidate.json", "audit.v00.json", "model_review.json",
    "reading_dossier.json", "reading_dossier.corrected.v01.json", "coverage_correction_scope.json",
    "coverage_correction.v01.json", "coverage_correction_proof.json", "coverage_parent_receipt.json")


def authenticate_context_parent(settings, parent_run):
    """Accept only the known contract mismatch, after replaying all old proofs."""
    from .reader_workflow import reading_checks
    from .validation import validate_record

    records, run, plan, parent = _authenticate_plan(settings, Path(parent_run) / "plan.json")
    if plan.get("coverage_validation_policy", LEGACY_POLICY) != LEGACY_POLICY:
        raise ValueError("local context revalidation requires an unchanged legacy-v1 report")
    if plan.get("max_calls") != 1:
        raise ValueError("context revalidation requires one completed source-bound report")
    report = CoverageCorrectionReport.model_validate(_json(run / "coverage_correction.v01.json"))
    call_proof = _call_auth(records, run, plan, parent, report)
    effective = corrected_dossier(parent.scope, report, parent.dossier, parent.documents)
    legacy = coverage_correction_issues(parent.scope, report, parent.dossier, parent.documents)
    current = coverage_correction_issues(parent.scope, report, parent.dossier, parent.documents,
        validation_policy=EQUATION_CONTEXT_POLICY)
    checks = reading_checks(effective, parent.record, parent.audit, parent.documents)
    validation = validate_record(parent.record, source_documents=parent.documents)
    if (not legacy or any(not x.startswith("Coverage disposition ") or not x.endswith(
            " must quote its own registered block content.") for x in legacy)
            or current or checks or any(i.severity == "error" for i in validation)):
        raise ValueError("parent has defects beyond the registered equation-parent quote contract")
    result = _json(run / "result.json")
    expected_proof = {"policy": LEGACY_POLICY, "parent_receipt_sha256": hash_file(run / "coverage_parent_receipt.json"),
        "original_dossier_sha256": parent.receipt["dossier_sha256"],
        "correction_sha256": hash_file(run / "coverage_correction.v01.json"),
        "corrected_dossier_sha256": hash_file(run / "reading_dossier.corrected.v01.json"),
        "scope_sha256": parent.scope["scope_sha256"], **call_proof,
        **({"validation_policy": plan["coverage_validation_policy"]} if "coverage_validation_policy" in plan else {}),
        "interpretation": "Inherited completed scientific source audit plus a separate source-bound coverage-ledger correction; not a new scientific audit or scientific validation."}
    history = result.get("history", [])
    if (result.get("status") != "candidate_incomplete_budget"
            or result.get("stop_reason") != "coverage_correction_unresolved"
            or result.get("reading_check_issues") != legacy
            or result.get("model_calls") != 1 or result.get("validation_error_count") != 0
            or result.get("human_validated") is not False or result.get("graph_published") is not False
            or result.get("revisions") != 0 or result.get("coverage_source_limitations") != report.source_limitations
            or result.get("coverage_correction_proof") != expected_proof
            or _json(run / "coverage_correction_proof.json") != expected_proof
            or ReadingDossier.model_validate(_json(run / "reading_dossier.corrected.v01.json")) != effective
            or len(history) != 1 or history[0].get("audit_origin") != "inherited_scientific_audit_with_coverage_correction"
            or history[0].get("candidate_sha256") != parent.receipt["candidate_sha256"]
            or history[0].get("reading_check_issues") != legacy
            or history[0].get("validation_errors") != 0 or history[0].get("needs_revision") is not True):
        raise ValueError("legacy coverage result, report, proof or dossier changed")
    for name, checksum in {"candidate.json": parent.receipt["candidate_sha256"],
            "audit.v00.json": parent.receipt["audit_sha256"], "model_review.json": parent.receipt["audit_sha256"]}.items():
        if hash_file(run / name) != checksum:
            raise ValueError("immutable scientific candidate or audit changed")
    completed = _json(Path(call_proof["completed_call"]) / "result.json")
    # Freeze original artifacts as well as the source/tool proof. This retains
    # failures rather than overwriting the former controller outcome.
    paths = sorted(p for p in run.rglob("*") if p.is_file())
    receipt = {"policy": POLICY, "validation_policy": EQUATION_CONTEXT_POLICY,
        "parent_run": run.name, "scientific_parent_run": parent.run.name,
        "artifact_hashes": [{"path": str(records._safe_data_path(p)), "sha256": hash_file(p)} for p in paths],
        "source_hashes": parent.scope["source_hashes"], "scope_sha256": parent.scope["scope_sha256"],
        "legacy_reading_check_issues": legacy, "replayed_reading_check_issues": [],
        "inherited_model": {"completed_call": call_proof["completed_call"],
            "server_selected_model": completed["server_selected_model"],
            "server_selected_effort": completed["server_selected_effort"],
            "elapsed_seconds": completed.get("elapsed_seconds"),
            "events_sha256": call_proof["events_sha256"],
            "source_review_proof_sha256": call_proof["source_review_proof_sha256"]},
        "scientific_validation": "not_established"}
    return records, run, plan, parent, report, receipt, validation


def _expected_result(run, parent, report, receipt, proof):
    from .quantitative import quantitative_summary
    return {"status": "candidate_audited_with_limitations", "stop_reason": "audit_completed",
        "model_calls": 0, "reading_tool_calls": 0, "revisions": 0,
        "validation_error_count": 0, "reading_check_issues": [],
        "human_validated": False, "graph_published": False, "scientific_accuracy": "not_established",
        "dataset_role": parent.plan["cohort_role"], "inherited_model": receipt["inherited_model"],
        "coverage_context_revalidation_proof": proof,
        "coverage_source_limitations": report.source_limitations,
        "quantitative_coverage_summary": quantitative_summary(parent.record),
        "review_mode": "inherited_scientific_audit_and_source_delivery_local_contract_revalidation",
        "history": [{"version": 0, "candidate_sha256": hash_file(run / "candidate.json"),
            "audit_origin": ORIGIN, "validation_errors": 0, "needs_revision": False, "reading_check_issues": []}]}


def _proof(run, plan, receipt):
    return {"policy": POLICY, "validation_policy": EQUATION_CONTEXT_POLICY,
        "parent_receipt_sha256": hash_file(run / "context_revalidation_parent.json"),
        "parent_run": receipt["parent_run"], "scope_sha256": receipt["scope_sha256"],
        "copies": {name: hash_file(run / name) for name in COPIES},
        "manifest_sha256": hash_file(run / "manifest.json"),
        "validation_code": plan["validation_code"], "code_identity": plan["code_identity"],
        "inherited_model": receipt["inherited_model"], "model_calls": 0, "interpretation": INTERPRETATION}


def revalidate_context(settings, parent_run):
    """Publish a new local run only; never open a provider or graph connection."""
    from .codex import code_identity
    from .workflow import runtime_versions

    records, old, old_plan, parent, report, receipt, validation = authenticate_context_parent(settings, parent_run)
    for row in records.store.runs():
        manifest = _json(row["manifest_path"])
        if manifest.get("operation") == POLICY and manifest.get("parent_run") == old.name:
            existing = Path(row["manifest_path"]).parent
            if verify_context_revalidation_run(settings, existing, parent.record, parent.audit):
                return existing
            raise ValueError("a retained context revalidation attempt exists; do not overwrite it")
    run = records.store.new_run("extraction", {"paper_id": parent.record.paper.id,
        "cohort_role": old_plan["cohort_role"], "rule_version": old_plan["rule_version"],
        "schema_version": parent.record.schema_version, "workflow": POLICY,
        "operation": POLICY, "parent_run": old.name})
    for name in COPIES:
        _copy_new(old / name, run / name)
    write_new_json(run / "context_revalidation_parent.json", receipt)
    archived_code = []
    for name in ("coverage_correction.py", "coverage_context_revalidation.py"):
        target = run / "validation_code" / name
        target.parent.mkdir(exist_ok=True)
        _copy_new(Path(__file__).parent / name, target)
        archived_code.append({"path": str(target), "sha256": hash_file(target)})
    inputs = [run / name for name in (*COPIES, "context_revalidation_parent.json")]
    inputs += [Path(d.source.path) for d in parent.documents]
    plan = {"version": 1, "run_id": run.name, "paper_id": parent.record.paper.id,
        "cohort_role": old_plan["cohort_role"], "rule_version": old_plan["rule_version"],
        "schema_version": parent.record.schema_version, "workflow": POLICY, "operation": POLICY,
        "workdir": str(run), "manifest_sha256": hash_file(run / "manifest.json"), "max_calls": 0, "status": "prepared_local_only",
        "input_paths": [{"path": str(p), "sha256": hash_file(p)} for p in inputs],
        "reading_artifacts": archived_code, "validation_code": archived_code,
        "image_paths": [], "code_identity": code_identity(settings.project_root), "runtime": runtime_versions(),
        "model_settings": old_plan["model_settings"],
        "model_execution": "none_inherited_completed_call", "inherited_model": receipt["inherited_model"],
        "reading_settings": {"enabled": False, "max_revisions": 0, "max_tool_calls": 0},
        "context_revalidation_parent": receipt, "validation_policy": EQUATION_CONTEXT_POLICY,
        "extraction_policy": old_plan.get("extraction_policy")}
    write_new_json(run / "plan.json", plan)
    records.store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json"),
        "manifest_sha256": hash_file(run / "manifest.json")})
    proof = _proof(run, plan, receipt)
    write_new_json(run / "context_revalidation_proof.json", proof)
    write_new_json(run / "validation.json", {"issues": [v.model_dump(mode="json") for v in validation],
        "semantic_accuracy": "not_established"})
    result = _expected_result(run, parent, report, receipt, proof)
    write_new_json(run / "result.json", result)
    records.store.event(run.name, "candidate_completed", {"status": result["status"], "model_calls": 0,
        "result_sha256": hash_file(run / "result.json"), "proof_sha256": hash_file(run / "context_revalidation_proof.json")})
    records.store.record_development_use(run.name, parent.record.paper.id,
        [d.source.sha256 for d in parent.documents],
        "Local equation-parent context contract revalidation of inherited completed source reading; not independent validation; parent=" + old.name)
    if not verify_context_revalidation_run(settings, run, parent.record, parent.audit):
        raise ValueError("local context revalidation failed its own provenance replay")
    return run


def verify_context_revalidation_run(settings, run, record, parsed):
    from .web_records import WebRecords
    try:
        records = WebRecords(settings)
        run = records._safe_data_path(run)
        if run.parent != (settings.data_root / "runs").resolve():
            return False
        plan, _, _, _ = records._source(run.name)
        manifest = _json(run / "manifest.json")
        registered = [row for row in records.store.runs() if row["run_id"] == run.name]
        prepared = records.store.last_event(run.name, "prepared")
        if (len(registered) != 1 or registered[0]["kind"] != "extraction"
                or Path(registered[0]["manifest_path"]).resolve() != run / "manifest.json"
                or manifest.get("kind") != "extraction" or manifest.get("run_id") != run.name
                or manifest.get("operation") != POLICY or manifest.get("workflow") != POLICY
                or manifest.get("created_at") != registered[0]["created_at"]
                or any(manifest.get(key) != plan.get(key) for key in
                    ("paper_id", "cohort_role", "rule_version", "schema_version"))
                or plan.get("manifest_sha256") != hash_file(run / "manifest.json")
                or prepared.get("manifest_sha256") != hash_file(run / "manifest.json")):
            return False
        receipt = plan["context_revalidation_parent"]
        if manifest.get("parent_run") != receipt["parent_run"]:
            return False
        _, old, old_plan, parent, report, expected_receipt, validation = authenticate_context_parent(settings,
            settings.data_root / "runs" / receipt["parent_run"])
        if (receipt != expected_receipt or _json(run / "context_revalidation_parent.json") != receipt
                or record != parent.record or parsed != parent.audit
                or plan.get("workflow") != POLICY or plan.get("validation_policy") != EQUATION_CONTEXT_POLICY
                or plan.get("max_calls") != 0 or plan.get("model_execution") != "none_inherited_completed_call"
                or plan.get("model_settings") != old_plan["model_settings"]
                or plan.get("inherited_model") != receipt["inherited_model"]
                or (run / "calls").exists() or (run / "reading_tools").exists()
                or any(hash_file(run / name) != hash_file(old / name) for name in COPIES)):
            return False
        if (len(plan.get("validation_code", [])) != 2 or plan["reading_artifacts"] != plan["validation_code"]
                or {Path(item["path"]).name for item in plan["validation_code"]}
                    != {"coverage_correction.py", "coverage_context_revalidation.py"}):
            return False
        for item in plan["validation_code"]:
            path = records._safe_data_path(item["path"])
            if path.parent != run / "validation_code" or hash_file(path) != item["sha256"]:
                return False
        proof = _proof(run, plan, receipt)
        event = records.store.last_event(run.name, "candidate_completed")
        return (event is not None and event.get("result_sha256") == hash_file(run / "result.json")
            and event.get("proof_sha256") == hash_file(run / "context_revalidation_proof.json")
            and _json(run / "context_revalidation_proof.json") == proof
            and _json(run / "result.json") == _expected_result(run, parent, report, receipt, proof)
            and _json(run / "validation.json") == {"issues": [v.model_dump(mode="json") for v in validation],
                "semantic_accuracy": "not_established"})
    except (ValueError, OSError, KeyError, TypeError, IndexError, AttributeError):
        return False
