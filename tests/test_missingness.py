"""Missingness reminders never manufacture quantities or author absence."""
import copy

import pytest

from test_design_scope import design_record
from watertreatment.domain import Component, Fact
from watertreatment.missingness import missingness_issues


def record_with_unread_retained_amount():
    record = design_record()
    record.materials[0].evidence_ids = ["evidence"]
    record.materials[0].components = [Component(substance_id="retained", retention="retained", amount=Fact())]
    return record


def test_checked_materials_get_exact_warning_without_any_scientific_change():
    record = record_with_unread_retained_amount()
    before = copy.deepcopy(record.model_dump(mode="json"))
    issues = missingness_issues(record)
    assert len(issues) == 1
    assert issues[0].code == "COMPONENT_AMOUNT_READING_STATE"
    assert issues[0].path == "materials[0].components[0].amount"
    assert issues[0].severity == "warning"
    assert "not_reported" in issues[0].message and "Do not substitute" in issues[0].message
    assert record.model_dump(mode="json") == before


@pytest.mark.parametrize("status", ["not_reported", "not_available", "read_unquantified", "ambiguous", "not_applicable"])
def test_explicit_missing_states_are_not_rejected_or_collapsed(status):
    record = record_with_unread_retained_amount()
    record.materials[0].components[0].amount = Fact(status=status, note="Source-specific disposition.")
    assert missingness_issues(record) == []
    assert record.materials[0].components[0].amount.status.value == status


@pytest.mark.parametrize("changed", ["not_read", "precursor", "no_evidence", "removed", "old_rule"])
def test_unread_is_legitimate_without_a_checked_retained_final_component(changed):
    record = record_with_unread_retained_amount()
    if changed == "not_read":
        next(x for x in record.design_scope.core_checks if x.area == "preparation_structure").state = "unread"
    elif changed == "precursor": record.materials[0].role = "precursor"
    elif changed == "no_evidence": record.materials[0].evidence_ids = []
    elif changed == "removed": record.materials[0].components[0].retention = "removed_or_transformed"
    else: record.rule_version = "0.7"
    assert missingness_issues(record) == []
