"""Synthetic field-support semantics; no providers, database or source rewriting."""
import hashlib

import pytest

from watertreatment.domain import Fact, FactAlternative, NamedFact
from watertreatment.validation import _valid_observation_field_path, validate_record
from test_quantitative import sample


def issue_codes(record, severity=None):
    return {item.code for item in validate_record(record)
            if severity is None or item.severity == severity}


def with_fields(*fields, support_type="direct", schema="0.2.0"):
    record = sample(schema=schema)
    link = record.observations[0].evidence_links[0]
    link.supported_fields = list(fields)
    link.support_type = support_type
    return record


@pytest.mark.parametrize("field", ["value", "value.value"])
def test_explicit_numeric_value_support_retains_accepted_spellings(field):
    record = with_fields(field)
    assert "VALUE_EVIDENCE_NOT_FIELD_LINKED" not in issue_codes(record)
    assert "UNKNOWN_SUPPORTED_FIELD" not in issue_codes(record)


@pytest.mark.parametrize("field", ["value.note", "value.status", "value.raw", "value.normalized",
                                  "value.normalization_reason", "value.evidence_ids", "value.alternatives"])
def test_real_value_metadata_cannot_stand_in_for_the_numeric_claim(field):
    record = with_fields(field)
    assert "UNKNOWN_SUPPORTED_FIELD" not in issue_codes(record)
    assert "VALUE_EVIDENCE_NOT_FIELD_LINKED" in issue_codes(record, "warning")
    assert "VALUE_EVIDENCE_LINK_MISMATCH" not in issue_codes(record)


@pytest.mark.parametrize("support_type", ["context", "conflict"])
@pytest.mark.parametrize("field", ["value", "value.value"])
def test_context_or_conflict_is_not_promoted_to_direct_value_support(field, support_type):
    record = with_fields(field, support_type=support_type)
    assert "VALUE_EVIDENCE_NOT_FIELD_LINKED" in issue_codes(record, "warning")


@pytest.mark.parametrize("field", ["value.anything", "value.status.name", "value.value.unit",
    "coordinates.0.fact.value", "coordinates[0].fact", "coordinates[-1]", "coordinates.foo",
    "value..value", "value.value.", "value[value]", "value/value", " value", "value ",
    "value;id", "__class__", "model_fields", "evidence_ids", "evidence_links.0", "id", "series_id", ""])
def test_nonexistent_or_malformed_path_is_rejected_without_supplying_value_support(field):
    record = with_fields(field)
    assert "UNKNOWN_SUPPORTED_FIELD" in issue_codes(record, "error")
    assert "VALUE_EVIDENCE_NOT_FIELD_LINKED" in issue_codes(record, "warning")


@pytest.mark.parametrize("field", ["material_id", "experiment_id", "unit", "metric", "coordinates",
    "coordinates.0", "coordinates[0]", "coordinates.0.name", "coordinates[0].fact",
    "coordinates[0].fact.value", "coordinates.0.unit.value", "coordinates[0].basis.note",
    "time_window", "time_window.reference", "time_window.start.value", "time_window.end",
    "time_window.unit", "value.alternatives[0].value", "value.alternatives.0.raw"])
def test_existing_nested_paths_and_null_leaves_remain_valid(field):
    record = with_fields("value", field)
    point = record.observations[0]
    point.coordinates = [NamedFact(name="time", fact=Fact(value=10, status="reported"))]
    point.value.alternatives = [FactAlternative(value=0.7)]
    assert _valid_observation_field_path(point, field)
    assert "UNKNOWN_SUPPORTED_FIELD" not in issue_codes(record)
    assert "VALUE_EVIDENCE_NOT_FIELD_LINKED" not in issue_codes(record)


@pytest.mark.parametrize("field", ["coordinates.1", "coordinates[1].fact", "coordinates.0.unknown",
    "coordinates[0].fact.unknown", "coordinates.0.fact.value.0", "time_window.reference.value.unknown"])
def test_nested_path_must_resolve_against_the_actual_record(field):
    record = with_fields("value", field)
    record.observations[0].coordinates = [NamedFact(name="time", fact=Fact(value=10))]
    assert "UNKNOWN_SUPPORTED_FIELD" in issue_codes(record, "error")
    assert "VALUE_EVIDENCE_NOT_FIELD_LINKED" not in issue_codes(record)


def test_numeric_range_element_is_real_but_not_support_for_the_whole_value():
    record = with_fields("value.value[0]")
    record.observations[0].value.value = [0.7, 0.9]
    assert "UNKNOWN_SUPPORTED_FIELD" not in issue_codes(record)
    assert "VALUE_EVIDENCE_NOT_FIELD_LINKED" in issue_codes(record, "warning")
    record.observations[0].evidence_links[0].supported_fields.append("value.value")
    assert "VALUE_EVIDENCE_NOT_FIELD_LINKED" not in issue_codes(record)


def test_unbounded_index_text_is_an_invalid_path_rather_than_a_validation_crash():
    record = with_fields("coordinates[" + "9" * 5000 + "]")
    record.observations[0].coordinates = [NamedFact(name="time")]
    assert "UNKNOWN_SUPPORTED_FIELD" in issue_codes(record, "error")


def test_metadata_support_does_not_demand_a_numeric_value_citation():
    record = with_fields("value.note")
    record.observations[0].value.evidence_ids = []
    assert "VALUE_EVIDENCE_LINK_MISMATCH" not in issue_codes(record)
    record.observations[0].evidence_links[0].supported_fields = ["value.value"]
    assert "VALUE_EVIDENCE_LINK_MISMATCH" in issue_codes(record, "error")


def test_one_correct_direct_citation_does_not_cover_another_metadata_only_citation():
    record = with_fields("value")
    other = record.evidence[0].model_copy(deep=True)
    other.id = "context"
    record.evidence.append(other)
    record.observations[0].value.evidence_ids.append("context")
    link = record.observations[0].evidence_links[0].model_copy(deep=True)
    link.evidence_id = "context"
    link.supported_fields = ["value.note"]
    record.observations[0].evidence_links.append(link)
    assert "VALUE_EVIDENCE_NOT_FIELD_LINKED" in issue_codes(record, "warning")


@pytest.mark.parametrize("schema", ["0.1.0", "0.2.0"])
@pytest.mark.parametrize("field", ["value", "value.note", "value.anything", "coordinates.0"])
def test_validation_does_not_change_loadability_serialization_or_audit_fingerprint(schema, field):
    record = with_fields(field, schema=schema)
    before = (record.model_dump_json(), record.model_dump_json(exclude_defaults=True))
    before_hashes = tuple(hashlib.sha256(text.encode()).hexdigest() for text in before)
    loaded = type(record).model_validate_json(before[0])
    validate_record(loaded)
    after = (loaded.model_dump_json(), loaded.model_dump_json(exclude_defaults=True))
    assert after == before
    assert tuple(hashlib.sha256(text.encode()).hexdigest() for text in after) == before_hashes


def test_legacy_without_new_fields_keeps_full_and_compact_serialization():
    from watertreatment.domain import ExtractionRecord

    old = ExtractionRecord.model_validate({"paper": {"id": "legacy"}, "observations": [
        {"id": "point", "experiment_id": "experiment", "value": {"value": 0.8, "status": "reported"}}]})
    before = (old.model_dump_json(), old.model_dump_json(exclude_defaults=True))
    validate_record(old)
    assert (old.model_dump_json(), old.model_dump_json(exclude_defaults=True)) == before
    assert '"evidence_links"' not in before[0]
    assert '"quantitative_coverage"' not in before[0]
