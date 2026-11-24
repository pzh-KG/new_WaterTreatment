import json

import pytest
from pydantic import ValidationError

from watertreatment.domain import (
    Classification, ExtensionModule, ExtractionRecord, Fact, FactStatus,
    MaterialSample, NamedFact, Paper,
)


def test_unknown_zero_absence_and_unquantified_are_distinct():
    facts = [Fact(), Fact(value=0, status="reported"),
             Fact(value=False, status="explicit_absence"), Fact(status="read_unquantified")]
    restored = [Fact.model_validate_json(f.model_dump_json()) for f in facts]
    assert restored[0].value is None
    assert restored[0].status == FactStatus.UNREAD
    assert restored[1].value == 0 and not isinstance(restored[1].value, bool)
    assert restored[2].value is False
    assert len({(f.status, type(f.value)) for f in restored}) == 4


def test_all_material_categories_are_open_but_record_shape_is_closed():
    sample = MaterialSample(id="m", classification=[Classification(
        axis="chemical_family", label=Fact(value="organic supramolecular assembly", status="reported"))])
    record = ExtractionRecord(paper=Paper(id="p"), materials=[sample],
                              extensions=[ExtensionModule(module="new_optical_layout", version="1",
                                  fields=[NamedFact(name="reflector_count", fact=Fact(value=2, status="reported"))])])
    assert ExtractionRecord.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError):
        ExtractionRecord.model_validate({"paper": {"id": "p"}, "invented_material_bucket": []})
    with pytest.raises(ValidationError):
        Fact.model_validate({"value": 7, "unit_without_schema": "hours"})


def test_response_schema_contains_no_free_form_object():
    schema = ExtractionRecord.model_json_schema()

    def check(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(schema)
    json.dumps(schema, allow_nan=False)


def test_numeric_nonfinite_values_cannot_be_exported_as_evidence():
    with pytest.raises(ValidationError):
        Fact(value=float("nan"), status=FactStatus.REPORTED)


def test_source_conflict_retains_alternatives_instead_of_selecting_maximum():
    fact = Fact(status="conflict", alternatives=[
        {"value": 90.1, "evidence_ids": ["abstract"]},
        {"value": 82.7, "evidence_ids": ["figure"]},
    ])
    assert fact.value is None
    assert [x.value for x in fact.alternatives] == [90.1, 82.7]
