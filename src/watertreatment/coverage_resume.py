"""Resume source-backed parts of a failed reading-ledger turn without changing its outcome."""
from dataclasses import replace
from pathlib import Path
from typing import Literal

from pydantic import Field

from .coverage_correction import (
    CoverageCorrectionReport, EQUATION_CONTEXT_POLICY, build_coverage_correction_scope,
    corrected_dossier, disposition_issues,
)
from .domain import StrictModel
from .reading import SourceAnchor
from .storage import digest_json, hash_file


class ConcernReview(StrictModel):
    concern_index: int = Field(ge=0)
    decision: Literal["supported_as_recorded", "requires_scientific_revision", "unresolved"]
    explanation: str = Field(min_length=16)
    evidence: list[SourceAnchor] = Field(min_length=1)


class ResumedCoverageReport(CoverageCorrectionReport):
    concern_reviews: list[ConcernReview]


def report_type(parent):
    return ResumedCoverageReport if parent.receipt.get("coverage_resume") else CoverageCorrectionReport


def concern_issues(parent, report):
    retained = parent.receipt.get("coverage_resume", {})
    concerns = retained.get("scientific_concerns", [])
    if not retained:
        return []
    rows = report.concern_reviews
    if sorted(row.concern_index for row in rows) != list(range(len(concerns))):
        return ["Every retained scientific concern needs exactly one named source disposition."]
    delivered = {(a["source_id"], a["block_id"]) for a in parent.scope["source_anchors"]}
    blocks = {(d.source.source_id, b.block_id): b for d in parent.documents for b in d.blocks}
    issues = []
    for row in rows:
        old = concerns[row.concern_index]
        required = {(old["source_id"], bid) for bid in old["block_ids"]}
        cited = {(a.source_id, a.block_id) for a in row.evidence}
        exact = all((b := blocks.get((a.source_id, a.block_id))) is not None
            and (bool(a.quote and a.quote.strip() and a.quote in b.text) or bool(not b.text.strip() and a.figure_or_table
                 and b.kind in {"image", "pdf_page"})) for a in row.evidence)
        if (row.decision != "supported_as_recorded" or not required <= cited <= delivered or not exact):
            issues.append("Retained scientific concern requires further source review: " + old["explanation"])
    return issues


def eligible_entries(scope, report, progress, documents, candidate_read, concerns):
    """A grouped declaration is indivisible; neither empty tools nor an answer alone count."""
    delivered = {(x["source_id"], x["block_id"]) for x in progress["delivered"]}
    viewed = {(x["source_id"], x["block_id"]) for x in progress["full_views"]}
    pending = {(x["source_id"], x["block_id"]) for key in
               ("missing_blocks", "missing_full_views", "missing_native_structures") for x in progress.get(key, [])}
    pending.update((c["source_id"], bid) for c in concerns for bid in c["block_ids"])
    counts = {}
    for entry in report.dispositions:
        for bid in entry.block_ids:
            key = entry.source_id, bid
            counts[key] = counts.get(key, 0) + 1
    valid = []
    for entry in report.dispositions:
        keys = {(entry.source_id, bid) for bid in entry.block_ids}
        anchors = {(a.source_id, a.block_id) for a in entry.evidence}
        if (not (keys | anchors) <= delivered or (keys | anchors) & pending
                or any(counts[k] != 1 for k in keys) or entry.visual_checked and not keys <= viewed
                or entry.category == "substantive" and not candidate_read):
            continue
        single = report.model_copy(update={"dispositions": [entry], "scientific_concerns": []})
        if not disposition_issues(scope, single, documents, validation_policy=EQUATION_CONTEXT_POLICY, require_all=False):
            valid.append(entry)
    return valid


def authenticate_failed_coverage(settings, run):
    from .coverage_correction_workflow import _authenticate_plan, _json, _substantive_candidate_read
    from .partial_audit_completion_workflow import _source_receipts
    from .partial_audit_remaining_resume import _timeout_protocol
    from .audit_addendum_workflow import _completed_call_protocol
    from .source_review import partial_source_review_progress
    from .staged_source_resume import _call

    records, run, plan, old = _authenticate_plan(settings, Path(run) / "plan.json")
    paths = list((run / "calls").glob("*/request.json"))
    if len(paths) != 1 or any((run / f).exists() for f in ("result.json", "coverage_correction.v01.json", "candidate.json")):
        raise ValueError("阅读补正已有后续报告，或无法确认唯一的失败调用")
    call = paths[0].parent
    request, output, inputs, _ = _call(records, run, paths[0], plan, stage="coverage_correction_0", status="failed")
    failure = _json(run / "failure.json")
    if (failure.get("model_calls") != 1 or failure.get("error") != output.get("error")
            or request.get("citation_review") != {"scope_path": str(run / "coverage_correction_scope.json"),
                "scope_file_sha256": hash_file(run / "coverage_correction_scope.json")}
            or request.get("source_review_call") != call.name
            or request.get("source_review_scope_sha256") != old.scope["scope_sha256"]
            or any(hash_file(records._safe_data_path(k)) != val for k, val in inputs.items())):
        raise ValueError("阅读补正的候选、范围或来源与原调用不符")
    progress = partial_source_review_progress(run, old.scope, _source_receipts(run, call.name), call.name)
    if progress != _json(call / "source_review_progress.json") or progress != output.get("source_review_progress"):
        raise ValueError("历史阅读交付与失败时保存的回执不一致")
    prior = old.receipt.get("coverage_resume", {})
    concerns = list(prior.get("scientific_concerns", []))
    limits = list(prior.get("source_limitations", []))
    accepted = []
    if (call / "answer.json").is_file():
        raw = _json(call / "answer.json")
        events = _completed_call_protocol(run, output, raw)
        report = report_type(old).model_validate(raw)
        if report.scope_sha256 != old.scope["scope_sha256"] or not str(failure["error"]).startswith("ValueError: bounded source review delivery incomplete;"):
            raise ValueError("阅读补正的失败不属于可验证的局部来源交付")
        for c in report.scientific_concerns:
            if c.model_dump(mode="json") not in concerns:
                concerns.append(c.model_dump(mode="json"))
        limits.extend(x for x in report.source_limitations if x not in limits)
        accepted = eligible_entries(old.scope, report, progress, old.documents,
            _substantive_candidate_read(records, run, old, call, request, inputs), concerns)
    elif str(failure["error"]).startswith("TimeoutError:"):
        events = _timeout_protocol(run, output)
    else:
        raise ValueError("阅读补正未保存可核验的模型交付或超时回执")
    partial = CoverageCorrectionReport(scope_sha256=old.scope["scope_sha256"], dispositions=accepted)
    dossier = corrected_dossier(old.scope, partial, old.dossier, old.documents)
    # An older dossier may already cover a block subsequently challenged by the
    # report. Reopen that specific block in the derived ledger so that its next
    # disposition requires original-source delivery again.
    reopened = {(c["source_id"], bid) for c in concerns for bid in c["block_ids"]}
    retained = []
    for entry in dossier.coverage:
        bids = [bid for bid in entry.block_ids if (entry.source_id, bid) not in reopened]
        if bids:
            retained.append(entry.model_copy(update={"block_ids": bids}))
    dossier = dossier.model_copy(update={"coverage": retained})
    scope = build_coverage_correction_scope(dossier, old.documents)
    receipt = {**old.receipt, "parent_run": run.name, "candidate_path": str(run / "candidate.v00.json"),
        "audit_path": str(run / "coverage_parent_audit.json"), "dossier_path": str(run / "reading_dossier.json"),
        "coverage_resume": {"failed_run": run.name, "failed_plan_sha256": hash_file(run / "plan.json"),
            "events_sha256": events, "progress_sha256": progress["progress_sha256"],
            "dossier_hash": digest_json(dossier.model_dump(mode="json")), "scientific_concerns": concerns,
            "source_limitations": limits, "inherited_blocks": sum(len(x.block_ids) for x in accepted),
            "remaining_blocks": len(scope["required_dispositions"])}}
    return replace(old, run=run, plan=plan, dossier=dossier, scope=scope, receipt=receipt)
