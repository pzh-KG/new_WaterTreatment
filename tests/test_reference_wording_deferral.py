import pytest

from watertreatment.domain import ReferenceAssociation
from watertreatment.focused_review_packet import FocusedReview, pending_reference_wording
from test_scientific_revision_audit import fixture


def example():
    record, _, _ = fixture()
    record.reference_associations = [ReferenceAssociation(id="ref", target_kind="experiment", target_id="experiment",
        observation_ids=["measurement"], relationship="shared_reference", applicability="ambiguous",
        evidence_ids=["text"], rationale="Shared label only.", matched_conditions=["common sample label"])]
    name = "reference_associations[0]"
    packet = {"findings":[{"id":"D1","paths":[name]}], "changes":[], "primary_source_cards":{},
        "sources":[{"id":"S1","source":"source","block":"page1"}, {"id":"S2","source":"source","block":"page3"}],
        "current_targets":{name:record.reference_associations[0].model_dump(mode="json")}}
    report = FocusedReview(packet_hash="example", decisions=[{"groups":["D1"], "verdict":"limited",
        "source_ids":["S1","S2"], "explanation":"Checked the label under the original conditions."}],
        reading=[], reconciled_source_groups=[], additional_issues=[], restrictions=[],
        final_corrections=[{"group":"D1","path":name+".matched_conditions[0]", "replacement_json":'"sample label only"',
            "source_ids":["S1"], "explanation":"The specimen label supports a restricted comparison."}])
    return record, packet, report


def test_condition_description_requires_another_review():
    record, packet, report = example()
    candidate, pending = pending_reference_wording(record, packet, report)
    assert candidate == record
    assert pending == report.final_corrections


@pytest.mark.parametrize("change", ["source","unshown","blocker","overlap"])
def test_condition_deferral_is_bound_to_what_reviewer_received(change):
    record, packet, report = example()
    if change == "source":
        report.final_corrections[0].source_ids = ["S2"]
    elif change == "unshown":
        packet["current_targets"]["reference_associations[0]"]["matched_conditions"] = ["different value"]
    elif change == "blocker":
        report.decisions[0].verdict = "requires_revision"
    else:
        report.final_corrections.append(report.final_corrections[0].model_copy(deep=True))
    assert pending_reference_wording(record, packet, report) is None
