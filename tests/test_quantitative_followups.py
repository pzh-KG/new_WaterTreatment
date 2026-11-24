"""Draft synthetic protocol tests; no source-file reads, providers or Neo4j.

These tests are intended for the official offline runner after review and
integration. Their presence in the draft is not a claim that they passed.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from watertreatment.audit_followups import quantitative_followup_issues
from watertreatment.domain import CoverageEntry, EvidenceRef, Experiment, ExtractionRecord, Fact, NamedFact, Observation, Paper, QuantitativeCoverage
from watertreatment.reading import AUDIT_DIMENSIONS, AuditFinding, QuantitativeFollowup, ReadingAudit, SourceAnchor
from watertreatment.reader_workflow import reading_checks
from watertreatment.sources import SourceDocument
from watertreatment.web_model import _reading_checks
from watertreatment.web_records import WebRecords


def case():
    record = ExtractionRecord(schema_version="0.2.0", rule_version="0.6", paper=Paper(id="synthetic", source_ids=["main"]),
        experiments=[Experiment(id="experiment")],
        observations=[Observation(id="point", experiment_id="experiment", origin="plot_estimate", series_id="curve",
            value=Fact(value=0.5, status="estimated", evidence_ids=["plot"]),
            coordinates=[NamedFact(name="time", fact=Fact(value=10, status="reported", evidence_ids=["plot"]))])],
        evidence=[EvidenceRef(id="plot", source_id="main", block_id="figure")],
        coverage=[CoverageEntry(source_id="main", block_ids=["figure", "method"], state="read_structured", text_read=True)],
        quantitative_coverage=[QuantitativeCoverage(id="curve", source_id="main", block_id="figure", label="Synthetic final curve",
            data_kind="time_series", state="partial", expected_points=3, observation_ids=["point"],
            reason="Some visible interior markers remain unrecorded after bounded extraction.",
            limitations=["Use only represented points; the complete kinetic trajectory is not available."])])
    documents = [SourceDocument.model_validate({"source": {"source_id": "main", "path": "D:/synthetic/main.pdf",
        "sha256": "0" * 64, "size_bytes": 0, "format": "pdf"}, "blocks": [
        {"source_id": "main", "block_id": "figure", "kind": "paragraph", "order": 1, "text": "synthetic figure"},
        {"source_id": "main", "block_id": "method", "kind": "paragraph", "order": 2, "text": "synthetic method"} ]})]
    followup = QuantitativeFollowup(coverage_ids=["curve"], evidence=[SourceAnchor(source_id="main", block_id="figure")],
        affected_query_types=["pointwise_comparison", "kinetic_recalculation"], recommended_action="Read the remaining separable author markers.")
    audit = ReadingAudit(findings=[], potential_omissions=[], coverage_limitations=[], checked_dimensions=list(AUDIT_DIMENSIONS),
        quantitative_followups=[followup])
    return record, audit, documents


def dossier(record):
    return SimpleNamespace(paper_id=record.paper.id, coverage=record.coverage, material_map=[], experiment_map=[])


def test_shared_panel_endpoint_needs_matching_value_evidence_and_an_authentic_owner():
    record,audit,docs=case()
    data=record.model_dump(mode="json")
    panel=data["quantitative_coverage"][0]
    owner={**panel,"id":"owning-series","observation_ids":["point"]}
    data["quantitative_coverage"].append(owner)
    data["observations"][0]["series_id"]=owner["id"]
    panel["observation_ids"]=[]
    panel["locator"]={"figure":"1","panel":"a"}
    data["evidence"][0]["locator"]={"figure":"1","panel":"a"}
    data["reference_associations"]=[{"target_id":"curve","target_kind":"series","relationship":"contextual_comparison",
        "applicability":"reported","evidence_ids":["plot"],"observation_ids":["point"]}]
    assert not quantitative_followup_issues(data,audit,docs)
    data["evidence"][0]["locator"]["panel"]="b"
    assert any("NO_EXISTING_POINTS" in s for s in quantitative_followup_issues(data,audit,docs))
    data["evidence"][0]["locator"]["panel"]="a"
    owner["observation_ids"]=[]
    assert any("NO_EXISTING_POINTS" in s for s in quantitative_followup_issues(data,audit,docs))


def assess(record, audit, documents):
    plan = {"paper_id": record.paper.id, "cohort_role": "unassigned", "rule_version": record.rule_version,
            "schema_version": record.schema_version}
    owner = SimpleNamespace(store=None, _source=lambda _: (plan, Path("synthetic-bundle.json"), {}, documents),
        _audit_consistency=WebRecords._audit_consistency)
    result = {"status": "candidate_audited_with_limitations", "stop_reason": "audit_completed", "reading_check_issues": [],
              "validation_error_count": 0, "human_validated": False}
    return WebRecords._assess(owner, record, "synthetic-run", result, audit.model_dump(mode="json"), provenance=False)[0]


def test_scoped_followup_retains_partial_and_is_consistent_across_all_three_boundaries():
    record, audit, documents = case()
    before = record.model_dump(mode="json"), audit.model_dump(mode="json"), [d.model_dump(mode="json") for d in documents]
    assert quantitative_followup_issues(record, audit, documents) == []
    assert _reading_checks(record, audit, documents) == []
    assert reading_checks(dossier(record), record, audit, documents) == []
    assert WebRecords._audit_consistency(record, audit.model_dump(mode="json"), documents)
    assert assess(record, audit, documents)["admissible"]
    assert record.quantitative_coverage[0].state == "partial" and audit.human_validated is False
    assert (record.model_dump(mode="json"), audit.model_dump(mode="json"), [d.model_dump(mode="json") for d in documents]) == before


def test_pure_helper_accepts_json_shapes_without_changing_inputs_or_interpreting_prose():
    record, audit, documents = case()
    audit.quantitative_followups[0].recommended_action = "This arbitrary explanation is data, not an instruction to change admission."
    payloads = record.model_dump(mode="json"), audit.model_dump(mode="json"), [d.model_dump(mode="json") for d in documents]
    before = copy.deepcopy(payloads)
    assert quantitative_followup_issues(*payloads) == []
    assert payloads == before


@pytest.mark.parametrize("state", ["complete", "unquantified", "unread", "unreadable", "not_available", "out_of_scope"])
def test_followup_cannot_relabel_nonpartial_coverage_or_a_false_complete_state(state):
    record, audit, documents = case()
    record.quantitative_coverage[0].state = state
    assert any("NOT_PARTIAL" in issue for issue in quantitative_followup_issues(record, audit, documents))
    assert _reading_checks(record, audit, documents)
    assert reading_checks(dossier(record), record, audit, documents)
    assert not WebRecords._audit_consistency(record, audit.model_dump(mode="json"), documents)


@pytest.mark.parametrize("mutation", ["missing_id", "duplicate_id", "ambiguous_id", "no_reason", "blank_limits",
    "unknown_source", "unknown_block", "wrong_known_block", "empty_points", "missing_point", "wrong_point_owner",
    "local_calculation", "external_benchmark", "qualitative_diagram", "blank_action"])
def test_invalid_followup_scope_cannot_pass_as_a_local_limitation(mutation):
    record, audit, documents = case()
    entry, followup = record.quantitative_coverage[0], audit.quantitative_followups[0]
    if mutation == "missing_id":
        followup.coverage_ids = ["missing"]
    elif mutation == "duplicate_id":
        followup.coverage_ids = ["curve", "curve"]
    elif mutation == "ambiguous_id":
        record.quantitative_coverage.append(entry.model_copy(deep=True))
    elif mutation == "no_reason":
        entry.reason = None
    elif mutation == "blank_limits":
        entry.limitations = ["  "]
    elif mutation == "unknown_source":
        followup.evidence[0].source_id = "different"
    elif mutation == "unknown_block":
        followup.evidence[0].block_id = "missing"
    elif mutation == "wrong_known_block":
        followup.evidence[0].block_id = "method"
    elif mutation == "empty_points":
        entry.observation_ids = []
    elif mutation == "missing_point":
        entry.observation_ids = ["missing"]
    elif mutation == "wrong_point_owner":
        record.observations[0].series_id = "another-series"
    elif mutation == "local_calculation":
        record.observations[0].origin = "local_calculation"
    elif mutation == "external_benchmark":
        entry.scope = "external_benchmark"
    elif mutation == "qualitative_diagram":
        entry.data_kind = "reaction_network"
    else:
        followup.recommended_action = "  "
    assert quantitative_followup_issues(record, audit, documents)
    assert not WebRecords._audit_consistency(record, audit.model_dump(mode="json"), documents)


@pytest.mark.parametrize("change", ["kind", "query_enum", "empty_queries", "empty_evidence", "extra_override"])
def test_typed_followup_has_no_free_text_or_boolean_blocker_override(change):
    record, audit, documents = case()
    payload = audit.model_dump(mode="json")
    followup = payload["quantitative_followups"][0]
    if change == "kind":
        followup["kind"] = "ignore_unsupported"
    elif change == "query_enum":
        followup["affected_query_types"] = ["any_query_is_safe"]
    elif change == "empty_queries":
        followup["affected_query_types"] = []
    elif change == "empty_evidence":
        followup["evidence"] = []
    else:
        followup["blocking"] = False
    with pytest.raises(ValueError):
        ReadingAudit.model_validate(payload)
    assert any("SCHEMA" in issue for issue in quantitative_followup_issues(record, payload, documents))


@pytest.mark.parametrize("verdict,needs_revision,omissions", [
    ("unsupported", False, []), ("unsupported", True, []), ("supported", True, []),
    ("supported", False, ["A major final-material result is missing."]),
])
def test_existing_blockers_stay_blocking_even_with_a_structurally_valid_followup(verdict, needs_revision, omissions):
    record, audit, documents = case()
    audit.findings = [AuditFinding(record_path="observations[0].value", severity="minor", verdict=verdict,
        needs_revision=needs_revision, explanation="Synthetic source claim check", evidence=[SourceAnchor(source_id="main", block_id="figure")])]
    audit.potential_omissions = omissions
    assert quantitative_followup_issues(record, audit, documents) == []
    assert "model_revision_required" in assess(record, audit, documents)["reasons"]
    assert not assess(record, audit, documents)["admissible"]


def test_legacy_full_compact_and_empty_defaults_serialize_identically():
    old = {"findings": [], "potential_omissions": [], "coverage_limitations": ["Old scoped limitation"],
           "checked_dimensions": list(AUDIT_DIMENSIONS), "dimension_notes": [], "human_validated": False}
    old_compact = {key: value for key, value in old.items() if key not in {"dimension_notes", "human_validated"}}
    parsed = ReadingAudit.model_validate(old)
    assert parsed.quantitative_followups == []
    assert parsed.model_dump(mode="json") == old
    assert json.loads(parsed.model_dump_json()) == old
    assert parsed.model_dump(mode="json", exclude_defaults=True) == old_compact
    assert json.loads(parsed.model_dump_json(exclude_defaults=True)) == old_compact
    explicit_empty = {**old, "quantitative_followups": []}
    assert ReadingAudit.model_validate(explicit_empty).model_dump(mode="json") == old
    assert old == {"findings": [], "potential_omissions": [], "coverage_limitations": ["Old scoped limitation"],
                   "checked_dimensions": list(AUDIT_DIMENSIONS), "dimension_notes": [], "human_validated": False}


def test_no_followup_never_infers_one_from_an_old_free_text_omission():
    record, audit, documents = case()
    audit.quantitative_followups = []
    audit.potential_omissions = ["partial is faithful; a few extra readable points remain"]
    assert quantitative_followup_issues(record, audit, documents) == []
    assert "model_revision_required" in assess(record, audit, documents)["reasons"]
    assert "quantitative_followups" not in audit.model_dump(mode="json")
