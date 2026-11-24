"""Source-bound review of localized interpretation repairs, not numerical re-extraction.

The full audit is authenticated by the caller. This module compiles exact changes,
their direct scientific dependencies and source obligations. It never approves a
claim, changes a record, or treats an omitted field as author-not-reported.
"""
from __future__ import annotations

import re
from pydantic import BaseModel

from .citation_audit import _parts, _path, _prefix, _resolve, _evidence_ids
from .domain import ExtractionRecord, Fact, NamedFact, TimeWindow
from .reading import AUDIT_DIMENSIONS, ReadingAudit
from .reference_associations import reference_association_issues
from .storage import digest_json
from .word_review_links import LINKED_POLICY

POLICY = "bounded-scientific-revision-v1"
# Runtime callers opt in explicitly. Omitting the context budget retains the
# exact historical scope semantics so frozen proofs remain reproducible.
NEW_SCOPE_LIMITS = {"max_entities": 200, "max_context_entities": 240, "max_changes": 180,
                    "compiler_version": 6, "source_requirement_policy": LINKED_POLICY}
MAX_NAVIGATION_ENTITIES = 480
SECTIONS = ("materials", "substances", "configurations", "experiments", "observations",
            "comparisons", "extensions", "quantitative_coverage", "reference_associations")
DESIGN_SECTIONS = ("core_checks", "modules", "observation_assignments", "source_index", "eligibility")


class ScientificScopeRejected(ValueError):
    """A recoverable scope boundary, never permission to silently review everything."""

    def __init__(self, code, message, path=()):
        super().__init__(message)
        self.code = code
        self.path = _path(path) if path else None


def scope_rejection(diagnostics, code, message, path=None):
    if diagnostics is not None:
        diagnostics.append({"code": code, "message": str(message), "path": path,
                            "recoverable": True, "automatic_full_review_allowed": False})


def _condition_fact(path):
    """Explicit non-identity scientific fields admitted since compiler v2.

    Numeric primary outcomes, source/ownership identity and source coverage still
    require a separate boundary decision. A condition/unit change is not approved
    here: the complete before/after claim and its consumers must be source reviewed.
    """
    if not path or path[0] not in SECTIONS:
        return False
    if path[0] == "observations" and path[-1] == "unit" and len(path) == 3:
        return True
    if "time_window" in path and path[0] in {"observations", "experiments"}:
        return True
    if path[0] == "experiments" and any(x in path for x in ("targets", "reaction_inputs")):
        return path[-1] in {"concentration", "amount", "unit", "basis", "timing"}
    return (path[-1] in {"fact", "unit", "basis"}
            and any(x in path for x in ("conditions", "coordinates", "parameters", "changed_conditions", "held_conditions")))


def _named_scientific_list(path, old, new):
    return (path and path[0] in SECTIONS and path[-1] in
            {"conditions", "coordinates", "parameters", "changed_conditions", "held_conditions"}
            and all(isinstance(x, NamedFact) for x in [*old, *new]))


def _retained_constraints(value):
    """Prevent an atomic array edit from hiding removed alternatives/restrictions."""
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"alternatives", "limitations", "unresolved_conditions"}:
                result.update((key, digest_json(x)) for x in item)
            result.update(_retained_constraints(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_retained_constraints(item))
    return result


def _plain(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_plain(x) for x in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _same(a, b):
    return digest_json(_plain(a)) == digest_json(_plain(b))


def _split_paths(text):
    """Split only at top level. No eval, ranges, wildcards or natural-language repair."""
    if not isinstance(text, str) or not text or len(text) > 24000:
        raise ValueError("missing path")
    result, start, depth = [], 0, 0
    for i, char in enumerate(text):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if depth < 0 or depth > 1:
            raise ValueError("invalid path brackets")
        if char in ";," and not depth:
            result.append(text[start:i].strip())
            start = i + 1
    result.append(text[start:].strip())
    if depth or not all(result) or len(result) > 128:
        raise ValueError("invalid path list")
    return result


def _expand_path(text, record):
    # Read old explicit selectors without changing their immutable audit text.
    match = re.search(r"\[([^\]]+)\]", text)
    if match and not match[1].isdigit():
        prefix = text[:match.start()]
        parts = _parts(prefix)
        if not parts:
            raise ValueError("selector parent is not a path")
        values = _resolve(record, parts)
        if not isinstance(values, list):
            raise ValueError("selector is not a list")
        selector = match[1]
        if re.fullmatch(r"(?:0|[1-9]\d*)(?:\s*,\s*(?:0|[1-9]\d*))+", selector):
            indices = [int(x.strip()) for x in selector.split(",")]
        elif re.fullmatch(r"id=[A-Za-z0-9_.:-]+", selector):
            indices = [i for i, x in enumerate(values) if isinstance(x, dict) and x.get("id") == selector[3:]]
            if len(indices) != 1:
                raise ValueError("ID selector must resolve uniquely")
        else:
            raise ValueError("unsupported path selector")
        if len(indices) > 128 or len(indices) != len(set(indices)):
            raise ValueError("invalid selector set")
        expanded = []
        for i in indices:
            expanded.extend(_expand_path(prefix + f"[{i}]" + text[match.end():], record))
        return expanded
    parts = _parts(text)
    if not parts:
        raise ValueError("unresolvable audit path")
    _resolve(record, parts)
    return [parts]


def finding_paths(finding, record):
    items = finding.record_paths or _split_paths(finding.record_path)
    paths = [p for item in items for p in _expand_path(item, record)]
    if not paths or len(paths) > 128 or len(paths) != len(set(paths)):
        raise ValueError("empty/duplicate audit paths")
    return paths


def _full_coverage(audit, record):
    if (audit.review_scope != "full" or audit.review_scope_sha256 is not None
            or len(audit.checked_dimensions) != len(AUDIT_DIMENSIONS)
            or set(audit.checked_dimensions) != set(AUDIT_DIMENSIONS)
            or any(f.verdict == "not_checked" for f in audit.findings)):
        return False
    located = set()
    for finding in audit.findings:
        if finding.needs_revision or finding.verdict == "unsupported":
            finding_paths(finding, record)
            if not finding.evidence:
                return False
            located.update(finding.omission_indices)
    # An omission may be scoped only when the completed auditor explicitly locates it.
    return located == set(range(len(audit.potential_omissions)))


def _change(path, kind, old, new):
    old, new = _plain(old), _plain(new)
    return {"changed_path": _path(path), "kind": kind, "old_value": old, "new_value": new,
            "old_value_hash": digest_json(old), "new_value_hash": digest_json(new)}


def _fact_changes(old, new, path, *, compiler_version=1):
    a, b = old.model_dump(mode="json"), new.model_dump(mode="json")
    if _same(a, b):
        return []
    condition = compiler_version >= 2 and _condition_fact(path)
    if not _same(a["normalized"], b["normalized"]) and not condition:
        raise ScientificScopeRejected("protected_quantity_changed", "normalized scientific quantity changed", path)
    if not _same(a["value"], b["value"]):
        qualitative = (path[0] == "observations" and path[-1] == "value"
                       and isinstance(a["value"], (str, type(None)))
                       and isinstance(b["value"], (str, type(None))))
        text_only = isinstance(a["value"], (str, type(None))) and isinstance(b["value"], (str, type(None)))
        object_repair = text_only and path[0] == "observations" and path[-1] == "measured_object"
        temporal_reference = text_only and len(path) > 1 and path[-2:] == ("time_window", "reference")
        basis_explanation = text_only and path[0] == "experiments" and path[-1] == "basis"
        if not (condition or qualitative or object_repair or temporal_reference or basis_explanation):
            raise ScientificScopeRejected("protected_quantity_or_identity_changed",
                "Primary outcome/identity changed outside the declared condition and unit repair boundary.", path)
        if qualitative and (b["status"] not in {"conflict", "ambiguous", "read_unquantified"}
                            or not b["alternatives"]):
            raise ValueError("qualitative outcome repair must preserve source alternatives")
    if not {digest_json(x) for x in a["alternatives"]}.issubset({digest_json(x) for x in b["alternatives"]}):
        raise ValueError("source alternatives removed")
    scientific = condition and any(not _same(a[k], b[k]) for k in ("value", "normalized"))
    return [_change(path, "scientific_condition" if scientific else "fact_interpretation", a, b)]


def _changes(old, new, path=(), *, allow_reference_repairs=False, compiler_version=1):
    if type(old) is not type(new):
        raise ScientificScopeRejected("typed_membership_changed", "Typed scientific membership changed outside the admitted fields.", path)
    if isinstance(old, Fact):
        return _fact_changes(old, new, path, compiler_version=compiler_version)
    if (compiler_version == 3 and isinstance(old, TimeWindow) and len(path) == 3
            and path[0] in {"observations", "experiments"} and path[-1] == "time_window"
            and old.kind != new.kind):
        if not new.kind.strip():
            raise ScientificScopeRejected("invalid_time_semantics", "A revised time-window kind must be explicit.", path)
        # Validate every nested Fact before treating the complete temporal claim
        # as one change. A kind edit cannot hide removed source alternatives.
        for field in ("start", "end", "unit", "reference"):
            _fact_changes(getattr(old, field), getattr(new, field), (*path, field), compiler_version=compiler_version)
        return [_change(path, "time_window_semantics", old, new)]
    if isinstance(old, BaseModel):
        return [c for field in type(old).model_fields
                for c in _changes(getattr(old, field), getattr(new, field), (*path, field),
                                  allow_reference_repairs=allow_reference_repairs, compiler_version=compiler_version)]
    if _same(old, new):
        return []
    if isinstance(old, list):
        if compiler_version == 3 and path == ("evidence",):
            if len(new) < len(old) or any(not _same(a, b) for a, b in zip(old, new)):
                raise ScientificScopeRejected("existing_evidence_changed",
                    "Existing evidence must remain an identical ordered prefix, including IDs, quotes and source locators.", path)
            return [_change((*path, i), "evidence_addition", None, item)
                    for i, item in enumerate(new[len(old):], len(old))]
        if compiler_version == 3 and path == ("design_scope", "eligibility"):
            key = lambda item: (item.use, item.target_kind, item.target_id)
            old_keys, new_keys = [key(item) for item in old], [key(item) for item in new]
            if (len(new) < len(old) or new_keys[:len(old)] != old_keys
                    or len(set(new_keys)) != len(new_keys)):
                raise ScientificScopeRejected("eligibility_identity_changed",
                    "Existing eligibility identities/order must remain; each added use/target binding must be unique.", path)
            additions = []
            for i, item in enumerate(new[len(old):], len(old)):
                if (item.status != "limited" or not item.reason.strip() or not item.limitations
                        or any(not text.strip() for text in item.limitations)
                        or not item.evidence_ids or len(set(item.evidence_ids)) != len(item.evidence_ids)):
                    raise ScientificScopeRejected("unsafe_eligibility_addition",
                        "A new local eligibility entry must be a source-bound limitation, never a grant of research eligibility.", (*path, i))
                additions.append(_change((*path, i), "eligibility_restriction", None, item))
            return [c for i, (a, b) in enumerate(zip(old, new))
                    for c in _changes(a, b, (*path, i), allow_reference_repairs=allow_reference_repairs,
                                      compiler_version=compiler_version)] + additions
        if compiler_version >= 2 and _named_scientific_list(path, old, new):
            names_before, names_after = [x.name for x in old], [x.name for x in new]
            if len(set(names_before)) != len(names_before) or len(set(names_after)) != len(names_after):
                raise ScientificScopeRejected("ambiguous_condition_membership", "Condition names must be unique within a scoped list.", path)
            if names_before != names_after:
                a, b = _plain(old), _plain(new)
                if not _retained_constraints(a).issubset(_retained_constraints(b)):
                    raise ScientificScopeRejected("source_restriction_removed", "Condition membership repair removed source alternatives or restrictions.", path)
                return [_change(path, "scientific_membership", a, b)]
        if path == ("reference_associations",):
            if len(new) < len(old):
                raise ValueError("existing reference associations changed")
            repairs = []
            for i, (a, b) in enumerate(zip(old, new)):
                before, after = a.model_dump(mode="json"), b.model_dump(mode="json")
                if _same(before, after):
                    continue
                if not allow_reference_repairs:
                    raise ValueError("existing reference associations changed")
                protected = {"id", "target_kind", "target_id", "observation_ids", "relationship"}
                if any(not _same(before[key], after[key]) for key in protected):
                    raise ValueError("reference binding or relationship changed")
                mutable = {"applicability", "rationale", "evidence_ids", "matched_conditions",
                           "differing_conditions", "unresolved_conditions", "limitations"}
                if any(not _same(before[key], after[key]) for key in before.keys() - protected - mutable):
                    raise ValueError("unsupported reference interpretation field changed")
                for key in ("limitations", "differing_conditions", "unresolved_conditions"):
                    if after[key][:len(before[key])] != before[key]:
                        raise ValueError("existing reference uncertainty removed/replaced")
                repairs.append(_change((*path, i), "reference_association", before, after))
            return repairs + [_change((*path, i), "reference_association", None, item.model_dump(mode="json"))
                              for i, item in enumerate(new[len(old):], len(old))]
        if path[-1:] in {("limitations",), ("unresolved_conditions",)}:
            if new[:len(old)] != old or any(not isinstance(x, str) or not x.strip() for x in new):
                raise ValueError("limitations removed/replaced")
            return [_change(path, "restriction", old, new)]
        if path[-1:] in {("evidence_ids",), ("evidence_links",)}:
            return [_change(path, "support", old, new)]
        if len(old) != len(new):
            raise ScientificScopeRejected("protected_membership_changed", "Scientific entity/source membership changed outside typed condition lists.", path)
        return [c for i, (a, b) in enumerate(zip(old, new))
                for c in _changes(a, b, (*path, i), allow_reference_repairs=allow_reference_repairs,
                                  compiler_version=compiler_version)]
    if path and path[0] == "design_scope":
        if path[-1] in {"summary", "reason", "material_relation"}:
            return [_change(path, "scope_explanation", old, new)]
        if path[-1] == "status" and new == "limited":
            return [_change(path, "scope_restriction", old, new)]
    if path and path[0] in SECTIONS and path[-1] in {"reason", "role_reason"}:
        return [_change(path, "scope_explanation", old, new)]
    if path and path[0] == "observations" and path[-1] == "origin":
        return [_change(path, "provenance", old, new)]
    raise ScientificScopeRejected("protected_identity_or_coverage_changed",
        "Scientific identity, source coverage, membership or an unsupported field changed.", path)


def _entities(data, *, include_evidence=False):
    result = {}
    for name in SECTIONS:
        for i, item in enumerate(data.get(name, [])):
            result[(name, i)] = item
    for name in DESIGN_SECTIONS:
        for i, item in enumerate((data.get("design_scope") or {}).get(name, [])):
            result[("design_scope", name, i)] = item
    if include_evidence:
        for i, item in enumerate(data.get("evidence", [])):
            result[("evidence", i)] = item
    return result


def _change_evidence_ids(change, side):
    value = change[side]
    ids = _evidence_ids(value)
    if _parts(change["changed_path"])[-1:] == ("evidence_ids",):
        ids.update(value or [])
    return ids


def _validate_evidence_additions(old, new, changes):
    """Recognize registered source pairs and real typed references, not prose.

    The authenticated old record's coverage is the source inventory here.
    Frozen original documents and fresh source-tool delivery are independently
    verified downstream; compiling a locator never verifies its scientific claim.
    """
    additions = [c for c in changes if c["kind"] == "evidence_addition"]
    if not additions:
        return
    old_ids = {e.id for e in old.evidence}
    new_ids = [e.id for e in new.evidence]
    if len(new_ids) != len(set(new_ids)):
        raise ScientificScopeRejected("duplicate_evidence_identity", "New evidence needs an unused, unique evidence ID.", ("evidence",))
    pairs = {(entry.source_id, block_id) for entry in old.coverage for block_id in entry.block_ids}
    seen_definitions = {digest_json(e.model_dump(mode="json", exclude={"id"})) for e in old.evidence}
    previously_referenced = _evidence_ids(old.model_dump(mode="json"))
    consumers = [c for c in changes if _parts(c["changed_path"])[0] != "evidence"]
    for change in additions:
        evidence = change["new_value"]
        path = _parts(change["changed_path"])
        identifier = evidence["id"]
        if identifier in old_ids or identifier in previously_referenced:
            raise ScientificScopeRejected("preexisting_evidence_identity",
                "An appended evidence ID cannot replace an old definition or retroactively resolve an old dangling reference.", path)
        if (evidence["source_id"] not in old.paper.source_ids
                or (evidence["source_id"], evidence["block_id"]) not in pairs):
            raise ScientificScopeRejected("unregistered_evidence_source",
                "New evidence must locate a source/block pair in this paper's unchanged registered coverage.", path)
        definition = digest_json({k: v for k, v in evidence.items() if k != "id"})
        if definition in seen_definitions:
            raise ScientificScopeRejected("aliased_evidence_definition",
                "A new ID cannot disguise an existing or repeated evidence definition.", path)
        seen_definitions.add(definition)
        if not any(identifier in _change_evidence_ids(c, "new_value") for c in consumers):
            raise ScientificScopeRejected("unused_evidence_addition",
                "Every appended evidence must support an actually revised typed claim or evidence link.", path)


def _eligibility_targets(old, new, changes):
    """Added restrictions bind existing typed targets; they do not create subjects."""
    sections = {"material": "materials", "configuration": "configurations", "experiment": "experiments",
                "observation": "observations", "comparison": "comparisons"}
    targets = set()
    for change in changes:
        if change["kind"] != "eligibility_restriction":
            continue
        item = change["new_value"]
        if item["target_kind"] == "paper":
            valid = item["target_id"] == old.paper.id == new.paper.id
        else:
            section = sections[item["target_kind"]]
            old_matches = [i for i, target in enumerate(getattr(old, section)) if target.id == item["target_id"]]
            new_matches = [i for i, target in enumerate(getattr(new, section)) if target.id == item["target_id"]]
            valid = len(old_matches) == 1 and old_matches == new_matches
            if valid:
                targets.add((section, new_matches[0]))
        if not valid:
            raise ScientificScopeRejected("invalid_eligibility_target",
                "A local eligibility restriction must bind one unchanged registered target in this paper.",
                _parts(change["changed_path"]))
    return targets


def _citation_local_owners(changes, obligations, entities, forced_targets):
    """Only exact evidence-ID edits may avoid semantic downstream propagation.

    A broader prior scientific finding defeats the shortcut even if the latest
    revision changed only citations. The owner and both sources still require
    explicit review; no old blocker or interpretation is silently downgraded.
    """
    grouped = {}
    for change in changes:
        key = _owner(_parts(change["changed_path"]), entities)
        grouped.setdefault(key, []).append(change)
    result = set()
    for key, own_changes in grouped.items():
        if key in forced_targets:
            continue
        citation_paths = []
        for change in own_changes:
            path = _parts(change["changed_path"])
            a, b = change["old_value"], change["new_value"]
            if change["kind"] == "support" and path[-1:] == ("evidence_ids",):
                citation_paths.append(path)
            elif (isinstance(a, dict) and isinstance(b, dict) and "evidence_ids" in a
                    and "evidence_ids" in b and _same({k: v for k, v in a.items() if k != "evidence_ids"},
                                                       {k: v for k, v in b.items() if k != "evidence_ids"})):
                citation_paths.append((*path, "evidence_ids"))
            else:
                break
        else:
            blockers = [_parts(path) for item in obligations for path in item["paths"]
                        if _owner(_parts(path), entities) == key]
            if all(any(_prefix(citation, blocker) for citation in citation_paths) for blocker in blockers):
                result.add(key)
    return result


def _citation_consumer_context(local, entities, index):
    """Freeze direct typed uses without promoting unchanged uses to new seeds."""
    if not local:
        return set()
    ids = {}
    for key in local:
        identifier = entities[key].get("id")
        if identifier is not None:
            ids.setdefault(key[0], set()).add(identifier)
    named = lambda section: ids.get(section, set())
    context = set()
    target_sections = {"material": "materials", "configuration": "configurations", "experiment": "experiments",
                       "observation": "observations", "comparison": "comparisons", "series": "quantitative_coverage"}
    for key, item in entities.items():
        section = key[0]
        related = False
        if section == "materials":
            substances = {c["substance_id"] for c in item.get("components", [])}
            substances.update(s for step in item.get("preparation", []) for s in step.get("input_substance_ids", []))
            related = bool(substances & named("substances"))
        elif section == "configurations":
            materials = set(item.get("material_ids", [])) | {p["material_id"] for p in item.get("placements", [])}
            related = bool(set(item.get("accessory_substance_ids", [])) & named("substances")
                           or materials & named("materials"))
            if key in local:
                context.update(index["materials", mid] for mid in materials)
        elif section == "experiments":
            substances = {x.get("substance_id") for x in item.get("targets", []) + item.get("reaction_inputs", [])}
            materials = set(item.get("material_ids", [])) | {s["material_id"] for s in item.get("material_states", [])}
            related = bool(substances & named("substances") or materials & named("materials")
                           or item.get("configuration_id") in named("configurations"))
            if key in local:
                context.update(index["materials", mid] for mid in materials)
            for pair in item.get("pairs", []):
                pair_ids = {pair["left_observation_id"], pair["right_observation_id"]}
                if pair_ids & named("observations"):
                    related = True
                    context.update(index["observations", oid] for oid in pair_ids)
        elif section == "observations":
            related = any(item.get(field) in named(parent) for field, parent in
                          (("substance_id", "substances"), ("material_id", "materials"), ("experiment_id", "experiments")))
        elif section in {"comparisons", "quantitative_coverage"}:
            related = bool(set(item.get("observation_ids", [])) & named("observations")
                           or set(item.get("material_ids", [])) & named("materials"))
            if related or key in local:
                context.update(index["observations", oid] for oid in item.get("observation_ids", []))
        elif section == "reference_associations":
            target = index[target_sections[item["target_kind"]], item["target_id"]]
            related = target in local or bool(set(item["observation_ids"]) & named("observations"))
            if related or key in local:
                context.add(target)
                context.update(index["observations", oid] for oid in item["observation_ids"])
        elif section == "extensions":
            related = any(item.get(field) in named(parent) for field, parent in
                          (("material_id", "materials"), ("experiment_id", "experiments"), ("observation_id", "observations")))
        elif section == "design_scope":
            target_section = target_sections.get(item.get("target_kind"))
            target = index.get((target_section, item.get("target_id"))) if target_section else None
            related = target in local
            if key in local and target is not None:
                context.add(target)
            related = related or item.get("observation_id") in named("observations")
            related = related or bool(set(item.get("retained_observation_ids", [])) & named("observations"))
            if key[1] == "modules":
                related = related or bool(set(item.get("material_ids", [])) & named("materials")
                                          or set(item.get("experiment_ids", [])) & named("experiments"))
                if key in local:
                    context.update(index["materials", mid] for mid in item.get("material_ids", []))
                    context.update(index["experiments", eid] for eid in item.get("experiment_ids", []))
            elif key[1] == "observation_assignments":
                related = related or any(parent[:2] == ("design_scope", "modules")
                    and entities[parent].get("id") == item.get("module_id") for parent in local)
        if related:
            context.add(key)
    return context


def _owner(path, entities):
    size = 3 if path[:1] == ("design_scope",) else 2
    key = path[:size]
    if key not in entities:
        raise ValueError("repair outside scientific entities")
    return key


def _local_material_missingness(change):
    """A null recipe amount is not a new measured performance condition.

    Only typed component/preparation null facts qualify. There is no prose-based
    guess about which measurement used a composition-derived denominator.
    """
    path = _parts(change["changed_path"])
    if not path or path[0] != "materials" or change["kind"] != "fact_interpretation":
        return False
    component = len(path) == 5 and path[2] == "components" and path[-1] in {"amount", "unit", "basis"}
    recipe = len(path) == 7 and path[2] == "preparation" and path[4] == "parameters" and path[-1] in {"fact", "unit", "basis"}
    return (component or recipe) and all(
        change[side][field] is None for side in ("old_value", "new_value") for field in ("value", "normalized"))


def _has_normalization(value):
    if isinstance(value, dict):
        return value.get("normalized") is not None or any(_has_normalization(x) for x in value.values())
    return isinstance(value, list) and any(_has_normalization(x) for x in value)


def build_scientific_revision_scope(old_record, new_record, baseline_audit, *, baseline_origin,
                                    additional_audits=(), max_entities=200, max_changes=180,
                                    max_context_entities=None, compiler_version=1, diagnostics=None,
                                    source_requirement_policy=None):
    """Changed metadata + direct consumers, without a common-material recursive explosion."""
    try:
        if (baseline_origin != "fresh_model_audit" or type(max_entities) is not int
                or not 1 <= max_entities <= 240 or type(max_changes) is not int or not 1 <= max_changes <= 240):
            raise ScientificScopeRejected("invalid_baseline_or_budget", "The full-audit origin or bounded entity/change budget is invalid.")
        if (max_context_entities is not None and (type(max_context_entities) is not int
                or not 1 <= max_context_entities <= 240)):
            raise ScientificScopeRejected("invalid_context_budget", "The context navigation budget is invalid.")
        if type(compiler_version) is not int or compiler_version not in {1, 2, 3, 4, 5, 6}:
            raise ScientificScopeRejected("unsupported_compiler", "Unsupported scientific scope compiler version.")
        if source_requirement_policy not in (None, "native-word-structure-v1", LINKED_POLICY):
            raise ScientificScopeRejected("unsupported_source_requirement_policy", "Unsupported source requirement policy.")
        old, new = ExtractionRecord.model_validate(old_record), ExtractionRecord.model_validate(new_record)
        before, after = old.model_dump(mode="json"), new.model_dump(mode="json")
        baseline = ReadingAudit.model_validate(baseline_audit)
        audits = [baseline, *[ReadingAudit.model_validate(x) for x in additional_audits]]
        if compiler_version in {4, 5, 6}:
            from .ownership_audit import build_ownership_scope, ownership_changed, configuration_context_changed, check_contextual_edits
            limits = {"max_entities": max_entities, "max_changes": max_changes, "compiler_version": compiler_version}
            if max_context_entities is not None:
                limits["max_context_entities"] = max_context_entities
            if source_requirement_policy is not None:
                limits["source_requirement_policy"] = source_requirement_policy
            if ownership_changed(before, after):
                return build_ownership_scope(old, new, baseline, audits, limits)
            if compiler_version >= 5 and configuration_context_changed(before, after):
                check_contextual_edits(before, after, include_existing_interpretations=True)
                return build_ownership_scope(old, new, baseline, audits, limits)
            # Preserve the established interpretation boundary for ordinary edits.
            scope = build_scientific_revision_scope(old, new, baseline, baseline_origin=baseline_origin,
                additional_audits=audits[1:], diagnostics=diagnostics, **{**limits, "compiler_version": 3})
            if scope is not None:
                scope["scope_limits"] = limits
                scope.pop("scope_sha256")
                scope["scope_sha256"] = digest_json(scope)
            return scope
        if not _full_coverage(baseline, before):
            raise ScientificScopeRejected("incomplete_full_baseline", "The inherited full audit does not cover all dimensions or locate every blocker/omission.")
        if len(audits) > 4:
            raise ScientificScopeRejected("audit_chain_budget", "The bounded cumulative audit chain limit was exceeded.")
        for audit in audits[1:]:
            if audit.review_scope not in {"scientific_delta", "citation_delta"} or any(f.verdict == "not_checked" for f in audit.findings):
                raise ScientificScopeRejected("invalid_additional_audit", "An intermediate audit is not a completed scientific delta audit.")
        changes = _changes(old, new, allow_reference_repairs=max_context_entities is not None,
                           compiler_version=compiler_version)
        if compiler_version == 3:
            _validate_evidence_additions(old, new, changes)
        if not 1 <= len(changes) <= max_changes:
            raise ScientificScopeRejected("change_budget", f"Changed claim count {len(changes)} is outside the bounded limit 1..{max_changes}.")
        if any(issue.severity == "error" for issue in reference_association_issues(new)):
            raise ScientificScopeRejected("invalid_reference_association", "Reference associations contain invalid ownership or target bindings.")
        entities = _entities(after, include_evidence=compiler_version == 3)
        old_entities = _entities(before, include_evidence=compiler_version == 3)
        index = {(k[0], item.get("id")): k for k, item in entities.items() if item.get("id") is not None}
        if len(index) != sum(item.get("id") is not None for item in entities.values()):
            raise ScientificScopeRejected("duplicate_entity_identity", "Entity IDs are not unique in their scientific sections.")
        affected = {_owner(_parts(c["changed_path"]), entities) for c in changes}
        forced_targets = set()
        if compiler_version == 3:
            # A restriction on an existing experimental/material result also
            # requires review of that exact subject and its normal consumers.
            forced_targets = _eligibility_targets(old, new, changes)
            affected.update(forced_targets)
        obligations = []
        for ai, audit in enumerate(audits):
            located = set()
            for fi, finding in enumerate(audit.findings):
                if not (finding.needs_revision or finding.verdict == "unsupported"):
                    continue
                paths = finding_paths(finding, before if ai == 0 else after)
                original_paths = paths
                if compiler_version >= 2:
                    # If a condition list is split/reordered, its historical index
                    # no longer identifies the same condition. Review the whole
                    # changed list, retaining exact historical paths in the proof.
                    memberships = [_parts(c["changed_path"]) for c in changes
                                   if c["kind"] == "scientific_membership"]
                    paths = list(dict.fromkeys(next((p for p in memberships if _prefix(p, path)), path)
                                               for path in paths))
                if not finding.evidence:
                    raise ScientificScopeRejected("unanchored_blocker", "A prior blocking finding has no original source anchor.")
                affected.update(_owner(path, entities) for path in paths)
                located.update(finding.omission_indices)
                obligations.append({"audit_index": ai, "finding_index": fi,
                    "paths": [_path(path) for path in paths],
                    "source_blocks": [list(pair) for pair in sorted({(a.source_id, a.block_id) for a in finding.evidence})]})
                if paths != original_paths:
                    obligations[-1]["historical_paths"] = [_path(path) for path in original_paths]
            if located != set(range(len(audit.potential_omissions))):
                raise ScientificScopeRejected("unlocated_omission", "A prior omission is not explicitly located by a source-grounded finding.")
        citation_local = (_citation_local_owners(changes, obligations, entities, forced_targets)
                          if compiler_version == 3 else set())
        # These owners remain mandatory affected review subjects. They enter
        # again after semantic propagation, with their direct uses as context.
        affected -= citation_local
        changed_owners = set(affected)
        observations = {o.id: o for o in new.observations}
        changed_experiments = {entities[k]["id"] for k in affected if k[0] == "experiments"}
        changed_configs = {entities[k]["id"] for k in affected if k[0] == "configurations"}
        changed_materials = {entities[k]["id"] for k in affected if k[0] == "materials"}
        changed_substances = {entities[k]["id"] for k in affected if k[0] == "substances"}
        local_recipe_materials = set()
        for mid in changed_materials:
            key = index["materials", mid]
            own_changes = [c for c in changes if _owner(_parts(c["changed_path"]), entities) == key]
            own_blockers = [_parts(p) for item in obligations for p in item["paths"]
                            if _owner(_parts(p), entities) == key]
            local_paths = [_parts(c["changed_path"]) for c in own_changes if _local_material_missingness(c)]
            if (own_changes and len(local_paths) == len(own_changes)
                    and all(any(_prefix(local, blocker) for local in local_paths) for blocker in own_blockers)):
                local_recipe_materials.add(mid)
        changed_materials -= local_recipe_materials
        # A changed substance interpretation affects its explicit consumers.
        # Contextual parents are never reverse-expanded just for sharing a name.
        for i, material in enumerate(new.materials):
            ids = {c.substance_id for c in material.components}
            ids.update(s for step in material.preparation for s in step.input_substance_ids)
            if ids & changed_substances:
                affected.add(("materials", i)); changed_materials.add(material.id)
        for i, configuration in enumerate(new.configurations):
            if set(configuration.accessory_substance_ids) & changed_substances:
                affected.add(("configurations", i)); changed_configs.add(configuration.id)
        # Shared conditions affect every result owned by those exact experiments.
        for i, experiment in enumerate(new.experiments):
            substance_ids = {t.substance_id for t in experiment.targets}
            substance_ids.update(x.substance_id for x in experiment.reaction_inputs if x.substance_id)
            if (experiment.configuration_id in changed_configs or substance_ids & changed_substances
                    or (compiler_version >= 2 and set(experiment.material_ids) & changed_materials)):
                affected.add(("experiments", i)); changed_experiments.add(experiment.id)
        for i, observation in enumerate(new.observations):
            if (observation.experiment_id in changed_experiments or observation.material_id in changed_materials
                    or observation.substance_id in changed_substances
                    or (observation.material_id in local_recipe_materials and _has_normalization(after["observations"][i]))):
                affected.add(("observations", i))
        for i, comparison in enumerate(new.comparisons):
            if set(comparison.material_ids) & (changed_materials | local_recipe_materials):
                affected.add(("comparisons", i))
        result_ids = {entities[k]["id"] for k in affected if k[0] == "observations"}
        pair_context = set()
        if compiler_version >= 2:
            for i, experiment in enumerate(new.experiments):
                for pair in experiment.pairs:
                    ids = {pair.left_observation_id, pair.right_observation_id}
                    if ids & result_ids:
                        affected.add(("experiments", i))
                        pair_context.update(index["observations", oid] for oid in ids)
        # A primary result's direct series/comparison is a changed reference
        # target too. Freeze this one-hop set before adding shared controls so
        # an unchanged reused control does not recursively expand every panel.
        for section in ("comparisons", "quantitative_coverage"):
            affected.update((section, i) for i, item in enumerate(after.get(section, []))
                            if set(item.get("observation_ids", [])) & result_ids)
        directly_affected = set(affected)
        context = set(pair_context)
        for i, association in enumerate(getattr(new, "reference_associations", [])):
            key = ("reference_associations", i)
            section = {"experiment": "experiments", "series": "quantitative_coverage", "comparison": "comparisons"}[association.target_kind]
            target = index[section, association.target_id]
            if key in changed_owners or target in directly_affected or set(association.observation_ids) & result_ids:
                affected.add(key)
                # A new reference does not alter or remeasure its source values.
                # Keep genuinely changed/blocked observations in affected, but
                # do not seed every unrelated consumer of an unchanged control.
                # The legacy branch must reproduce already frozen scope hashes.
                referenced = {index["observations", oid] for oid in association.observation_ids}
                if max_context_entities is None:
                    affected.update(referenced)
                else:
                    context.update(referenced)
                affected.add(target)
                if association.target_kind == "experiment":
                    context.update(("observations", j) for j, o in enumerate(new.observations)
                                   if o.experiment_id == association.target_id)
        # Consumer nodes must check compatibility; their unchanged members are context,
        # not new seeds that recursively pull the entire paper through a shared control.
        result_ids = {entities[k]["id"] for k in affected if k[0] == "observations"}
        for section in ("comparisons", "quantitative_coverage"):
            for i, item in enumerate(after.get(section, [])):
                key = (section, i)
                if key in affected or set(item.get("observation_ids", [])) & result_ids:
                    affected.add(key)
                    context.update(index["observations", oid] for oid in item.get("observation_ids", []))
        for i, experiment in enumerate(new.experiments):
            for pair in experiment.pairs:
                ids = {pair.left_observation_id, pair.right_observation_id}
                if ids & result_ids:
                    affected.add(("experiments", i)); context.update(index["observations", oid] for oid in ids)
        # Any genuine condition/interpretation change reaching the same owner
        # overrides its citation-only classification; never weaken real closure.
        citation_local -= affected
        context.update(_citation_consumer_context(citation_local, entities, index))
        affected.update(citation_local)
        # Resolve upstream context to a fixed point, never downstream consumers
        # of context-only entities (in particular not every use of a material).
        pending = list(affected | context)
        visited = set()
        while pending:
            key = pending.pop()
            if key in visited:
                continue
            visited.add(key)
            item = entities[key]
            parents = set()
            for section, ids in (("experiments", [item.get("experiment_id")]),
                ("materials", [item.get("material_id"), *item.get("material_ids", [])]),
                ("configurations", [item.get("configuration_id")]),
                ("substances", [item.get("substance_id"), *item.get("accessory_substance_ids", [])])):
                parents.update(index[section, identifier] for identifier in ids if identifier is not None)
            if key[0] == "experiments":
                parents.update(index["substances", x["substance_id"]]
                    for x in item.get("targets", []) + item.get("reaction_inputs", []) if x.get("substance_id"))
            if key[0] == "materials":
                parents.update(index["substances", x["substance_id"]] for x in item.get("components", []))
                parents.update(index["substances", sid] for step in item.get("preparation", [])
                               for sid in step.get("input_substance_ids", []))
            context.update(parents)
            pending.extend(parents - visited)
        # Scope/eligibility descriptions are dependencies, not source proof.
        affected_ids = {entities[k]["id"] for k in affected
                        if entities[k].get("id") is not None and k[0] != "evidence" and k not in citation_local}
        if compiler_version >= 2:
            # Typed extension modules may carry operating requirements or a
            # downstream interpretation of the same experimental result.
            for key, item in entities.items():
                if key[0] == "extensions" and any(item.get(field) in affected_ids
                        for field in ("observation_id", "experiment_id", "material_id")):
                    affected.add(key)
        for key, item in entities.items():
            if key[0] == "design_scope" and (key in affected or item.get("target_id") in affected_ids):
                if key[1] == "eligibility":
                    affected.add(key)
                else:
                    context.add(key)
        keys = affected | context
        if max_context_entities is None:
            if len(keys) > max_entities:
                raise ScientificScopeRejected("entity_budget", f"The necessary scope has {len(keys)} entities, above limit {max_entities}.")
        elif (len(affected) > max_entities or len(context - affected) > max_context_entities
                or len(keys) > MAX_NAVIGATION_ENTITIES):
            raise ScientificScopeRejected("entity_budget",
                f"Necessary affected/context/total entities {len(affected)}/{len(context - affected)}/{len(keys)} "
                f"exceed bounded limits {max_entities}/{max_context_entities}/{MAX_NAVIGATION_ENTITIES}.")
        evidence = {e.id: e for e in new.evidence}
        if len(evidence) != len(new.evidence):
            raise ScientificScopeRejected("duplicate_evidence_identity", "Evidence IDs are not unique.")
        anchors = {}
        def add_anchors(ids):
            if not ids.issubset(evidence):
                raise ValueError("unknown required evidence")
            for eid in ids:
                e = evidence[eid]
                anchors[eid] = {"evidence_id": eid, "source_id": e.source_id, "block_id": e.block_id,
                                "locator": e.locator.model_dump(mode="json")}

        for change in changes:
            key = _owner(_parts(change["changed_path"]), entities)
            ids = _evidence_ids(change["old_value"]) | _evidence_ids(change["new_value"])
            if compiler_version == 3 and key[0] == "evidence":
                ids.add(entities[key]["id"])
            if _parts(change["changed_path"])[-1] == "evidence_ids":
                ids.update(change["old_value"] or []); ids.update(change["new_value"] or [])
            if not ids:
                ids.update(_evidence_ids(entities[key])); ids.update(_evidence_ids(old_entities.get(key, {})))
            if (not ids and max_context_entities is not None and
                    (key[0] == "quantitative_coverage" or key[:2] == ("design_scope", "source_index"))):
                # These typed source inventories bind their original block
                # directly rather than carrying Fact.evidence_ids. Resolve only
                # that exact registered block; never guess from prose or IDs.
                owner = entities[key]
                ids.update(e.id for e in evidence.values()
                           if (e.source_id, e.block_id) == (owner["source_id"], owner["block_id"]))
            if not ids or not ids.issubset(evidence):
                raise ScientificScopeRejected("unanchored_change", "A changed claim lacks known original-source evidence.", _parts(change["changed_path"]))
            change["required_evidence_ids"] = sorted(ids)
            add_anchors(ids)
        for obligation in obligations:
            for pair in obligation["source_blocks"]:
                if tuple(pair) not in {(x["source_id"], x["block_id"]) for x in anchors.values()}:
                    matches = [e for e in evidence.values() if (e.source_id, e.block_id) == tuple(pair)]
                    if not matches:
                        covered_blocks = {(entry.source_id, bid) for entry in new.coverage for bid in entry.block_ids}
                        if compiler_version >= 2 and tuple(pair) in covered_blocks and pair[0] in new.paper.source_ids:
                            # An authenticated full auditor may cite a caption or
                            # methods block without adding it to candidate.evidence.
                            # Keep that original obligation separately; never invent
                            # an EvidenceRef or change scientific source definitions.
                            # Actual frozen source existence/hash and fresh delivery
                            # are independently required by source_review.
                            anchor_id = "audit-source:" + digest_json(pair)
                            anchors[anchor_id] = {"evidence_id": anchor_id, "source_id": pair[0],
                                "block_id": pair[1], "locator": {}, "origin": "prior_audit_blocker"}
                            continue
                        raise ScientificScopeRejected("unregistered_blocker_source", "A blocking source block is absent from unchanged evidence and covered source inventory.")
                    e = sorted(matches, key=lambda x: x.id)[0]
                    add_anchors({e.id})
        refs = []
        for key in sorted(keys, key=_path):
            item = entities[key]
            ids = set()
            if key in affected:
                ids.update(_evidence_ids(item)); ids.update(_evidence_ids(old_entities.get(key, {})))
                if compiler_version == 3 and key[0] == "evidence":
                    ids.add(item["id"])
                if key[0] == "quantitative_coverage":
                    ids.update(e.id for e in evidence.values()
                               if (e.source_id, e.block_id) == (item["source_id"], item["block_id"]))
                related = set(item.get("observation_ids", []))
                if key[0] == "experiments":
                    related.update(entities[k]["id"] for k in keys if k[0] == "observations"
                                   and entities[k]["experiment_id"] == item["id"])
                    related.update(oid for pair in item.get("pairs", [])
                                   for oid in (pair["left_observation_id"], pair["right_observation_id"]))
                for oid in related:
                    ids.update(_evidence_ids(after["observations"][index["observations", oid][1]]))
                if not ids:
                    # Eligibility/interpretive consumer checks can be supported
                    # by the changed claims whose consequences they assess.
                    # Their explicit entity disposition is still mandatory.
                    ids.update(eid for c in changes for eid in c["required_evidence_ids"])
                add_anchors(ids)
            refs.append({"record_path": _path(key), "role": "affected" if key in affected else "context",
                         "old_hash": digest_json(old_entities.get(key)), "new_hash": digest_json(item),
                         "required_evidence_ids": sorted(ids)})
        result = {"policy": POLICY, "baseline_origin": baseline_origin,
            "old_record_hash": digest_json(before), "new_record_hash": digest_json(after),
            "baseline_audit_hash": digest_json(baseline.model_dump(mode="json")),
            "additional_audit_hashes": [digest_json(a.model_dump(mode="json")) for a in audits[1:]],
            "changes": changes, "changed_paths": [c["changed_path"] for c in changes],
            "entity_refs": refs, "source_anchors": [anchors[k] for k in sorted(anchors)],
            "blocking_obligations": obligations,
            "scope_limits": {"max_entities": max_entities, "max_changes": max_changes},
            "interpretation": "Authenticated full baseline plus source review of every cumulative changed claim and its direct consumers. No new experimental values or identity/coverage changes; not scientific validation."}
        if max_context_entities is not None:
            result["scope_limits"]["max_context_entities"] = max_context_entities
        if compiler_version != 1:
            result["scope_limits"]["compiler_version"] = compiler_version
            result["interpretation"] = (
                "Authenticated full baseline plus source review of every cumulative changed claim and its direct consumers. "
                "Explicit condition/unit and typed condition-array repairs are reviewed against both versions; scientific "
                "identity, primary numeric outcomes, source definitions and coverage remain unchanged. Not scientific validation.")
        if compiler_version == 3:
            result["interpretation"] = (
                "Authenticated full baseline plus source review of every cumulative changed claim, semantically affected consumer, "
                "and appended evidence definition. New evidence must resolve an unchanged source/block inventory and "
                "be cited by a revised claim; its locator and scientific support still require original-source review. "
                "Time-window semantics include both complete versions and their pairing/reference consequences. "
                "New eligibility entries may only restrict existing targets and require their own source review. "
                "Pure evidence-ID edits keep their exact owners affected and unchanged typed direct uses as navigation "
                "context, not independently re-audited claims, unless "
                "an existing scientific blocker or another semantic change requires full dependency propagation. "
                "Existing evidence, scientific identity, primary numeric outcomes and source coverage are unchanged. "
                "Not scientific validation.")
        if source_requirement_policy is not None:
            result["source_requirement_policy"] = source_requirement_policy
            result["scope_limits"]["source_requirement_policy"] = source_requirement_policy
        result["scope_sha256"] = digest_json(result)
        return result
    except ScientificScopeRejected as exc:
        scope_rejection(diagnostics, exc.code, exc, exc.path)
        return None
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        scope_rejection(diagnostics, "unsafe_or_malformed_scope", exc)
        return None


def disposition_gaps(scope, groups):
    """Check a set of (record paths, source pairs); presence alone never approves a finding."""
    anchors = {x["evidence_id"]: (x["source_id"], x["block_id"]) for x in scope["source_anchors"]}
    gaps = []

    def check(kind, name, required, *, exact=False):
        path = _parts(name)
        matching = [pairs for paths, pairs in groups
                    if (path in paths if exact else any(_prefix(p, path) for p in paths))]
        missing = required.difference(*matching)
        if not matching or missing:
            gaps.append({"kind": kind, "record_path": name, "missing_source_blocks": sorted(missing)})

    for change in scope["changes"]:
        check("changed_claim", change["changed_path"], {anchors[x] for x in change["required_evidence_ids"]})
    for previous in scope["blocking_obligations"]:
        for path in previous["paths"]:
            check("previous_blocker", path, {tuple(x) for x in previous["source_blocks"]})
    for ref in scope["entity_refs"]:
        if ref["role"] == "affected":
            check("affected_entity", ref["record_path"], {anchors[x] for x in ref["required_evidence_ids"]}, exact=True)
    return gaps


def validate_scientific_revision_audit(scope, baseline_audit, new_audit, *, old_record, new_record, additional_audits=(),
                                       previous_step=None):
    """Recompile scope and require source-grounded dispositions for every change/blocker."""
    try:
        baseline, current = ReadingAudit.model_validate(baseline_audit), ReadingAudit.model_validate(new_audit)
        if scope.get("structural_followup"):
            from .ownership_followup import build_followup_scope
            if previous_step is None or len(additional_audits) != 1:
                return ["A structural follow-up requires the exact previous reviewed candidate and scope."]
            previous_scope, previous_record = previous_step
            expected = build_followup_scope(old_record, baseline, previous_record, new_record,
                                            additional_audits[0], previous_scope)
        else:
            expected = build_scientific_revision_scope(old_record, new_record, baseline,
                baseline_origin="fresh_model_audit", additional_audits=additional_audits,
                **scope["scope_limits"])
        if expected is None or expected != scope:
            return ["Scientific revision scope does not match the exact records and authenticated audits."]
        data = ExtractionRecord.model_validate(new_record).model_dump(mode="json")
        issues = []
        if (current.review_scope != "scientific_delta" or current.review_scope_sha256 != scope["scope_sha256"]
                or set(current.checked_dimensions) != set(AUDIT_DIMENSIONS)
                or len(current.checked_dimensions) != len(AUDIT_DIMENSIONS)):
            issues.append("Scientific revision audit lacks exact scope binding or nine cumulative dimensions.")
        parsed = []
        inherited_unresolved = {digest_json(f.model_dump(mode="json"))
            for audit in [baseline, *[ReadingAudit.model_validate(a) for a in additional_audits]]
            for f in audit.findings if f.verdict == "unresolved" and not f.needs_revision}
        entities = _entities(data, include_evidence=scope["scope_limits"].get("compiler_version", 1) >= 3)
        for finding in current.findings:
            try:
                paths = finding_paths(finding, data)
                for path in paths:
                    _owner(path, entities)  # collection-wide blanket claims are not a disposition
            except ValueError:
                paths = []
                if digest_json(finding.model_dump(mode="json")) not in inherited_unresolved:
                    issues.append("Audit finding has an unresolvable or non-entity path: " + finding.record_path)
            parsed.append((paths, finding))
            if finding.verdict == "not_checked":
                issues.append("A scoped claim remains unchecked: " + finding.record_path)
        messages = {"changed_claim": "Changed claim lacks explicit source review: ",
                    "previous_blocker": "Previous blocking claim was not reconsidered: ",
                    "affected_entity": "Affected entity/dependency lacks explicit source disposition: "}
        groups = [(paths, {(a.source_id, a.block_id) for a in finding.evidence}) for paths, finding in parsed]
        issues.extend(messages[gap["kind"]] + gap["record_path"] for gap in disposition_gaps(scope, groups))
        fingerprints = {digest_json(f.model_dump(mode="json")) for f in current.findings}
        for audit in [baseline, *[ReadingAudit.model_validate(x) for x in additional_audits]]:
            for finding in audit.findings:
                if (finding.verdict == "unresolved" and not finding.needs_revision
                        and digest_json(finding.model_dump(mode="json")) not in fingerprints):
                    issues.append("Inherited unresolved finding was dropped: " + finding.record_path)
            if not set(audit.coverage_limitations).issubset(current.coverage_limitations):
                issues.append("Inherited source limitations were removed.")
            if not {digest_json(x.model_dump(mode="json")) for x in audit.quantitative_followups}.issubset(
                    {digest_json(x.model_dump(mode="json")) for x in current.quantitative_followups}):
                issues.append("Inherited optional followups were removed.")
        return issues
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return ["Malformed scientific revision proof; no scoped approval is possible."]
