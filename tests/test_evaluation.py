from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from watertreatment.evaluation import (
    REQUIRED_VERSIONS, PaperRoleHistory, ReviewEvidence, ReviewItem, ReviewJudgment,
    ReviewKey, ReviewScope, RoleEvent, compare_reviewed_items,
)

STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def item(**changes):
    key_changes = changes.pop("key_changes", {})
    key = dict(paper_id="synthetic-A", material_id="final-A", experiment_id="run-A", metric="test-rate", target_id="target-A", time_window="interval-A", stream_id="reactor-liquid", oxidant_id="oxidant-A")
    key.update(key_changes)
    payload = dict(key=ReviewKey(**key), status="reported", value="7", unit="test-unit", evidence=(ReviewEvidence(source_id="source-A", locator="page:2;figure:X"),))
    payload.update(changes)
    return ReviewItem(**payload)


def complete(paper="synthetic-A"):
    return [ReviewScope(paper_id=paper, coverage="complete_paper")]


@pytest.mark.parametrize("field,replacement", [
    ("material_id", "precursor-B"), ("experiment_id", "run-B"),
    ("target_id", "target-B"), ("time_window", "interval-B"),
    ("stream_id", "condensate"), ("oxidant_id", "oxidant-B"),
])
def test_same_number_with_wrong_binding_is_not_correct(field, replacement):
    report = compare_reviewed_items([item()], [item(key_changes={field: replacement})], paper_scopes=complete())
    assert report.counts["experimental_binding"].tp == 0
    assert report.counts["experimental_binding"].fp == 1
    assert report.counts["experimental_binding"].fn == 1
    assert report.counts["value_unit_exact"].tp == 0
    assert len(report.per_paper["synthetic-A"].missing_keys) == 1


def test_default_partial_scope_does_not_call_unreviewed_extra_a_hallucination():
    extra = item(key_changes={"experiment_id": "unreviewed-run"})
    report = compare_reviewed_items([item()], [item(), extra])
    paper = report.per_paper["synthetic-A"]
    assert paper.metrics["experimental_binding"].tp == 1
    assert paper.metrics["experimental_binding"].fp == 0
    assert paper.unscored_actual_keys == (extra.key,)
    full = compare_reviewed_items([item()], [item(), extra], paper_scopes=complete())
    assert full.counts["experimental_binding"].fp == 1


def test_omissions_are_reported_even_if_all_returned_values_are_correct():
    second = item(key_changes={"experiment_id": "run-B"}, value="11")
    report = compare_reviewed_items([item(), second], [item()])
    assert report.counts["value_unit_exact"].precision == 1
    assert report.counts["value_unit_exact"].recall == 0.5
    assert report.per_paper["synthetic-A"].missing_keys == (second.key,)


def test_paper_grouping_never_pairs_identical_records_from_different_papers():
    report = compare_reviewed_items([item()], [item(key_changes={"paper_id": "synthetic-B"})], paper_scopes=complete() + complete("synthetic-B"))
    assert set(report.per_paper) == {"synthetic-A", "synthetic-B"}
    assert report.counts["value_unit_exact"].tp == 0
    assert report.counts["value_unit_exact"].fp == 1
    assert report.counts["value_unit_exact"].fn == 1


def test_not_reported_and_unread_and_inferred_are_different():
    missing = item(status="not_reported", value=None, unit=None, evidence=())
    unread = item(status="unread", value=None, unit=None, evidence=())
    report = compare_reviewed_items([missing], [unread])
    assert report.counts["field_status"].tp == 0
    assert report.counts["field_status"].fn == 1
    assert report.counts["value_unit_exact"].f1 is None
    inferred = item(status="inferred", status_reason="unsupported mechanistic guess")
    report = compare_reviewed_items([item()], [inferred])
    assert report.counts["value_unit_exact"].tp == 0
    assert report.counts["value_unit_exact"].fn == 1
    assert report.per_paper["synthetic-A"].actual_status_counts == {"inferred": 1}


def test_unread_gold_is_not_a_true_negative_or_a_reviewed_denominator():
    unread = item(status="read_not_quantified", value=None, unit=None, evidence=())
    report = compare_reviewed_items([unread], [item()], paper_scopes=complete())
    paper = report.per_paper["synthetic-A"]
    assert paper.unreviewed_gold_keys == (unread.key,)
    assert paper.unscored_actual_keys == (item().key,)
    assert report.counts["experimental_binding"].recall is None
    assert report.eligible_for_independent_summary is False


def judgment(expected, actual, **changes):
    fields = dict(aspect="value_unit", verdict="accepted", reviewer_kind="human", reviewer_id="reviewer-test", rationale="synthetic unit equivalence checked", recorded_at=STAMP)
    fields.update(changes)
    return ReviewJudgment.for_items(expected, actual, **fields)


def test_semantic_equivalence_requires_explicit_human_judgment():
    expected, actual = item(value="2", unit="kg"), item(value="2000", unit="g")
    raw = compare_reviewed_items([expected], [actual])
    assert raw.counts["value_unit_exact"].tp == 0
    model_judgment = judgment(expected, actual, reviewer_kind="model_judge", judge_version="synthetic-model/prompt-v1")
    automatic = compare_reviewed_items([expected], [actual], semantic_judgments=[model_judgment])
    assert automatic.counts["value_unit_human_semantic"].tp == 0
    assert automatic.per_paper["synthetic-A"].judgment_counts["model_judge:value_unit"]["accepted"] == 1
    reviewed = compare_reviewed_items([expected], [actual], semantic_judgments=[judgment(expected, actual)])
    assert reviewed.counts["value_unit_exact"].tp == 0
    assert reviewed.counts["value_unit_human_semantic"].tp == 1


def test_stale_and_conflicting_semantic_judgments_are_rejected():
    expected, actual = item(), item(value="different wording")
    review = judgment(expected, actual)
    with pytest.raises(ValueError, match="Stale judgment"):
        compare_reviewed_items([expected], [item(value="changed again")], semantic_judgments=[review])
    with pytest.raises(ValueError, match="Conflicting/duplicate"):
        compare_reviewed_items([expected], [actual], semantic_judgments=[review, review])
    with pytest.raises(ValueError, match="different experimental binding"):
        judgment(expected, item(key_changes={"experiment_id": "wrong"}))
    with pytest.raises(ValidationError, match="judge_version"):
        judgment(expected, actual, reviewer_kind="model_judge")


def test_location_match_is_not_semantic_evidence_support():
    expected, actual = item(), item()
    review = judgment(expected, actual, aspect="evidence_support", verdict="rejected", rationale="location exists but does not support scientific attribution")
    report = compare_reviewed_items([expected], [actual], semantic_judgments=[review])
    assert report.counts["evidence_locator_overlap"].tp == 1
    assert report.per_paper["synthetic-A"].judgment_counts["human:evidence_support"]["rejected"] == 1
    assert "semantic support" in report.warning


def test_missing_evidence_is_distinct_from_wrong_value():
    report = compare_reviewed_items([item()], [item(evidence=())])
    assert report.counts["value_unit_exact"].tp == 1
    assert report.counts["evidence_locator_overlap"].fn == 1


def test_development_history_cannot_be_undone_by_relabeling_heldout():
    history = PaperRoleHistory(paper_id="synthetic-A", events=(
        RoleEvent(role="heldout", recorded_at=STAMP, reason="reserved"),
        RoleEvent(role="expanded_calibration", recorded_at=STAMP, reason="rules revised", used_for_rule_revision=True),
        RoleEvent(role="heldout", recorded_at=STAMP, reason="incorrect attempted relabel"),
    ))
    report = compare_reviewed_items([item()], [item()], paper_roles=[history], versions={k: "frozen-test" for k in REQUIRED_VERSIONS})
    assert history.independent_eligible is False
    assert report.eligible_for_independent_summary is False


def test_independence_requires_role_history_and_frozen_versions():
    history = PaperRoleHistory(paper_id="synthetic-A", events=(RoleEvent(role="heldout", recorded_at=STAMP, reason="reserved before development"),))
    report = compare_reviewed_items([item()], [item()], paper_roles=[history])
    assert report.missing_versions and not report.eligible_for_independent_summary
    report = compare_reviewed_items([item()], [item()], paper_roles=[history], versions={k: "frozen-test" for k in REQUIRED_VERSIONS})
    assert report.eligible_for_independent_summary
    assert "unreviewed papers" in report.warning


def test_abstract_screening_is_not_fulltext_rule_development():
    history = PaperRoleHistory(paper_id="synthetic-A", events=(
        RoleEvent(role="background", recorded_at=STAMP, reason="screened title and abstract only", exposure="abstract"),
        RoleEvent(role="heldout", recorded_at=STAMP, reason="full text reserved", exposure="none"),
    ))
    assert history.independent_eligible
    changed = PaperRoleHistory(paper_id="synthetic-A", events=(
        RoleEvent(role="background", recorded_at=STAMP, reason="abstract used to change rule", exposure="abstract", used_for_rule_revision=True),
        history.events[-1],
    ))
    assert not changed.independent_eligible


def test_false_positives_from_development_paper_do_not_enter_independent_summary():
    heldout = PaperRoleHistory(paper_id="synthetic-A", events=(RoleEvent(role="heldout", recorded_at=STAMP, reason="reserved"),))
    development = PaperRoleHistory(paper_id="synthetic-B", events=(RoleEvent(role="development", recorded_at=STAMP, reason="development"),))
    report = compare_reviewed_items(
        [item()], [item(), item(key_changes={"paper_id": "synthetic-B"})],
        paper_roles=[heldout, development], paper_scopes=complete() + complete("synthetic-B"),
        versions={k: "frozen-test" for k in REQUIRED_VERSIONS},
    )
    assert report.counts["experimental_binding"].fp == 1
    assert not report.eligible_for_independent_summary


def test_duplicate_keys_do_not_silently_double_count_or_select_best_value():
    with pytest.raises(ValueError, match="Duplicate actual key"):
        compare_reviewed_items([item()], [item(), item(value="another")])
    with pytest.raises(ValueError, match="Duplicate expected key"):
        compare_reviewed_items([item(), item()], [item()])


def test_invalid_missing_payload_and_nonfinite_values_fail():
    with pytest.raises(ValidationError):
        item(status="unread")
    with pytest.raises(ValidationError):
        item(value=float("nan"))
