"""Synthetic coordinate regressions; no provider, database or real-paper answers."""
import pytest

from watertreatment.domain import Fact, NamedFact
from watertreatment.projection import project_record
from watertreatment.quantitative import (
    coordinate_value_key, has_numeric_value, has_quantitative_value,
    has_source_numeric_point, has_usable_coordinates, quantitative_summary,
)
from watertreatment.validation import validate_record
from test_quantitative import sample, series, codes


def coordinate(name, value, *, status="reported", unit=None, basis=None):
    return NamedFact(name=name, fact=Fact(value=value, status=status, evidence_ids=["plot"]),
        unit=Fact(value=unit, status="reported", evidence_ids=["plot"]) if unit is not None else Fact(),
        basis=Fact(value=basis, status="reported", evidence_ids=["plot"]) if basis is not None else Fact())


@pytest.mark.parametrize("schema", ["0.1.0", "0.2.0"])
@pytest.mark.parametrize("kind", ["time_series", "cycle_series"])
def test_group_name_does_not_replace_required_axis_and_preserves_record(schema, kind):
    record = sample(schema=schema, origin="reported_measurement", status="reported")
    series(record, state="complete", expected=1)
    record.quantitative_coverage[0].data_kind = kind
    record.observations[0].coordinates = [coordinate("condition_group", "matrix A")]
    before = record.model_dump(mode="json")
    assert has_usable_coordinates(record.observations[0])  # old untyped availability API
    assert not has_usable_coordinates(record.observations[0], kind)
    assert codes(record, "error") == set()
    assert "SERIES_POINT_COORDINATE_MISSING" in codes(record, "warning")
    summary = quantitative_summary(record)
    assert summary["incomplete_series"][0]["point_count_matched"] is True
    assert summary["incomplete_series"][0]["coordinates_available"] is False
    paper = next(n for n in project_record(record).nodes if n.label == "Paper")
    assert paper.properties["quantitative_incomplete_items"] == 1
    assert record.model_dump(mode="json") == before


@pytest.mark.parametrize("name,value", [
    ("time", -30), ("elapsed_time", 10.0), ("reaction_time", 0),
    ("Elapsed Time", 10), ("illumination-time", 5), ("t", 1),
    ("clock_time", "09:20"), ("time_of_day", "9:20"), ("local_time", "23:59:59"),
])
def test_explicit_time_coordinates_and_signed_dark_period_are_accepted(name, value):
    record = sample()
    series(record, state="complete", expected=1)
    record.observations[0].coordinates = [coordinate(name, value)]
    assert has_usable_coordinates(record.observations[0], "time_series")
    assert not quantitative_summary(record)["incomplete_series"]


@pytest.mark.parametrize("name,value", [
    ("condition_group", "sample A"), ("storage_duration", 3), ("x", 10),
    ("time", "10"), ("time", "after reaction"), ("cycle_index", 1),
    ("clock_time", "24:00"), ("clock_time", "10:61"),
    ("clock_time", "09:20 tomorrow"), ("clock_time", 10),
])
def test_unknown_semantics_numeric_text_and_invalid_clock_are_not_guessed(name, value):
    record = sample()
    record.observations[0].coordinates = [coordinate(name, value)]
    assert not has_usable_coordinates(record.observations[0], "time_series")


@pytest.mark.parametrize("name", ["cycle", "cycle_index", "cycle_number", "cycle count", "cycle-no"])
def test_explicit_cycle_names_are_accepted_without_converting_units(name):
    record = sample()
    record.observations[0].coordinates = [coordinate(name, 2.0)]
    assert has_usable_coordinates(record.observations[0], "cycle_series")


@pytest.mark.parametrize("kind", ["condition_series", "table_cells"])
def test_category_coordinates_remain_usable(kind):
    record = sample()
    series(record, state="complete", expected=1)
    record.quantitative_coverage[0].data_kind = kind
    record.observations[0].coordinates = [coordinate("condition_group", "sample A")]
    assert has_usable_coordinates(record.observations[0], kind)
    assert quantitative_summary(record)["incomplete_series"] == []


def test_known_axis_does_not_hide_another_missing_coordinate():
    record = sample()
    record.observations[0].coordinates = [coordinate("time", 10),
        NamedFact(name="replicate", fact=Fact(status="not_reported"))]
    assert not has_usable_coordinates(record.observations[0], "time_series")


def test_inferred_time_is_not_promoted_by_free_text_basis():
    record = sample()
    series(record, state="complete", expected=1)
    record.observations[0].coordinates = [coordinate("elapsed_time", 10,
        status="inferred", basis="Derived from two author clock readings")]
    assert not has_usable_coordinates(record.observations[0], "time_series")
    assert quantitative_summary(record)["incomplete_series"][0]["coordinates_available"] is False
    assert "SERIES_POINT_COORDINATE_MISSING" in codes(record, "warning")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_untyped_nonfinite_values_do_not_count_or_certify_coordinates(bad):
    observation = {"origin": "reported_measurement", "value": {
        "value": bad, "status": "reported", "evidence_ids": ["plot"]},
        "coordinates": [{"name": "time", "fact": {"value": bad, "status": "reported"}}]}
    assert not has_numeric_value(observation)
    assert not has_quantitative_value(observation)
    assert not has_source_numeric_point(observation)
    assert not has_usable_coordinates(observation)
    assert not has_usable_coordinates(observation, "time_series")
    observation["value"]["value"] = [0, bad]
    assert not has_quantitative_value(observation)
    assert coordinate_value_key(bad)[0] == "literal"
    with pytest.raises(ValueError):
        Fact(value=bad, status="reported")


def duplicate_record(left=10, right=10.0):
    record = sample(origin="reported_measurement", status="reported")
    series(record, state="complete", expected=2)
    record.observations[0].coordinates = [coordinate("time", left, unit="min", basis="reaction start")]
    second = record.observations[0].model_copy(deep=True)
    second.id = "point2"
    second.coordinates[0].fact.value = right
    record.observations.append(second)
    record.quantitative_coverage[0].observation_ids.append("point2")
    return record


@pytest.mark.parametrize("left,right", [(10, 10.0), (0, -0.0), (-30, -30.0)])
def test_equal_finite_numbers_detect_duplicate_without_merging(left, right):
    record = duplicate_record(left, right)
    before = record.model_dump(mode="json")
    assert "DUPLICATE_SERIES_COORDINATES" in codes(record, "error")
    with pytest.raises(ValueError, match="DUPLICATE_SERIES_COORDINATES"):
        project_record(record)
    assert record.model_dump(mode="json") == before
    assert len(record.observations) == 2


@pytest.mark.parametrize("difference", ["unit", "basis", "replicate", "numerically_different"])
def test_distinct_units_reference_origins_and_explicit_replicates_not_collapsed(difference):
    record = duplicate_record()
    first, second = record.observations
    if difference == "unit":
        second.coordinates[0].unit.value = "s"
    elif difference == "basis":
        second.coordinates[0].basis.value = "illumination start"
    elif difference == "replicate":
        first.coordinates.append(coordinate("replicate_id", "A"))
        second.coordinates.append(coordinate("replicate_id", "B"))
    else:
        second.coordinates[0].fact.value = 10.0001
    before = record.model_dump(mode="json")
    assert "DUPLICATE_SERIES_COORDINATES" not in codes(record, "error")
    assert len([n for n in project_record(record).nodes if n.label == "Observation"]) == 2
    assert record.model_dump(mode="json") == before


def test_numeric_keys_preserve_large_integer_precision_and_do_not_parse_strings():
    assert coordinate_value_key(10) == coordinate_value_key(10.0)
    assert coordinate_value_key(10) != coordinate_value_key("10")
    assert coordinate_value_key(1) != coordinate_value_key(True)
    assert coordinate_value_key(9007199254740993) != coordinate_value_key(9007199254740992.0)
    enormous = 10 ** 400
    assert coordinate_value_key(enormous) == ("number", enormous)
    assert has_numeric_value({"value": {"value": enormous, "status": "reported"}})


def test_negative_mass_change_remains_signed_estimate():
    record = sample()
    series(record, state="partial", expected=None)
    record.observations[0].metric_kind = "evaporation_mass_change"
    record.observations[0].value.value = -1.25
    record.observations[0].coordinates = [coordinate("elapsed_time", 1800, status="estimated", unit="s")]
    node = next(n for n in project_record(record).nodes if n.label == "Observation")
    assert node.properties["value"] == -1.25
    assert node.properties["value_status"] == "estimated"
    assert "MASS_LOSS_SIGN_REVIEW" not in codes(record)
    assert quantitative_summary(record)["incomplete_series"][0]["state"] == "partial"
