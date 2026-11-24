"""Recheck exact interpretations repaired after a completed structural source audit."""
from __future__ import annotations

from .citation_audit import _parts, _path, _evidence_ids
from .domain import ExtractionRecord
from .ownership_audit import _changes, _links, MODE
from .reading import ReadingAudit
from .scientific_revision_audit import (_entities, _owner, finding_paths, validate_scientific_revision_audit,
    ScientificScopeRejected)
from .storage import digest_json
from .validation import validate_record

POLICY = "located-structural-followup-v1"


def _only_fields(old, new, allowed):
    return {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)} <= allowed


def _local_change(change):
    section, _, field, *rest = _parts(change["changed_path"])
    a, b = change["old_value"], change["new_value"]
    if section == "observations" and field in {"numerator", "denominator", "reference"}:
        return not rest and _only_fields(a, b, {"note"})
    if section == "observations" and field == "evidence_links":
        return (not rest and len(a) == len(b) and all(
            _only_fields(x, y, {"support_type", "explanation"}) for x, y in zip(a, b)))
    if section == "experiments" and field == "pairs":
        return (not rest and len(a) == len(b) and all(
            _only_fields(x, y, {"unresolved_conditions", "differing_conditions", "note"}) for x, y in zip(a, b)))
    if section == "comparisons" and field == "outcomes" and len(rest) == 2 and rest[1] == "fact":
        return (isinstance(a.get("value"), str) and isinstance(b.get("value"), str)
                and _only_fields(a, b, {"value", "raw", "note"}))
    return False


def build_followup_scope(root, full_audit, before, after, previous, prior_scope):
    """The caller authenticates model turns; identical citations alone convey no approval.

    Carry forward only an already dispositioned structural scope. New numeric facts,
    changes to typed links, membership or coverage go back through the regular compiler.
    """
    if prior_scope.get("structural_review") != MODE or prior_scope.get("additional_audit_hashes"):
        return None
    before, after = map(ExtractionRecord.model_validate, (before, after))
    previous = ReadingAudit.model_validate(previous)
    if validate_scientific_revision_audit(prior_scope, full_audit, previous, old_record=root, new_record=before):
        return None
    left, right = (r.model_dump(mode="json") for r in (before, after))
    try:
        changes = _changes(left, right)
        if (not 1 <= len(changes) <= 24 or any(c["kind"] != "scientific_field" or not _local_change(c) for c in changes)
                or any(i.severity == "error" for i in validate_record(after))):
            return None
        rows = _entities(right, include_evidence=True)
        changed = {_owner(_parts(c["changed_path"]), rows) for c in changes}
        blocked, obligations, omissions = set(), [], set()
        locations = {}
        for i, finding in enumerate(previous.findings):
            if not (finding.needs_revision or finding.verdict == "unsupported"):
                continue
            paths = finding_paths(finding, left)
            if not paths or not finding.evidence:
                return None
            pairs = {(a.source_id, a.block_id) for a in finding.evidence}
            names = []
            for p in paths:
                key = _owner(p, rows)
                blocked.add(key)
                locations.setdefault(key,set()).update(pairs)
                names.append(_path(p))
            omissions.update(finding.omission_indices)
            obligations.append({"audit_index":1, "finding_index":i, "paths":names,
                "historical_paths":list(names), "source_blocks":[list(pair) for pair in sorted(pairs)]})
        if not blocked or omissions != set(range(len(previous.potential_omissions))):
            return None
        # The sole extra owner allowed by this route is the explicitly linked
        # pairing whose applicability qualifier changes along with its endpoint.
        links = _links(rows)
        for key in changed - blocked:
            if key[0] != "experiments":
                return None
            old = left["experiments"][key[1]]["pairs"]
            for a, b in zip(old, rows[key]["pairs"]):
                if a != b and not any(k[0] == "observations" and rows[k]["id"] in
                        (a["left_observation_id"], a["right_observation_id"]) for k in blocked):
                    return None
        affected = changed | blocked
        if len(affected) > 12:
            return None
        # Explicit comparison/experiment consumers remain obligations even if
        # their own wording was left untouched. Series membership is invariant.
        observations = {k for k in affected if k[0] == "observations"}
        affected.update(k for k,v in links.items() if k[0] in {"comparisons","reference_associations","experiments"} and v & observations)
        if len(affected) > 12:
            return None
        all_anchors = {a["evidence_id"]:a for a in prior_scope["source_anchors"]}
        all_anchors.update({e.id:{"evidence_id":e.id,"source_id":e.source_id,"block_id":e.block_id,
            "locator":e.locator.model_dump(mode="json")} for e in after.evidence if e.id not in all_anchors})
        required = {k:set() for k in affected}
        def at_block(pair):
            matches=[a for a in prior_scope["source_anchors"] if (a["source_id"],a["block_id"])==pair]
            if not matches:
                raise ValueError("previous blocker outside the original structural audit sources")
            return {a["evidence_id"] for a in matches}
        for k,pairs in locations.items():
            for pair in pairs:
                required[k].update(at_block(pair))
        for c in changes:
            key=_owner(_parts(c["changed_path"]), rows)
            ids=_evidence_ids(c["old_value"]) | _evidence_ids(c["new_value"])
            c["required_evidence_ids"]=sorted(ids or required[key] or set(rows[key].get("evidence_ids",())))
            required[key].update(c["required_evidence_ids"])
        for k in affected:
            if not required[k]:
                required[k].update(rows[k].get("evidence_ids",()))
            if not required[k]:
                return None
        ids=set().union(*required.values())
        covered={(row.source_id,bid) for row in before.coverage for bid in row.block_ids}
        if any((all_anchors[e]["source_id"],all_anchors[e]["block_id"]) not in covered for e in ids):
            return None
        context=set().union(*(links[key] for key in affected)) - affected
        old_rows = _entities(left, include_evidence=True)
        refs=[{"record_path":_path(k),"role":"affected" if k in affected else "context","entity_id":rows[k].get("id"),
            "old_hash":digest_json(old_rows[k]),"new_hash":digest_json(rows[k]),
            "required_evidence_ids":sorted(required.get(k,()))} for k in sorted(affected|context,key=_path)]
        scope={"policy":prior_scope["policy"],"structural_followup":POLICY,"scope_limits":prior_scope["scope_limits"],
            "old_record_hash":prior_scope["old_record_hash"],"new_record_hash":digest_json(right),
            "baseline_audit_hash":prior_scope["baseline_audit_hash"],"additional_audit_hashes":[digest_json(previous.model_dump(mode="json"))],
            "previous_scope_sha256":prior_scope["scope_sha256"],"previous_record_hash":digest_json(left),
            "source_requirement_policy":prior_scope.get("source_requirement_policy"),"changes":changes,
            "changed_paths":[c["changed_path"] for c in changes],"entity_refs":refs,"blocking_obligations":obligations,
            "source_anchors":[all_anchors[e] for e in sorted(ids)]}
        scope["scope_sha256"]=digest_json(scope)
        return scope
    except (KeyError, IndexError, TypeError, ValueError, ScientificScopeRejected):
        return None
