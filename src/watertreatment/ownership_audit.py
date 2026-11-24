"""Compile a source-bound review when revisions add subjects or change scientific ownership."""
from __future__ import annotations

from collections import defaultdict

from .citation_audit import _evidence_ids, _parts, _path, _prefix, _resolve
from .domain import ExtractionRecord
from .reading import ReadingAudit
from .scientific_revision_audit import (DESIGN_SECTIONS, MAX_NAVIGATION_ENTITIES, POLICY, SECTIONS,
    ScientificScopeRejected, _change, _change_evidence_ids, _entities, _full_coverage, _owner,
    _validate_evidence_additions, finding_paths)
from .storage import digest_json
from .validation import validate_record


MODE = "ownership-and-controls-v1"
TARGETS = {"experiment": "experiments", "material": "materials", "configuration": "configurations",
           "observation": "observations", "comparison": "comparisons", "series": "quantitative_coverage"}
REGISTERED = (*SECTIONS, "evidence")


def configuration_context_changed(before, after):
    """Select a dependency review for a textual apparatus/protocol clarification."""
    for old, new in zip(before["configurations"], after["configurations"]):
        a, b = old["boundary"], new["boundary"]
        if (old["id"] == new["id"] and a["value"] != b["value"]
                and all(isinstance(v, (str, type(None))) for v in (a["value"], b["value"]))
                and a["normalized"] == b["normalized"] and b["evidence_ids"]):
            return True
    return False


def check_contextual_edits(before, after, *, include_existing_interpretations=False):
    """Never let a simultaneous configuration edit admit an unrelated outcome or ID."""
    allowed = {"configurations": {"boundary", "streams"}, "experiments": {"conditions", "time_window", "evidence_ids", "pairs", "joint_evidence"},
        "observations": {"coordinates", "time_window", "limitations", "evidence_links", "evidence_ids"},
        "comparisons": {"limitations", "held_conditions", "changed_conditions", "outcomes", "evidence_ids"},
        "quantitative_coverage": {"limitations"}, "core_checks": {"limitations", "evidence_ids"},
        "source_index": {"limitations", "evidence_ids"}, "eligibility": {"limitations", "evidence_ids"}}
    previous, current = None, None
    for change in _changes(before, after):
        path = _parts(change["changed_path"])
        if path[0] == "reference_associations" and len(path) == 2:
            a, b = change["old_value"], change["new_value"]
            if all(a[k] == b[k] for k in ("id", "target_kind", "target_id", "observation_ids", "relationship")):
                continue
        section, parts = (path[1], path[2:]) if path[0] == "design_scope" else (path[0], path[1:])
        if len(parts) < 2 or parts[1] not in allowed.get(section, set()):
            if include_existing_interpretations and len(parts) >= 2 and (
                    (section == "materials" and parts[1] == "components" and "roles" in parts)
                    or (section == "observations" and parts[1] in {"metric", "unit", "value"})):
                # In a cumulative delta, a previously permitted typed
                # interpretation may coexist with a new apparatus clarification.
                # Apply the established scientific field policy to both exact values.
                from .scientific_revision_audit import _changes as interpretation_changes
                if previous is None:
                    previous = ExtractionRecord.model_validate(before)
                    current = ExtractionRecord.model_validate(after)
                a, b = previous, current
                for token in path:
                    a, b = (a[token], b[token]) if isinstance(token, int) else (getattr(a, token), getattr(b, token))
                interpretation_changes(a, b, path, compiler_version=3)
                continue
            raise ScientificScopeRejected("unscoped_context_edit", "Change is outside the supported contextual fields.", path)
        if section == "configurations" and parts[1] == "streams" and parts[3:4] != ("description",):
            raise ScientificScopeRejected("protected_stream_identity", "Stream identity cannot change in an apparatus description repair.", path)
        if section == "experiments" and parts[1] == "joint_evidence" and parts[2:] not in {("evidence_ids",), ("limitations",)}:
            raise ScientificScopeRejected("protected_operation", "Joint-operation identity cannot be inferred from a timing edit.", path)
        if section == "experiments" and parts[1] == "pairs":
            def pair_keys(record):
                return [(p["left_observation_id"], p["right_observation_id"], p["level"]) for p in record[section][parts[0]]["pairs"]]
            if pair_keys(before) != pair_keys(after):
                raise ScientificScopeRejected("protected_pair_identity", "Endpoints and pairing levels require separate evidence.", path)
        if section == "comparisons" and parts[1] == "outcomes":
            original = before[section][parts[0]]["outcomes"]
            revised = after[section][parts[0]]["outcomes"]
            if [(v["name"], v["fact"]["value"], v["fact"]["normalized"]) for v in original] != [
                    (v["name"], v["fact"]["value"], v["fact"]["normalized"]) for v in revised]:
                raise ScientificScopeRejected("protected_primary_outcome", "Comparison outcome changed.", path)
DESIGN_IDS = {"core_checks": ("area",), "observation_assignments": ("observation_id",),
              "eligibility": ("use", "target_kind", "target_id")}
DEPENDENCY_FIELDS = {
    "materials": ("name", "role", "family_id"),
    "substances": ("name", "identity_scope"),
    "configurations": ("material_ids", "placements", "streams", "boundary"),
    "experiments": ("reported_group", "material_ids", "configuration_id", "conditions", "pairs", "joint_evidence", "time_window"),
    "observations": ("experiment_id", "material_id", "substance_id", "stream_id", "value", "unit", "coordinates", "time_window", "origin"),
    "comparisons": ("material_ids", "observation_ids", "held_conditions", "changed_conditions", "outcomes", "counterevidence"),
    "quantitative_coverage": ("locator", "observation_ids", "state"),
    "reference_associations": ("target_kind", "target_id", "observation_ids", "relationship", "applicability", "limitations"),
}


def ownership_changed(before, after):
    """Only a real membership/ownership change enters the structural protocol."""
    if before.get("rule_version") == after.get("rule_version") == "0.9":
        research_fields = {"materials": ("components", "preparation", "classification", "morphology"),
            "experiments": ("arm_kind", "comparison_group", "reaction_inputs", "targets", "pairs", "joint_evidence"),
            "observations": ("endpoint_kind", "removal_attribution"), "comparisons": ("contrasts",)}
        for section, names in research_fields.items():
            if section == "comparisons" and len(before.get(section, [])) != len(after.get(section, [])):
                return True
            for a, b in zip(before.get(section, []), after.get(section, [])):
                if any(a.get(name) != b.get(name) for name in names):
                    return True
    for section, ownership in (("materials", ("family_id", "role")), ("experiments", ("material_ids", "configuration_id")),
            ("observations", ("experiment_id", "material_id", "substance_id", "series_id", "stream_id"))):
        left, right = before.get(section, []), after.get(section, [])
        if len(left) != len(right) or any(
                a["id"] != b["id"] or any(a.get(k) != b.get(k) for k in ownership) for a, b in zip(left, right)):
            return True
        if section == "experiments" and any(
                [state["material_id"] for state in a.get("material_states", [])] !=
                [state["material_id"] for state in b.get("material_states", [])] for a, b in zip(left, right)):
            return True
    return False


def _members(section, rows):
    fields = (("module", "version") if section == "extensions" else DESIGN_IDS.get(section, ("id",)))
    return [tuple(row[k] for k in fields) for row in rows]


def _field_changes(old, new, path):
    if old == new:
        return []
    # Scientific Facts and evidence-bearing relations remain intelligible units.
    # A changed collection is kept whole when its ordinal correspondence changes.
    if isinstance(old, dict) and isinstance(new, dict):
        if "status" in old and "value" in old or path[-1] in {"locator", "time_window"}:
            return [_change(path, "scientific_field", old, new)]
        if set(old) == set(new):
            return [change for key in sorted(old) for change in _field_changes(old[key], new[key], (*path, key))]
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        if (old and all(isinstance(x, dict) for x in old + new)
                and path[-1] not in {"pairs", "evidence_links"}
                and all(a.get(k) == b.get(k) for a, b in zip(old, new)
                        for k in ("id", "name", "material_id", "substance_id", "role"))):
            return [change for i, (a, b) in enumerate(zip(old, new))
                    for change in _field_changes(a, b, (*path, i))]
    return [_change(path, "scientific_field", old, new)]


def literal_quote_extension(previous, current):
    """Identify a longer passage at the same physical source location."""
    from .validation import _text

    new_quote = current.get("quote")
    old_quote = previous.get("quote") or ""
    if not isinstance(new_quote, str) or not new_quote.strip() or new_quote == old_quote:
        return False
    if _text(old_quote) not in _text(new_quote):
        return False
    if any(previous.get(k) != current.get(k) for k in (previous.keys() | current.keys()) - {"quote", "locator"}):
        return False
    old_loc = previous.get("locator") or {}
    new_loc = current.get("locator") or {}
    if any(old_loc.get(k) != new_loc.get(k) for k in (old_loc.keys() | new_loc.keys()) - {"panel"}):
        return False
    return bool(old_quote.strip() or old_loc != new_loc)


def _changes(before, after, *, adjudicate_outcomes=False, review_source_edits=False, review_quote_extensions=False):
    roots = set(REGISTERED) | {"design_scope"}
    changes = []
    if review_source_edits:
        roots.add("coverage")
        old_rows, new_rows = before["coverage"], after["coverage"]
        if len(old_rows) != len(new_rows):
            raise ScientificScopeRejected("changed_reading_claim", "Reading ledger membership changed.")
        for i, (a, b) in enumerate(zip(old_rows, new_rows)):
            if a == b:
                continue
            if not a["block_ids"] or any(a.get(k) != b.get(k) for k in (a.keys() | b.keys()) - {"note"}):
                raise ScientificScopeRejected("changed_reading_claim", "A note review cannot alter reading claims or source membership.")
            changes.append(_change(("coverage", i, "note"), "coverage_note", a["note"], b["note"]))
    if any(before.get(k) != after.get(k) for k in (before.keys() | after.keys()) - roots):
        raise ScientificScopeRejected("protected_record_or_sources", "Paper identity, scope and original reading coverage must be unchanged.")
    old_design, new_design = before.get("design_scope") or {}, after.get("design_scope") or {}
    if old_design.get("version") != new_design.get("version"):
        raise ScientificScopeRejected("protected_design_scope", "The research scope contract changed.")
    collections = [((s,), before.get(s, []), after.get(s, [])) for s in REGISTERED]
    collections.extend((("design_scope", s), old_design.get(s, []), new_design.get(s, [])) for s in DESIGN_SECTIONS)
    for path, old, new in collections:
        section = path[-1]
        old_ids, new_ids = _members(section, old), _members(section, new)
        if new_ids[:len(old_ids)] != old_ids or (section != "extensions" and len(set(new_ids)) != len(new_ids)):
            raise ScientificScopeRejected("unstable_entity_identity", "Existing subjects must keep stable IDs and order; deletion or ambiguous identity needs a different review.", path)
        for i, (a, b) in enumerate(zip(old, new)):
            if a == b:
                continue
            key = (*path, i)
            if section == "evidence":
                if (review_source_edits and before.get("rule_version") == after.get("rule_version") == "0.9"
                        and not a.get("quote") and isinstance(b.get("quote"), str) and b["quote"].strip()
                        and all(a.get(k) == b.get(k) for k in (a.keys() | b.keys()) - {"quote"})):
                    changes.append(_change(key, "located_quote_addition", a, b))
                    continue
                if (review_source_edits and a["source_id"] == b["source_id"] and a["id"] == b["id"]
                        and all(a.get(k) == b.get(k) for k in (a.keys() | b.keys()) - {"block_id", "locator"})):
                    changes.append(_change(key, "evidence_relocation", a, b))
                    continue
                if (review_source_edits and review_quote_extensions and before.get("rule_version") == after.get("rule_version") == "0.9"
                        and literal_quote_extension(a, b)):
                    changes.append(_change(key, "literal_quote_extension", a, b))
                    continue
                raise ScientificScopeRejected("existing_evidence_changed", "A previously registered evidence definition changed.", key)
            if section in {"source_index", "quantitative_coverage"} and any(a.get(k) != b.get(k) for k in ("source_id", "block_id")):
                raise ScientificScopeRejected("changed_source_identity", "A source series or panel no longer has its original source/block.", key)
            if section == "source_index" and a["reading_state"] != b["reading_state"]:
                raise ScientificScopeRejected("changed_reading_claim", "Scientific ownership review cannot change a reading disposition.", key)
            if section == "observations":
                for field in ("value", "unit"):
                    if all(a[field].get(k) == b[field].get(k) for k in ("value", "normalized")):
                        continue
                    if not adjudicate_outcomes:
                        raise ScientificScopeRejected("protected_primary_outcome", "An existing primary value/unit changed. Ownership review cannot certify its replacement.", key)
                    if not (b[field].get("evidence_ids") or any(field in link["supported_fields"] and
                            link["support_type"] in {"direct", "conflict"} for link in b.get("evidence_links", []))):
                        raise ScientificScopeRejected("unanchored_outcome_revision", "Changed outcomes require an explicit source for the proposed value or limitation.", (*key, field))
            if section == "reference_associations":
                changes.append(_change(key, "control_relationship", a, b))
                continue
            for field in sorted(a.keys() | b.keys()):
                if field in a and field not in b:
                    # Legacy serializers elide optional empty values. Use a
                    # resolvable whole-entity path, preserving both full versions.
                    changes.append(_change(key, "scientific_entity", a, b))
                    break
            else:
                for field in sorted(a.keys() | b.keys()):
                    changes.extend(_field_changes(a.get(field), b.get(field), (*key, field)))
        for i in range(len(old), len(new)):
            item = new[i]
            if section == "observations" and item["value"]["value"] is not None and not (
                    item["value"].get("evidence_ids") or any("value" in link["supported_fields"]
                    and link["support_type"] in {"direct", "conflict"} for link in item.get("evidence_links", []))):
                raise ScientificScopeRejected("unanchored_new_value", "A newly added measurement needs explicit primary value evidence.", (*path, i))
            changes.append(_change((*path, i), "evidence_addition" if section == "evidence" else "entity_addition", None, item))
    return changes


def _links(entities):
    """Typed edges only. Identical names, formulae and experimental maxima create no edge."""
    index = {(key[0], row["id"]): key for key, row in entities.items() if len(key) == 2 and "id" in row}
    modules = {row["id"]: key for key, row in entities.items() if key[:2] == ("design_scope", "modules")}
    edges = {}
    for key, row in entities.items():
        links = set()

        def add(section, *ids):
            for identifier in ids:
                if identifier is not None:
                    target = index.get((section, identifier))
                    if target is None:
                        raise ScientificScopeRejected("dangling_structural_reference", "Typed relationship has no unique registered target.", key)
                    links.add(target)

        for field, section in (("material_id", "materials"), ("experiment_id", "experiments"),
                ("substance_id", "substances"), ("configuration_id", "configurations"),
                ("series_id", "quantitative_coverage"), ("observation_id", "observations")):
            add(section, row.get(field))
        for field, section in (("material_ids", "materials"), ("experiment_ids", "experiments"),
                ("observation_ids", "observations"), ("retained_observation_ids", "observations"),
                ("quantitative_coverage_ids", "quantitative_coverage"), ("accessory_substance_ids", "substances")):
            add(section, *row.get(field, ()))
        if row.get("target_kind") in TARGETS:
            add(TARGETS[row["target_kind"]], row["target_id"])
        for field in ("placements", "material_states"):
            add("materials", *(entry["material_id"] for entry in row.get(field, ())))
        for field in ("components", "targets", "reaction_inputs"):
            add("substances", *(entry.get("substance_id") for entry in row.get(field, ())))
        add("materials", *(entry.get("material_id") for entry in row.get("reaction_inputs", ())))
        for step in row.get("preparation", ()):
            add("substances", *step.get("input_substance_ids", ()))
        for pair in row.get("pairs", ()):
            add("observations", pair["left_observation_id"], pair["right_observation_id"])
        for contrast in row.get("contrasts", ()):
            add("observations", contrast["baseline_observation_id"], contrast["modified_observation_id"])
        if row.get("module_id"):
            links.add(modules[row["module_id"]])
        edges[key] = links
    return edges


def _claim_without_citations(value):
    """Keep scientific meaning, uncertainty and the fields covered by each strength of evidence."""
    if isinstance(value, list):
        return [_claim_without_citations(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {k: _claim_without_citations(v) for k, v in value.items() if k not in {"evidence_ids", "evidence_links"}}
    if "evidence_links" in value:
        result["evidence_links"] = sorted({(link["support_type"], field)
                                           for link in value["evidence_links"] for field in link["supported_fields"]})
    return result


def _dependencies(old, new, changed, blockers):
    left, right = _links(old), _links(new)
    links = {key: left.get(key, set()) | right[key] for key in new}
    affected = set(changed) | set(blockers)
    # Downstream propagation starts ONLY from changed scientific owners.
    # Retrieving the former owner of one measurement must not reseed every
    # measurement ever made with the same material.
    seeds = {k for k in changed if k[0] in ("substances", "materials", "configurations", "experiments", "observations")}
    substances = {k for k in seeds if k[0] == "substances"}
    materials = {k for k in links if k[0] == "materials" and (k in seeds or links[k] & substances)}
    material_ids = {new[k]["id"] for k in materials}
    configs = {k for k in seeds if k[0] == "configurations"}
    for key, edges in links.items():
        if key[0] == "configurations" and edges & (substances | materials):
            affected.add(key)
            # A catalog of tested variants is not an assertion that each test
            # installed every variant. A changed installed component can affect
            # the entire apparatus; an explicitly named test sample is local.
            placed = {p["material_id"] for row in (old.get(key, {}), new[key]) for p in row.get("placements", [])}
            if placed & material_ids or edges & substances:
                configs.add(key)
    experiments = {k for k in links if k[0] == "experiments" and (k in seeds or links[k] & (substances | configs))}
    material_experiments = {k for k in links if k[0] == "experiments" and links[k] & materials}
    # Compare affected owners against their explicitly registered sibling arms,
    # including both sides of a reassignment. Fetch only these observations.
    arm_owners = set(experiments) | material_experiments
    for key in changed:
        if key[0] == "observations":
            arm_owners.update(k for k in links[key] if k[0] == "experiments")
        if key[0] == "comparisons" and new[key].get("contrasts") != old.get(key, {}).get("contrasts"):
            affected.update(k for k in links[key] if k[0] == "observations")
    groups = {r["comparison_group"] for key in arm_owners for r in (old.get(key, {}), new[key]) if r.get("comparison_group")}
    if groups:
        siblings = {k for k in links if k[0] == "experiments" and k not in arm_owners and any(
            row.get("comparison_group") in groups for row in (old.get(k, {}), new[k]))}
        affected.update(siblings)
        def endpoint(row):
            return (row.get("endpoint_kind"), row.get("metric_kind") if row.get("endpoint_kind") in {None, "other", "unresolved"} else None,
                    row.get("substance_id"))
        relevant = {endpoint(r) for k in links if k[0] == "observations" and
                    (k in changed or links[k] & (experiments | material_experiments))
                    for r in (old.get(k, {}), new[k]) if r}
        affected.update(k for k in links if k[0] == "observations" and links[k] & siblings
                        and any(endpoint(r) in relevant for r in (old.get(k, {}), new[k]) if r))
    for key in links:
        if key[0] == "experiments":
            for row in (old.get(key, {}), new[key]):
                if row and not row.get("material_ids") and any(
                        c[0] == "configurations" and links[c] & materials for c in links[key]):
                    experiments.add(key)
    seeds.update(materials | configs | experiments)
    seeds.update(k for k, edges in links.items() if k[0] == "observations" and edges & (materials | experiments | substances))
    for key, edges in links.items():
        if key[0] == "observations" and edges & material_experiments and any(
                row and row.get("material_id") is None for row in (old.get(key), new[key])):
            seeds.add(key)
    affected.update(seeds | material_experiments)
    changed_links = {k for k in changed if left.get(k, set()) != right[k]}
    # Old and new owners, pairs and control endpoints both need a disposition.
    owner_targets = set()
    for key in changed_links:
        if key[0] == "observations":
            owner_targets.update(k for k in left.get(key, set()) ^ right[key] if k[0] in {"experiments", "quantitative_coverage"})
        affected.update(k for k in (left.get(key, set()) ^ right[key]) if k[0] in
                        {"observations", "experiments", "materials", "configurations", "comparisons", "quantitative_coverage"})
    endpoints = {k for k in affected if k[0] == "observations"}
    for key in changed:
        if key[0] == "reference_associations":
            endpoints.update(k for k in links[key] if k[0] == "observations")
            affected.update(links[key])
    for key, row in new.items():
        if key[0] != "experiments":
            continue
        for version in (old.get(key, {}), row):
            for pair in version.get("pairs", []):
                ids = {pair["left_observation_id"], pair["right_observation_id"]}
                mates = {k for k in links[key] if k[0] == "observations" and new[k]["id"] in ids}
                if mates & endpoints or key in changed and old.get(key, {}).get("pairs") != row.get("pairs"):
                    affected.add(key)
                    affected.update(mates)
    # Distinguish a control whose data/ownership changed from an unchanged
    # control whose *use at one target* changed. Verify other uses of that
    # exact control without re-seeding the unrelated arms of those experiments.
    for key, edges in links.items():
        if key[0] in {"comparisons", "quantitative_coverage"} and edges & (seeds | endpoints):
            affected.add(key)
    targets = set(affected)
    for key, edges in links.items():
        if key[0] != "reference_associations" or not (key in changed or edges & targets):
            continue
        affected.add(key)
        affected.update(k for k in edges if k[0] != "observations" or key in changed or k in endpoints)
        # For a changed target, examine the controls supporting that target;
        # the full set of control IDs remains visible in the association.
        if any(k in (changed | owner_targets) for k in edges if k[0] != "observations"):
            affected.update(edges)
    subjects = set(affected)
    for key, edges in links.items():
        if (key[0] == "extensions" or key[:2] == ("design_scope", "eligibility")) and edges & subjects:
            affected.add(key)
    context = set()
    for key in affected:
        context.update(links[key])
    for key, edges in links.items():
        if key[0] == "design_scope" and edges & subjects:
            context.add(key)
    return affected, context - affected, links


def build_ownership_scope(old, new, baseline, audits, limits):
    before, after = old.model_dump(mode="json"), new.model_dump(mode="json")
    if not _full_coverage(baseline, before) or len(audits) > 4 or any(
            a.review_scope not in {"scientific_delta", "citation_delta"} or any(f.verdict == "not_checked" for f in a.findings)
            for a in audits[1:]):
        raise ScientificScopeRejected("invalid_structural_baseline", "A complete located baseline and authenticated contiguous local audits are required.")
    changes = _changes(before, after)
    if not 1 <= len(changes) <= limits["max_changes"]:
        raise ScientificScopeRejected("change_budget", f"{len(changes)} scientific changes exceed the bounded protocol.")
    _validate_evidence_additions(old, new, changes)
    old_errors = [i for i in validate_record(old) if i.severity == "error"]
    if any(i.severity == "error" for i in validate_record(new)):
        raise ScientificScopeRejected("invalid_structural_candidate", "The revised scientific record must pass machine validation before review.")
    resolved_errors = []
    if old_errors:
        # A located arm/dose error in an audited baseline may be the very
        # finding being repaired. No unrelated error or broken reference passes.
        repairable = {"EXTERNAL_DOSE_UNSUPPORTED", "ARM_SUPPLY_CONFLICT", "BOUND_SUPPLY_UNSUPPORTED",
                      "DOSE_EVIDENCE_SCOPE", "ABSENCE_SCOPE_SOURCE", "PARENT_FATE_UNCLASSIFIED", "ENDPOINT_UNCLASSIFIED"}
        changed_paths = [_parts(c["changed_path"]) for c in changes]
        for issue in old_errors:
            path = _parts(issue.path)
            if (old.rule_version != "0.9" or issue.code not in repairable or not any(
                    _prefix(path, changed_path) or _prefix(changed_path, path) for changed_path in changed_paths)):
                raise ScientificScopeRejected("invalid_structural_candidate", "An original error is outside the verified arm repair.", path)
            resolved_errors.append({"code": issue.code, "path": issue.path})
    old_rows, rows = (_entities(data, include_evidence=True) for data in (before, after))
    changed = {_owner(_parts(c["changed_path"]), rows) for c in changes}
    obligations, blockers = [], set()
    for ai, audit in enumerate(audits):
        located = set()
        for fi, finding in enumerate(audit.findings):
            if not (finding.needs_revision or finding.verdict == "unsupported"):
                continue
            original = finding_paths(finding, before if ai == 0 else after)
            paths = []
            for path in original:
                # A surviving index may now denote a different condition/member.
                # Retain the historical address but review the changed container.
                containers = [_parts(c["changed_path"]) for c in changes
                              if isinstance(c["old_value"], list) and _prefix(_parts(c["changed_path"]), path)]
                if containers:
                    path = max(containers, key=len)
                else:
                    try:
                        _resolve(after, path)
                    except (ValueError, IndexError, KeyError, TypeError):
                        ancestors = [_parts(c["changed_path"]) for c in changes if _prefix(_parts(c["changed_path"]), path)]
                        if not ancestors:
                            raise ScientificScopeRejected("unlocated_structural_blocker", "A historical finding lost its target without an explicit parent change.", path)
                        path = max(ancestors, key=len)
                if path not in paths:
                    paths.append(path)
                blockers.add(_owner(path, rows))
            if not finding.evidence:
                raise ScientificScopeRejected("unanchored_blocker", "An inherited blocker has no original source.")
            located.update(finding.omission_indices)
            obligations.append({"audit_index": ai, "finding_index": fi, "paths": [_path(p) for p in paths],
                "historical_paths": [_path(p) for p in original],
                "source_blocks": [list(pair) for pair in sorted({(e.source_id, e.block_id) for e in finding.evidence})]})
        if located != set(range(len(audit.potential_omissions))):
            raise ScientificScopeRejected("unlocated_omission", "Every prior omission needs a located finding.")
    # An edited source attribution must itself be checked on both original sources.
    # When the arm, claim and strength of support are unchanged it does not
    # invalidate every other result measured with that arm or material.
    citation_edits = set()
    if limits.get("compiler_version") == 6:
        citation_edits = {key for key in changed if key in old_rows and
                          _claim_without_citations(old_rows[key]) == _claim_without_citations(rows[key])}
    affected, context, links = _dependencies(old_rows, rows, changed - citation_edits, blockers | citation_edits)
    if len(affected) > limits["max_entities"] or len(context) > limits.get("max_context_entities", limits["max_entities"]) or len(affected | context) > MAX_NAVIGATION_ENTITIES:
        raise ScientificScopeRejected("entity_budget", f"Necessary affected/context entities {len(affected)}/{len(context)} exceed the bounded review limits.")
    inventory = {(entry.source_id, bid) for entry in old.coverage for bid in entry.block_ids}
    evidence = {e.id: e for e in new.evidence}
    by_block = defaultdict(set)
    for eid, e in evidence.items():
        by_block[e.source_id, e.block_id].add(eid)
    anchors = {}

    def source_anchor(pair, locator=None):
        if pair not in inventory:
            raise ScientificScopeRejected("unregistered_structural_source", "Every structural audit location must belong to the unchanged covered source inventory.")
        eid = "audit-source:" + digest_json([*pair, locator or {}])
        if eid in evidence:
            raise ScientificScopeRejected("ambiguous_source_id", "Synthetic navigation ID collides with registered evidence.")
        anchors[eid] = {"evidence_id": eid, "source_id": pair[0], "block_id": pair[1],
                        "locator": locator or {}, "origin": "structural_audit_block"}
        return eid

    def register(ids):
        for eid in ids:
            if eid in anchors:
                continue
            e = evidence[eid]
            if (e.source_id, e.block_id) not in inventory:
                raise ScientificScopeRejected("unregistered_structural_source", "Required science evidence lies outside the unchanged source coverage.")
            anchors[eid] = {"evidence_id": eid, "source_id": e.source_id, "block_id": e.block_id,
                            "locator": e.locator.model_dump(mode="json")}
        return sorted(ids)

    changed_fields = defaultdict(set)
    for change in changes:
        path = _parts(change["changed_path"])
        key = _owner(path, rows)
        changed_fields[key].add(path[len(key)] if len(path) > len(key) else "*")
    blocker_fields = defaultdict(set)
    for obligation in obligations:
        for name in obligation["paths"]:
            path = _parts(name)
            owner = _owner(path, rows)
            blocker_fields[owner].add(path[len(owner)] if len(path) > len(owner) else "*")
    own, fields = {}, {}
    for key in affected | context:
        section = key[-2] if key[0] == "design_scope" else key[0]
        fields[key] = sorted(changed_fields[key] | set(DEPENDENCY_FIELDS.get(section, ())))
        if new.rule_version == "0.9":
            typed_context = {"experiments": ("arm_kind", "comparison_group", "reaction_inputs", "targets", "joint_evidence", "pairs"),
                "materials": ("components",), "observations": ("endpoint_kind", "removal_attribution", "time_window"),
                "comparisons": ("contrasts",)}
            fields[key] = sorted(set(fields[key]) | {k for k in typed_context.get(section, ()) if k in rows[key]})
        local_citations = key in citation_edits and "*" not in changed_fields[key] | blocker_fields[key]
        if local_citations:
            fields[key] = sorted(changed_fields[key] | blocker_fields[key])
        ids = set()
        for row in (old_rows.get(key, {}), rows[key]):
            if "*" in fields[key] or section not in DEPENDENCY_FIELDS:
                ids.update(_evidence_ids(row))
            else:
                row_ids = set()
                if not local_citations or "evidence_ids" in fields[key]:
                    row_ids.update(row.get("evidence_ids", ()))
                for field in fields[key]:
                    row_ids.update(_evidence_ids(row.get(field)))
                for link in row.get("evidence_links", ()):
                    if "evidence_links" in fields[key] or set(link["supported_fields"]) & set(fields[key]):
                        row_ids.add(link["evidence_id"])
                if local_citations and not row_ids:
                    row_ids.update(row.get("evidence_ids", ()))
                ids.update(row_ids)
            if row.get("source_id") and row.get("block_id"):
                ids.add(source_anchor((row["source_id"], row["block_id"]), row.get("locator")))
            if key[0] == "evidence" and row:
                ids.add(row["id"])
        own[key] = ids
    refs, bound_ids = [], {}
    for key in sorted(affected | context, key=_path):
        ids = set(own[key])
        if key in affected and not ids:
            for target in links[key]:
                ids.update(own.get(target, ()))
        if key in affected and not ids:
            raise ScientificScopeRejected("unanchored_structural_entity", "An affected subject or relationship lacks original-source evidence.", key)
        refs.append({"record_path": _path(key), "role": "affected" if key in affected else "context",
                     "entity_id": rows[key].get("id", rows[key].get("target_id", rows[key].get("observation_id"))),
                     "review_fields": fields[key] if key in affected else [],
                     "old_hash": digest_json(old_rows.get(key)), "new_hash": digest_json(rows[key]),
                     "required_evidence_ids": register(ids) if key in affected else []})
        if key in affected:
            bound_ids[key] = ids
    for change in changes:
        key = _owner(_parts(change["changed_path"]), rows)
        ids = _change_evidence_ids(change, "old_value") | _change_evidence_ids(change, "new_value")
        change["required_evidence_ids"] = register(ids or bound_ids[key])
    for obligation in obligations:
        for pair in map(tuple, obligation["source_blocks"]):
            if by_block[pair]:
                register({min(by_block[pair])})
            # A prior blocking figure may cite only the source block. A text
            # evidence ID on the same PDF page must not suppress its full view.
            blocker_id = source_anchor(pair)
            anchors[blocker_id]["origin"] = "prior_audit_blocker"
    scope = {"policy": POLICY, "structural_review": MODE, "baseline_origin": "fresh_model_audit",
        "old_record_hash": digest_json(before), "new_record_hash": digest_json(after),
        "baseline_audit_hash": digest_json(baseline.model_dump(mode="json")),
        "additional_audit_hashes": [digest_json(a.model_dump(mode="json")) for a in audits[1:]],
        "changes": changes, "changed_paths": [c["changed_path"] for c in changes], "entity_refs": refs,
        "source_anchors": [anchors[k] for k in sorted(anchors)], "blocking_obligations": obligations,
        "scope_limits": limits, "interpretation": "Bounded source review of additions, previous and current ownership, their controls, pairs, comparisons and eligibility. Retrieval or scope construction supplies no scientific approval."}
    if limits.get("source_requirement_policy"):
        scope["source_requirement_policy"] = limits["source_requirement_policy"]
    if resolved_errors:
        scope["baseline_errors_for_source_review"] = resolved_errors
    scope["scope_sha256"] = digest_json(scope)
    return scope
