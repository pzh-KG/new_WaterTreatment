"""Immutable research projections, independent of extraction/audit admission.

Legacy records require an explicit, hash-bound scope mapping. No metric names,
paper identifiers, numeric thresholds or material formulas select a layer here.
The full source record remains the authoritative archive; this is not a new
ExtractionRecord and does not inherit an audit as evidence of a new rule run.
"""
from __future__ import annotations

from .domain import DESIGN_VERSIONS

import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from .storage import digest_json


VIEW_VERSION = "research-core-view-v2"
LEGACY_VIEW_VERSION = "research-core-view-v1"
MAPPING_VERSION = "research-core-mapping-v1"
MANIFEST_VERSION = "research-core-view-manifest-v1"
_FINAL_ROLES = {"primary_final", "optimized_variant", "final_component"}


def _plain(record):
    result = record.model_dump(mode="json") if hasattr(record, "model_dump") else record
    if not isinstance(result, dict):
        raise ValueError("A record object is required")
    return copy.deepcopy(result)


def _sha(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{name} must be a lowercase SHA-256")


def _unique(items, name):
    identifiers = [item.get("id") for item in items]
    if any(not isinstance(value, str) or not value for value in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError(f"{name} requires unique nonempty IDs")
    return dict(zip(identifiers, items))


def _protected(record):
    """Protect explicit design/stability/key ownership, without inferring science."""
    result = {o["id"] for o in record.get("observations", [])
              if o.get("analysis_role") in {"stability", "key_result", "design_control"}}
    for comparison in record.get("comparisons", []):
        result.update(comparison.get("observation_ids", []))
    for association in record.get("reference_associations", []):
        result.update(association.get("observation_ids", []))
    for extension in record.get("extensions", []):
        if extension.get("observation_id"):
            result.add(extension["observation_id"])
    return result


def _native_mapping(record, source_sha256):
    """Build the only accepted native mapping from the actual typed contract."""
    if record.get("rule_version") not in DESIGN_VERSIONS or record.get("schema_version") != DESIGN_VERSIONS[record["rule_version"]] or not record.get("design_scope"):
        raise ValueError("Native core projection requires a supported rule/schema design scope")
    from .domain import ExtractionRecord
    from .design_scope import design_scope_issues
    issues = design_scope_issues(ExtractionRecord.model_validate(record))
    if any(issue.severity == "error" for issue in issues):
        raise ValueError("Native design scope failed structural validation")
    scope = record["design_scope"]
    return {"version": MAPPING_VERSION, "policy_version": record["rule_version"], "basis": "native_extraction_scope",
        "source_record_sha256": source_sha256, "source_record_fingerprint": digest_json(record),
        "modules": copy.deepcopy(scope["modules"]), "limitations": [
            "Projection of the extracted scope; it does not constitute another source audit or scientific validation."],
        "decisions": [{"observation_id": a["observation_id"], "layer": {"core": "A", "module": "B", "detail": "C"}[a["layer"]],
            "purpose": a["purpose"], "reason": a["reason"],
            "module": a.get("module_id") if a["layer"] == "module" else None} for a in scope["observation_assignments"]]}


def _mapping(record, mapping, source_sha256):
    fingerprint = digest_json(record)
    observations = _unique(record.get("observations", []), "observations")
    if mapping is None:
        return {"version": MAPPING_VERSION, "policy_version": "0.8",
            "basis": "unclassified_fallback", "source_record_sha256": source_sha256,
            "source_record_fingerprint": fingerprint, "modules": [], "limitations": [
                "No scope mapping exists. All observations are conservatively retained; layer selection has not been assessed."],
            "decisions": [{"observation_id": identifier, "layer": "A", "purpose": "unclassified",
                "reason": "Retained because no explicit scope decision exists.", "module": None}
                for identifier in observations]}
    mapping = copy.deepcopy(mapping)
    if mapping.get("basis") == "native_extraction_scope" and record.get("rule_version") not in DESIGN_VERSIONS:
        raise ValueError("Native core projection requires a rule 0.8 or 0.9 design scope matching its source.")
    if mapping.get("version") != MAPPING_VERSION or mapping.get("policy_version") != (record.get("rule_version") if mapping.get("basis") == "native_extraction_scope" else "0.8"):
        raise ValueError("Unsupported research core mapping version")
    if mapping.get("source_record_sha256") != source_sha256 or mapping.get("source_record_fingerprint") != fingerprint:
        raise ValueError("Scope mapping does not bind this exact source record")
    if mapping.get("basis") not in {"existing_candidate_review_only", "native_extraction_scope"}:
        raise ValueError("Scope mapping must declare its actual review basis")
    if mapping["basis"] == "native_extraction_scope" and mapping != _native_mapping(record, source_sha256):
        raise ValueError("Native projection mapping must exactly match the extracted design scope")
    decisions = mapping.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("Explicit scope decisions are required")
    identifiers = [x.get("observation_id") for x in decisions]
    if len(identifiers) != len(set(identifiers)) or set(identifiers) != set(observations):
        raise ValueError("Scope decisions must partition every source observation exactly once")
    modules = _unique(mapping.get("modules", []), "modules")
    evidence = {e["id"] for e in record.get("evidence", [])}
    for module in modules.values():
        if not str(module.get("reason", "")).strip() or not module.get("evidence_ids"):
            raise ValueError("Module activation requires a reason and source evidence")
        if not set(module["evidence_ids"]) <= evidence:
            raise ValueError("Module activation references unknown evidence")
    protected = _protected(record)
    for decision in decisions:
        if decision.get("layer") not in {"A", "B", "C"}:
            raise ValueError("Scope decision layer must be A, B or C")
        if not isinstance(decision.get("reason"), str) or not decision["reason"].strip() or not decision.get("purpose"):
            raise ValueError("Every scope decision needs a purpose and reason")
        if decision["layer"] == "B" and decision.get("module") not in modules:
            raise ValueError("B-layer decisions require an explicitly activated module")
        if decision["layer"] != "B" and decision.get("module") is not None:
            raise ValueError("Only B-layer decisions may own a module")
        if decision["layer"] == "C" and (decision["observation_id"] in protected
                or decision.get("purpose") in {"stability", "adverse_result", "design_comparison"}):
            raise ValueError("Core stability, key results and design/adverse evidence cannot be hidden as detail")
        if "condition_group" in decision and (not isinstance(decision["condition_group"], str) or not decision["condition_group"].strip()):
            raise ValueError("An explicit condition_group must be a nonempty string")
    return mapping


def _analysis_tables(record, decisions, selected, *, compact=False):
    """Lossless tables; grouping does not assert matched conditions or causality."""
    observations = {o["id"]: o for o in selected}
    experiments = _unique(record.get("experiments", []), "experiments")
    materials = []
    for material in record.get("materials", []):
        if material.get("role") not in _FINAL_ROLES:
            continue
        row = {"material_id": material["id"], "material": copy.deepcopy(material),
            "component_features": [], "preloaded_consumables": [], "unresolved_components": [],
            "feature_observation_ids": [], "performance_is_feature": False,
            "composition_normalized": False, "missing_is_zero": False,
            "eligibility": "not_automatically_established"}
        for component in material.get("components", []):
            target = ("component_features" if component.get("retention") == "retained" else
                      "preloaded_consumables" if component.get("retention") == "preloaded_consumable" else
                      "unresolved_components")
            row[target].append(copy.deepcopy(component))
        row["feature_observation_ids"] = [identifier for identifier, observation in observations.items()
            if observation.get("material_id") == material["id"] and decisions[identifier]["purpose"] == "material_feature"]
        materials.append(row)
    groups = defaultdict(list)
    for observation in selected:
        decision = decisions[observation["id"]]
        # With no asserted grouping, avoid pooling observations by experiment ID:
        # one Experiment can contain several pH/dose/irradiance conditions.
        group = decision.get("condition_group", observation["id"])
        key = (observation["experiment_id"], observation.get("material_id"), group)
        groups[key].append(observation)
    rows = []
    for (experiment_id, material_id, group), members in groups.items():
        if experiment_id not in experiments:
            raise ValueError("Observation references a missing experiment")
        experiment = experiments[experiment_id]
        ids = {o["id"] for o in members}
        pairs = [copy.deepcopy(p) for p in experiment.get("pairs", []) if
                 p.get("left_observation_id") in ids or p.get("right_observation_id") in ids]
        row = {"id": "condition-" + digest_json([experiment_id, material_id, group])[:20],
            "experiment_id": experiment_id, "material_id": material_id,
            "configuration_id": experiment.get("configuration_id"), "condition_group": group,
            "source_experiment_pointer": f"/source_context/experiments/{list(experiments).index(experiment_id)}",
            "observation_ids": [o["id"] for o in members],
            "source_pairs": pairs, "new_pairing_inferred": False,
            "grouping_is_comparability_proof": False}
        if compact:
            row["observation_refs"] = [{"observation_id": o["id"],
                "view_pointer": f"/observations/{list(observations).index(o['id'])}"} for o in members]
        else:
            row["native_coordinates"] = [{"observation_id": o["id"], "coordinates": copy.deepcopy(o.get("coordinates", [])),
                                          "time_window": copy.deepcopy(o.get("time_window"))} for o in members]
        rows.append(row)
    return {"materials": materials, "configurations": copy.deepcopy(record.get("configurations", [])), "experiments": rows}


def build_core_view(record, mapping=None, *, source_sha256, source_path=None, view_version=VIEW_VERSION):
    """Return a new, deterministic JSON value; never modify the source record.

    source_sha256 identifies archived bytes; its verification is the caller's
    responsibility. The mapping additionally binds the canonical record data.
    Scientific findings, statuses, values and evidence are copied unchanged.
    """
    _sha(source_sha256, "source_sha256")
    if view_version not in {LEGACY_VIEW_VERSION, VIEW_VERSION}:
        raise ValueError("Unsupported research core view layout version")
    compact = view_version == VIEW_VERSION
    record = _plain(record)
    references = record.get("reference_associations", [])
    if references:
        from .domain import ExtractionRecord
        from .reference_associations import reference_association_issues
        if reference_association_issues(ExtractionRecord.model_validate(record)):
            raise ValueError("Shared-reference associations failed structural validation")
    mapping = _mapping(record, mapping, source_sha256)
    decisions = {x["observation_id"]: x for x in mapping["decisions"]}
    source_observations = record.get("observations", [])
    selected = [copy.deepcopy(o) for o in source_observations if decisions[o["id"]]["layer"] in {"A", "B"}]
    selected_ids = {o["id"] for o in selected}
    counts = Counter(x["layer"] for x in decisions.values())
    archive = [{"observation_id": o["id"], "record_pointer": f"/observations/{i}",
                "reason": decisions[o["id"]]["reason"]} for i, o in enumerate(source_observations)
               if o["id"] not in selected_ids]
    source_index, series = [], []
    for item in record.get("quantitative_coverage", []):
        original_ids = item.get("observation_ids", [])
        associations = [r for r in references if r["target_kind"] == "series" and r["target_id"] == item["id"]]
        reference_ids = list(dict.fromkeys(oid for r in associations for oid in r["observation_ids"]))
        if not set(original_ids) <= set(decisions):
            raise ValueError("A source index refers to an unknown observation")
        retained = [identifier for identifier in original_ids if identifier in selected_ids]
        deferred = [identifier for identifier in original_ids if identifier not in selected_ids]
        indexed = {"id": item["id"], "source_id": item["source_id"], "block_id": item["block_id"],
                   "label": item["label"], "state": item.get("state"), "reason": item.get("reason"),
                   "locator": copy.deepcopy(item.get("locator")), "source_coverage": copy.deepcopy(item),
                   "retained_observation_ids": retained, "deferred_observation_ids": deferred,
                   "reading_state_changed": False,
                   "deferral_is_not_unread_or_not_reported": True}
        source_index.append(indexed)
        if associations:
            indexed["reference_association_ids"] = [r["id"] for r in associations]
            indexed["referenced_observation_ids"] = reference_ids
        if retained or reference_ids:
            row = {"id": item["id"], "label": item["label"], "data_kind": item.get("data_kind"),
                "source_id": item["source_id"], "block_id": item["block_id"], "locator": copy.deepcopy(item.get("locator")),
                "state": item.get("state"), "observation_ids": retained,
                "deferred_observation_ids": deferred, "numerical_aggregation": "none"}
            if associations:
                row["reference_association_ids"] = [r["id"] for r in associations]
                row["referenced_observation_ids"] = reference_ids
                row["reference_semantics"] = "Reuse only; primary members, physical runs and replicate counts remain unchanged."
                row["reference_entry_refs"] = [{"observation_id": o["id"], "view_pointer": f"/observations/{i}"}
                                               for i, o in enumerate(selected) if o["id"] in reference_ids]
            if compact:
                row["entry_refs"] = [{"observation_id": o["id"], "view_pointer": f"/observations/{i}"}
                    for i, o in enumerate(selected) if o["id"] in set(retained)]
            else:
                row["entries"] = [copy.deepcopy(o) for o in selected if o["id"] in set(retained)]
            series.append(row)
    pair_dependencies = []
    for experiment in record.get("experiments", []):
        for index, pair in enumerate(experiment.get("pairs", [])):
            ids = {pair["left_observation_id"], pair["right_observation_id"]}
            if not ids <= set(decisions):
                raise ValueError("A source pair refers to an unknown observation")
            archived = sorted(ids - selected_ids)
            if archived:
                pair_dependencies.append({"experiment_id": experiment["id"], "pair_index": index,
                    "archived_observation_ids": archived, "pair": copy.deepcopy(pair),
                    "core_pairing_eligible": False})
    mapping_hash = digest_json(mapping)
    view_id = "core-" + digest_json([view_version, source_sha256, mapping_hash])[:24]
    native = mapping["basis"] == "native_extraction_scope"
    result = {"version": view_version, "view_id": view_id,
        "provenance": {"kind": "projection_of_extracted_scope" if native else "derived_existing_candidate",
            "source_record_sha256": source_sha256, "source_record_fingerprint": digest_json(record),
            "source_rule_version": record.get("rule_version", "0.5"),
            "source_schema_version": record.get("schema_version", "0.1.0"),
            "source_path": str(source_path) if source_path is not None else None,
            "policy_version": mapping["policy_version"], "mapping_sha256": mapping_hash, "mapping_basis": mapping["basis"],
            "human_validated": False, "scientific_validation": "not_established",
            "new_extraction_performed": False, "new_source_audit_performed": False,
            "scope_selection_assessed": mapping["basis"] != "unclassified_fallback"},
        "counts": {"source_observations": len(source_observations), "core_observations": counts["A"],
            "module_observations": counts["B"], "deferred_observations": counts["C"],
            "retained_observations": len(selected), "source_series": len(source_index),
            "retained_series": len(series), "unclassified_observations": sum(d["purpose"] == "unclassified" for d in decisions.values())},
        "decisions": copy.deepcopy(mapping["decisions"]), "modules": copy.deepcopy(mapping.get("modules", [])),
        "observations": selected, "core_observation_ids": [o["id"] for o in source_observations if decisions[o["id"]]["layer"] == "A"],
        "module_observation_ids": [o["id"] for o in source_observations if decisions[o["id"]]["layer"] == "B"],
        "deferred_observation_ids": [o["id"] for o in source_observations if decisions[o["id"]]["layer"] == "C"],
        "series": series, "source_index": source_index, "archive_references": archive,
        "relation_archive_dependencies": pair_dependencies,
        "analysis_tables": _analysis_tables(record, decisions, selected, compact=compact),
        "limitations": copy.deepcopy(mapping.get("limitations", [])),
        "source_context": {key: copy.deepcopy(record.get(key, [])) for key in
            ("materials", "substances", "configurations", "experiments", "evidence", "coverage", "extensions")},
        "paper": copy.deepcopy(record.get("paper", {})), "comparisons": copy.deepcopy(record.get("comparisons", []))}
    if references:
        result["reference_associations"] = copy.deepcopy(references)
        result["counts"]["reference_associations"] = len(references)
        result["reference_semantics"] = "References reuse existing observations; no new measurements, physical-run identity or performance pairing is inferred."
        for row in result["analysis_tables"]["experiments"]:
            associations = [r for r in references if r["target_kind"] == "experiment" and r["target_id"] == row["experiment_id"]]
            if associations:
                row["reference_association_ids"] = [r["id"] for r in associations]
                row["referenced_observation_ids"] = list(dict.fromkeys(oid for r in associations for oid in r["observation_ids"]))
    if native:
        result["research_source_index"] = copy.deepcopy(record["design_scope"]["source_index"])
        result["eligibility"] = copy.deepcopy(record["design_scope"]["eligibility"])
        result["core_checks"] = copy.deepcopy(record["design_scope"]["core_checks"])
        result["counts"]["deferred_topics"] = sum(len(x.get("deferred_topics", [])) for x in result["research_source_index"])
        # Native source summaries are the primary C-layer index. Original
        # quantitative inventories remain separately accessible, unchanged.
        result["quantitative_source_index"] = result["source_index"]
        result["source_index"] = []
        coverage_by_id = {item["id"]: item for item in record.get("quantitative_coverage", [])}
        for item in result["research_source_index"]:
            indexed = {**copy.deepcopy(item), "label": item["summary"],
                "state": item["reading_state"], "deferred_observation_ids": [],
                "deferral_is_not_unread_or_not_reported": True}
            if compact:
                all_ids = list(item.get("retained_observation_ids", []))
                for identifier in item.get("quantitative_coverage_ids", []):
                    all_ids.extend(coverage_by_id[identifier].get("observation_ids", []))
                all_ids = list(dict.fromkeys(all_ids))
                indexed["retained_observation_ids"] = [identifier for identifier in all_ids if identifier in selected_ids]
                indexed["deferred_observation_ids"] = [identifier for identifier in all_ids if identifier not in selected_ids]
            result["source_index"].append(indexed)
        if record["rule_version"] == "0.9":
            from .arm_research import research_tables
            from .domain import ExtractionRecord
            result["analysis_tables"].update(research_tables(ExtractionRecord.model_validate(record)))
    return result


def build_native_core_view(record, *, source_sha256, source_path=None):
    record = _plain(record)
    mapping = _native_mapping(record, source_sha256)
    return build_core_view(record, mapping, source_sha256=source_sha256, source_path=source_path)


def write_core_view_artifact(record_path, mapping_path, output_directory, *, view_version=VIEW_VERSION):
    """Write a new view/manifest only; refuse to replace different file content."""
    record_path, mapping_path, output_directory = Path(record_path), Path(mapping_path), Path(output_directory)
    source_bytes, mapping_bytes = record_path.read_bytes(), mapping_path.read_bytes()
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    record, mapping = json.loads(source_bytes), json.loads(mapping_bytes)
    view = build_core_view(record, mapping, source_sha256=source_sha, source_path=str(record_path), view_version=view_version)
    view_bytes = (json.dumps(view, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output_directory.mkdir(parents=True, exist_ok=True)
    mapping_target, view_target = output_directory / "mapping.json", output_directory / "core-view.json"
    manifest = {"version": MANIFEST_VERSION, "artifact_type": "research_core_view", "view_id": view["view_id"],
        "source_record_sha256": source_sha, "source_record_fingerprint": digest_json(record),
        "source_record_path": str(record_path), "view_path": view_target.name,
        "view_sha256": hashlib.sha256(view_bytes).hexdigest(), "mapping_path": mapping_target.name,
        "mapping_sha256": hashlib.sha256(mapping_bytes).hexdigest(), "policy_version": view["provenance"]["policy_version"],
        "status": "derived_not_reaudited"}
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    outputs = {mapping_target: mapping_bytes, view_target: view_bytes,
               output_directory / "core-view-manifest.json": manifest_bytes}
    # Check every destination before writing any new file.
    for path, payload in outputs.items():
        if path.exists() and path.read_bytes() != payload:
            raise ValueError(f"Refusing to overwrite a different research artifact: {path}")
    for path, payload in outputs.items():
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(payload)
    if record_path.read_bytes() != source_bytes:
        raise ValueError("Source record changed during view generation")
    return manifest
