import json

import pytest

from watertreatment.domain import (
    Component, Experiment, ExtractionRecord, Fact, MaterialSample, Observation,
    Paper, EvidenceRef, ReactionInput, Substance, NamedFact, ObservationEvidenceLink, QuantitativeCoverage,
)
from watertreatment.projection import project_record, render_cypher
from watertreatment.quantitative import quantitative_summary


def fact(value):
    return Fact(value=value, status="reported", evidence_ids=["evidence"])


def sample():
    return ExtractionRecord(
        paper=Paper(id="p", title=fact("Source"), source_ids=["source"]),
        materials=[MaterialSample(id="m", role="primary_final", name=fact("Composite"))],
        substances=[Substance(id="oxidant", name=fact("Reagent"))],
        experiments=[Experiment(id="exp", material_ids=["m"], reaction_inputs=[
            ReactionInput(substance_id="oxidant", role="oxidant", source="external", amount=fact(1))])],
        observations=[Observation(id="rate", experiment_id="exp", material_id="m", value=fact(2.0), origin="reported_measurement")],
        evidence=[EvidenceRef(id="evidence", source_id="source", block_id="block")])


def test_graph_keeps_operating_oxidant_out_of_material_composition():
    graph = project_record(sample())
    assert any(e.relationship == "HAS_REACTION_INPUT" for e in graph.edges)
    assert not any(e.relationship == "HAS_RETAINED_COMPONENT" for e in graph.edges)
    assert not any(n.label in {"Fact", "EvidenceRef", "Stream"} for n in graph.nodes)


def test_legacy_plot_estimate_remains_queryable_without_rewriting_source_status():
    record = sample()
    record.schema_version = "0.1.0"
    record.observations[0].origin = "plot_estimate"
    before = record.model_dump_json()
    graph = project_record(record)
    properties = next(n.properties for n in graph.nodes if n.label == "Observation")
    assert properties["value"] == 2.0
    assert properties["value_status"] == "estimated"
    assert properties["source_value_status"] == "reported"
    assert properties["origin"] == "plot_estimate"
    assert properties["is_estimate"] is True
    assert properties["author_stated"] is False and properties["is_author_measurement"] is False
    assert record.model_dump_json() == before


@pytest.mark.parametrize("origin", ["plot_estimate", "author_simulation"])
def test_new_explicit_estimate_is_projected_with_source_provenance(origin):
    record = sample()
    record.observations[0].origin = origin
    record.observations[0].value.status = "estimated"
    graph = project_record(record)
    properties = next(n.properties for n in graph.nodes if n.label == "Observation")
    assert properties["value"] == 2.0
    assert properties["source_value_status"] == properties["value_status"] == "estimated"
    assert properties["is_estimate"] is True and properties["author_stated"] is False
    assert properties["origin"] == origin


@pytest.mark.parametrize("origin,measurement", [("reported_measurement", True), ("author_calculation", False), ("author_simulation", False)])
def test_author_calculations_and_simulations_are_not_presented_as_measurements(origin, measurement):
    record = sample()
    record.observations[0].origin = origin
    properties = next(n.properties for n in project_record(record).nodes if n.label == "Observation")
    assert properties["value_status"] == "reported" and properties["is_estimate"] is False
    assert properties["author_stated"] is True
    assert properties["is_author_measurement"] is measurement


def test_unresolved_origin_does_not_become_an_author_stated_value():
    record = sample()
    record.observations[0].origin = "unresolved"
    properties = next(n.properties for n in project_record(record).nodes if n.label == "Observation")
    assert properties["author_stated"] is False and properties["is_author_measurement"] is False


def test_missing_quantitative_inventory_is_not_reported_as_complete():
    properties = next(n.properties for n in project_record(sample()).nodes if n.label == "Paper")
    assert properties["quantitative_coverage_assessment"] == "not_assessed"
    assert properties["quantitative_inventory_items"] == 0
    assert properties["scientific_validation"] == "not_established"


def test_series_coordinates_and_field_support_survive_graph_projection():
    record = sample()
    observation = record.observations[0]
    observation.series_id = "cycle-performance"
    observation.coordinates = [NamedFact(name="cycle", fact=fact(2), unit=fact("cycle"))]
    observation.evidence_links = [ObservationEvidenceLink(
        evidence_id="evidence", supported_fields=["value", "coordinates"], support_type="direct",
        explanation="Point marker in the second cycle of the identified curve.")]
    record.quantitative_coverage = [QuantitativeCoverage(id="cycle-performance", source_id="source",
        block_id="block", label="Cycle performance", data_kind="cycle_series", scope="final_material",
        state="partial", expected_points=3, observation_ids=[observation.id],
        reason="Only one of three visible cycle points has been extracted.")]
    graph = project_record(record)
    properties = next(n.properties for n in graph.nodes if n.label == "Observation")
    assert properties["series_id"] == "cycle-performance"
    assert json.loads(properties["coordinates_json"])[0]["fact"]["value"] == 2
    assert json.loads(properties["evidence_links_json"])[0]["supported_fields"] == ["value", "coordinates"]
    paper = next(n.properties for n in graph.nodes if n.label == "Paper")
    assert paper["quantitative_coverage_assessment"] == "model_inventory_not_independent_validation"
    assert paper["quantitative_incomplete_items"] == 1
    assert paper["scientific_validation"] == "not_established"


@pytest.mark.parametrize("limitation", ["unknown_expected_count", "declared_source_limitation", "missing_coordinates"])
def test_graph_retains_quantitative_restrictions_even_when_inventory_declares_complete(limitation):
    """A complete label alone cannot make incomplete/ambiguous series comparable."""
    record = sample()
    observation = record.observations[0]
    observation.series_id = "time-course"
    observation.coordinates = [] if limitation == "missing_coordinates" else [
        NamedFact(name="time", fact=fact(20), unit=fact("min"))]
    observation.evidence_links = [ObservationEvidenceLink(
        evidence_id="evidence", supported_fields=["value"], support_type="direct",
        explanation="The plotted value belongs to the identified final-material series.")]
    record.quantitative_coverage = [QuantitativeCoverage(
        id="time-course", source_id="source", block_id="block", label="Final-material time course",
        data_kind="time_series", scope="final_material", state="complete",
        expected_points=None if limitation == "unknown_expected_count" else 1,
        observation_ids=[observation.id],
        limitations=["The marker overlaps a second curve; the series identity remains unresolved."]
            if limitation == "declared_source_limitation" else [])]
    before = record.model_dump_json()
    summary = quantitative_summary(record)
    assert len(summary["incomplete_series"]) == 1
    assert summary["incomplete_series"][0]["id"] == "time-course"
    assert summary["incomplete_series"][0]["limitations"]
    graph = project_record(record)
    paper = next(n.properties for n in graph.nodes if n.label == "Paper")
    assert paper["quantitative_incomplete_items"] == len(summary["incomplete_series"])
    assert paper["scientific_validation"] == "not_established"
    assert record.model_dump_json() == before


def test_preloaded_consumable_and_retained_component_use_different_edges():
    record = sample()
    record.materials[0].components = [Component(substance_id="oxidant", retention="preloaded_consumable", evidence_ids=["evidence"])]
    graph = project_record(record)
    assert any(e.relationship == "PRELOADS" for e in graph.edges)
    assert not any(e.relationship == "HAS_RETAINED_COMPONENT" for e in graph.edges)


def test_inference_and_conflicts_do_not_become_reported_graph_observations():
    record = sample()
    inference = record.observations[0].model_copy(deep=True)
    inference.id = "prediction"
    inference.origin = "model_inference"
    inference.value = Fact(value=4.8, status="inferred")
    record.observations.append(inference)
    conflict = record.observations[0].model_copy(deep=True)
    conflict.id = "conflict"
    conflict.value = Fact(status="conflict", alternatives=[{"value": 80}, {"value": 99}])
    record.observations.append(conflict)
    graph = project_record(record)
    observed = [n for n in graph.nodes if n.label == "Observation"]
    assert len(observed) == 1 and observed[0].properties["value"] == 2.0
    assert all(e.target != 'prediction' for e in graph.edges)


def test_invalid_record_requires_explicit_diagnostic_override():
    record = sample()
    record.observations[0].experiment_id = "wrong"
    with pytest.raises(ValueError, match="MISSING_REFERENCE"):
        project_record(record)
    graph = project_record(record, allow_invalid=True)
    node_ids = {n.id for n in graph.nodes}
    assert all(e.source in node_ids and e.target in node_ids for e in graph.edges)


def test_local_identifiers_are_namespaced_and_cypher_remains_an_offline_string():
    left = project_record(sample())
    other = sample()
    other.paper.id = "second paper"
    right = project_record(other)
    assert not ({n.id for n in left.nodes} & {n.id for n in right.nodes})
    record = sample()
    record.paper.title = fact("Author's claim\nMATCH (n) DETACH DELETE n; //")
    cypher = render_cypher(project_record(record))
    assert isinstance(cypher, str)
    assert "Author\\'s claim\\nMATCH" in cypher
    assert "Offline projection only" in cypher


def test_repeated_input_events_do_not_collapse_into_one_cypher_relationship():
    record = sample()
    second = record.experiments[0].reaction_inputs[0].model_copy(deep=True)
    second.timing = fact("after cycle 2")
    record.experiments[0].reaction_inputs.append(second)
    graph = project_record(record)
    inputs = [e for e in graph.edges if e.relationship == "HAS_REACTION_INPUT"]
    assert len(inputs) == 2 and inputs[0].id != inputs[1].id
    assert render_cypher(graph).count("r:`HAS_REACTION_INPUT` {id:") == 2


def test_route_evidence_does_not_replace_missing_value_evidence():
    record = sample()
    record.observations[0].value.evidence_ids = []
    record.observations[0].evidence_ids = ["evidence"]
    graph = project_record(record)
    assert not any(n.label == "Observation" for n in graph.nodes)
    assert graph.source_check == "not_performed"


def test_revised_candidate_cannot_merge_with_withdrawn_values_from_old_candidate():
    original = sample()
    old_graph = project_record(original)
    revised = original.model_copy(deep=True)
    revised.paper.title = Fact(status="conflict", alternatives=[{"value": "Source"}, {"value": "Other source"}])
    revised.observations[0].value = Fact(status="read_unquantified")
    new_graph = project_record(revised)
    assert old_graph.record_fingerprint != new_graph.record_fingerprint
    assert not ({n.id for n in old_graph.nodes} & {n.id for n in new_graph.nodes})
    assert not ({e.id for e in old_graph.edges} & {e.id for e in new_graph.edges})
    assert "title" not in next(n for n in new_graph.nodes if n.label == "Paper").properties
    assert not any(n.label == "Observation" for n in new_graph.nodes)
    # Identical content remains idempotent when exported again.
    assert project_record(original).model_dump() == old_graph.model_dump()
