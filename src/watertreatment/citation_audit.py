"""Conservative scopes for one citation-only review after a full model audit.

Pure functions: no source access, model call, registration or approval. The
caller must authenticate the baseline audit/origin and freeze both candidates,
audit and this manifest in the new call. Rebuild the manifest when verifying
provenance; a self-consistent hash alone is not proof of trustworthy input.
"""
from __future__ import annotations

from collections import deque
import re

from pydantic import BaseModel

from .domain import ExtractionRecord, Fact
from .reading import AUDIT_DIMENSIONS, ReadingAudit
from .storage import digest_json


POLICY = "citation-only-audit-v1"
_SECTIONS = ("materials", "substances", "configurations", "experiments", "observations",
             "comparisons", "extensions", "quantitative_coverage")
_OWNERS = set(_SECTIONS) - {"quantitative_coverage"}
_PATH = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|\[(0|[1-9][0-9]*)\]")


def _path(parts):
    return "".join(f"[{part}]" if type(part) is int else ("." if i else "") + part
                   for i, part in enumerate(parts))


def _parts(path):
    if not isinstance(path, str) or not path or len(path) > 2000:
        return None
    parts, position = [], 0
    for match in _PATH.finditer(path):
        separator = path[position:match.start()]
        if separator != ("." if parts and not match[0].startswith("[") else ""):
            return None
        token = match[0]
        if token.startswith("["):
            if not parts or len(token) > 12:
                return None
            parts.append(int(token[1:-1]))
        else:
            parts.append(token)
        position = match.end()
    return tuple(parts) if position == len(path) else None


def _prefix(left, right):
    return len(left) <= len(right) and right[:len(left)] == left


def _resolve(data, parts):
    value = data
    for part in parts:
        if isinstance(value, dict) and type(part) is str and part in value:
            value = value[part]
        elif isinstance(value, list) and type(part) is int and 0 <= part < len(value):
            value = value[part]
        else:
            raise ValueError("unknown record path")
    return value


def _changes(old, new, path=()):
    """Return actual typed Fact citation changes; any other change fails closed."""
    if type(old) is not type(new):
        raise ValueError("changed type")
    if isinstance(old, Fact):
        left, right = old.model_dump(mode="json"), new.model_dump(mode="json")
        previous, current = left.pop("evidence_ids"), right.pop("evidence_ids")
        if digest_json(left) != digest_json(right):
            raise ValueError("scientific fact changed")
        if previous == current:
            return []
        if (len(set(previous)) != len(previous) or len(set(current)) != len(current)
                or set(previous) == set(current)):
            raise ValueError("duplicate/reordered citations are outside this protocol")
        return [{"fact_path": _path(path), "changed_path": _path((*path, "evidence_ids")),
                 "old_evidence_ids": previous, "new_evidence_ids": current,
                 "changed_evidence_ids": sorted(set(previous) ^ set(current)),
                 "old_fact_hash": digest_json(old.model_dump(mode="json")),
                 "new_fact_hash": digest_json(new.model_dump(mode="json"))}]
    if isinstance(old, BaseModel):
        result = []
        for field in type(old).model_fields:
            result.extend(_changes(getattr(old, field), getattr(new, field), (*path, field)))
        return result
    if isinstance(old, list):
        if len(old) != len(new):
            raise ValueError("array membership changed")
        return [change for i, (a, b) in enumerate(zip(old, new, strict=True))
                for change in _changes(a, b, (*path, i))]
    if digest_json(old) != digest_json(new):
        raise ValueError("noncitation field changed")
    return []


def _evidence_ids(value):
    found = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidence_ids" and isinstance(child, list):
                found.update(child)
            elif key == "evidence_id" and isinstance(child, str):
                found.add(child)
            else:
                found.update(_evidence_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_evidence_ids(child))
    return found


def _audit_complete(audit):
    return (audit.review_scope == "full" and audit.review_scope_sha256 is None
            and len(audit.checked_dimensions) == len(AUDIT_DIMENSIONS)
            and set(audit.checked_dimensions) == set(AUDIT_DIMENSIONS)
            and not audit.potential_omissions
            and not any(f.verdict == "not_checked" for f in audit.findings))


def _manifest_shape(scope):
    """Validate the protocol envelope in addition to its accidental-change hash."""
    hexadecimal = re.compile(r"[0-9a-f]{64}")
    for name in ("old_record_hash", "new_record_hash", "baseline_audit_hash", "scope_sha256"):
        if not isinstance(scope.get(name), str) or not hexadecimal.fullmatch(scope[name]):
            return False
    if scope["old_record_hash"] == scope["new_record_hash"]:
        return False
    limits = scope["scope_limits"]
    if (type(limits["max_entities"]) is not int or not 1 <= limits["max_entities"] <= 500
            or type(limits["max_changes"]) is not int or not 1 <= limits["max_changes"] <= 100):
        return False
    if not 1 <= len(scope["changes"]) <= limits["max_changes"]:
        return False
    if not 1 <= len(scope["entity_refs"]) <= limits["max_entities"]:
        return False
    if scope["changed_paths"] != [change["changed_path"] for change in scope["changes"]]:
        return False
    if len(set(scope["changed_paths"])) != len(scope["changed_paths"]):
        return False
    for change in scope["changes"]:
        parts = _parts(change["fact_path"])
        if not parts or _parts(change["changed_path"]) != (*parts, "evidence_ids"):
            return False
        old, new = change["old_evidence_ids"], change["new_evidence_ids"]
        if (not isinstance(old, list) or not isinstance(new, list)
                or any(not isinstance(item, str) or not item for item in [*old, *new])
                or len(set(old)) != len(old) or len(set(new)) != len(new)):
            return False
        if not change["changed_evidence_ids"] or change["changed_evidence_ids"] != sorted(set(old) ^ set(new)):
            return False
        for name in ("old_fact_hash", "new_fact_hash"):
            if not isinstance(change[name], str) or not hexadecimal.fullmatch(change[name]):
                return False
    paths = set()
    for entity in scope["entity_refs"]:
        if (entity["section"] not in _SECTIONS or type(entity["index"]) is not int
                or entity["index"] < 0 or entity["role"] not in {"affected", "context"}
                or _parts(entity["record_path"]) != (entity["section"], entity["index"])
                or entity["record_path"] in paths):
            return False
        paths.add(entity["record_path"])
        for name in ("old_hash", "new_hash"):
            if not isinstance(entity[name], str) or not hexadecimal.fullmatch(entity[name]):
                return False
    ids = set()
    for anchor in scope["source_anchors"]:
        if (any(not isinstance(anchor.get(key), str) or not anchor[key]
                for key in ("evidence_id", "source_id", "block_id"))
                or anchor["evidence_id"] in ids or not isinstance(anchor.get("locator"), dict)):
            return False
        ids.add(anchor["evidence_id"])
    return bool(ids)


def build_citation_audit_scope(old_record, new_record, baseline_audit, *, baseline_origin,
                               max_entities=40, max_changes=12):
    """Return a bounded citation/dependency manifest, or None for a full audit.

    Only one step from authenticated ``fresh_model_audit`` is eligible. Changes
    to values, conditions, identities, evidence definitions, membership or
    coverage are never covered. Shared context is included without incorrectly
    propagating a local observation citation through every use of its material.
    """
    if (baseline_origin != "fresh_model_audit" or type(max_entities) is not int
            or not 1 <= max_entities <= 500 or type(max_changes) is not int
            or not 1 <= max_changes <= 100):
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
        for section in _SECTIONS:
            for index, entity in enumerate(after.get(section, [])):
                key = (section, index)
                entities[key] = entity
                identifier = entity.get("id")
                if identifier is not None:
                    if (section, identifier) in by_id:
                        return None
                    by_id[section, identifier] = key
        evidence = {item.id: item for item in new.evidence}
        if len(evidence) != len(new.evidence):
            return None
        changed_ids = {eid for change in changes for eid in change["changed_evidence_ids"]}
        if not changed_ids.issubset(evidence):
            return None
        seeds = set()
        for change in changes:
            parts = _parts(change["fact_path"])
            if (not parts or len(parts) < 3 or parts[0] not in _OWNERS
                    or type(parts[1]) is not int or parts[:2] not in entities):
                return None
            seeds.add(parts[:2])
        # A modified citation can affect every claim that cites it, not just
        # the one leaf that was edited. Scan full entities, including links.
        for key, entity in entities.items():
            if _evidence_ids(entity) & changed_ids:
                seeds.add(key)
        if _evidence_ids(after["paper"]) & changed_ids:
            return None  # paper-wide semantics are deliberately not localized

        edges = {key: set() for key in entities}

        def add(owner, section, identifier):
            if identifier is None:
                return
            target = by_id.get((section, identifier))
            if target is None:
                raise ValueError("unknown dependency")
            edges[owner].add(target)

        for key, entity in entities.items():
            section = key[0]
            if section == "materials":
                for component in entity.get("components", []):
                    substance = by_id.get(("substances", component["substance_id"]))
                    if substance is None:
                        raise ValueError("unknown component")
                    edges[substance].add(key)
            elif section == "configurations":
                for mid in entity.get("material_ids", []):
                    add(by_id["materials", mid], "configurations", entity["id"])
                for sid in entity.get("accessory_substance_ids", []):
                    add(by_id["substances", sid], "configurations", entity["id"])
            elif section == "experiments":
                for mid in set(entity.get("material_ids", [])) | {
                        item["material_id"] for item in entity.get("material_states", [])}:
                    add(by_id["materials", mid], "experiments", entity["id"])
                if entity.get("configuration_id") is not None:
                    add(by_id["configurations", entity["configuration_id"]], "experiments", entity["id"])
                for item in [*entity.get("targets", []), *entity.get("reaction_inputs", [])]:
                    sid = item.get("substance_id")
                    if sid is not None:
                        add(by_id["substances", sid], "experiments", entity["id"])
                for pair in entity.get("pairs", []):
                    for field in ("left_observation_id", "right_observation_id"):
                        add(key, "observations", pair[field])
                        edges[by_id["observations", pair[field]]].add(key)
            elif section == "observations":
                add(key, "experiments", entity["experiment_id"])
                edges[by_id["experiments", entity["experiment_id"]]].add(key)
                if entity.get("material_id") is not None:
                    edges[by_id["materials", entity["material_id"]]].add(key)
                if entity.get("substance_id") is not None:
                    edges[by_id["substances", entity["substance_id"]]].add(key)
                if entity.get("series_id") is not None:
                    add(key, "quantitative_coverage", entity["series_id"])
            elif section == "quantitative_coverage":
                for oid in entity.get("observation_ids", []):
                    add(key, "observations", oid)
                    edges[by_id["observations", oid]].add(key)
            elif section == "comparisons":
                for oid in entity.get("observation_ids", []):
                    add(key, "observations", oid)
                    edges[by_id["observations", oid]].add(key)
                for mid in entity.get("material_ids", []):
                    edges[by_id["materials", mid]].add(key)
            elif section == "extensions":
                owners = [(group, entity.get(field)) for group, field in (
                    ("materials", "material_id"), ("experiments", "experiment_id"),
                    ("observations", "observation_id")) if entity.get(field) is not None]
                if not owners and key in seeds:
                    return None
                for group, identifier in owners:
                    target = by_id[group, identifier]
                    edges[key].add(target)
                    edges[target].add(key)

        affected, queue = set(), deque(sorted(seeds))
        while queue:
            key = queue.popleft()
            if key in affected:
                continue
            affected.add(key)
            if len(affected) > max_entities:
                return None
            queue.extend(sorted(edges[key] - affected))
        # Include the immediate identities/conditions needed to interpret the
        # affected facts; their unchanged citations do not become new seeds.
        context = set()
        for key in affected:
            entity = entities[key]
            for section, ids in (
                ("materials", [entity.get("material_id"), *entity.get("material_ids", [])]),
                ("configurations", [entity.get("configuration_id")]),
                ("substances", [entity.get("substance_id"),
                    *[x.get("substance_id") for x in entity.get("targets", [])],
                    *[x.get("substance_id") for x in entity.get("reaction_inputs", [])]])):
                for identifier in ids:
                    if identifier is not None:
                        context.add(by_id[section, identifier])
        keys = affected | context
        if len(keys) > max_entities:
            return None

        for finding in baseline.findings:
            if not (finding.needs_revision or finding.verdict in {"unsupported", "unresolved"}):
                continue
            parts = _parts(finding.record_path)
            if not parts:
                return None
            _resolve(before, parts)
            if (finding.needs_revision or finding.verdict == "unsupported") and not any(
                    _prefix(key, parts) for key in keys):
                return None

        refs, all_ids = [], set(changed_ids)
        for key in sorted(keys):
            entity = entities[key]
            refs.append({"section": key[0], "index": key[1], "id": entity.get("id"),
                "record_path": _path(key), "role": "affected" if key in affected else "context",
                "old_hash": digest_json(_resolve(before, key)), "new_hash": digest_json(entity)})
            all_ids.update(_evidence_ids(entity))
        if not all_ids.issubset(evidence):
            return None
        anchors = [{"evidence_id": eid, "source_id": evidence[eid].source_id,
                    "block_id": evidence[eid].block_id,
                    "locator": evidence[eid].locator.model_dump(mode="json")}
                   for eid in sorted(all_ids)]
        manifest = {"policy": POLICY, "incremental_depth": 1, "baseline_origin": baseline_origin,
            "old_record_hash": digest_json(before), "new_record_hash": digest_json(after),
            "baseline_audit_hash": digest_json(baseline.model_dump(mode="json")),
            "changed_paths": [item["changed_path"] for item in changes], "changes": changes,
            "entity_refs": refs, "source_anchors": anchors,
            "scope_limits": {"max_entities": max_entities, "max_changes": max_changes},
            "interpretation": "One citation-only model review plus authenticated unchanged baseline coverage; not a new full-paper audit or scientific validation."}
        manifest["scope_sha256"] = digest_json(manifest)
        return manifest
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None


def validate_citation_audit(scope, baseline_audit, new_audit):
    """Validate review-scope coverage, never decide scientific acceptance.

    Ordinary errors/unsupported/needs_revision/omissions gates still apply.
    Unlocated prose limitations and optional followups are conservatively kept:
    changing their interpretation requires a full audit.
    """
    try:
        baseline = ReadingAudit.model_validate(baseline_audit)
        current = ReadingAudit.model_validate(new_audit)
        if (not isinstance(scope, dict) or scope.get("policy") != POLICY
                or scope.get("baseline_origin") != "fresh_model_audit"
                or type(scope.get("incremental_depth")) is not int or scope["incremental_depth"] != 1
                or scope.get("scope_sha256") != digest_json({k: v for k, v in scope.items() if k != "scope_sha256"})
                or scope.get("baseline_audit_hash") != digest_json(baseline.model_dump(mode="json"))
                or not _audit_complete(baseline) or not _manifest_shape(scope)):
            return ["Citation audit scope or authenticated baseline is inconsistent."]
        if not scope.get("changes") or not scope.get("entity_refs") or not scope.get("source_anchors"):
            return ["Citation audit scope has no checkable changes, entities or sources."]
        entities = [_parts(item["record_path"]) for item in scope["entity_refs"]]
        if any(item is None for item in entities):
            return ["Citation audit scope contains an invalid entity path."]
        anchors = {item["evidence_id"]: (item["source_id"], item["block_id"])
                   for item in scope["source_anchors"]}
        findings = [(_parts(f.record_path), f) for f in current.findings]
        issues = []
        if (len(current.checked_dimensions) != len(AUDIT_DIMENSIONS)
                or set(current.checked_dimensions) != set(AUDIT_DIMENSIONS)):
            issues.append("Citation audit must account for all nine dimensions, including inherited baseline coverage.")
        if any(f.verdict == "not_checked" for f in current.findings):
            issues.append("Citation audit contains an unchecked claim.")
        for change in scope["changes"]:
            path = _parts(change["fact_path"])
            if not path or not any(_prefix(entity, path) for entity in entities):
                return ["Changed fact is outside the citation audit scope."]
            required = {anchors[eid] for eid in change["changed_evidence_ids"]}
            citation_path = _parts(change["changed_path"])
            matched = [f for parts, f in findings if parts and citation_path and _prefix(parts, citation_path)
                       and len(parts) >= 2 and parts[:2] == path[:2]]
            covered = {(a.source_id, a.block_id) for f in matched for a in f.evidence}
            if not matched or not required.issubset(covered):
                issues.append(f"Citation change lacks an explicit source-grounded finding: {change['fact_path']}")
        current_fingerprints = {digest_json(f.model_dump(mode="json")) for f in current.findings}
        for finding in baseline.findings:
            parts = _parts(finding.record_path)
            in_scope = parts and any(_prefix(entity, parts) for entity in entities)
            blocking = finding.needs_revision or finding.verdict == "unsupported"
            if blocking and not in_scope:
                issues.append("Baseline blocking finding lies outside the citation audit scope: " + finding.record_path)
            if (blocking or finding.verdict == "unresolved") and in_scope:
                matched = [f for path, f in findings if path and len(path) >= 2
                           and path[:2] == parts[:2] and (_prefix(path, parts) or _prefix(parts, path))
                           and f.evidence]
                if not matched:
                    issues.append("Baseline issue has not been explicitly reconsidered: " + finding.record_path)
            elif finding.verdict == "unresolved" and digest_json(finding.model_dump(mode="json")) not in current_fingerprints:
                issues.append("Unchanged unresolved finding was removed or altered: " + finding.record_path)
        if not set(baseline.coverage_limitations).issubset(current.coverage_limitations):
            issues.append("Baseline coverage limitations must remain explicit in a citation-only audit.")
        old_followups = {digest_json(f.model_dump(mode="json")) for f in baseline.quantitative_followups}
        new_followups = {digest_json(f.model_dump(mode="json")) for f in current.quantitative_followups}
        if not old_followups.issubset(new_followups):
            issues.append("Unchanged quantitative followups were removed or altered.")
        return issues
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return ["Citation audit scope/report is malformed; a full audit is required."]
