"""Synthetic source/conversion separation; no provider or database is used."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from test_projection import sample, fact
from test_web_graph import Driver, FIRST, graph_context, graph_settings
from test_web_model import fixture, model_fixture, graph_context as record_context, graph_result, read
from watertreatment.graph_store import _rows
from watertreatment.projection import project_record, render_cypher
from watertreatment.storage import digest_json
from watertreatment.web_graph import read_graph
from watertreatment.web_model import QuestionTools


def observation_node(record):
    return next(n for n in project_record(record).nodes if n.label == "Observation")


@pytest.mark.parametrize("source,converted,source_unit,converted_unit", [
    (-2.3, 2.3, "kg m-2", None),
    (-41, 41, "%", None),
    (0, 0, "mg", "g"),
    (2300, 2.3, "mg", "g"),
])
def test_source_value_metric_and_unit_remain_paired(source, converted, source_unit, converted_unit):
    record = sample()
    o = record.observations[0]
    o.metric = fact("Source-defined quantity")
    o.metric.normalized = "Different declared convention"
    o.metric.normalization_reason = "Keep as a separately registered name."
    o.value = fact(source)
    o.value.normalized = converted
    o.value.raw = f"Source axis value: {source}"
    o.value.normalization_reason = "Synthetic conversion statement; not proof of physical equivalence."
    o.unit = fact(source_unit)
    o.unit.normalized = converted_unit
    o.unit.normalization_reason = "Synthetic unit conversion." if converted_unit else None
    o.origin = "plot_estimate"
    o.value.status = "estimated"
    original = record.model_dump_json()
    properties = observation_node(record).properties
    assert properties["value"] == source
    assert properties["unit"] == source_unit
    assert properties["metric"] == "Source-defined quantity"
    assert properties["normalized_value"] == converted
    assert properties.get("normalized_unit") == converted_unit
    assert properties["normalized_metric"] == "Different declared convention"
    assert properties["value_normalization_reason"] == o.value.normalization_reason
    assert properties["value_raw"] == o.value.raw
    assert properties["value_semantics"] == "source_fact_value_v1"
    assert properties["normalization_semantics"] == "separate_unvalidated_conversion"
    assert properties["normalized_value_status"] == "unvalidated_conversion"
    assert properties["is_estimate"] is True
    assert properties["is_author_measurement"] is False
    assert record.model_dump_json() == original


def test_separating_conversion_does_not_invent_metric_unit_or_reason():
    record = sample()
    o = record.observations[0]
    o.value.normalized = -o.value.value
    properties = observation_node(record).properties
    assert properties["normalized_value"] == -2.0
    assert properties["normalized_value_status"] == "unvalidated_conversion"
    assert "normalized_metric" not in properties and "normalized_unit" not in properties
    assert "value_normalization_reason" not in properties
    assert "metric" not in properties and "unit" not in properties


def test_ordinary_conversion_stays_available_without_replacing_source_quantity():
    record = sample()
    o = record.observations[0]
    o.metric = fact("Source amount")
    o.value = fact(2300)
    o.value.normalized = 2.3
    o.unit = fact("mg")
    o.unit.normalized = "g"
    graph = project_record(record)
    properties = next(n.properties for n in graph.nodes if n.label == "Observation")
    assert (properties["value"], properties["unit"]) == (2300, "mg")
    assert (properties["normalized_value"], properties["normalized_unit"]) == (2.3, "g")
    # The explicit atomic importer retains all properties in a different
    # immutable graph digest; it does not mutate the older snapshot.
    rows, _ = _rows(graph, digest_json(graph.model_dump(mode="json")))
    stored = rows["Observation"][0]["properties"]
    assert stored["value"] == 2300 and stored["normalized_value"] == 2.3
    assert "`normalized_value`: 2.3" in render_cypher(graph)


@pytest.mark.parametrize("status", ["conflict", "inferred", "not_reported", "unread"])
def test_conversion_cannot_make_an_unresolved_value_publishable(status):
    record = sample()
    o = record.observations[0]
    o.value.status = status
    o.value.normalized = 731
    graph = project_record(record, allow_invalid=True)
    assert not any(n.label == "Observation" for n in graph.nodes)


@pytest.mark.parametrize("field,label", [
    ("properties", "labels"), ("source_properties", "source_labels"), ("target_properties", "target_labels"),
])
@pytest.mark.parametrize("source_preserved", [False, True])
def test_graph_response_marks_legacy_values_without_rewriting_them(
        graph_settings, graph_context, field, label, source_preserved):
    properties = {"value": 2.3, "unit": "kg m-2", "metric": "Signed change", "origin": "plot_estimate"}
    if source_preserved:
        properties.update(value=-2.3, normalized_value=2.3, value_semantics="source_fact_value_v1")
    row = {"snapshot_id": FIRST, field: properties, label: ["WTEntity", "Observation"]}
    original = copy.deepcopy(row)
    result = read_graph(graph_settings, graph_context,
        {"mode": "nodes" if field == "properties" else "relationships"},
        live=True, driver_factory=Driver([row]).factory)
    actual = result["items"][0]
    assert actual[field] == properties
    assert row == original
    assert actual["observation_value_semantics"][field] == (
        "source_values_with_separate_normalization" if source_preserved else "legacy_projection_unverified")
    assert result["legacy_observation_occurrences"] == (0 if source_preserved else 1)
    assert "Do not use legacy graph values directly" in result["evidence_instruction"]
    assert "wt_query_records" in result["evidence_instruction"]


@pytest.mark.parametrize("source_preserved", [False, True])
def test_question_tool_retains_normalization_limits_and_original_values(model_fixture, tmp_path, source_preserved):
    detail = model_fixture.records.get(model_fixture.run.name)
    result = graph_result(detail)
    properties = {"value": 2.3, "unit": "kg m-2", "metric": "Signed change",
        "origin": "plot_estimate", "value_status": "reported"}
    if source_preserved:
        properties.update(value=-2.3, normalized_value=2.3, value_semantics="source_fact_value_v1")
    result["items"][0].update(labels=["Observation"], properties=properties)
    original = copy.deepcopy(result)
    run = tmp_path / "source-normalization-tools"
    run.mkdir()
    tool = QuestionTools(read(Path(detail["sources_bundle"])), run, max_calls=1,
        expected_inputs=[], records=[detail], graph_context=record_context(detail), graph_reader=lambda _: result)
    response = tool.handle("wt_query_graph", {})
    assert response["success"]
    payload = json.loads(response["contentItems"][0]["text"])
    actual = payload["items"][0]
    assert actual["properties"]["value"] == properties["value"]
    assert actual["properties"]["value_status"] == "estimated"
    assert actual["observation_value_semantics"]["properties"] == (
        "source_values_with_separate_normalization" if source_preserved else "legacy_projection_unverified")
    assert "Do not use legacy graph values directly" in payload["evidence_instruction"]
    assert "recomputation: first call wt_query_records" in payload["evidence_instruction"]
    assert payload["normalization_instruction"] in payload["evidence_instruction"]
    assert result == original
