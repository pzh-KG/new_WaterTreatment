"""Source-unit/basis safety in synthetic graph relationships and context facts."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from test_projection import sample, fact
from test_web_graph import Driver, FIRST, graph_context, graph_settings
from test_web_model import fixture, model_fixture, graph_context as record_context, graph_result, read
from watertreatment.domain import Component, Target
from watertreatment.projection import project_record
from watertreatment.web_graph import read_graph
from watertreatment.web_model import QuestionTools


RELATIONS = ["HAS_RETAINED_COMPONENT", "PRELOADS", "TARGETS", "HAS_REACTION_INPUT"]


def relation_record(relationship):
    record = sample()
    amount, unit, basis = fact(20), fact("mg"), fact("source basis")
    amount.normalized, amount.normalization_reason = 0.02, "Synthetic conversion."
    unit.normalized, unit.normalization_reason = "g", "Synthetic unit conversion."
    basis.normalized, basis.normalization_reason = "different basis", "Candidate reference change."
    if relationship in {"HAS_RETAINED_COMPONENT", "PRELOADS"}:
        record.materials[0].components = [Component(substance_id="oxidant", amount=amount, unit=unit, basis=basis,
            retention="retained" if relationship == "HAS_RETAINED_COMPONENT" else "preloaded_consumable")]
    elif relationship == "TARGETS":
        record.experiments[0].targets = [Target(substance_id="oxidant", concentration=amount, unit=unit, basis=basis)]
    else:
        reaction = record.experiments[0].reaction_inputs[0]
        reaction.amount, reaction.unit, reaction.basis = amount, unit, basis
    return record


@pytest.mark.parametrize("relationship", RELATIONS)
def test_relationship_source_value_unit_basis_and_conversions_are_separate(relationship):
    record = relation_record(relationship)
    before = record.model_dump_json()
    props = next(e.properties for e in project_record(record).edges if e.relationship == relationship)
    name = "concentration" if relationship == "TARGETS" else "amount"
    assert (props[name], props["unit"], props["basis"]) == (20, "mg", "source basis")
    assert (props[f"normalized_{name}"], props["normalized_unit"], props["normalized_basis"]) == (
        0.02, "g", "different basis")
    assert props["quantity_semantics"] == "source_fact_value_v1"
    for field in (name, "unit", "basis"):
        assert props[f"{field}_status"] == "reported"
        assert props[f"normalized_{field}_status"] == "unvalidated_conversion"
        assert props[f"{field}_evidence_ids"] == ["evidence"]
        assert props[f"{field}_normalization_reason"]
    assert record.model_dump_json() == before


@pytest.mark.parametrize("status", ["conflict", "inferred", "unread", "estimated"])
def test_relation_conversion_does_not_bypass_previous_value_gate(status):
    record = relation_record("HAS_REACTION_INPUT")
    record.experiments[0].reaction_inputs[0].amount.status = status
    props = next(e.properties for e in project_record(record, allow_invalid=True).edges
                 if e.relationship == "HAS_REACTION_INPUT")
    assert props["amount_status"] == status
    assert "amount" not in props and "normalized_amount" not in props
    assert props["unit"] == "mg"


def test_missing_conversion_unit_and_basis_are_not_invented_and_zero_is_kept():
    record = sample()
    reaction = record.experiments[0].reaction_inputs[0]
    reaction.amount = fact(0)
    reaction.amount.normalized = 0
    reaction.unit, reaction.basis = fact("source unit"), fact("source basis")
    props = next(e.properties for e in project_record(record).edges if e.relationship == "HAS_REACTION_INPUT")
    assert props["amount"] == props["normalized_amount"] == 0
    assert "normalized_unit" not in props and "normalized_basis" not in props
    assert "amount_normalization_reason" not in props


def test_observation_object_denominator_and_reference_preserve_source_context():
    record = sample()
    o = record.observations[0]
    for name, value, normalized in [("measured_object", "source object", "different object"),
            ("denominator", 20, 0.02), ("reference", "source reference", "different reference")]:
        item = fact(value)
        item.normalized, item.normalization_reason = normalized, "Candidate context conversion."
        setattr(o, name, item)
    before = record.model_dump_json()
    props = next(n.properties for n in project_record(record).nodes if n.label == "Observation")
    for name in ("measured_object", "denominator", "reference"):
        item = getattr(o, name)
        assert props[name] == item.value
        assert props[f"normalized_{name}"] == item.normalized
        assert props[f"normalized_{name}_status"] == "unvalidated_conversion"
        assert props[f"{name}_status"] == "reported"
        assert props[f"{name}_normalization_reason"] == item.normalization_reason
    assert props["context_semantics"] == "source_fact_value_v1"
    assert record.model_dump_json() == before
    # This narrow fix leaves existing name/formula alias normalization intact.
    record.materials[0].name.normalized = "Canonical alias"
    material = next(n.properties for n in project_record(record).nodes if n.label == "MaterialSample")
    assert material["name"] == "Canonical alias"


def test_estimated_context_retains_its_own_status_separate_from_reported_value():
    record = sample()
    record.observations[0].denominator = fact(20)
    record.observations[0].denominator.status = "estimated"
    record.observations[0].denominator.normalized = 0.02
    props = next(n.properties for n in project_record(record).nodes if n.label == "Observation")
    assert props["value_status"] == "reported"
    assert props["denominator"] == 20 and props["denominator_status"] == "estimated"
    assert props["normalized_denominator_status"] == "unvalidated_conversion"


@pytest.mark.parametrize("relationship", RELATIONS)
@pytest.mark.parametrize("source_preserved", [False, True])
def test_graph_relation_response_flags_legacy_without_rewriting_properties(
        graph_settings, graph_context, relationship, source_preserved):
    props = {"amount": 0.02, "concentration": 0.02, "unit": "mg", "basis": "old stored basis"}
    if source_preserved:
        props["quantity_semantics"] = "source_fact_value_v1"
    row = {"snapshot_id": FIRST, "relationship": relationship, "properties": props}
    original = copy.deepcopy(row)
    response = read_graph(graph_settings, graph_context, {"mode": "relationships"},
        live=True, driver_factory=Driver([row]).factory)
    item = response["items"][0]
    assert item["properties"] == props and row == original
    assert item["relationship_quantity_semantics"] == (
        "source_values_with_separate_normalization" if source_preserved else "legacy_projection_unverified")
    assert response["legacy_quantitative_relationship_occurrences"] == int(not source_preserved)
    assert "before any condition, dosage or composition comparison" in response["evidence_instruction"]


def test_question_response_does_not_treat_first_source_value_version_as_safe_context(model_fixture, tmp_path):
    detail = model_fixture.records.get(model_fixture.run.name)
    response = graph_result(detail)
    response["items"][0].update(relationship="TARGETS", properties={"concentration": 0.02, "unit": "mg/L"},
        source_labels=["Observation"], source_properties={"value": -2.3, "value_status": "reported",
            "origin": "reported_measurement", "value_semantics": "source_fact_value_v1",
            "denominator": "legacy converted denominator"})
    original = copy.deepcopy(response)
    run = tmp_path / "relation-normalization-tools"
    run.mkdir()
    tool = QuestionTools(read(Path(detail["sources_bundle"])), run, max_calls=1,
        expected_inputs=[], records=[detail], graph_context=record_context(detail), graph_reader=lambda _: response)
    result = tool.handle("wt_query_graph", {"mode": "relationships"})
    assert result["success"]
    payload = json.loads(result["contentItems"][0]["text"])
    item = payload["items"][0]
    assert item["observation_value_semantics"]["source_properties"] == "source_values_with_separate_normalization"
    assert item["observation_context_semantics"]["source_properties"] == "legacy_context_unverified"
    assert item["relationship_quantity_semantics"] == "legacy_projection_unverified"
    assert "before any condition, dosage or composition comparison" in payload["evidence_instruction"]
    assert "even when value_semantics is already source_fact_value_v1" in payload["evidence_instruction"]
    assert item["properties"] == original["items"][0]["properties"]
    assert item["source_properties"]["denominator"] == "legacy converted denominator"
    assert response == original
