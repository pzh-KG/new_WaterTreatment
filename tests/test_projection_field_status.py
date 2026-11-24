"""Source metric/unit statuses cannot inherit the numerical value's status."""
import pytest

from test_projection import fact, sample
from watertreatment.domain import Fact
from watertreatment.projection import project_record
from watertreatment.web_graph import NORMALIZATION_INSTRUCTION


@pytest.mark.parametrize("field,source,normalized", [
    ("metric", "Estimated axis quantity", "Candidate quantity label"),
    ("unit", "Estimated axis unit", "Candidate unit"),
])
def test_reported_value_keeps_estimated_metric_or_unit_status(field, source, normalized):
    record = sample()
    context = fact(source)
    context.status, context.normalized = "estimated", normalized
    setattr(record.observations[0], field, context)
    before = record.model_dump_json()
    props = next(n.properties for n in project_record(record).nodes if n.label == "Observation")
    assert props["value_status"] == props["source_value_status"] == "reported"
    assert props[field] == source
    assert props[f"source_{field}_status"] == "estimated"
    assert props[f"normalized_{field}"] == normalized
    assert props[f"normalized_{field}_status"] == "unvalidated_conversion"
    assert record.model_dump_json() == before
    assert "source_metric_status and source_unit_status" in NORMALIZATION_INSTRUCTION
    assert "must not inherit reported from the numerical value" in NORMALIZATION_INSTRUCTION


def test_unknown_unit_remains_queryable_as_missing_without_inventing_a_unit():
    record = sample()
    record.observations[0].unit = Fact(status="not_reported")
    props = next(n.properties for n in project_record(record).nodes if n.label == "Observation")
    assert props["value_status"] == "reported"
    assert props["source_unit_status"] == "not_reported"
    assert props["source_metric_status"] == "unread"
    assert "unit" not in props and "normalized_unit" not in props
