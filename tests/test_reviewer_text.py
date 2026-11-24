import json

import pytest

from watertreatment.domain import DesignComparison, ExtractionRecord, Fact, Paper
from watertreatment.retained_review_packet import LocalReview, ReviewDecision
from watertreatment.reviewer_text import _event_once, _freeze, prescribed_edit
from watertreatment.storage import DataStore


def example():
    record = ExtractionRecord(paper=Paper(id="study"), comparisons=[DesignComparison(
        id="comparison", counterevidence=[Fact(value="Control A gave 75%, B gave 74%, proving a direct effect.",
                                               status="reported", evidence_ids=["source"])] )])
    path = "comparisons[0].counterevidence[0].value"
    replacement = "Control A and B gave 75% and 74%. The direct effect is unresolved."
    instruction = f"Replace {path} with {json.dumps(replacement)} Use S1 and Table S2 in S3."
    decision = ReviewDecision(groups=["F1"], verdict="requires_revision", source_ids=["S1"],
                              explanation="The two endpoints do not establish a direct effect.")
    report = LocalReview(packet_hash="synthetic", decisions=[decision], reading=[], reconciled_source_groups=[],
                         additional_issues=[instruction], restrictions=["No replicate variance provided."])
    packet = {"findings":[{"id":"F1", "paths":["comparisons[0]"]}],
              "sources":[{"id":"S1","source":"article","block":"a","text":"Control A, 75%"},
                         {"id":"S3","source":"supplement","block":"b","text":"Control B, 74"}]}
    return record, packet, report


def test_reviewer_text_applies_only_quoted_interpretation_and_cited_cards():
    record, packet, report = example()
    original = record.model_dump(mode="json")
    revised, edit = prescribed_edit(record, packet, report)
    assert record.model_dump(mode="json") == original
    assert [c["id"] for c in edit["source_cards"]] == ["S1", "S3"]
    assert revised.comparisons[0].counterevidence[0].value == edit["replacement"]
    revised.comparisons[0].counterevidence[0].value = edit["previous_text"]
    assert revised == record


@pytest.mark.parametrize("old,new", [
    ("counterevidence[0].value", "observation_ids[0]"),
    ("comparisons[0]", "comparisons[1]"),
    ("74%", "78%"),
    ("Use S1 and Table S2 in S3.", "Use S4."),
    ("Use S1 and Table S2 in S3.", "Use Table S1 and Fig. S3."),
    ("Use S1 and Table S2 in S3.", "Use S1 and also change the evidence."),
    ("Use S1 and Table S2 in S3.", "Use S1. Update another field."),
    (" with ", " to say "),
])
def test_reviewer_text_rejects_nonliteral_foreign_or_ambiguous_edit(old, new):
    record, packet, report = example()
    report.additional_issues[0] = report.additional_issues[0].replace(old, new)
    with pytest.raises(ValueError):
        prescribed_edit(record, packet, report)


def test_reviewer_text_leaves_multiple_obligations_and_unsourced_values_open():
    record, packet, report = example()
    report.decisions.append(report.decisions[0].model_copy(update={"groups":["F2"]}))
    with pytest.raises(ValueError, match="Exactly one"):
        prescribed_edit(record, packet, report)
    report.decisions.pop()
    packet["sources"][1]["text"] = "Control B result unavailable"
    with pytest.raises(ValueError, match="percentage"):
        prescribed_edit(record, packet, report)


def test_reviewer_text_replay_keeps_immutable_file_and_single_receipt(tmp_path):
    store = DataStore(tmp_path)
    store.initialize()
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        run = store.new_run_in_transaction(db, "extraction", {"operation":"example"})
    path = run/"local.json"
    _freeze(path, {"a":"retained"})
    _freeze(path, {"a":"retained"})
    with pytest.raises(ValueError, match="differs"):
        _freeze(path, {"a":"modified"})
    with store.connection() as db:
        _event_once(db, run, "prepared", {"a":"retained"})
        _event_once(db, run, "prepared", {"a":"retained"})
        with pytest.raises(ValueError, match="changed"):
            _event_once(db, run, "prepared", {"a":"modified"})
        count = db.execute("SELECT COUNT(*) FROM run_events WHERE run_id=? AND event='prepared'", (run.name,)).fetchone()
    assert count[0] == 1
