"""Coverage-only continuation after authenticated incremental scientific review.

These receipts describe a completed full-baseline-plus-delta chain or a partial
audit composite. They never relabel either as a new single full-paper audit.
"""
from __future__ import annotations

import json
from pathlib import Path

from .coverage_correction import POLICY, build_coverage_correction_scope
from .domain import ExtractionRecord
from .reading import ReadingAudit, ReadingDossier
from .reading_coverage_state import only_deferred_coverage_issues
from .storage import hash_file


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _coverage_only(records, run, plan, documents, record, dossier, audit, terminal):
    from .reader_workflow import reading_checks
    from .validation import validate_record
    from .workflow import validate_plan_metadata

    gaps = reading_checks(dossier, record, audit, documents)
    history = terminal.get("history", [])
    if (terminal.get("status") != "candidate_incomplete_reading_coverage"
            or terminal.get("stop_reason") != "coverage_correction_required"
            or terminal.get("human_validated") is not False or terminal.get("graph_published") is not False
            or terminal.get("validation_error_count") != 0
            or not history or history[-1].get("needs_revision") is not False
            or history[-1].get("candidate_sha256") != hash_file(run / "candidate.json")
            or not gaps or not only_deferred_coverage_issues(gaps)
            or terminal.get("reading_check_issues") != gaps
            or audit.potential_omissions
            or any(f.needs_revision or f.verdict == "unsupported" for f in audit.findings)
            or any(i.severity == "error" for i in [*validate_record(record, source_documents=documents),
                *validate_plan_metadata(record, plan, documents, store=records.store)])):
        raise ValueError("incremental/composite parent has defects beyond authenticated dossier coverage gaps")
    return gaps


def _registered(settings, parent_run):
    from .web_records import WebRecords
    records = WebRecords(settings)
    run = records._safe_data_path(parent_run)
    if (run.parent != (settings.data_root / "runs").resolve()
            or not any(r["run_id"] == run.name and r["kind"] == "extraction" for r in records.store.runs())):
        raise ValueError("coverage parent must be a registered extraction run")
    plan, _, bundle, documents = records._source(run.name)
    if plan.get("rule_version") != settings.rules_version or plan.get("coverage_correction_parent"):
        raise ValueError("coverage parent rules or ancestry are not eligible")
    return records, run, plan, bundle, documents


def _receipt(records, run, version, dossier_path, origin, gaps, **proof):
    # Recomputed on admission and execution; source/ancestry verifiers run before
    # this snapshot, so copying a receipt alone cannot authenticate a parent.
    paths = [p for p in sorted(run.rglob("*")) if p.is_file()]
    return {"policy": POLICY, "parent_run": run.name, "parent_version": version,
        "candidate_path": str(run / f"candidate.v{version:02d}.json"),
        "candidate_sha256": hash_file(run / f"candidate.v{version:02d}.json"),
        "audit_path": str(run / "model_review.json"), "audit_sha256": hash_file(run / "model_review.json"),
        "dossier_path": str(dossier_path), "dossier_sha256": hash_file(dossier_path),
        "source_bundle_sha256": hash_file(run / "sources.json"), "scientific_audit_origin": origin,
        "original_dossier_path": str(run / "reading_dossier.json"),
        "original_dossier_sha256": hash_file(run / "reading_dossier.json"),
        "artifact_hashes": [{"path": str(records._safe_data_path(p)), "sha256": hash_file(p)} for p in paths],
        "original_reading_check_issues": gaps, "replayed_reading_check_issues": gaps,
        "scientific_validation": "not_established", **proof}


def authenticate_incremental_coverage_parent(settings, parent_run):
    from .audit_addendum_workflow import _completed_call_protocol
    from .coverage_correction_workflow import CoverageParent
    from .staged_source_resume import _call

    records, run, plan, bundle, documents = _registered(settings, parent_run)
    terminal = _json(run / "result.json")
    history = terminal.get("history", [])
    version = history[-1].get("version") if history else None
    if type(version) is not int or not 0 <= version <= 5:
        raise ValueError("incremental coverage parent has an ambiguous candidate version")
    candidate_path, audit_path = run / f"candidate.v{version:02d}.json", run / f"audit.v{version:02d}.json"
    record, audit = ExtractionRecord.model_validate(_json(candidate_path)), ReadingAudit.model_validate(_json(audit_path))
    if (audit.review_scope not in {"citation_delta", "scientific_delta"}
            or hash_file(candidate_path) != hash_file(run / "candidate.json")
            or hash_file(audit_path) != hash_file(run / "model_review.json")
            or not records._audit_provenance(run, record, terminal, audit.model_dump(mode="json"))):
        raise ValueError("incremental coverage parent lacks its authenticated full-baseline and delta audit chain")
    dossier_path = run / ("reading_dossier.structural.v01.json"
        if terminal.get("structural_coverage_correction") else "reading_dossier.json")
    dossier = ReadingDossier.model_validate(_json(dossier_path))
    gaps = _coverage_only(records, run, plan, documents, record, dossier, audit, terminal)
    matches = []
    for request_path in (run / "calls").glob("*/request.json"):
        call = request_path.parent
        if not (call / "answer.json").is_file() or not (call / "result.json").is_file():
            continue
        try:
            if ReadingAudit.model_validate(_json(call / "answer.json")) != audit:
                continue
            request, completed, sources, _ = _call(records, run, request_path, plan,
                stage=f"audit_{version}", status="schema_valid_candidate")
            if any(hash_file(records._safe_data_path(path)) != checksum for path, checksum in sources.items()):
                raise ValueError("incremental source input changed")
            candidate_matches = (records._queried_candidate_snapshot(run, record, request, sources)
                if "candidate_binding" in request else record.model_dump_json(exclude_defaults=True) in request.get("prompt", ""))
            if (not candidate_matches or not request.get("citation_review")
                    or not records._citation_provenance(run, record, audit, request, sources)):
                continue
            scope = _json(records._safe_data_path(request["citation_review"]["scope_path"]))
            if audit.review_scope_sha256 != scope.get("scope_sha256"):
                continue
            if scope.get("policy") in {"bounded-revision-audit-v1", "bounded-scientific-revision-v1"}:
                from .source_review import verify_source_review
                if (request.get("source_review_call") != call.name
                        or request.get("source_review_scope_sha256") != scope.get("scope_sha256")
                        or not verify_source_review(run, scope, completed.get("source_review_proof"), call.name)):
                    continue
            events = _completed_call_protocol(run, completed, _json(call / "answer.json"))
            matches.append((call, events))
        except (ValueError, KeyError, OSError, TypeError):
            continue
    if len(matches) != 1:
        raise ValueError("incremental coverage parent needs one exact completed source-bound delta call")
    receipt = _receipt(records, run, version, dossier_path, history[-1]["audit_origin"], gaps,
        completed_incremental_audit_call=str(matches[0][0]), events_sha256=matches[0][1])
    return CoverageParent(run, plan, bundle, documents, record, dossier, audit,
        build_coverage_correction_scope(dossier, documents), receipt)


def authenticate_partial_coverage_parent(settings, parent_run):
    from .coverage_correction_workflow import CoverageParent
    from .partial_audit_completion_workflow import _authenticate_plan, verify_partial_audit_completion_run

    _registered(settings, parent_run)
    records, run, plan, parent = _authenticate_plan(settings, Path(parent_run) / "plan.json")
    terminal = _json(run / "result.json")
    record = ExtractionRecord.model_validate(_json(run / "candidate.json"))
    audit = ReadingAudit.model_validate(_json(run / "model_review.json"))
    dossier_path = run / "reading_dossier.effective.json"
    dossier = ReadingDossier.model_validate(_json(dossier_path))
    if not verify_partial_audit_completion_run(settings, run, record, audit):
        raise ValueError("partial composite coverage parent lacks its authenticated scientific audit chain")
    gaps = _coverage_only(records, run, plan, parent.documents, record, dossier, audit, terminal)
    proof = terminal["partial_audit_completion_proof"]
    receipt = _receipt(records, run, 0, dossier_path, "partial_scientific_audit_composite", gaps,
        completed_composite_addendum_call=proof["completed_call"], events_sha256=proof["events_sha256"],
        composite_proof_sha256=hash_file(run / "partial_audit_completion_proof.json"),
        scientific_parent_run=parent.run.name)
    return CoverageParent(run, plan, parent.bundle, parent.documents, record, dossier, audit,
        build_coverage_correction_scope(dossier, parent.documents), receipt)
