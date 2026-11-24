"""Replay the signed audit inputs of a stopped run whose final history was never written."""
from pathlib import Path
import re

from .audit_addendum_workflow import _completed_call_protocol
from .audit_execution import select_scientific_scope_limits
from .domain import ExtractionRecord
from .reading import ReadingAudit
from .retained_review_parent import read
from .scientific_revision_audit import build_scientific_revision_scope, validate_scientific_revision_audit
from .source_review import verify_source_review
from .storage import hash_file


def confirm_delta_lineage(records, run, plan, version, record, audit):
    from .native_repair_review import _one_call

    def delivered(index):
        call, request, result, inputs = _one_call(records, run, plan, f"audit_{index}", "schema_valid_candidate")
        answer = read(call / "answer.json")
        report = ReadingAudit.model_validate(read(run / f"audit.v{index:02d}.json"))
        material = ExtractionRecord.model_validate(read(run / f"candidate.v{index:02d}.json"))
        if ReadingAudit.model_validate(answer) != report:
            raise ValueError("Source audit differs from the delivered verdict")
        _completed_call_protocol(run, result, answer)
        return call, request, result, inputs, material, report

    if audit.review_scope != "scientific_delta" or version < 1:
        raise ValueError("Recovery requires a source-scoped scientific audit")
    last = delivered(version)
    base_path = Path(last[1]["citation_review"]["baseline_candidate_path"])
    match = re.fullmatch(r"candidate\.v(\d{2})\.json", base_path.name)
    if not match or base_path.parent != run or last[-2:] != (record, audit):
        raise ValueError("Invalid source-audit lineage for the stopped candidate")
    base_index = int(match[1])
    if not 1 <= version - base_index <= 4:
        raise ValueError("The retained audit chain exceeds the supported cumulative scope")
    _, _, _, _, before, original = delivered(base_index)
    if (original.review_scope != "full" or original.review_scope_sha256 is not None
            or not records._completed_audit_provenance(run, before, original, allow_citation=False)):
        raise ValueError("The retained source chain lacks its completed full baseline")
    previous_audits, previous_scopes, extra = [], [], []
    previous_material = before
    for index in range(base_index + 1, version + 1):
        call, request, result, inputs, current, report = last if index == version else delivered(index)
        scope_path = run / f"citation_scope.v{index:02d}.json"
        paths = {"scope": scope_path, "current_candidate": run / f"candidate.v{index:02d}.json",
                 "baseline_candidate": base_path, "baseline_audit": run / f"audit.v{base_index:02d}.json"}
        expected_inputs = {"additional_audits": list(extra)}
        for key, path in paths.items():
            checksum = hash_file(path)
            if inputs.get(str(path)) != checksum:
                raise ValueError("A prior review did not receive the registered candidate, scope or audit")
            expected_inputs[key + "_path"] = str(path)
            expected_inputs["scope_file_sha256" if key == "scope" else key + "_sha256"] = checksum
        if (request.get("citation_review") != expected_inputs
                or not records._queried_candidate_snapshot(run, current, request, inputs)
                or any(inputs.get(item[key + "_path"]) != item[key + "_sha256"] for item in extra for key in ("candidate", "audit"))):
            raise ValueError("A retained delta references a different review baseline")
        scope = read(scope_path)
        limits = select_scientific_scope_limits(run, plan if "scientific_scope_limits" in plan else None,
                                                previous_scopes=[scope, *previous_scopes])
        if limits != scope["scope_limits"] or (limits.get("compiler_version") in {3, 4, 6}
                and plan.get("scientific_scope_limits", {}).get("compiler_version") != limits["compiler_version"]):
            raise ValueError("Source-scope compiler differs from the frozen run")
        previous_step = None
        if scope.get("structural_followup"):
            from .ownership_followup import POLICY, build_followup_scope
            if plan.get("structural_followup_policy") != POLICY or len(previous_audits) != 1:
                raise ValueError("Unsupported structural source follow-up lineage")
            previous_step = (previous_scopes[0], previous_material)
            rebuilt = build_followup_scope(before, original, previous_material, current, previous_audits[0], previous_scopes[0])
        else:
            rebuilt = build_scientific_revision_scope(before, current, original, baseline_origin="fresh_model_audit",
                                                     additional_audits=previous_audits, **limits)
        if (rebuilt != scope or report.review_scope != "scientific_delta" or report.review_scope_sha256 != scope["scope_sha256"]
                or request.get("source_review_call") != call.name or request.get("source_review_scope_sha256") != scope["scope_sha256"]
                or not verify_source_review(run, scope, result.get("source_review_proof"), call.name)
                or validate_scientific_revision_audit(scope, original, report, old_record=before, new_record=current,
                                                      additional_audits=previous_audits, previous_step=previous_step)):
            raise ValueError("The retained local review or its source deliveries could not be replayed")
        candidate_path = paths["current_candidate"]
        audit_path = run / f"audit.v{index:02d}.json"
        extra.append({"candidate_path": str(candidate_path), "candidate_sha256": hash_file(candidate_path),
                      "audit_path": str(audit_path), "audit_sha256": hash_file(audit_path)})
        previous_audits.append(report)
        previous_scopes.append(scope)
        previous_material = current
    return {"full_candidate_sha256": hash_file(base_path), "full_audit_sha256": hash_file(paths["baseline_audit"]),
            "reviewed_steps": extra, "scope_hashes": [s["scope_sha256"] for s in previous_scopes]}
