"""Conservative, offline graph projection. No database/network client exists here."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from pydantic import Field

from .domain import ExtractionRecord, Fact, FactStatus, ObservationOrigin, Scalar, StrictModel
from .quantitative import quantitative_summary
from .research_scope import archived_dense_ids, scoped_projection
from .validation import Severity, validate_record


class GraphNode(StrictModel):
    id: str
    label: str
    properties: dict[str, Scalar | list[Scalar] | None] = Field(default_factory=dict)


class GraphEdge(StrictModel):
    id: str
    source: str
    target: str
    relationship: str
    properties: dict[str, Scalar | list[Scalar] | None] = Field(default_factory=dict)


class GraphProjection(StrictModel):
    schema_version: str
    rule_version: str
    paper_id: str
    record_fingerprint: str
    source_check: str = "not_performed"
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _reported(fact: Fact) -> Any:
    if fact.status not in {FactStatus.REPORTED, FactStatus.EXPLICIT_ABSENCE}:
        return None
    if not fact.evidence_ids:
        return None
    return fact.normalized if fact.normalized is not None else fact.value


def _observation_value(fact: Fact) -> Any:
    """Keep source values, including estimates, without substituting conversions.

    A normalized value may change the physical quantity (for example signed
    change versus positive loss), not merely the unit. Free text cannot prove
    equivalence, so value, metric and unit must retain their source meaning.
    """
    if fact.status.value not in {"reported", "estimated", "explicit_absence"} or not fact.evidence_ids:
        return None
    return fact.value


def _observation_normalization(observation) -> dict[str, Any]:
    """Expose registered conversions separately; never infer a paired unit."""
    properties: dict[str, Any] = {"value_semantics": "source_fact_value_v1",
                                  "context_semantics": "source_fact_value_v1"}
    fields = ("metric", "value", "unit", "measured_object", "denominator", "reference")
    for name in fields:
        fact = getattr(observation, name)
        if name in {"metric", "unit"}:
            properties[f"source_{name}_status"] = fact.status.value
        if name in {"measured_object", "denominator", "reference"}:
            properties[f"{name}_status"] = fact.status.value
        if fact.status.value not in {"reported", "estimated", "explicit_absence"} or not fact.evidence_ids:
            continue
        properties[f"{name}_raw"] = fact.raw
        properties[f"normalized_{name}"] = fact.normalized
        properties[f"{name}_normalization_reason"] = fact.normalization_reason
        if fact.normalized is not None:
            properties[f"normalized_{name}_status"] = "unvalidated_conversion"
    if any(properties.get(f"normalized_{name}") is not None for name in fields):
        properties["normalization_semantics"] = "separate_unvalidated_conversion"
    return properties


def _relation_quantities(**facts: Fact) -> dict[str, Any]:
    """Retain sourced relationship quantities and their source unit/basis.

    Keep the previous relationship eligibility gate: this does not make
    unreported, conflicting, inferred or estimated relation quantities public.
    Each registered conversion is a separate candidate, never a source fact.
    """
    properties: dict[str, Any] = {"quantity_semantics": "source_fact_value_v1"}
    for name, fact in facts.items():
        properties[f"{name}_status"] = fact.status.value
        if fact.status not in {FactStatus.REPORTED, FactStatus.EXPLICIT_ABSENCE} or not fact.evidence_ids:
            continue
        properties[name] = fact.value
        properties[f"{name}_raw"] = fact.raw
        properties[f"{name}_evidence_ids"] = fact.evidence_ids
        properties[f"normalized_{name}"] = fact.normalized
        properties[f"{name}_normalization_reason"] = fact.normalization_reason
        if fact.normalized is not None:
            properties[f"normalized_{name}_status"] = "unvalidated_conversion"
            properties["normalization_semantics"] = "separate_unvalidated_conversion"
    return properties


def project_record(record: ExtractionRecord, *, source_documents: Iterable[Any] = (),
                   allow_invalid: bool = False) -> GraphProjection:
    """Project key entities and sourced report values, never individual Fact nodes.

    ``allow_invalid`` is an explicit diagnostic escape hatch. Inference and
    conflicting fact values are still omitted. The result is not a scientific
    acceptance decision and contains no live Neo4j connection.

    Rule 0.7 records retain explicitly optional dense points in the bound JSON
    and expose series indexes instead of individual graph observations. Legacy
    records retain their original projection behavior, without reclassification.
    """
    documents = list(source_documents)
    scoped = scoped_projection(record)
    archived = archived_dense_ids(record)
    pair_qualifications = {}
    if record.rule_version == "0.9":
        from .arm_research import research_tables
        for row in research_tables(record)["dual_performance_pairs"]:
            key = (row["experiment_id"], frozenset((row["flux_observation_id"], row["liquid_observation_id"])))
            pair_qualifications[key] = row
    checks = validate_record(record, documents)
    errors = [issue for issue in checks if issue.severity == Severity.ERROR]
    if errors and not allow_invalid:
        raise ValueError("Record failed validation: " + "; ".join(f"{x.code}@{x.path}" for x in errors))
    fingerprint = hashlib.sha256(json.dumps(record.model_dump(mode="json"), ensure_ascii=False,
                                           sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    result = GraphProjection(schema_version=record.schema_version, rule_version=record.rule_version,
                             paper_id=record.paper.id,
                             record_fingerprint=fingerprint,
                             source_check="checked_with_issues" if documents and checks else ("checked" if documents else "not_performed"),
                             warnings=[f"{x.code}@{x.path}: {x.message}" for x in checks])
    # A candidate is an immutable snapshot. Re-exporting the same record is
    # idempotent; another extraction must not retain withdrawn values or edges
    # through MERGE/SET += against an earlier candidate for the same paper.
    def gid(kind: str, identifier: str) -> str:
        return json.dumps([record.paper.id, fingerprint, kind, identifier], ensure_ascii=False, separators=(",", ":"))

    def node(kind: str, identifier: str, **properties: Any) -> None:
        result.nodes.append(GraphNode(id=gid(kind, identifier), label=kind,
                                      properties={k: v for k, v in properties.items() if v is not None}))

    def edge(source_kind: str, source_id: str, target_kind: str, target_id: str,
             relationship: str, **properties: Any) -> None:
        edge_id = json.dumps([record.paper.id, fingerprint, "edge", len(result.edges)], ensure_ascii=False, separators=(",", ":"))
        result.edges.append(GraphEdge(id=edge_id, source=gid(source_kind, source_id), target=gid(target_kind, target_id),
                                      relationship=relationship,
                                      properties={k: v for k, v in properties.items() if v is not None}))

    paper = record.paper
    quantitative = quantitative_summary(record)
    node("Paper", paper.id, title=_reported(paper.title), doi=_reported(paper.doi),
         dataset_role=paper.dataset_role.value, source_ids=paper.source_ids,
         record_fingerprint=fingerprint,
         quantitative_coverage_assessment="model_inventory_not_independent_validation" if record.quantitative_coverage else "not_assessed",
         quantitative_inventory_items=len(record.quantitative_coverage),
         quantitative_incomplete_items=len(quantitative["incomplete_series"]),
         scientific_validation="not_established",
         extraction_scope=record.extraction_scope if scoped else None,
         projection_policy="research_core_with_series_archive_v1" if scoped else None,
         archived_dense_observation_count=len(archived) if scoped else None,
         schema_version=record.schema_version, rule_version=record.rule_version,
         **({"design_scope_policy": record.design_scope.version,
             "research_scope_pointer": "/design_scope",
             "research_use_eligibility_json": json.dumps([item.model_dump(mode="json") for item in record.design_scope.eligibility], ensure_ascii=False),
             "deferred_source_index_json": json.dumps([item.model_dump(mode="json") for item in record.design_scope.source_index], ensure_ascii=False),
             "research_modules_json": json.dumps([item.model_dump(mode="json") for item in record.design_scope.modules], ensure_ascii=False),
             "scope_semantics": "model_assessment_not_independent_validation"} if record.design_scope else {}))
    for substance in record.substances:
        node("Substance", substance.id, name=_reported(substance.name), formula=_reported(substance.formula),
             salt_or_isomer=_reported(substance.salt_or_isomer), evidence_ids=substance.evidence_ids)
    for material in record.materials:
        node("MaterialSample", material.id, name=_reported(material.name), role=material.role.value,
             family_id=material.family_id, state=_reported(material.state), evidence_ids=material.evidence_ids)
        edge("Paper", paper.id, "MaterialSample", material.id, "DESCRIBES")
        for component in material.components:
            # Precursor/analytical inputs are not projected as retained composition.
            relation = {"retained": "HAS_RETAINED_COMPONENT", "preloaded_consumable": "PRELOADS"}.get(component.retention.value)
            if relation:
                edge("MaterialSample", material.id, "Substance", component.substance_id, relation,
                     evidence_ids=component.evidence_ids,
                     measurements_json=json.dumps([m.model_dump(mode="json") for m in component.measurements], ensure_ascii=False) if component.measurements else None,
                     **_relation_quantities(amount=component.amount, unit=component.unit, basis=component.basis))
    for index, config in enumerate(record.configurations):
        # Keep apparatus-wide conditions on this configuration. An Observation's
        # stream_id is local to the configuration used by its owning experiment.
        config_context = {}
        if scoped:
            config_context = {
                "source_configuration_id": config.id,
                "record_pointer": f"/configurations/{index}",
                "parameters_json": json.dumps([p.model_dump(mode="json") for p in config.parameters], ensure_ascii=False),
                "streams_json": json.dumps([s.model_dump(mode="json") for s in config.streams], ensure_ascii=False),
                "placements_json": json.dumps([p.model_dump(mode="json") for p in config.placements], ensure_ascii=False),
            }
        node("Configuration", config.id, boundary=_reported(config.boundary), evidence_ids=config.evidence_ids,
             **config_context)
        for mid in config.material_ids:
            edge("Configuration", config.id, "MaterialSample", mid, "USES")
        for sid in config.accessory_substance_ids:
            edge("Configuration", config.id, "Substance", sid, "HAS_ACCESSORY")
    for experiment_index, experiment in enumerate(record.experiments):
        node("Experiment", experiment.id, purpose=experiment.purpose.value,
             arm_kind=experiment.arm_kind, comparison_group=experiment.comparison_group,
             joint_level=experiment.joint_evidence.level.value,
             joint_evidence_ids=experiment.joint_evidence.evidence_ids,
             evidence_ids=experiment.evidence_ids,
             **({"source_experiment_id": experiment.id,
                 "record_pointer": f"/experiments/{experiment_index}",
                 "conditions_json": json.dumps([item.model_dump(mode="json") for item in experiment.conditions], ensure_ascii=False),
                 "time_window_json": experiment.time_window.model_dump_json(),
                 "energy_inputs_json": json.dumps([item.model_dump(mode="json") for item in experiment.energy_inputs], ensure_ascii=False),
                 "targets_json": json.dumps([item.model_dump(mode="json") for item in experiment.targets], ensure_ascii=False),
                 "reaction_inputs_json": json.dumps([item.model_dump(mode="json") for item in experiment.reaction_inputs], ensure_ascii=False),
                 "context_semantics": "source_facts_with_status_not_independently_validated"} if scoped else {}))
        edge("Paper", paper.id, "Experiment", experiment.id, "DESCRIBES")
        if experiment.configuration_id is not None:
            edge("Experiment", experiment.id, "Configuration", experiment.configuration_id, "USES_CONFIGURATION")
        for mid in experiment.material_ids:
            edge("Experiment", experiment.id, "MaterialSample", mid, "TESTS")
        for target in experiment.targets:
            edge("Experiment", experiment.id, "Substance", target.substance_id, "TARGETS",
                 **_relation_quantities(concentration=target.concentration, unit=target.unit, basis=target.basis),
                 role=target.role)
        for reaction in experiment.reaction_inputs:
            if reaction.material_id is not None:
                edge("Experiment", experiment.id, "MaterialSample", reaction.material_id, "HAS_REACTION_INPUT",
                     role=reaction.role, supply=reaction.source.value, component_substance_id=reaction.substance_id,
                     evidence_ids=reaction.evidence_ids, addition_evidence_ids=reaction.addition_evidence_ids or None)
            if reaction.substance_id is not None:
                edge("Experiment", experiment.id, "Substance", reaction.substance_id, "HAS_REACTION_INPUT",
                     role=reaction.role, supply=reaction.source.value,
                     addition_evidence_ids=reaction.addition_evidence_ids or None,
                     **_relation_quantities(amount=reaction.amount, unit=reaction.unit, basis=reaction.basis),
                     timing=_reported(reaction.timing),
                     stream_id=reaction.stream_id, evidence_ids=reaction.evidence_ids)
    projected_observations = set()
    for observation_index, observation in enumerate(record.observations):
        if observation.id in archived:
            # The exact point remains in the bound JSON and series index.
            # Do not promote it to an independent research-result node.
            continue
        if observation.importance == "index_only":
            continue
        if observation.origin in {ObservationOrigin.MODEL_INFERENCE, ObservationOrigin.LOCAL_CALCULATION}:
            result.warnings.append(f"Omitted non-reported observation {observation.id} ({observation.origin.value}).")
            continue
        if observation.value.status.value not in {"reported", "estimated", "explicit_absence"}:
            continue
        if not observation.value.evidence_ids:
            result.warnings.append(f"Omitted unsourced observation {observation.id}.")
            continue
        projected_observations.add(observation.id)
        estimated = (observation.origin == ObservationOrigin.PLOT_ESTIMATE
                     or observation.value.status.value == "estimated")
        author_stated = not estimated and observation.origin.value in {
            "reported_measurement", "author_calculation", "author_simulation", "author_hypothesis",
        }
        node("Observation", observation.id, metric=_observation_value(observation.metric), metric_kind=observation.metric_kind,
             endpoint_kind=observation.endpoint_kind, removal_attribution=observation.removal_attribution,
             value=_observation_value(observation.value), unit=_observation_value(observation.unit),
             **_observation_normalization(observation),
             value_status="estimated" if estimated else observation.value.status.value,
             source_value_status=observation.value.status.value,
             origin=observation.origin.value, is_estimate=estimated, author_stated=author_stated,
             is_author_measurement=author_stated and observation.origin == ObservationOrigin.REPORTED_MEASUREMENT,
             series_id=observation.series_id,
             coordinates_json=json.dumps([item.model_dump(mode="json") for item in observation.coordinates],
                                         ensure_ascii=False) if observation.coordinates else None,
             evidence_links_json=json.dumps([item.model_dump(mode="json") for item in observation.evidence_links],
                                            ensure_ascii=False) if observation.evidence_links else None,
             stream_id=observation.stream_id, measured_object=_observation_value(observation.measured_object),
             denominator=_observation_value(observation.denominator), reference=_observation_value(observation.reference),
             evidence_ids=list(dict.fromkeys(observation.evidence_ids + observation.value.evidence_ids)),
             limitations=observation.limitations,
             **({"source_observation_id": observation.id,
                 "record_pointer": f"/observations/{observation_index}",
                 "analysis_role": observation.analysis_role, "role_reason": observation.role_reason,
                 "time_window_json": observation.time_window.model_dump_json(),
                 "uncertainty_json": observation.uncertainty.model_dump_json(),
                 "numerator_json": observation.numerator.model_dump_json(),
                 "denominator_json": observation.denominator.model_dump_json(),
                 "reference_json": observation.reference.model_dump_json(),
                 "analytical_method_json": observation.analytical_method.model_dump_json(),
                 "volume_correction_json": observation.volume_correction.model_dump_json()} if scoped else {}))
        edge("Experiment", observation.experiment_id, "Observation", observation.id, "REPORTS")
        if observation.material_id is not None:
            edge("Observation", observation.id, "MaterialSample", observation.material_id, "MEASURES")
        if observation.substance_id is not None:
            edge("Observation", observation.id, "Substance", observation.substance_id, "ANALYZES")
    if scoped:
        observations = {observation.id: observation for observation in record.observations}
        for index, series in enumerate(record.quantitative_coverage):
            member_ids = series.observation_ids
            node("ObservationSeries", series.id, label=series.label, data_kind=series.data_kind,
                 scope=series.scope, coverage_state=series.state, expected_points=series.expected_points,
                 source_id=series.source_id, block_id=series.block_id,
                 locator_json=series.locator.model_dump_json(), reason=series.reason,
                 limitations=series.limitations, member_observation_ids=member_ids,
                 recorded_observation_count=len(member_ids),
                 projected_observation_count=sum(identifier in projected_observations for identifier in member_ids),
                 archived_dense_observation_count=sum(identifier in archived for identifier in member_ids),
                 record_fingerprint=fingerprint, record_pointer=f"/quantitative_coverage/{index}",
                 archive_semantics="Members are retained in the bound source_record JSON; an index is not independent experimental evidence.",
                 scientific_validation="not_established")
            edge("Paper", paper.id, "ObservationSeries", series.id, "HAS_SERIES")
            experiments = {observations[identifier].experiment_id for identifier in member_ids if identifier in observations}
            for identifier in sorted(experiments):
                edge("Experiment", identifier, "ObservationSeries", series.id, "HAS_SERIES")
            for identifier in member_ids:
                if identifier in projected_observations:
                    edge("Observation", identifier, "ObservationSeries", series.id, "IN_SERIES")
    for experiment in record.experiments:
        for pair in experiment.pairs:
            if {pair.left_observation_id, pair.right_observation_id} <= projected_observations:
                qualified = pair_qualifications.get((experiment.id, frozenset((pair.left_observation_id, pair.right_observation_id))), {})
                edge("Observation", pair.left_observation_id, "Observation", pair.right_observation_id,
                     "PAIRED_WITH", pairing_level=pair.level.value, evidence_ids=pair.evidence_ids,
                     qualified_flux_parent=qualified.get("eligible", False) if record.rule_version == "0.9" else None,
                     same_arm_co_observed=qualified.get("same_arm_co_observed", False) if record.rule_version == "0.9" else None,
                     qualified_degradation=qualified.get("eligible_as_degradation", False) if record.rule_version == "0.9" else None,
                     qualification_limits=qualified.get("restrictions"),
                     differing_conditions=pair.differing_conditions, unresolved_conditions=pair.unresolved_conditions)
    for comparison in record.comparisons:
        node("DesignComparison", comparison.id, operation=_reported(comparison.operation),
             causal_status=comparison.causal_status, limitations=comparison.limitations,
             evidence_ids=comparison.evidence_ids,
             **({"design_record_json": comparison.model_dump_json(),
                 "interpretation_semantics": "source_claims_with_status_not_causal_validation"}
                if record.design_scope else {}))
        for mid in comparison.material_ids:
            edge("DesignComparison", comparison.id, "MaterialSample", mid, "COMPARES")
        for oid in comparison.observation_ids:
            if oid in projected_observations:
                edge("DesignComparison", comparison.id, "Observation", oid, "USES_EVIDENCE")
    # Shared controls remain their original observations. A separately typed
    # reference never creates REPORTS/IN_SERIES/PAIRED_WITH edges or replicates.
    # Diagnostic projection must not turn invalid applicability into a link.
    from .reference_associations import reference_association_issues
    if scoped and record.reference_associations and not reference_association_issues(record):
        target_labels = {"experiment": "Experiment", "series": "ObservationSeries", "comparison": "DesignComparison"}
        for index, reference in enumerate(record.reference_associations):
            node("ReferenceAssociation", reference.id,
                 source_reference_id=reference.id, label=reference.relationship,
                 target_kind=reference.target_kind, target_id=reference.target_id,
                 relationship=reference.relationship, applicability=reference.applicability,
                 observation_ids=reference.observation_ids, evidence_ids=reference.evidence_ids,
                 rationale=reference.rationale, matched_conditions=reference.matched_conditions,
                 differing_conditions=reference.differing_conditions,
                 unresolved_conditions=reference.unresolved_conditions, limitations=reference.limitations,
                 record_pointer=f"/reference_associations/{index}", record_fingerprint=fingerprint,
                 reference_record_json=reference.model_dump_json(),
                 reference_semantics="Existing observations reused as an evidenced reference, not a new measurement.",
                 automatic_pairing=False, independent_replicates_added=0,
                 physical_run_identity_inferred=False, scientific_validation="not_established")
            edge(target_labels[reference.target_kind], reference.target_id,
                 "ReferenceAssociation", reference.id, "USES_REFERENCE",
                 applicability=reference.applicability, reference_relationship=reference.relationship)
            for oid in reference.observation_ids:
                if oid in projected_observations:
                    edge("ReferenceAssociation", reference.id, "Observation", oid,
                         "REFERENCES_OBSERVATION", ownership_changed=False, independent_replicate=False)
    # Invalid-mode output must not leave dangling graph edges.
    node_ids = {n.id for n in result.nodes}
    result.edges = [e for e in result.edges if e.source in node_ids and e.target in node_ids]
    return result


def _identifier(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def _literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_literal(item) for item in value) + "]"
    # Cypher strings use explicit escaping; do not interpolate raw source text.
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'").replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t") + "'"


def _map(properties: dict[str, Any]) -> str:
    return "{" + ", ".join(f"{_identifier(key)}: {_literal(value)}" for key, value in properties.items()) + "}"


def render_cypher(projection: GraphProjection) -> str:
    """Return an explicit offline export. This function never executes Cypher."""
    lines = ["// Offline projection only. Scientific acceptance and database import are separate steps."]
    for node in projection.nodes:
        lines.append(f"MERGE (n:{_identifier(node.label)} {{id: {_literal(node.id)}}}) SET n += {_map(node.properties)};")
    for edge in projection.edges:
        lines.append(f"MATCH (a {{id: {_literal(edge.source)}}}), (b {{id: {_literal(edge.target)}}}) "
                     f"MERGE (a)-[r:{_identifier(edge.relationship)} {{id: {_literal(edge.id)}}}]->(b) SET r += {_map(edge.properties)};")
    return "\n".join(lines) + "\n"
