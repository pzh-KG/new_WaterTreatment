"""Source-qualified research projections. A missing assertion never becomes a negative feature."""
from __future__ import annotations

from collections import defaultdict

from .domain import FINAL_ROLES, ExtractionRecord

PARENT = {"liquid_parent_removal", "liquid_parent_ratio", "liquid_parent_concentration"}
OPERATING = {"treatment", "control"}
QUANTIFIED = {"reported", "estimated"}
MEASURED = {"reported_measurement", "author_calculation", "plot_estimate"}
TIME_SECONDS = {"s": 1, "sec": 1, "second": 1, "seconds": 1, "min": 60, "minute": 60,
                "minutes": 60, "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600}
PLOT_CLOCK = {"plot_coordinate", "plotted_time", "reported_coordinate"}


def _text(fact):
    return str(fact.value).strip().casefold() if fact.status in QUANTIFIED and fact.value is not None else ""


def _measured(obs):
    return (obs.value.status in QUANTIFIED and type(obs.value.value) in (int, float)
            and bool(_text(obs.unit)) and obs.origin in MEASURED
            and bool(obs.value.evidence_ids or any(link.support_type == "direct" and "value" in link.supported_fields
                     for link in obs.evidence_links)))


def _coordinate_time(obs):
    points = set()
    for coordinate in obs.coordinates:
        if coordinate.name.strip().casefold() not in {"time", "time_coordinate", "elapsed_time", "reaction_time", "illumination_time", "irradiation_time"}:
            continue
        scale = TIME_SECONDS.get(_text(coordinate.unit))
        if not scale or not _text(coordinate.basis) or coordinate.fact.status not in QUANTIFIED or type(coordinate.fact.value) not in (float, int):
            return None
        points.add((round(scale * coordinate.fact.value, 6), _text(coordinate.basis)))
    return next(iter(points)) if len(points) == 1 else None


def _window(obs):
    w = obs.time_window
    point = _coordinate_time(obs)
    if w.end.value is None and w.start.value is None:
        if (point and w.kind in {"unspecified", "point", "time_point", "endpoint", "instantaneous"}
                and all(f.status not in {"conflict", "ambiguous"} for f in (w.start, w.end, w.reference, w.unit))
                and (not _text(w.reference) or _text(w.reference) == point[1])):
            return ("endpoint", None, *point)
        return None
    scale = TIME_SECONDS.get(_text(w.unit))
    if not scale or w.end.status not in QUANTIFIED or type(w.end.value) not in (int, float) or not _text(w.reference):
        return None
    if w.start.value is None:
        if w.kind not in {"point", "time_point", "endpoint", "instantaneous"} | PLOT_CLOCK:
            return None
        start = None
    elif w.start.status in QUANTIFIED and type(w.start.value) in (int, float):
        start = round(scale * w.start.value, 6)
    else:
        return None
    end = round(scale * w.end.value, 6)
    if point and point != (end, _text(w.reference)):
        return None
    return (w.kind, start, end, _text(w.reference))


def _installed(exp, configs):
    ids = set(exp.material_ids)
    cfg = configs.get(exp.configuration_id)
    if cfg:
        ids.update(p.material_id for p in cfg.placements)
    return ids


def _alignment(flux, liquid):
    a, b = _window(flux), _window(liquid)
    if not a or not b or a[2:] != b[2:]:
        return None
    if a[0] in PLOT_CLOCK or b[0] in PLOT_CLOCK:
        return "same_reported_coordinate_only"
    if a == b or a[1] is None and b[1] is None:
        return "same_window" if a[1] is not None else "same_time_point"
    if a[1] is not None and b[1] is None:
        return "flux_interval_with_liquid_endpoint"
    return None


def _route_problems(exp, installed, materials):
    retained = {c.substance_id for mid in installed if mid in materials for c in materials[mid].components
                if c.retention == "retained"}
    routes = defaultdict(set)
    by_role = defaultdict(set)
    def identity(r):
        if r.substance_id is not None:
            return ("substance", r.substance_id)
        if r.material_id is not None:
            return ("material", r.material_id)
        return None
    for r in exp.reaction_inputs:
        if identity(r) is not None:
            routes[identity(r)].add(r.source)
        if r.role.strip():
            by_role[r.role.strip().casefold()].add(r.source)
    for j, r in enumerate(exp.reaction_inputs):
        key = identity(r)
        found = routes[key] if key is not None else by_role[r.role.strip().casefold()]
        if r.material_id and r.material_id not in materials:
            yield j, "INPUT_MATERIAL_REFERENCE", "An input material must identify a registered sample."
        incompatible = {"external"} if r.absence_scope == "external_addition" else {"external", "preloaded", "in_situ", "material_bound"}
        if r.source == "explicit_none" and found & incompatible:
            yield j, "ARM_SUPPLY_CONFLICT", "Presence and explicit absence of one species belong to separate arms or require an explicit source conflict."
        if r.absence_scope and r.source != "explicit_none":
            yield j, "ABSENCE_SCOPE_SOURCE", "Scope of absence belongs only to an explicit negative input."
        if r.source == "material_bound":
            hosts = [mid for mid in installed if mid in materials and (r.material_id is None or mid == r.material_id)]
            anchored = (r.material_id in hosts if r.substance_id is None else any(
                c.substance_id == r.substance_id and c.retention == "retained" for mid in hosts for c in materials[mid].components))
            if not anchored or not r.evidence_ids:
                yield j, "BOUND_SUPPLY_UNSUPPORTED", "Name the installed catalyst through material_id, or cite its actual retained chemical component. Never add a material as its own component."
        if r.source == "external" and (r.substance_id in retained or r.material_id in installed) and not r.addition_evidence_ids:
            yield j, "EXTERNAL_DOSE_UNSUPPORTED", "The same retained species needs explicit evidence for a separate operating dose."
        if r.addition_evidence_ids and (r.source != "external" or not set(r.addition_evidence_ids) <= set(r.evidence_ids)):
            yield j, "DOSE_EVIDENCE_SCOPE", "Separate-dose citations must be operating-input evidence on this external input."


def check_research_contract(record, issue):
    if record.rule_version != "0.9":
        if (any(e.arm_kind or e.comparison_group or any(r.source == "material_bound" or r.addition_evidence_ids or r.absence_scope or r.material_id
                    for r in e.reaction_inputs) for e in record.experiments)
                or any(o.endpoint_kind or o.removal_attribution for o in record.observations)
                or any(c.measurements for m in record.materials for c in m.components) or any(c.contrasts for c in record.comparisons)):
            issue("RESEARCH_FIELDS_VERSION", "rule_version", "Arm-aware fields require native extraction under their matching contract.")
        return
    materials = {m.id: m for m in record.materials}
    configs = {c.id: c for c in record.configurations}
    observations = {o.id: o for o in record.observations}
    by_arm = defaultdict(list)
    for i, obs in enumerate(record.observations):
        by_arm[obs.experiment_id].append(obs)
        if obs.endpoint_kind is None and obs.analysis_role in {"key_result", "condition_comparison", "stability", "design_control"}:
            issue("ENDPOINT_UNCLASSIFIED", f"observations[{i}].endpoint_kind", "Classify the measured endpoint, including other or unresolved.")
        if obs.endpoint_kind in PARENT and obs.removal_attribution in {None, "not_applicable"}:
            issue("PARENT_FATE_UNCLASSIFIED", f"observations[{i}].removal_attribution", "Distinguish supported degradation from disappearance, transfer or unresolved fate.")
    for i, exp in enumerate(record.experiments):
        path = f"experiments[{i}]"
        if not exp.arm_kind:
            issue("ARM_UNCLASSIFIED", path + ".arm_kind", "Give the physical arm kind or use unresolved for unclear ownership.")
        if exp.arm_kind not in OPERATING:
            continue
        installed = _installed(exp, configs)
        for obs in by_arm[exp.id]:
            if obs.material_id and installed and obs.material_id not in installed:
                issue("ARM_MATERIAL_MISMATCH", path + ".material_ids", f"Observation {obs.id} asserts a specimen outside the installed arm.")
        labels = {_text(c.fact) for o in by_arm[exp.id] for c in o.coordinates if c.name.strip().casefold() in {"arm", "treatment_arm", "experimental_arm"} and _text(c.fact)}
        if len(labels) > 1:
            issue("MIXED_ARM_LABELS", path, "Observations explicitly name different treatment arms under one owner; split them or retain an unresolved summary.")
        for j, code, message in _route_problems(exp, installed, materials):
            issue(code, f"{path}.reaction_inputs[{j}]", message)
    for i, comp in enumerate(record.comparisons):
        for j, contrast in enumerate(comp.contrasts):
            path = f"comparisons[{i}].contrasts[{j}]"
            ids = (contrast.baseline_observation_id, contrast.modified_observation_id)
            if ids[0] == ids[1] or not set(ids) <= set(comp.observation_ids) or not all(k in observations for k in ids):
                issue("DESIGN_CONTRAST_ENDPOINT", path, "Both distinct baseline and modified observations must belong to this comparison.")
            elif any(observations[k].material_id and observations[k].material_id not in comp.material_ids for k in ids):
                issue("DESIGN_CONTRAST_MATERIAL", path, "A contrast endpoint material is outside its comparison.")
    known_evidence = {e.id for e in record.evidence}
    for i, m in enumerate(record.materials):
        for j, c in enumerate(m.components):
            for k, q in enumerate(c.measurements):
                if not set(q.evidence_ids) <= known_evidence or (q.basis == "bulk_retained" and c.retention != "retained"):
                    issue("COMPOSITION_BASIS", f"materials[{i}].components[{j}].measurements[{k}]",
                          "Check the located measurement and whether it actually concerns a retained component.")
    for i, c in enumerate(record.comparisons):
        for j, contrast in enumerate(c.contrasts):
            if not set(contrast.evidence_ids) <= known_evidence:
                issue("DESIGN_CONTRAST_EVIDENCE", f"comparisons[{i}].contrasts[{j}]", "The contrast must cite registered evidence.")


def _performance(exp, pair, observations, configs, materials):
    ends = [observations.get(k) for k in (pair.left_observation_id, pair.right_observation_id)]
    if not all(ends):
        return None
    flux = next((o for o in ends if o.endpoint_kind == "evaporation_flux"), None)
    liquid = next((o for o in ends if o.endpoint_kind in PARENT), None)
    if flux is None or liquid is None:
        return None
    cfg = configs.get(exp.configuration_id)
    streams = {s.id: s.kind for s in cfg.streams} if cfg else {}
    reasons = []
    if pair.level != "P1" or exp.joint_evidence.level != "J1" or not exp.joint_evidence.evidence_ids or not pair.evidence_ids:
        reasons.append("same_run_evidence")
    if exp.arm_kind not in OPERATING or {o.experiment_id for o in ends} != {exp.id}:
        reasons.append("experimental_arm")
    if not flux.material_id or flux.material_id != liquid.material_id or flux.material_id not in _installed(exp, configs):
        reasons.append("material_variant")
    if liquid.substance_id not in {t.substance_id for t in exp.targets} or streams.get(liquid.stream_id) not in {"feed", "reaction_liquid", "recirculation"}:
        reasons.append("target_or_liquid_stream")
    alignment = _alignment(flux, liquid)
    if not alignment:
        reasons.append("time_basis_or_window")
    if pair.differing_conditions or any(_route_problems(exp, _installed(exp, configs), materials)):
        reasons.append("conditions_or_dose")
    from .validation import _named_differences
    if _named_differences(flux.coordinates, liquid.coordinates, "coordinate"):
        reasons.append("observation_coordinates")
    if not all(_measured(o) for o in ends):
        reasons.append("value_unit_or_provenance")
    co_observed = not reasons
    if alignment == "same_reported_coordinate_only":
        reasons.append("measurement_window_unspecified")
    if pair.unresolved_conditions:
        reasons.append("unresolved_comparison_conditions")
    fate_supported = any(link.support_type == "direct" and "removal_attribution" in link.supported_fields for link in liquid.evidence_links)
    unconfirmed_supply = any(r.source in {"unknown", "conflict", "not_reported"} for r in exp.reaction_inputs)
    eligible_treatment = (not reasons and exp.arm_kind == "treatment" and flux.material_id in materials
                          and materials[flux.material_id].role in FINAL_ROLES)
    return {"experiment_id": exp.id, "material_id": flux.material_id, "pollutant_id": liquid.substance_id,
            "flux_observation_id": flux.id, "liquid_observation_id": liquid.id, "liquid_endpoint": liquid.endpoint_kind,
            "alignment": alignment, "removal_attribution": liquid.removal_attribution,
            "same_arm_co_observed": co_observed, "unresolved_conditions": list(pair.unresolved_conditions),
            "eligible_as_degradation": not reasons and liquid.removal_attribution == "degradation_supported" and fate_supported,
            "qualified_treatment_arm": eligible_treatment, "unresolved_operating_inputs": unconfirmed_supply,
            "flux_value": flux.value.model_dump(mode="json"), "flux_unit": flux.unit.model_dump(mode="json"),
            "liquid_value": liquid.value.model_dump(mode="json"), "liquid_unit": liquid.unit.model_dump(mode="json"),
            "time_basis": _window(liquid), "flux_origin": flux.origin.value, "liquid_origin": liquid.origin.value,
            "limitations": list(dict.fromkeys([*exp.joint_evidence.limitations, *flux.limitations, *liquid.limitations])),
            "pairing_level": pair.level.value, "eligible": not reasons, "restrictions": reasons,
            "evidence_ids": list(pair.evidence_ids)}


def research_tables(record: ExtractionRecord):
    """Use for native research records only; preserve the original values, states and evidence."""
    if record.rule_version != "0.9":
        raise ValueError("An arm-aware research record is required")
    obs = {o.id: o for o in record.observations}
    configs = {c.id: c for c in record.configurations}
    materials = {m.id: m for m in record.materials}
    substances = {s.id: s for s in record.substances}
    arms = {e.id: e for e in record.experiments}
    responses = defaultdict(list)
    for o in record.observations:
        if o.endpoint_kind in PARENT and o.substance_id:
            responses[(o.experiment_id, o.substance_id)].append(o)
    structural = defaultdict(list)
    for assignment in record.design_scope.observation_assignments if record.design_scope else ():
        o = obs.get(assignment.observation_id)
        if (assignment.purpose == "material_feature" and o and o.material_id and o.endpoint_kind in {None, "other"}
                and arms.get(o.experiment_id) and arms[o.experiment_id].purpose == "characterization"):
            structural[o.material_id].append({"observation_id": o.id, "metric": o.metric.model_dump(mode="json"),
                "value": o.value.model_dump(mode="json"), "unit": o.unit.model_dump(mode="json"),
                "method": o.analytical_method.model_dump(mode="json"), "origin": o.origin.value,
                "evidence_ids": list(o.evidence_ids)})
    routes, pairs, features, contrasts, composition, treatments = [], [], [], [], [], []
    for e in arms.values():
        installed = sorted(_installed(e, configs))
        route_errors = {index for index, _, _ in _route_problems(e, installed, materials)}
        input_groups = defaultdict(list)
        uncertain_inputs = []
        for index, reaction in enumerate(e.reaction_inputs):
            input_groups[reaction.source.value].append({"substance_id": reaction.substance_id,
                "material_id": reaction.material_id, "role": reaction.role})
            if (index in route_errors or not reaction.evidence_ids
                    or reaction.source in {"unknown", "conflict", "not_reported"}
                    or not (reaction.material_id or reaction.substance_id or reaction.source == "explicit_none")):
                uncertain_inputs.append(index)
        cfg = configs.get(e.configuration_id)
        liquid_streams = {s.id for s in cfg.streams if s.kind in {"feed", "reaction_liquid", "recirculation"}} if cfg else set()
        for target in e.targets:
            supported = [o.id for o in responses[(e.id, target.substance_id)] if o.stream_id in liquid_streams
                         and o.material_id in installed and _measured(o)]
            response_materials = sorted({obs[identifier].material_id for identifier in supported})
            treatment_response = bool(e.arm_kind == "treatment" and not route_errors
                and any(mid in materials and materials[mid].role in FINAL_ROLES for mid in response_materials))
            treatments.append({"experiment_id": e.id, "arm_kind": e.arm_kind, "comparison_group": e.comparison_group,
                "configuration_id": e.configuration_id, "tested_material_ids": list(e.material_ids),
                "installed_material_ids": installed, "target": target.model_dump(mode="json"), "mixture_type": e.mixture_type.value,
                "liquid_response_observation_ids": supported, "response_material_ids": response_materials,
                "response_scope": "tested_configuration", "input_ids_by_supply": dict(input_groups),
                "input_inventory_status": "entries_recorded" if e.reaction_inputs else "unspecified",
                "unresolved_input_indices": uncertain_inputs, "has_supported_treatment_response": treatment_response,
                "eligible_for_input_specific_lookup": bool(treatment_response and e.reaction_inputs and not uncertain_inputs),
                "evidence_ids": list(e.evidence_ids)})
        for i, r in enumerate(e.reaction_inputs):
            routes.append({"experiment_id": e.id, "arm_kind": e.arm_kind, "comparison_group": e.comparison_group,
                           "supported_external_dose": e.arm_kind in OPERATING and i not in route_errors and r.source == "external" and bool(r.evidence_ids),
                           "material_ids": sorted(_installed(e, configs)), "target_ids": [t.substance_id for t in e.targets],
                           **r.model_dump(mode="json")})
        for pair in e.pairs:
            row = _performance(e, pair, obs, configs, materials)
            if row:
                pairs.append(row)
    for m in materials.values():
        if m.role not in FINAL_ROLES:
            continue
        for c in m.components:
            chemical = substances.get(c.substance_id)
            for q in c.measurements:
                quantity = q.quantity
                comparable = (quantity.fact.status in QUANTIFIED and type(quantity.fact.value) in (int, float)
                              and bool(_text(quantity.unit)) and bool(q.evidence_ids))
                if q.basis == "bulk_retained":
                    comparable = comparable and c.retention == "retained"
                if q.basis == "local_composition":
                    comparable = comparable and bool(_text(q.sampled_region))
                composition.append({"material_id": m.id, "substance_id": c.substance_id,
                    "substance_name": chemical.name.value if chemical else None, "retention": c.retention.value,
                    "measurement_basis": q.basis, "quantity": quantity.name, "unit": quantity.unit.model_dump(mode="json"),
                    "denominator": quantity.basis.model_dump(mode="json"), "observed": quantity.fact.model_dump(mode="json"),
                    "method": q.method.model_dump(mode="json"), "sampled_region": q.sampled_region.model_dump(mode="json"),
                    "feature_value": quantity.fact.value if comparable else None, "numeric_available": comparable,
                    "evidence_ids": list(q.evidence_ids)})
        features.append({"material_id": m.id, "family_id": m.family_id, "role": m.role.value,
                         "material_name": m.name.model_dump(mode="json"), "structural_descriptors": structural[m.id],
                         "classification": [x.model_dump(mode="json") for x in m.classification],
                         "morphology": [x.model_dump(mode="json") for x in m.morphology],
                         "components": [{**x.model_dump(mode="json"),
                                         "substance_name": substances[x.substance_id].name.model_dump(mode="json") if x.substance_id in substances else None,
                                         "substance_formula": substances[x.substance_id].formula.model_dump(mode="json") if x.substance_id in substances else None}
                                        for x in m.components if x.retention in {"retained", "preloaded_consumable", "unknown"}],
                         "component_inventory_status": "recorded" if m.components else "unknown",
                         "preparation": [p.model_dump(mode="json") for p in m.preparation]})
    for c in record.comparisons:
        for d in c.contrasts:
            a, b = obs.get(d.baseline_observation_id), obs.get(d.modified_observation_id)
            reasons = []
            if not a or not b or not all(_measured(o) for o in (a, b)):
                reasons.append("unquantified_or_unsupported_endpoint")
            if a and b:
                if (not a.material_id or not b.material_id or a.material_id == b.material_id
                        or a.experiment_id == b.experiment_id or any(not arms.get(o.experiment_id) or arms[o.experiment_id].arm_kind not in OPERATING for o in (a,b))):
                    reasons.append("variant_or_arm")
                if (a.endpoint_kind in {None, "unresolved"} or a.endpoint_kind != b.endpoint_kind or not _text(a.metric) or
                        _text(a.metric) != _text(b.metric) or a.substance_id != b.substance_id or _text(a.unit) != _text(b.unit)):
                    reasons.append("measurement_comparability")
                if a.endpoint_kind in PARENT | {"evaporation_flux", "condensate_yield", "mineralization"} and (not _window(a) or _window(a) != _window(b)):
                    reasons.append("time_basis_or_window")
                if a.experiment_id in arms and b.experiment_id in arms:
                    from .validation import _condition_differences
                    differences = _condition_differences(arms[a.experiment_id], arms[b.experiment_id], configs)
                    if any(k not in {"material_ids", "configuration.material_ids"} for k in differences):
                        reasons.append("different_operating_conditions")
            if not _text(c.operation) or not c.held_conditions or not all(_text(k.fact) for k in c.held_conditions) or c.confounders or c.limitations:
                reasons.append("design_or_controls")
            documented = bool(a and b and a.id != b.id and a.material_id and b.material_id and a.material_id != b.material_id
                              and _text(c.operation) and d.evidence_ids and c.evidence_ids)
            contrasts.append({"comparison_id": c.id, **d.model_dump(mode="json"), "operation": c.operation.model_dump(mode="json"),
                              "source_linked_variant_contrast": documented,
                              "causal_status": c.causal_status, "eligible_for_controlled_comparison": not reasons,
                              "restrictions": reasons})
    return {"arm_reagents": routes, "pollutant_treatment_arms": treatments, "dual_performance_pairs": pairs, "variant_features": features,
            "typed_composition_features": composition,
            "design_outcome_contrasts": contrasts}
