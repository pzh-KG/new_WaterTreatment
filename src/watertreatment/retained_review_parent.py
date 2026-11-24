"""Authenticate delivered local findings, including reports rejected after the native turn."""
from dataclasses import dataclass
from pathlib import Path
import json

from .domain import ExtractionRecord
from .reading import ReadingAudit, ReadingDossier
from .storage import hash_file


def read(path):
    return json.loads(Path(path).read_bytes())


@dataclass
class RetainedParent:
    run: Path
    plan: dict
    bundle: dict
    documents: list
    before: ExtractionRecord
    candidate: ExtractionRecord
    dossier: ReadingDossier
    audit: ReadingAudit
    findings: list
    context: list
    receipt: dict


def _coverage(settings, run):
    from .coverage_correction import corrected_dossier, coverage_correction_issues, VISUAL_CONTEXT_POLICY
    from .coverage_correction_workflow import _authenticate_plan, _call_auth, delivered_context
    from .coverage_resume import report_type
    records, _, plan, parent = _authenticate_plan(settings, run / "plan.json")
    result = read(run / "result.json")
    report = report_type(parent).model_validate(read(run / "coverage_correction.v01.json"))
    if result.get("stop_reason") != "coverage_correction_unresolved":
        raise ValueError("No completed but unresolved reading correction")
    proof = _call_auth(records, run, plan, parent, report)
    effective = corrected_dossier(parent.scope, report, parent.dossier, parent.documents)
    if (ReadingDossier.model_validate(read(run / "reading_dossier.corrected.v01.json")) != effective
            or hash_file(run / "candidate.json") != parent.receipt["candidate_sha256"]
            or hash_file(run / "coverage_parent_audit.json") != parent.receipt["audit_sha256"]):
        raise ValueError("Coverage candidate, dossier or inherited scientific audit changed")
    # Require a new literal delivery of any linked caption the older turn did not deliver.
    context = {(a.source_id, a.block_id) for row in report.dispositions for a in row.evidence
               if a.block_id not in row.block_ids}
    delivered = delivered_context(run, parent.scope, VISUAL_CONTEXT_POLICY)
    checks = coverage_correction_issues(parent.scope, report, parent.dossier, parent.documents,
        validation_policy=VISUAL_CONTEXT_POLICY, delivered=delivered | context)
    other = [x for x in checks if not x.startswith("Coverage recheck found a scientific concern")]
    if other:
        raise ValueError("Unresolved reading dispositions: " + "; ".join(other))
    concerns = parent.receipt.get("coverage_resume", {}).get("scientific_concerns", [])
    rows = getattr(report, "concern_reviews", [])
    if sorted(x.concern_index for x in rows) != list(range(len(concerns))):
        raise ValueError("Reading concerns are not individually accounted for")
    findings = [{"text": concerns[x.concern_index]["explanation"] + " " + x.explanation,
        "evidence": [a.model_dump(mode="json") for a in x.evidence]}
        for x in rows]
    findings.extend({"text": x.explanation, "evidence": [{"source_id": x.source_id, "block_id": bid}
        for bid in x.block_ids]} for x in report.scientific_concerns)
    if not findings:
        raise ValueError("No located scientific findings to repair")
    return RetainedParent(run, plan, read(run / "sources.json"), parent.documents, parent.record, parent.record,
        effective, parent.audit, findings, sorted(context), proof)


def _partial(settings, run):
    from . import partial_audit_completion_workflow as workflow
    from .partial_audit_remaining_resume import POLICY, _authenticate_plan
    from .partial_audit_completion import PartialAuditAddendum, merge_partial_audit_addendum, validate_partial_audit_addendum
    if read(run / "plan.json").get("workflow") == POLICY:
        records, _, plan, parent, _ = _authenticate_plan(settings, run / "plan.json")
    else:
        records, _, plan, parent = workflow._authenticate_plan(settings, run / "plan.json")
    supplement = PartialAuditAddendum.model_validate(read(run / "partial_audit_addendum.json"))
    proof, progress = workflow._call_auth(records, run, plan, parent, supplement)
    extra = workflow.delivered_table_captions(parent, supplement, progress)
    args = workflow._scientific_inputs(parent)
    merged = merge_partial_audit_addendum(parent.scope, supplement, parent.audit, **args, contextual_citations=extra)
    checks = validate_partial_audit_addendum(parent.scope, supplement, parent.audit, **args, contextual_citations=extra)
    findings = [{"text": f.explanation, "paths": f.record_paths or [f.record_path],
        "evidence": [a.model_dump(mode="json") for a in f.evidence]} for f in supplement.findings
        if f.needs_revision or f.verdict != "supported"]
    findings.extend({"text": text, "evidence": []} for text in supplement.scientific_concerns)
    # Retain exact paths lacking a disposition, including those masked by a blocker.
    prefixes = ("Completion addendum lacks an exact source disposition: ", "Affected entity/dependency lacks explicit source disposition: ")
    missing = {text[len(prefix):] for text in checks for prefix in prefixes if text.startswith(prefix)}
    targets = {t["record_path"]: t for t in parent.scope["targets"]}
    for path in sorted(missing):
        if path not in targets:
            raise ValueError("Unlocated partial-report obligation: " + path)
        prior = [f.explanation for f in supplement.findings if path in (f.record_paths or [f.record_path])]
        findings.append({"text": "Reconcile the retained disposition and its required evidence for " + path,
            "paths": [path], "inherited_judgments": prior, "evidence": [],
            "required_evidence_ids": targets[path]["required_evidence_ids"]})
    other = [x for x in checks if not x.startswith((*prefixes, "Completion addendum reports a new scientific concern",
        "Completion addendum retains scientific concerns"))]
    if other:
        raise ValueError("Additional unlocated review obligations: " + "; ".join(other))
    if not findings or hash_file(run / "candidate.v00.json") != parent.receipt["candidate_sha256"]:
        raise ValueError("No authentic unfinished partial report on this candidate")
    return RetainedParent(run, plan, parent.bundle, parent.documents, parent.record, parent.record,
        parent.dossier, merged, findings, sorted(extra), proof)


def _guided(settings, run):
    from .audit_guidance_resume import verify_guidance
    from .completed_continuation_resume import _completed_protocol
    from .completed_native_resume import _final_submission
    from .staged_source_resume import _call, _replay
    from .web_records import WebRecords
    from .ownership_audit import check_contextual_edits
    records = WebRecords(settings)
    plan, _, bundle, docs = records._source(run.name)
    history, _ = verify_guidance(settings, run, plan)
    rejection = read(run / "scope_rejection.v01.json")
    path = run / "candidate.v01.json"
    candidate = ExtractionRecord.model_validate(read(path))
    before = history["record"]
    if (rejection.get("status") != "recoverable_scope_block" or rejection.get("automatic_full_audit") is not False
            or rejection.get("candidate_sha256") != hash_file(path) or (run / "audit.v01.json").exists()
            or sorted(run.glob("candidate.v[0-9][0-9].json"))[-1] != path):
        raise ValueError("No exact unaudited guided revision")
    calls = list((run / "calls").glob("*/request.json"))
    if len(calls) != 1:
        raise ValueError("Guided recovery requires a single completed revision")
    call = calls[0].parent
    req, completed, inputs, _ = _call(records, run, calls[0], plan, stage="revision_1", status="schema_valid_candidate")
    snap = req["candidate_binding"]
    bound = records._safe_data_path(snap["path"])
    if hash_file(bound) != inputs.get(str(bound)) or ExtractionRecord.model_validate(read(bound)) != before:
        raise ValueError("Revision base differs from the guided candidate")
    steps, events_hash = _completed_protocol(run / "app_server/events.jsonl", completed)
    replay = before
    for step in steps:
        if step["tool"] != "wt_patch_record":
            raise ValueError("The retained local revision contains a whole-record submission")
        replay = _replay(replay, step)
    final = _final_submission(run / "app_server/events.jsonl", completed)
    if (replay != candidate or read(call / "answer.json") != final or final["submission_id"] != steps[-1]["data"]["submission_id"]
            or ExtractionRecord.model_validate(read(call / "record.json")) != candidate):
        raise ValueError("Native patch chain differs from the retained candidate")
    check_contextual_edits(before.model_dump(mode="json"), candidate.model_dump(mode="json"))
    audit = history["audit"]
    findings = [{"text": f.explanation, "paths": f.record_paths or [f.record_path],
        "evidence": [a.model_dump(mode="json") for a in f.evidence]} for f in audit.findings
        if f.needs_revision or f.verdict in {"unsupported", "not_checked"}]
    findings.extend({"text": text, "evidence": []} for text in audit.potential_omissions)
    dossier = ReadingDossier.model_validate(read(run / "reading_dossier.json"))
    if (run / "reading_dossier.structural.v01.json").exists():
        # Reconstruct without mutating the retained run.
        from .coverage_correction import build_coverage_correction_scope, corrected_dossier, CoverageCorrectionReport
        sc = build_coverage_correction_scope(dossier, docs)
        dossier = corrected_dossier(sc, CoverageCorrectionReport(scope_sha256=sc["scope_sha256"], dispositions=[]), dossier, docs)
        if dossier != ReadingDossier.model_validate(read(run / "reading_dossier.structural.v01.json")):
            raise ValueError("Structural reading ledger changed")
    return RetainedParent(run, plan, bundle, docs, before, candidate, dossier, audit, findings, [],
        {"call": call.name, "events_sha256": events_hash, "candidate_sha256": hash_file(path)})


def authenticate(settings, run, *, source_amendment=None):
    run = Path(run).resolve(strict=True)
    if run.parent != (settings.data_root / "runs").resolve():
        raise ValueError("Expected a registered local extraction run")
    plan = read(run / "plan.json")
    if plan.get("cohort_role") not in {"development", "unassigned"} or plan["rule_version"] != settings.rules_version:
        raise ValueError("Development recovery cannot alter a held-out paper or change its extraction policy")
    if source_amendment is not None and plan.get("workflow") != "retained-issue-source-review-v1":
        raise ValueError("A source follow-up requires a signed completed local review")
    if plan.get("workflow") == "retained-issue-source-review-v1":
        from .retained_review import _adjudicated, _bound, _outcome, _public
        records, ancestor, current, _, packet = _bound(settings, run)
        from .retained_tool_retry import failed_parent, is_source_interruption
        if is_source_interruption(run):
            if source_amendment is not None:
                raise ValueError("An interrupted review must reconcile all of its original obligations")
            return failed_parent(settings, run, records, ancestor, plan, packet)
        report, proof = _public(records, run, plan, packet)
        result, dossier, audit = _outcome(run, ancestor, current, packet, report, proof)
        current = _adjudicated(ancestor,current,packet,report)
        if (result != read(run / "result.json")
                or current.model_dump(mode="json") != read(run / "candidate.json")
                or dossier.model_dump(mode="json") != read(run / "reading_dossier.corrected.v01.json")
                or audit.model_dump(mode="json") != read(run / "model_review.json")):
            raise ValueError("No authenticated unresolved local review on this candidate")
        if source_amendment is not None:
            from .retained_amendment import declared_findings
            from .storage import digest_json
            if (result["status"] != "candidate_audited_with_limitations" or result["reading_check_issues"]
                    or packet.get("version", 0) < 3):
                raise ValueError("First resolve outstanding scientific questions on this review")
            findings, anchors = declared_findings(run, ancestor.documents, source_amendment)
            return RetainedParent(run, plan, ancestor.bundle, ancestor.documents, current, current, dossier, audit,
                findings, anchors, {**proof, "mode": "native-repair-targets-v1", "source_amendment": digest_json(source_amendment)})
        if not result["reading_check_issues"]:
            raise ValueError("No authenticated unresolved local review on this candidate")
        findings = [{"text": f.explanation, "paths": f.record_paths,
            "evidence": [a.model_dump(mode="json") for a in f.evidence]}
            for f in audit.findings if f.needs_revision]
        findings.extend({"text": item, "evidence": []} for item in report.additional_issues)
        if not findings:
            raise ValueError("The document requires a reading-ledger recovery")
        return RetainedParent(run, plan, ancestor.bundle, ancestor.documents, current, current, dossier, audit,
            findings, [], ({**proof,"mode":"native-repair-targets-v1"} if packet.get("version",0) >= 3 else proof))
    if plan.get("workflow") == "reading_coverage_correction":
        return _coverage(settings, run)
    if plan.get("workflow") in {"partial_scientific_audit_completion", "partial-audit-remaining-resume-v1"}:
        return _partial(settings, run)
    if plan.get("audit_guidance"):
        return _guided(settings, run)
    from .native_repair_review import available, authenticate_native
    if available(run):
        return authenticate_native(settings,run)
    raise ValueError("No supported retained issue report for targeted resolution")
