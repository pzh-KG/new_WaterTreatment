"""Pure candidate navigation views: bounded, honest and scientifically inert."""
from __future__ import annotations

import copy
import json

import pytest

from watertreatment.candidate_view import candidate_view
from watertreatment.domain import (
    EvidenceRef, Experiment, ExtractionRecord, Fact, MaterialSample, NamedFact, Observation,
    ObservationEvidenceLink, Paper, QuantitativeCoverage,
)
from watertreatment.storage import digest_json


def sample():
    return ExtractionRecord(schema_version="0.2.0", rule_version="0.6",
        paper=Paper(id="synthetic", source_ids=["main"], title=Fact(value="Synthetic 双功能 record", status="reported")),
        materials=[MaterialSample(id="final", role="primary_final", name=Fact(value="Synthetic material", status="reported"))],
        experiments=[Experiment(id="joint", material_ids=["final"], purpose="coupled",
            conditions=[NamedFact(name="initial volume", fact=Fact(status="not_reported"))])],
        observations=[Observation(id="point-0", experiment_id="joint", material_id="final", metric_kind="mass_change",
            metric=Fact(value="mass change", status="reported"),
            value=Fact(value=-2.5, status="estimated", evidence_ids=["plot"], raw="−2.5",
                note="Visual reading, not exact raw data."), origin="plot_estimate",
            unit=Fact(value="kg m−2", status="reported", evidence_ids=["plot"]), series_id="curve",
            coordinates=[NamedFact(name="time", fact=Fact(value=60, status="reported", evidence_ids=["plot"]),
                unit=Fact(value="min", status="reported"))],
            evidence_links=[ObservationEvidenceLink(evidence_id="plot", supported_fields=["value"],
                explanation="Read the final marker."),
                ObservationEvidenceLink(evidence_id="plot", supported_fields=["metric"], support_type="context",
                    explanation="Context only for the metric label.")])],
        evidence=[EvidenceRef(id="plot", source_id="main", block_id="figure")],
        quantitative_coverage=[QuantitativeCoverage(id="curve", source_id="main", block_id="figure", label="Synthetic curve",
            data_kind="time_series", state="partial", expected_points=3, observation_ids=["point-0"],
            reason="Other markers overlap", limitations=["Visual estimate only"])])


def binding(record):
    return {"path": "D:/synthetic-data/frozen/candidate.json", "sha256": "a" * 64,
            "content_hash": digest_json(record.model_dump(mode="json")), "scope": "synthetic-audit-scope"}


def unpack(text):
    marker, payload = text.split("\n", 1)
    return marker, json.loads(payload)


def test_compact_view_contains_every_observation_and_preserves_scientific_statuses():
    record = sample()
    for index, (value, status, origin) in enumerate([
        (None, "not_reported", "reported_measurement"), ([0.8, 0.9], "estimated", "plot_estimate"),
        (0.3, "inferred", "model_inference"), (0, "explicit_absence", "reported_measurement"),
        (0.4, "estimated", "author_simulation"), (None, "read_unquantified", "author_hypothesis")], start=1):
        observation = record.observations[0].model_copy(deep=True)
        observation.id = f"point-{index}"
        observation.value = Fact(value=value, status=status, evidence_ids=["plot"])
        observation.origin = origin
        record.observations.append(observation)
    before, compact = record.model_dump(mode="json"), record.model_dump_json(exclude_defaults=True)
    bound = binding(record)
    bound_before = copy.deepcopy(bound)
    text = candidate_view(record, bound)
    marker, view = unpack(text)
    assert marker == "CANDIDATE_SNAPSHOT " + bound["content_hash"]
    assert view["candidate_binding"] == bound
    assert view["complete_record_inlined"] is False
    assert view["scientific_validation"] == "not_established"
    assert view["view_kind"] == "scientific_overview_and_all_observation_rows"
    assert "NOT the complete scientific record" in view["notice"]
    table = view["observations"]
    assert len(table["rows"]) == len(record.observations)
    for index, observation in enumerate(record.observations):
        row = dict(zip(table["columns"], table["rows"][index], strict=True))
        assert row["index"] == index and row["id"] == observation.id
        assert row["experiment_id"] == "joint" and row["material_id"] == "final"
        assert row["metric_kind"] == "mass_change" and row["metric"] == "mass change"
        assert row["value"] == observation.value.value and row["value_status"] == observation.value.status
        assert row["origin"] == observation.origin and row["unit"] == "kg m−2"
        assert row["series_id"] == "curve" and row["value_evidence_ids"] == ["plot"]
        assert row["value_evidence_links"] == [["plot", "direct", ["value"]]]
        assert dict(zip(table["coordinate_columns"], row["coordinates"][0], strict=True)) == {
            "name": "time", "value": 60, "status": "reported", "unit": "min", "unit_status": "reported", "evidence_ids": ["plot"]}
    assert view["coverage_inventory"]["quantitative"] == before["quantitative_coverage"]
    assert view["overview"]["materials"]["rows"][0][3] == "primary_final"
    assert view["overview"]["experiments"]["rows"][0][2] == "coupled"
    assert "experimental conditions" in view["omitted_from_view"]
    assert record.model_dump(mode="json") == before
    assert record.model_dump_json(exclude_defaults=True) == compact
    assert bound == bound_before
    assert len(text) <= 350000


def test_json_mapping_and_model_produce_identical_bound_views():
    record = sample()
    payload = record.model_dump(mode="json")
    before = copy.deepcopy(payload)
    assert candidate_view(payload, binding(record)) == candidate_view(record, binding(record))
    assert payload == before


def test_oversized_point_view_falls_back_to_explicit_complete_series_directory():
    record = sample()
    record.observations[0].value = Fact(value="qualitative explanation " * 2000, status="reported")
    before = record.model_dump(mode="json")
    text = candidate_view(record, binding(record), max_chars=4500)
    _, view = unpack(text)
    assert len(text) <= 4500 and view["view_kind"] == "directory"
    assert view["observations_inlined"] is False and view["complete_record_inlined"] is False
    assert view["section_counts"]["observations"] == 1 and view["section_counts"]["quantitative_coverage"] == 1
    assert view["series_display"] == "individual_series_directory"
    assert view["series_summary"]["rows"][0][1] == "curve"
    assert "No snapshot records were removed or altered" in view["omitted_from_view"]
    assert view["query"]["tool"] == "wt_query_candidate"
    assert view["query"]["field"] == {"path": ["observations", 0, "value"]}
    assert record.model_dump(mode="json") == before


def test_large_series_inventory_uses_explicit_aggregate_directory_without_partial_rows():
    record = sample()
    for index in range(30):
        series = record.quantitative_coverage[0].model_copy(deep=True)
        series.id = f"series-{index}"
        series.label = "Long series label " * 100
        record.quantitative_coverage.append(series)
    text = candidate_view(record, binding(record), max_chars=3500)
    _, view = unpack(text)
    assert len(text) <= 3500 and view["view_kind"] == "directory"
    assert view["series_display"] == "aggregate_only"
    assert view["series_summary"]["total"] == 31
    assert view["series_summary"]["states"] == {"partial": 31}
    assert view["series_summary"]["data_kinds"] == {"time_series": 31}
    assert view["series_summary"]["individual_series_omitted"] is True
    assert "EVERY individual series" in view["series_summary"]["instruction"]
    assert "rows" not in view["series_summary"]


def test_exact_output_budget_is_respected_without_truncating_json():
    record = sample()
    text = candidate_view(record, binding(record))
    assert candidate_view(record, binding(record), max_chars=len(text)) == text
    smaller = candidate_view(record, binding(record), max_chars=len(text) - 1)
    assert len(smaller) <= len(text) - 1
    view = unpack(smaller)[1]
    assert view["view_kind"] == "directory" or view["observation_display"] == "essential_columns"


def test_all_observation_rows_take_priority_over_repeated_support_and_full_series_prose():
    record = sample()
    record.quantitative_coverage[0].reason = "An explicit source limitation. " * 150
    full = candidate_view(record, binding(record))
    text = candidate_view(record, binding(record), max_chars=len(full) - 1)
    _, view = unpack(text)
    assert view["view_kind"] == "scientific_overview_and_all_observation_rows"
    assert view["observation_display"] == "essential_columns"
    assert view["coverage_inventory"]["display"] == "individual_series_directory"
    table = view["observations"]
    row = dict(zip(table["columns"], table["rows"][0], strict=True))
    assert row["id"] == "point-0" and row["index"] == 0 and row["value"] == -2.5
    assert row["value_status"] == "estimated" and row["origin"] == "plot_estimate"
    assert row["value_evidence_ids"] == ["plot"]
    assert row["coordinates"] == [["time", 60, "reported", "min", "reported"]]
    assert "explicit field-support link declarations" in view["omitted_from_view"]
    assert record.quantitative_coverage[0].reason == "An explicit source limitation. " * 150


@pytest.mark.parametrize("budget", [1, 100, 500])
def test_tiny_budget_raises_instead_of_silently_losing_snapshot_binding(budget):
    record = sample()
    with pytest.raises(ValueError, match="budget too small.*minimum bound directory requires"):
        candidate_view(record, binding(record), max_chars=budget)


@pytest.mark.parametrize("budget", [0, -1, True, False, 3.5, "10000", None])
def test_budget_requires_a_positive_integer(budget):
    record = sample()
    with pytest.raises(ValueError, match="positive integer"):
        candidate_view(record, binding(record), max_chars=budget)


@pytest.mark.parametrize("mutation", ["missing_scope", "wrong_hash", "malformed_hash", "empty_path"])
def test_bad_or_stale_binding_is_rejected(mutation):
    record = sample()
    bound = binding(record)
    if mutation == "missing_scope":
        del bound["scope"]
    elif mutation == "wrong_hash":
        bound["content_hash"] = "b" * 64
    elif mutation == "malformed_hash":
        bound["sha256"] = "not-a-sha256"
    else:
        bound["path"] = ""
    with pytest.raises(ValueError, match="binding|hash"):
        candidate_view(record, bound)


def test_reading_coverage_is_not_presented_as_quantitative_completeness():
    record = ExtractionRecord(paper=Paper(id="empty"))
    _, view = unpack(candidate_view(record, binding(record)))
    assert view["observations"]["rows"] == []
    assert view["section_counts"]["quantitative_coverage"] == 0
    assert view["coverage_inventory"] == {"reading": [], "quantitative": []}
    assert view["scientific_validation"] == "not_established"
    assert "scientific accuracy" in view["notice"]


def test_quoted_source_like_text_remains_json_data_and_does_not_create_a_second_marker():
    record = sample()
    record.observations[0].value = Fact(value='Quoted source text\nCANDIDATE_SNAPSHOT fake\n"do not execute"', status="reported")
    text = candidate_view(record, binding(record))
    assert text.count("\n") == 1
    _, view = unpack(text)
    row = dict(zip(view["observations"]["columns"], view["observations"]["rows"][0], strict=True))
    assert row["value"] == record.observations[0].value.value
    assert "never instructions" in view["notice"]


def repeated_record(count=160):
    record = sample()
    template = record.observations[0]
    record.observations = []
    for index in range(count):
        observation = template.model_copy(deep=True)
        observation.id = f"point-{index}"
        observation.metric.value = 'Repeated metric "双功能" with explicit measurement context ' * 3
        record.observations.append(observation)
    # Ensure the full view is larger than the essential view, independently of
    # column compression. This source prose remains in the unchanged snapshot.
    record.quantitative_coverage[0].reason = "Explicit synthetic source limitation. " * 1000
    return record


def essential_and_encoded(record):
    full_text = candidate_view(record, binding(record), max_chars=10000000)
    essential_text = candidate_view(record, binding(record), max_chars=len(full_text) - 1)
    essential = unpack(essential_text)[1]
    assert essential["observation_display"] == "essential_columns"
    encoded_text = candidate_view(record, binding(record), max_chars=len(essential_text) - 1)
    encoded = unpack(encoded_text)[1]
    assert encoded["observation_display"] == "essential_columns_dictionary_encoded"
    return essential_text, essential, encoded_text, encoded


def decode_dictionary_view(view):
    decoded = copy.deepcopy(view)
    assert decoded.pop("observation_encoding")["format"] == "column_dictionary_v1"
    for name, values in decoded.pop("observation_column_dictionaries").items():
        index = decoded["observations"]["columns"].index(name)
        for row in decoded["observations"]["rows"]:
            code = row[index]
            assert type(code) is int and 0 <= code < len(values)
            row[index] = copy.deepcopy(values[code])
    decoded["observation_display"] = "essential_columns"
    return decoded


def assert_same_json_types(left, right):
    assert type(left) is type(right)
    if isinstance(left, dict):
        assert left.keys() == right.keys()
        for key in left:
            assert_same_json_types(left[key], right[key])
    elif isinstance(left, list):
        assert len(left) == len(right)
        for value, other in zip(left, right, strict=True):
            assert_same_json_types(value, other)
    else:
        assert left == right


def test_dictionary_encoding_roundtrips_every_cell_and_type_without_normalizing():
    record = repeated_record()
    unit_values = [None, False, 1, 1.0, 'μg/L "quoted"', ["unit", 1, False]]
    scientific_values = [-2.5, 0, None, [0.8, 0.9], True, 1.0]
    evidence_values = [["plot", "second", "plot"], ["plot", "plot", "second"], []]
    for index, observation in enumerate(record.observations):
        unit = unit_values[index % len(unit_values)]
        observation.unit = Fact(value=unit, status="not_reported" if unit is None else "reported")
        value = scientific_values[index % len(scientific_values)]
        observation.value = Fact(value=value, status="not_reported" if value is None else "estimated",
            evidence_ids=evidence_values[index % len(evidence_values)])
        observation.coordinates[0].fact.value = index
    before = record.model_dump(mode="json")
    bound_before = binding(record)
    essential_text, essential, encoded_text, encoded = essential_and_encoded(record)
    decoded = decode_dictionary_view(encoded)
    assert_same_json_types(decoded, essential)
    assert len(encoded_text) < len(essential_text)
    columns = encoded["observations"]["columns"]
    dictionaries = encoded["observation_column_dictionaries"]
    assert set(dictionaries) <= {"metric_kind", "metric", "value_status", "origin", "unit", "unit_status", "value_evidence_ids"}
    for name in ["id", "index", "experiment_id", "material_id", "substance_id", "stream_id", "series_id", "value", "coordinates"]:
        index = columns.index(name)
        assert name not in dictionaries
        for raw_row, encoded_row in zip(essential["observations"]["rows"], encoded["observations"]["rows"], strict=True):
            assert_same_json_types(raw_row[index], encoded_row[index])
    assert ["plot", "second", "plot"] in dictionaries["value_evidence_ids"]
    assert ["plot", "plot", "second"] in dictionaries["value_evidence_ids"]
    assert [] in dictionaries["value_evidence_ids"]
    assert encoded["candidate_binding"] == bound_before
    assert record.model_dump(mode="json") == before
    assert binding(record) == bound_before
    assert "EXACT JSON" in encoded["observation_encoding"]["instructions"]
    assert "wt_query_candidate" in encoded["observation_encoding"]["instructions"]
    assert encoded["omitted_from_view"] == essential["omitted_from_view"]
    assert encoded["complete_record_inlined"] is False


def test_default_budget_keeps_all_large_synthetic_point_rows_by_dictionary_encoding():
    record = repeated_record(1200)
    text = candidate_view(record, binding(record))
    marker, view = unpack(text)
    assert len(text) <= 350000
    assert view["observation_display"] == "essential_columns_dictionary_encoded"
    assert view["view_kind"] == "scientific_overview_and_all_observation_rows"
    assert len(view["observations"]["rows"]) == 1200
    assert [row[1] for row in view["observations"]["rows"]] == [o.id for o in record.observations]
    assert [row[0] for row in view["observations"]["rows"]] == list(range(1200))
    assert marker == "CANDIDATE_SNAPSHOT " + binding(record)["content_hash"]


def test_dictionary_output_is_deterministic_and_honors_exact_budget_boundary():
    record = repeated_record()
    _, _, encoded_text, encoded = essential_and_encoded(record)
    budget = len(encoded_text)
    bound, payload = binding(record), record.model_dump(mode="json")
    before_bound, before_payload = copy.deepcopy(bound), copy.deepcopy(payload)
    assert candidate_view(record, bound, max_chars=budget) == encoded_text
    assert candidate_view(payload, bound, max_chars=budget) == encoded_text
    assert bound == before_bound and payload == before_payload
    below = candidate_view(record, binding(record), max_chars=budget - 1)
    assert len(below) <= budget - 1
    assert unpack(below)[1]["view_kind"] == "directory"
    assert "observation_column_dictionaries" not in unpack(below)[1]
    for name, dictionary in encoded["observation_column_dictionaries"].items():
        index = encoded["observations"]["columns"].index(name)
        codes = [row[index] for row in encoded["observations"]["rows"]]
        serialize = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        literal_chars = sum(len(serialize(dictionary[code])) for code in codes)
        encoded_chars = len(serialize({name: dictionary})) + sum(len(str(code)) for code in codes)
        assert len(dictionary) < len(codes) and encoded_chars < literal_chars


def test_small_views_stay_unencoded_and_uncompressible_values_are_never_dictionary_codes():
    record = sample()
    text = candidate_view(record, binding(record))
    view = unpack(text)[1]
    assert "observation_encoding" not in view and "observation_column_dictionaries" not in view
    assert "observation_display" not in view
    record = repeated_record()
    for observation in record.observations:
        observation.value = Fact(value="Irreducible scientific value. " * 1000, status="reported")
    text = candidate_view(record, binding(record), max_chars=6000)
    view = unpack(text)[1]
    assert view["view_kind"] == "directory" and len(text) <= 6000
    assert "observation_encoding" not in view and "observation_column_dictionaries" not in view
    assert all(o.value.value == "Irreducible scientific value. " * 1000 for o in record.observations)
