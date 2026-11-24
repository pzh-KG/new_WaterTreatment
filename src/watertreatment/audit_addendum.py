"""Small source-disposition addenda to an authenticated scientific review.

This module authenticates exact data relationships, not a model call or source
delivery. The caller must authenticate the completed parent, model, frozen
inputs and the addendum's current source-tool receipts before using its result.
An addendum appends findings; it never becomes a new full-paper audit.
"""
from __future__ import annotations

import copy
import json

from pydantic import Field

from .domain import ExtractionRecord, StrictModel
from .reading import AuditFinding, ReadingAudit
from .scientific_revision_audit import finding_paths, validate_scientific_revision_audit
from .citation_audit import _parts
from .storage import digest_json


POLICY = "bounded-audit-addendum-v1"
MAX_PATHS = 8
MAX_SOURCE_BLOCKS = 6
_SOURCE_DISPOSITION_PREFIX = "Affected entity/dependency lacks explicit source disposition: "


class AuditAddendum(StrictModel):
    """A source-grounded supplement, deliberately not a ReadingAudit."""
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_audit_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    findings: list[AuditFinding] = Field(min_length=1, max_length=16)


def _blockers(audit):
    return [f for f in audit.findings if f.needs_revision or f.verdict in {"unsupported", "not_checked"}]


def build_audit_addendum_scope(full_scope, baseline_audit, parent_audit, *, old_record, new_record,
                                additional_audits=(), max_paths=MAX_PATHS, max_source_blocks=MAX_SOURCE_BLOCKS):
    """Only exact affected-entity source dispositions qualify for this protocol.

    Other protocol failures, unfinished scientific repairs, omitted inherited
    limits and malformed paths require their own recovery, not this shortcut.
    Every required source of each selected entity is included, even if some
    sources were already cited in the parent finding.
    """
    try:
        if (type(max_paths) is not int or not 1 <= max_paths <= MAX_PATHS
                or type(max_source_blocks) is not int or not 1 <= max_source_blocks <= MAX_SOURCE_BLOCKS):
            return None
        parent = ReadingAudit.model_validate(parent_audit)
        record = ExtractionRecord.model_validate(new_record)
        if parent.potential_omissions or _blockers(parent):
            return None
        issues = validate_scientific_revision_audit(full_scope, baseline_audit, parent,
            old_record=old_record, new_record=record, additional_audits=additional_audits)
        if not issues or any(not issue.startswith(_SOURCE_DISPOSITION_PREFIX) for issue in issues):
            return None
        paths = [issue[len(_SOURCE_DISPOSITION_PREFIX):] for issue in issues]
        if not 1 <= len(paths) <= max_paths or len(set(paths)) != len(paths):
            return None
        refs = {r["record_path"]: r for r in full_scope["entity_refs"] if r["role"] == "affected"}
        anchors = {a["evidence_id"]: a for a in full_scope["source_anchors"]}
        selected, required_ids = [], set()
        for path in paths:
            ref = refs[path]
            ids = ref["required_evidence_ids"]
            if not ids or not set(ids).issubset(anchors):
                return None
            selected.append({"record_path": path, "entity_hash": ref["new_hash"],
                             "required_evidence_ids": list(ids)})
            required_ids.update(ids)
        selected_anchors = [copy.deepcopy(anchors[eid]) for eid in sorted(required_ids)]
        blocks = {(a["source_id"], a["block_id"]) for a in selected_anchors}
        if not 1 <= len(blocks) <= max_source_blocks:
            return None
        scope = {"policy": POLICY, "parent_scope_sha256": full_scope["scope_sha256"],
            "parent_audit_hash": digest_json(parent.model_dump(mode="json")),
            "candidate_hash": digest_json(record.model_dump(mode="json")),
            "targets": selected, "source_anchors": selected_anchors, "parent_protocol_issues": list(issues),
            "scope_limits": {"max_paths": max_paths, "max_source_blocks": max_source_blocks},
            "interpretation": "Append source dispositions for these exact entities only. The complete parent audit, its original scope, limits and unresolved findings remain authoritative and fallible. Not a new full audit or scientific validation."}
        scope["scope_sha256"] = digest_json(scope)
        return scope
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None


def merge_audit_addendum(scope, full_scope, baseline_audit, parent_audit, addendum, *, old_record, new_record,
                         additional_audits=()):
    """Append only. New blocking concerns remain in the merged audit.

    This method does not declare completion: call audit_addendum_issues and
    authenticate provider/source delivery separately before accepting a result.
    """
    parent = ReadingAudit.model_validate(parent_audit)
    supplement = AuditAddendum.model_validate(addendum)
    expected = build_audit_addendum_scope(full_scope, baseline_audit, parent,
        old_record=old_record, new_record=new_record, additional_audits=additional_audits,
        **scope["scope_limits"])
    if expected is None or expected != scope:
        raise ValueError("Addendum scope differs from the exact parent, candidate or original review obligations")
    for key in ("scope_sha256", "parent_audit_hash", "candidate_hash"):
        if getattr(supplement, key) != scope[key]:
            raise ValueError("Addendum is not bound to its exact " + key)
    data = ExtractionRecord.model_validate(new_record).model_dump(mode="json")
    allowed = {_parts(t["record_path"]) for t in scope["targets"]}
    for finding in supplement.findings:
        if finding.needs_revision or finding.verdict in {"unsupported", "not_checked"}:
            # Preserve a newly discovered problem, including a concern outside
            # the addendum's narrow successful-disposition scope. It blocks
            # completion; it is never silently thrown away or called resolved.
            continue
        if finding.omission_indices:
            raise ValueError("Addendum cannot index omissions that it does not define; report a blocking finding instead")
        paths = finding_paths(finding, data)
        if not paths or any(path not in allowed for path in paths):
            raise ValueError("Addendum nonblocking finding exceeds the exact permitted entity paths")
    merged = parent.model_copy(deep=True)
    merged.findings.extend(f.model_copy(deep=True) for f in supplement.findings)
    return merged


def audit_addendum_issues(scope, full_scope, baseline_audit, parent_audit, addendum, *, old_record, new_record,
                          additional_audits=()):
    """Recheck the complete original scientific contract after append-only merge."""
    try:
        merged = merge_audit_addendum(scope, full_scope, baseline_audit, parent_audit, addendum,
            old_record=old_record, new_record=new_record, additional_audits=additional_audits)
        issues = validate_scientific_revision_audit(full_scope, baseline_audit, merged,
            old_record=old_record, new_record=new_record, additional_audits=additional_audits)
        supplement = AuditAddendum.model_validate(addendum)
        data = ExtractionRecord.model_validate(new_record).model_dump(mode="json")
        anchors = {a["evidence_id"]: (a["source_id"], a["block_id"]) for a in scope["source_anchors"]}
        for target in scope["targets"]:
            path = _parts(target["record_path"])
            delivered = set()
            for finding in supplement.findings:
                try:
                    if path in finding_paths(finding, data):
                        delivered.update((e.source_id, e.block_id) for e in finding.evidence)
                except ValueError:
                    pass  # The original validator and blocker check retain the failure.
            if not {anchors[eid] for eid in target["required_evidence_ids"]}.issubset(delivered):
                issues.append("Addendum lacks a complete current source disposition: " + target["record_path"])
        if merged.potential_omissions:
            issues.append("Merged audit retains scientific omissions")
        for finding in _blockers(merged):
            issues.append("Merged audit retains a scientific blocker: " + finding.record_path)
        return issues
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return ["Malformed or unbound audit addendum; completion is not permitted"]


def audit_addendum_prompt(scope, parent_audit):
    """Keep a local source check small without concealing retained uncertainty."""
    parent = ReadingAudit.model_validate(parent_audit)
    allowed = {_parts(t["record_path"]) for t in scope["targets"]}
    # Findings can use grouped paths. Include the unchanged parent text, never
    # paraphrase its conclusion into a stronger form before source verification.
    relevant = []
    for finding in parent.findings:
        raw_paths = finding.record_paths or [finding.record_path]
        if any(_parts(p) in allowed for p in raw_paths):
            relevant.append(finding.model_dump(mode="json"))
    return ("BOUNDED SOURCE-DISPOSITION ADDENDUM. This is not a new full-paper audit or extraction. "
        "Read the exact permitted entities and every required original source block; examine the complete relevant "
        "figure/table page or image before enlarging details. Previously cited sources remain required alongside the "
        "missing ones. Check whether all of them support the entity's interpretation and limitations; do not add a "
        "citation merely to satisfy a list. Do not retrieve unrelated sections or rewrite the candidate. "
        "Return only AuditAddendum with the exact scope_sha256, parent_audit_hash and candidate_hash below. "
        "Use findings with record_paths listing exact allowed entity paths, source anchors and a source-grounded "
        "explanation. Nonblocking findings may concern only these paths. If the sources do not justify the claim, "
        "or reveal a broader problem, preserve it as unsupported/needs_revision with its exact path; completion will "
        "stop. Never manufacture agreement or a resolved missing value. Existing parent findings, limitations, "
        "nine dimensions and scope binding will be retained unchanged by append-only merging.\nADDENDUM SCOPE:\n"
        + json.dumps(scope, ensure_ascii=False, separators=(",", ":"))
        + "\nRELEVANT PARENT FINDINGS (fallible, retained unchanged):\n"
        + json.dumps(relevant, ensure_ascii=False, separators=(",", ":")))
