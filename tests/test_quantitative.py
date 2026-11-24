"""Synthetic figure/table extraction failures; no provider or graph access."""
import json

import pytest

from watertreatment.domain import (
    EvidenceRef, Experiment, ExtractionRecord, Fact, NamedFact, Observation,
    ObservationEvidenceLink, Paper, QuantitativeCoverage,
)
from watertreatment.quantitative import has_numeric_value, has_source_numeric_point, observation_value_kind, quantitative_summary
from watertreatment.validation import validate_record


def sample(*, schema="0.2.0", origin="plot_estimate", status="estimated"):
    return ExtractionRecord(schema_version=schema, paper=Paper(id="paper", source_ids=["main"]),
        experiments=[Experiment(id="experiment")],
        observations=[Observation(id="point", experiment_id="experiment", origin=origin,
            value=Fact(value=0.8, status=status, evidence_ids=["plot"]),
            evidence_links=[ObservationEvidenceLink(evidence_id="plot", supported_fields=["value"],
                explanation="Read the identified curve at its displayed time marker.")])],
        evidence=[EvidenceRef(id="plot", source_id="main", block_id="main:p1")])


def series(record, *, state="partial", expected=3):
    record.quantitative_coverage = [QuantitativeCoverage(id="curve", source_id="main", block_id="main:p1",
        label="final sample curve", data_kind="time_series", state=state, expected_points=expected,
        observation_ids=["point"], reason="Only one separated marker can be read at the available resolution.")]
    record.observations[0].series_id = "curve"
    record.observations[0].coordinates = [NamedFact(name="time",
        fact=Fact(value=10, status="reported", evidence_ids=["plot"]),
        unit=Fact(value="min", status="reported", evidence_ids=["plot"]))]


def codes(record, severity=None, docs=()):
    return {x.code for x in validate_record(record, docs) if severity is None or x.severity == severity}


def test_legacy_full_and_compact_payloads_keep_original_keys_and_defaults():
    old_compact = {"paper": {"id": "paper", "source_ids": ["main"]},
        "experiments": [{"id": "experiment"}],
        "observations": [{"id": "point", "experiment_id": "experiment", "value": {
            "value": 0.8, "status": "reported", "evidence_ids": ["plot"]}, "origin": "plot_estimate"}],
        "evidence": [{"id": "plot", "source_id": "main", "block_id": "main:p1"}]}
    legacy = ExtractionRecord.model_validate(old_compact)
    assert legacy.schema_version == "0.1.0" and legacy.rule_version == "0.5"
    assert legacy.model_dump(mode="json", exclude_defaults=True) == old_compact
    full = legacy.model_dump(mode="json")
    assert "quantitative_coverage" not in full
    assert not {"coordinates", "series_id", "evidence_links"} & full["observations"][0].keys()
    assert ExtractionRecord.model_validate_json(json.dumps(full)).model_dump(mode="json") == full
    assert json.loads(legacy.model_dump_json(exclude_defaults=True)) == old_compact


def test_current_and_legacy_plot_status_are_distinguished_without_mutating_input():
    old = sample(schema="0.1.0", status="reported")
    before = old.model_dump(mode="json")
    assert "PLOT_ESTIMATE_STATUS" in codes(old, "warning")
    assert "PLOT_ESTIMATE_STATUS" not in codes(old, "error")
    assert old.model_dump(mode="json") == before
    old.schema_version = "0.2.0"
    assert "PLOT_ESTIMATE_STATUS" in codes(old, "error")
    old.observations[0].value.status = "estimated"
    assert "PLOT_ESTIMATE_STATUS" not in codes(old)


def test_simulated_color_estimate_retains_author_simulation_and_estimated_precision():
    record = sample(origin="author_simulation")
    assert codes(record, "error") == set()
    assert observation_value_kind(record.observations[0]) == "estimated"
    assert quantitative_summary(record)["estimated_values"] == 1
    record.observations[0].origin = "author_hypothesis"
    record.observations[0].value.status = "reported"
    assert observation_value_kind(record.observations[0]) == "author_hypothesis"


def test_partial_series_is_local_limitation_not_whole_paper_error():
    record = sample()
    series(record)
    assert codes(record, "error") == set()
    assert "QUANTITATIVE_SERIES_INCOMPLETE" in codes(record, "warning")
    summary = quantitative_summary(record)
    assert summary["incomplete_series"][0]["quantified_points"] == 1
    assert summary["incomplete_series"][0]["expected_points"] == 3
    assert summary["incomplete_series"][0]["point_count_matched"] is False
    assert summary["incomplete_series"][0]["coordinates_available"] is True
    assert summary["scientific_validation"] == "not_established"
    assert quantitative_summary(record.model_dump(mode="json")) == summary


def test_complete_declaration_cannot_hide_missing_or_qualitative_points():
    record = sample()
    series(record, state="complete")
    assert "SERIES_COMPLETENESS_CONFLICT" in codes(record, "error")
    record.quantitative_coverage[0].expected_points = 1
    assert "SERIES_COMPLETENESS_CONFLICT" not in codes(record)
    record.observations[0].value = Fact(status="read_unquantified", note="curve observed")
    assert "SERIES_COMPLETENESS_CONFLICT" in codes(record, "error")


def test_series_foreign_keys_and_bidirectional_ownership_are_checked():
    record = sample()
    series(record)
    record.observations[0].series_id = "another"
    assert codes(record, "error") >= {"MISSING_REFERENCE", "SERIES_OWNER_MISMATCH"}
    record.observations[0].series_id = "curve"
    record.quantitative_coverage[0].observation_ids = ["missing"]
    assert codes(record, "error") >= {"MISSING_REFERENCE", "SERIES_OWNER_MISMATCH"}


def test_duplicate_points_and_series_cannot_inflate_coverage():
    record = sample()
    series(record)
    record.quantitative_coverage[0].observation_ids.append("point")
    assert "DUPLICATE_SERIES_POINT" in codes(record, "error")
    record.quantitative_coverage[0].observation_ids = ["point", "point2"]
    another = record.observations[0].model_copy(deep=True)
    another.id = "point2"
    record.observations.append(another)
    assert "DUPLICATE_SERIES_COORDINATES" in codes(record, "error")
    extra = record.quantitative_coverage[0].model_copy(deep=True)
    extra.id = "curve2"
    record.quantitative_coverage.append(extra)
    assert "DUPLICATE_QUANTITATIVE_SERIES" in codes(record, "error")


def test_context_reference_is_not_silently_accepted_as_direct_value_support():
    record = sample()
    record.observations[0].evidence_links[0].support_type = "context"
    assert "VALUE_EVIDENCE_NOT_FIELD_LINKED" in codes(record, "warning")
    record.observations[0].evidence_links[0].support_type = "direct"
    record.observations[0].value.evidence_ids = []
    assert "VALUE_EVIDENCE_LINK_MISMATCH" in codes(record, "error")


def test_wrong_evidence_link_id_and_field_are_rejected():
    record = sample()
    record.observations[0].evidence_links[0].evidence_id = "invented"
    record.observations[0].evidence_links[0].supported_fields = ["unknown_field"]
    assert codes(record, "error") >= {"MISSING_EVIDENCE", "UNKNOWN_SUPPORTED_FIELD"}


def test_quantitative_inventory_does_not_claim_a_different_source_page():
    record = sample()
    series(record)
    record.quantitative_coverage[0].locator.physical_page = 2
    docs = [{"source": {"source_id": "main", "format": "pdf", "page_count": 3},
             "blocks": [{"source_id": "main", "block_id": "main:p1", "text": "curve", "location": {"physical_page": 1}}]}]
    assert "LOCATOR_BLOCK_MISMATCH" in codes(record, "error", docs)


def test_external_benchmark_cannot_become_this_papers_experiment():
    record = sample()
    series(record)
    record.quantitative_coverage[0].scope = "external_benchmark"
    assert "EXTERNAL_BENCHMARK_AS_OWN_EXPERIMENT" in codes(record, "error")


def test_signed_mass_change_is_preserved_without_guessing_from_free_text():
    record = sample()
    record.observations[0].value.value = -2.5
    record.observations[0].metric_kind = "mass_loss"
    assert "MASS_LOSS_SIGN_REVIEW" in codes(record, "warning")
    record.observations[0].metric_kind = "mass_change"
    assert "MASS_LOSS_SIGN_REVIEW" not in codes(record)
    assert record.observations[0].value.value == -2.5


def test_no_inventory_and_missing_conditions_do_not_claim_completeness_or_reject_paper():
    record = sample()
    record.experiments[0].conditions = [NamedFact(name="volume", fact=Fact(status="not_reported"))]
    assert codes(record, "error") == set()
    summary = quantitative_summary(record)
    assert not summary["has_quantitative_inventory"] and summary["limitations"]
    assert summary["estimated_values"] == 1


def test_missing_coordinate_and_unknown_expected_count_are_explicit_limits():
    record = sample()
    series(record, state="complete", expected=None)
    record.observations[0].coordinates = []
    assert codes(record, "warning") >= {"SERIES_POINT_COORDINATE_MISSING", "SERIES_COMPLETENESS_UNCOUNTED"}


def test_evaporation_mass_loss_sign_is_checked_without_changing_signed_change():
    record = sample()
    observation = record.observations[0]
    observation.value.value = -2.5
    for kind in ("mass_loss", "evaporation_mass_loss"):
        observation.metric_kind = kind
        assert "MASS_LOSS_SIGN_REVIEW" in codes(record, "warning")
    for kind in ("mass_change", "evaporation_mass_change"):
        observation.metric_kind = kind
        assert "MASS_LOSS_SIGN_REVIEW" not in codes(record)
    assert observation.value.value == -2.5


def test_model_and_local_values_do_not_fill_missing_source_curve_points():
    for origin, status in (("model_inference", "inferred"), ("local_calculation", "reported")):
        record = sample(origin=origin, status=status)
        series(record, state="complete", expected=1)
        assert has_numeric_value(record.observations[0])
        assert not has_source_numeric_point(record.observations[0])
        assert "SERIES_COMPLETENESS_CONFLICT" in codes(record, "error")
        assert quantitative_summary(record)["incomplete_series"][0]["quantified_points"] == 0
        record.quantitative_coverage[0].state = "partial"
        assert "SERIES_COMPLETENESS_CONFLICT" not in codes(record)


def test_uncounted_complete_declaration_remains_limited_in_summary():
    record = sample()
    series(record, state="complete", expected=None)
    summary = quantitative_summary(record)
    assert summary["coverage_states"] == {"complete": 1}
    assert summary["incomplete_series"][0]["state"] == "complete"
    assert summary["incomplete_series"][0]["quantified_points"] == 1
    assert "point count is unknown" in summary["incomplete_series"][0]["limitations"][-1]
    assert summary["incomplete_series"][0]["point_count_matched"] is False
    assert summary["incomplete_series"][0]["coordinates_available"] is True
    assert summary["limitations"] and codes(record, "error") == set()


@pytest.mark.parametrize("value", [[70, 84], [0], [1, 2.5], [-2, 0]])
def test_numeric_range_or_vector_retains_estimate_origin_without_counting_points(value):
    record = sample()
    record.observations[0].value.value = value
    assert observation_value_kind(record.observations[0]) == "plot_estimate"
    assert quantitative_summary(record)["estimated_values"] == 1
    assert not has_numeric_value(record.observations[0])
    assert not has_source_numeric_point(record.observations[0])
    series(record, state="complete", expected=len(value))
    assert "SERIES_COMPLETENESS_CONFLICT" in codes(record, "error")
    assert quantitative_summary(record)["incomplete_series"][0]["quantified_points"] == 0


@pytest.mark.parametrize("schema,severity", [("0.1.0", "warning"), ("0.2.0", "error")])
def test_range_plot_estimate_cannot_hide_behind_reported_status(schema, severity):
    record = sample(schema=schema, status="reported")
    record.observations[0].value.value = [10, 20]
    before = record.model_dump(mode="json")
    assert "PLOT_ESTIMATE_STATUS" in codes(record, severity)
    assert record.model_dump(mode="json") == before


def test_simulated_numeric_range_preserves_estimated_reading_precision():
    record = sample(origin="author_simulation")
    record.observations[0].value.value = [-1.5, 0.5]
    assert observation_value_kind(record.observations[0]) == "estimated"
    assert quantitative_summary(record)["estimated_values"] == 1
    assert not has_source_numeric_point(record.observations[0])


@pytest.mark.parametrize("value", [True, False, [], [True, False], [1, True], [1, "unclear"]])
def test_bool_empty_and_mixed_lists_are_not_quantified_ranges(value):
    record = sample()
    record.observations[0].value.value = value
    assert observation_value_kind(record.observations[0]) == "not_quantified"
    assert quantitative_summary(record)["estimated_values"] == 0
    assert not has_source_numeric_point(record.observations[0])


def test_complete_point_count_keeps_the_series_own_analysis_limits():
    record = sample(origin="reported_measurement", status="reported")
    series(record, state="complete", expected=1)
    restriction = "The reference for the time origin is unclear; do not compare kinetic windows."
    record.quantitative_coverage[0].limitations = [restriction]
    before = record.model_dump(mode="json")
    summary = quantitative_summary(record)
    assert summary["coverage_states"] == {"complete": 1}
    assert summary["incomplete_series"][0]["quantified_points"] == 1
    assert restriction in summary["incomplete_series"][0]["limitations"]
    assert summary["incomplete_series"][0]["point_count_matched"] is True
    assert summary["incomplete_series"][0]["coordinates_available"] is True
    assert "coverage or analysis limitations" in summary["limitations"][0]
    assert quantitative_summary(record.model_dump(mode="json")) == summary
    assert record.model_dump(mode="json") == before
    assert codes(record, "error") == set()


@pytest.mark.parametrize("kind", ["time_series", "cycle_series", "condition_series", "table_cells"])
@pytest.mark.parametrize("coordinate_state", ["empty", "unread", "not_reported", "reported_null"])
def test_complete_series_with_unknown_coordinate_remains_analysis_limited(kind, coordinate_state):
    record = sample(origin="reported_measurement", status="reported")
    series(record, state="complete", expected=1)
    record.quantitative_coverage[0].data_kind = kind
    if coordinate_state == "empty":
        record.observations[0].coordinates = []
    else:
        status = "reported" if coordinate_state == "reported_null" else coordinate_state
        record.observations[0].coordinates = [NamedFact(name="time", fact=Fact(status=status))]
    summary = quantitative_summary(record)
    assert summary["incomplete_series"][0]["state"] == "complete"
    assert summary["incomplete_series"][0]["quantified_points"] == 1
    assert summary["incomplete_series"][0]["limitations"] and summary["limitations"]
    assert summary["incomplete_series"][0]["point_count_matched"] is True
    assert summary["incomplete_series"][0]["coordinates_available"] is False
    assert "SERIES_POINT_COORDINATE_MISSING" in codes(record, "warning")
    assert codes(record, "error") == set()


def test_operating_day_and_reuse_count_remain_distinct_series_axes():
    from watertreatment.quantitative import has_usable_coordinates

    value = sample(origin="reported_measurement", status="reported").observations[0]
    value.coordinates = [NamedFact(name="sampling day", fact=Fact(value=4, status="reported", evidence_ids=["e"]))]
    assert has_usable_coordinates(value, "time_series")
    assert not has_usable_coordinates(value, "cycle_series")
    value.coordinates[0].name = "cycle_number"
    value.coordinates[0].basis = Fact(value="operation day", status="reported", evidence_ids=["e"])
    assert not has_usable_coordinates(value, "cycle_series")
    value.coordinates[0].basis = Fact(value="cycles completed after regeneration", status="reported", evidence_ids=["e"])
    assert has_usable_coordinates(value, "cycle_series")
    assert not has_usable_coordinates(value, "time_series")


def test_one_known_coordinate_does_not_hide_a_second_unknown_coordinate():
    record = sample(origin="reported_measurement", status="reported")
    series(record, state="complete", expected=1)
    record.observations[0].coordinates.append(NamedFact(name="cycle", fact=Fact(status="not_reported")))
    assert quantitative_summary(record)["incomplete_series"][0]["limitations"]
    assert "SERIES_POINT_COORDINATE_MISSING" in codes(record, "warning")


def test_intentionally_excluded_external_table_does_not_block_complete_internal_series():
    record = sample(origin="reported_measurement", status="reported")
    series(record, state="complete", expected=1)
    record.quantitative_coverage.append(QuantitativeCoverage(id="external", source_id="main", block_id="main:p1",
        label="other papers benchmark table", data_kind="table_cells", scope="external_benchmark",
        state="out_of_scope", expected_points=30, observation_ids=[],
        reason="Cited benchmarks belong to their original studies, not this paper's experiments.",
        limitations=["External benchmark values were intentionally not imported into this study."]))
    summary = quantitative_summary(record)
    assert summary["incomplete_series"] == [] and summary["limitations"] == []
    assert codes(record, "error") == set()


@pytest.mark.parametrize("kind", ["time_series", "cycle_series", "condition_series", "table_cells", "scalar"])
def test_empty_zero_point_inventory_does_not_claim_unlimited_quantitative_completion(kind):
    record = sample(origin="reported_measurement", status="reported")
    record.observations = []
    record.quantitative_coverage = [QuantitativeCoverage(id="empty", source_id="main", block_id="main:p1",
        label="numeric panel with no extracted values", data_kind=kind, state="complete",
        expected_points=0, observation_ids=[])]
    summary = quantitative_summary(record)
    assert summary["incomplete_series"][0]["quantified_points"] == 0
    assert summary["incomplete_series"][0]["limitations"] and summary["limitations"]
    assert summary["incomplete_series"][0]["point_count_matched"] is True
    assert summary["incomplete_series"][0]["coordinates_available"] is (kind == "scalar")
    assert "SERIES_COMPLETENESS_EMPTY" in codes(record, "warning")
    assert codes(record, "error") == set()
    record.quantitative_coverage[0].state = "out_of_scope"
    record.quantitative_coverage[0].reason = "No in-scope numerical series; this panel belongs to another study."
    assert quantitative_summary(record)["incomplete_series"] == []
    assert quantitative_summary(record)["limitations"] == []
    assert "SERIES_COMPLETENESS_EMPTY" not in codes(record)


@pytest.mark.parametrize("kind", ["reaction_network", "other"])
def test_non_numeric_diagram_is_not_forced_into_scalar_point_inventory(kind):
    record = sample(origin="reported_measurement", status="reported")
    record.observations = []
    record.quantitative_coverage = [QuantitativeCoverage(id="diagram", source_id="main", block_id="main:p1",
        label="qualitative mechanism diagram", data_kind=kind, state="complete", expected_points=0,
        observation_ids=[], reason="Diagram has no numerical point count.")]
    assert "SERIES_COMPLETENESS_EMPTY" not in codes(record)
    assert codes(record, "error") == set()
