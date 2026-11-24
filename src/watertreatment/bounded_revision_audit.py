"""Bounded source review of metadata repairs after an authenticated full audit.

Pure scope construction is not provenance verification or scientific approval.
The caller must authenticate the full baseline call, its candidate and sources,
then rebuild this scope when admitting the completed incremental review.
"""
from __future__ import annotations

from collections import deque
import re

from pydantic import BaseModel

from .citation_audit import _audit_complete, _evidence_ids, _parts, _path, _prefix, _resolve
from .domain import ExtractionRecord, Fact, Observation, QuantitativeCoverage
from .reading import AUDIT_DIMENSIONS, ReadingAudit
from .storage import digest_json
from .word_review_links import LINKED_POLICY


POLICY = "bounded-revision-audit-v1"
SECTIONS = ("materials", "substances", "configurations", "experiments", "observations",
            "comparisons", "extensions", "quantitative_coverage")


def finding_paths(path):
    """Accept explicit comma-separated paths, never prose or wildcard selectors."""
    if not isinstance(path, str) or not path or len(path) > 12000:
        return None
    chunks = path.split(",")
    if not 1 <= len(chunks) <= 64:
        return None
    parsed = [_parts(chunk.strip()) for chunk in chunks]
    return parsed if all(parsed) and len(set(parsed)) == len(parsed) else None


def _same(left, right):
    return digest_json(left) == digest_json(right)


def _append_only(old, new):
    return (isinstance(old, list) and isinstance(new, list) and len(set(new)) == len(new)
            and new[:len(old)] == old)


def _change(path, kind, old, new):
    return {"changed_path": _path(path), "kind": kind, "old_value": old, "new_value": new,
            "old_value_hash": digest_json(old), "new_value_hash": digest_json(new)}


def _changes(old, new, path=()):
    if type(old) is not type(new):
        raise ValueError("typed membership changed")
    if isinstance(old, Fact):
        left, right = old.model_dump(mode="json"), new.model_dump(mode="json")
        if _same(left, right):
            return []
        a, b = dict(left), dict(right)
        old_ids, new_ids = a.pop("evidence_ids"), b.pop("evidence_ids")
        if _same(a, b):
            if (len(set(old_ids)) != len(old_ids) or len(set(new_ids)) != len(new_ids)
                    or not new_ids or set(old_ids) == set(new_ids)):
                raise ValueError("invalid/reordered evidence repair")
            return [_change((*path, "evidence_ids"), "fact_citations", old_ids, new_ids)]
        coordinate = (len(path) == 5 and path[0] == "observations" and type(path[1]) is int
                      and path[2] == "coordinates" and type(path[3]) is int and path[4] == "fact")
        # Coordinate values/normalizations cannot be silently corrected by this
        # protocol. Only evidence-backed qualification toward ambiguity/conflict
        # is eligible; a value/condition correction requires a full audit.
        protected = ("value", "normalized", "normalization_reason")
        if (not coordinate or any(not _same(left[k], right[k]) for k in protected)
                or right["status"] not in {"conflict", "ambiguous"}
                or (left["status"] == "conflict" and right["status"] != "conflict")
                or not right["evidence_ids"]):
            raise ValueError("substantive scientific fact changed")
        previous = {digest_json(item) for item in left["alternatives"]}
        if not previous.issubset({digest_json(item) for item in right["alternatives"]}):
            raise ValueError("source alternatives removed")
        return [_change(path, "coordinate_qualification", left, right)]
    if isinstance(old, BaseModel):
        changes = []
        support_model = isinstance(old, (Observation, QuantitativeCoverage))
        left_fields = old.model_dump(mode="json") if support_model else {}
        right_fields = new.model_dump(mode="json") if support_model else {}
        for field in type(old).model_fields:
            a, b = getattr(old, field), getattr(new, field)
            left, right = left_fields.get(field), right_fields.get(field)
            if isinstance(old, Observation) and field in {"evidence_links", "evidence_ids", "role_reason", "limitations"}:
                if _same(left, right):
                    continue
                if field == "limitations" and not _append_only(a, b):
                    raise ValueError("observation limitations removed")
                changes.append(_change((*path, field), "observation_support", left, right))
            elif isinstance(old, QuantitativeCoverage) and field == "limitations":
                if _same(a, b):
                    continue
                if not _append_only(a, b):
                    raise ValueError("coverage limitation removed")
                changes.append(_change((*path, field), "coverage_restriction", a, b))
            else:
                changes.extend(_changes(a, b, (*path, field)))
        return changes
    if isinstance(old, list):
        if len(old) != len(new):
            raise ValueError("entity/coordinate membership changed")
        return [c for i, (a, b) in enumerate(zip(old, new, strict=True))
                for c in _changes(a, b, (*path, i))]
    if not _same(old, new):
        raise ValueError("non-repair field changed")
    return []


def _limits(max_entities, max_changes):
    return (type(max_entities) is int and 1 <= max_entities <= 160
            and type(max_changes) is int and 1 <= max_changes <= 64)


def build_revision_audit_scope(old_record, new_record, baseline_audit, *, baseline_origin,
                               max_entities=160, max_changes=48, source_requirement_policy=None):
    """A single bounded step from a complete full audit, otherwise fail closed.

    No principal value/unit, ownership, evidence definition, source coverage,
    series membership or material/experiment science may change. All blockers
    must be located and reconsidered. Unchanged dependencies provide context;
    they do not recursively seed every use of a common final material.
    """
    if (baseline_origin != "fresh_model_audit" or not _limits(max_entities, max_changes)
            or source_requirement_policy not in (None, LINKED_POLICY)):
        return None
    try:
        old = ExtractionRecord.model_validate(old_record)
        new = ExtractionRecord.model_validate(new_record)
        baseline = ReadingAudit.model_validate(baseline_audit)
        if not _audit_complete(baseline):
            return None
        changes = _changes(old, new)
        if not 1 <= len(changes) <= max_changes:
            return None
        before, after = old.model_dump(mode="json"), new.model_dump(mode="json")
        entities, by_id = {}, {}
        for section in SECTIONS:
            for index, entity in enumerate(after.get(section, [])):
                key = (section, index)
                entities[key] = entity
                if entity.get("id") is not None:
                    identity = section, entity["id"]
                    if identity in by_id:
                        return None
                    by_id[identity] = key
        seeds = {_parts(c["changed_path"])[:2] for c in changes}
        if any(key not in entities for key in seeds):
            return None
        evidence = {e.id: e for e in new.evidence}
        if len(evidence) != len(new.evidence):
            return None

        # Links used for derived interpretation are affected. Experiment and
        # material identities are context, not routes to unrelated outcomes.
        edges = {key: set() for key in entities}
        def connect(a, b):
            if a not in entities or b not in entities:
                raise ValueError("unknown dependency")
            edges[a].add(b)
            edges[b].add(a)
        for key, entity in entities.items():
            if key[0] in {"quantitative_coverage", "comparisons"}:
                for oid in entity.get("observation_ids", []):
                    connect(key, by_id["observations", oid])
            elif key[0] == "observations" and entity.get("series_id"):
                connect(key, by_id["quantitative_coverage", entity["series_id"]])
            elif key[0] == "experiments":
                for pair in entity.get("pairs", []):
                    connect(by_id["observations", pair["left_observation_id"]],
                            by_id["observations", pair["right_observation_id"]])
            elif key[0] == "extensions":
                if entity.get("observation_id"):
                    connect(key, by_id["observations", entity["observation_id"]])
        temporal_groups = {after["observations"][_parts(c["changed_path"])[1]]["experiment_id"]
                           for c in changes if c["kind"] == "coordinate_qualification"}
        for key, entity in entities.items():
            if key[0] == "observations" and entity["experiment_id"] in temporal_groups:
                seeds.add(key)
        affected, queue = set(), deque(sorted(seeds))
        while queue:
            key = queue.popleft()
            if key in affected:
                continue
            affected.add(key)
            if len(affected) > max_entities:
                return None
            queue.extend(sorted(edges[key] - affected))
        context = set()
        for key in affected:
            entity = entities[key]
            for section, identifiers in (
                ("experiments", [entity.get("experiment_id")]),
                ("materials", [entity.get("material_id"), *entity.get("material_ids", [])]),
                ("substances", [entity.get("substance_id")])):
                context.update(by_id[section, identifier] for identifier in identifiers if identifier is not None)
        for key in list(context):
            entity = entities[key]
            if key[0] == "experiments":
                context.update(by_id["materials", mid] for mid in entity["material_ids"])
                if entity.get("configuration_id"):
                    context.add(by_id["configurations", entity["configuration_id"]])
                context.update(by_id["substances", x["substance_id"]]
                               for x in [*entity.get("targets", []), *entity.get("reaction_inputs", [])]
                               if x.get("substance_id"))
        keys = affected | context
        if len(keys) > max_entities:
            return None

        blockers = []
        for finding in baseline.findings:
            if not (finding.needs_revision or finding.verdict == "unsupported"):
                continue
            parts = finding_paths(finding.record_path)
            if not parts or any(len(p) < 2 or p[:2] not in affected for p in parts):
                return None
            for path in parts:
                _resolve(before, path)
            blockers.append(finding)
        if not blockers:
            return None  # do not scope arbitrary unsolicited scientific edits

        anchors = {}
        for change in changes:
            path = _parts(change["changed_path"])
            owner = entities[path[:2]]
            ids = _evidence_ids(change["old_value"]) | _evidence_ids(change["new_value"])
            if path[-1] == "evidence_ids":
                ids.update(change["old_value"] or [])
                ids.update(change["new_value"] or [])
            if not ids:
                ids.update(owner.get("evidence_ids", []))
            if not ids and path[0] == "quantitative_coverage":
                ids.update(eid for eid, e in evidence.items()
                           if (e.source_id, e.block_id) == (owner["source_id"], owner["block_id"]))
            if not ids or not ids.issubset(evidence):
                return None
            change["required_evidence_ids"] = sorted(ids)
            for eid in ids:
                e = evidence[eid]
                anchors[eid] = {"evidence_id": eid, "source_id": e.source_id, "block_id": e.block_id,
                                "locator": e.locator.model_dump(mode="json")}
        # Only explicitly located blocker sources and changed claim sources
        # are mandatory. Unchanged contextual citations are inherited, not
        # presented as new evidence checks.
        available_blocks = {(a["source_id"], a["block_id"]) for a in anchors.values()}
        blocker_blocks = {(a.source_id, a.block_id) for f in blockers for a in f.evidence}
        for pair in blocker_blocks - available_blocks:
            matching = [e for e in evidence.values() if (e.source_id, e.block_id) == pair]
            if not matching:
                return None
            e = sorted(matching, key=lambda e: e.id)[0]
            anchors[e.id] = {"evidence_id": e.id, "source_id": e.source_id, "block_id": e.block_id,
                             "locator": e.locator.model_dump(mode="json")}
        refs = [{"section": key[0], "index": key[1], "id": entities[key].get("id"),
                 "record_path": _path(key), "role": "affected" if key in affected else "context",
                 "old_hash": digest_json(_resolve(before, key)), "new_hash": digest_json(entities[key])}
                for key in sorted(keys)]
        manifest = {"policy": POLICY, "incremental_depth": 1, "baseline_origin": baseline_origin,
            "old_record_hash": digest_json(before), "new_record_hash": digest_json(after),
            "baseline_audit_hash": digest_json(baseline.model_dump(mode="json")),
            "changes": changes, "changed_paths": [c["changed_path"] for c in changes],
            "entity_refs": refs, "source_anchors": [anchors[k] for k in sorted(anchors)],
            "baseline_blocking_paths": [f.record_path for f in blockers],
            "scope_limits": {"max_entities": max_entities, "max_changes": max_changes},
            "interpretation": "Bounded source recheck plus authenticated unchanged full-audit coverage; not a fresh full audit or scientific validation."}
        if source_requirement_policy:
            manifest["source_requirement_policy"] = source_requirement_policy
        manifest["scope_sha256"] = digest_json(manifest)
        return manifest
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None


def validate_revision_audit(scope, baseline_audit, new_audit):
    """Validate explicit delta coverage. Normal scientific rejection gates remain."""
    try:
        baseline = ReadingAudit.model_validate(baseline_audit)
        current = ReadingAudit.model_validate(new_audit)
        limits = scope["scope_limits"]
        hashes = [scope.get(k) for k in ("old_record_hash", "new_record_hash", "baseline_audit_hash", "scope_sha256")]
        if (scope.get("policy") != POLICY or scope.get("baseline_origin") != "fresh_model_audit"
                or type(scope.get("incremental_depth")) is not int or scope["incremental_depth"] != 1
                or not _limits(limits["max_entities"], limits["max_changes"])
                or not 1 <= len(scope["changes"]) <= limits["max_changes"]
                or not 1 <= len(scope["entity_refs"]) <= limits["max_entities"]
                or scope["changed_paths"] != [c["changed_path"] for c in scope["changes"]]
                or len(set(scope["changed_paths"])) != len(scope["changed_paths"])
                or scope["scope_sha256"] != digest_json({k: v for k, v in scope.items() if k != "scope_sha256"})
                or scope["baseline_audit_hash"] != digest_json(baseline.model_dump(mode="json"))
                or scope["old_record_hash"] == scope["new_record_hash"] or not _audit_complete(baseline)):
            return ["Bounded revision scope or authenticated full baseline is inconsistent."]
        if any(not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes):
            return ["Bounded revision record/audit hashes are malformed."]
        entities = {_parts(ref["record_path"]) for ref in scope["entity_refs"]}
        if (None in entities or any(len(p) != 2 or p[0] not in SECTIONS or type(p[1]) is not int for p in entities)
                or len(entities) != len(scope["entity_refs"])):
            return ["Bounded revision scope contains invalid/duplicate entities."]
        for ref in scope["entity_refs"]:
            if (ref["role"] not in {"affected", "context"} or type(ref["index"]) is not int
                    or _parts(ref["record_path"]) != (ref["section"], ref["index"])
                    or any(not isinstance(ref[k], str) or not re.fullmatch(r"[0-9a-f]{64}", ref[k])
                           for k in ("old_hash", "new_hash"))):
                return ["Bounded revision entity binding is malformed."]
        anchors = {a["evidence_id"]: (a["source_id"], a["block_id"]) for a in scope["source_anchors"]}
        if len(anchors) != len(scope["source_anchors"]) or not anchors:
            return ["Bounded revision source anchors are missing or duplicated."]
        if any(not isinstance(eid, str) or not eid or any(not isinstance(p, str) or not p for p in pair)
               for eid, pair in anchors.items()):
            return ["Bounded revision source anchors are malformed."]
        issues = []
        if (set(current.checked_dimensions) != set(AUDIT_DIMENSIONS)
                or len(current.checked_dimensions) != len(AUDIT_DIMENSIONS)):
            issues.append("Bounded revision must account for all nine dimensions with inherited coverage identified.")
        if current.review_scope != "citation_delta" or current.review_scope_sha256 != scope["scope_sha256"]:
            issues.append("Bounded revision report does not bind its frozen scope.")
        if any(f.verdict == "not_checked" for f in current.findings):
            issues.append("Bounded revision contains an unchecked claim.")
        findings = [(finding_paths(f.record_path), f) for f in current.findings]
        for change in scope["changes"]:
            path = _parts(change["changed_path"])
            if (not path or path[:2] not in entities
                    or change["kind"] not in {"fact_citations", "coordinate_qualification", "observation_support", "coverage_restriction"}
                    or change["old_value_hash"] != digest_json(change["old_value"])
                    or change["new_value_hash"] != digest_json(change["new_value"])
                    or change["old_value_hash"] == change["new_value_hash"]
                    or not change["required_evidence_ids"]):
                return ["Bounded revision contains a malformed or out-of-scope change."]
            matched = [f for paths, f in findings if paths and any(
                len(p) >= 2 and p[:2] == path[:2] and _prefix(p, path) for p in paths)]
            required = {anchors[eid] for eid in change["required_evidence_ids"]}
            covered = {(a.source_id, a.block_id) for f in matched for a in f.evidence}
            if not matched or not required.issubset(covered):
                issues.append("Changed field lacks explicit before/after source review: " + change["changed_path"])
        fingerprints = {digest_json(f.model_dump(mode="json")) for f in current.findings}
        blockers = [f for f in baseline.findings if f.needs_revision or f.verdict == "unsupported"]
        if scope["baseline_blocking_paths"] != [f.record_path for f in blockers]:
            issues.append("Baseline blocking findings were omitted from the scope.")
        for old_finding in baseline.findings:
            blocking = old_finding.needs_revision or old_finding.verdict == "unsupported"
            paths = finding_paths(old_finding.record_path)
            if blocking:
                if not paths or any(len(p) < 2 or p[:2] not in entities for p in paths):
                    issues.append("Baseline blocker is unlocated or outside the bounded scope.")
                    continue
                for path in paths:
                    matched = [f for ps, f in findings if ps and any(
                        len(p) >= 2 and p[:2] == path[:2] and (_prefix(p, path) or _prefix(path, p)) for p in ps)]
                    required = {(a.source_id, a.block_id) for a in old_finding.evidence}
                    actual = {(a.source_id, a.block_id) for f in matched for a in f.evidence}
                    if not matched or not required.issubset(actual):
                        issues.append("Baseline blocker was not explicitly reconsidered: " + _path(path))
            elif old_finding.verdict == "unresolved" and digest_json(old_finding.model_dump(mode="json")) not in fingerprints:
                issues.append("Baseline unresolved finding must remain explicit: " + old_finding.record_path)
        if not set(baseline.coverage_limitations).issubset(current.coverage_limitations):
            issues.append("Baseline coverage limitations were removed.")
        old_followups = {digest_json(f.model_dump(mode="json")) for f in baseline.quantitative_followups}
        if not old_followups.issubset({digest_json(f.model_dump(mode="json")) for f in current.quantitative_followups}):
            issues.append("Baseline quantitative followups were removed.")
        return issues
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return ["Malformed bounded revision audit; a full source audit is required."]
