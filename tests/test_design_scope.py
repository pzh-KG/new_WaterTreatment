"""Synthetic three-layer scope checks; no provider, database or paper answers."""
import copy
import json

import pytest

from watertreatment.design_scope import (
    CORE_AREAS, RESEARCH_USES, design_scope_issues, design_source_index_issues,
)
from watertreatment.domain import (
    CoreResearchCheck, CoverageEntry, DesignComparison, EvidenceLocator, EvidenceRef, Experiment,
    ExtractionRecord, Fact, MaterialSample, Observation, ObservationPair, Paper,
    QuantitativeCoverage, ResearchDesignScope, ResearchEligibility,
    ResearchModuleActivation, ResearchObservationAssignment, ResearchSourceIndex,
)
from watertreatment.research_scope import archived_dense_ids, scope_issues, scoped_projection
from watertreatment.storage import digest_json


def design_record():
    return ExtractionRecord(
        schema_version="0.4.0", rule_version="0.8", extraction_scope="research_core",
        paper=Paper(id="paper", source_ids=["source"]),
        materials=[MaterialSample(id="material", role="primary_final")],
        experiments=[Experiment(id="experiment", material_ids=["material"], purpose="coupled")],
        observations=[Observation(id="endpoint", experiment_id="experiment", material_id="material",
            analysis_role="key_result", metric_kind="treatment_efficiency",
            value=Fact(value=72, status="reported", evidence_ids=["evidence"]),
            evidence_ids=["evidence"], series_id="series")],
        evidence=[EvidenceRef(id="evidence", source_id="source", block_id="page")],
        quantitative_coverage=[QuantitativeCoverage(id="series", source_id="source", block_id="page",
            label="Target response", data_kind="time_series", state="partial", observation_ids=["endpoint"],
            reason="Key endpoint retained; regular interior points deferred.")],
        design_scope=ResearchDesignScope(
            core_checks=[CoreResearchCheck(area=area, state="checked", reason="Source checked; missing values keep their original state.") for area in sorted(CORE_AREAS)],
            observation_assignments=[ResearchObservationAssignment(observation_id="endpoint", layer="core",
                purpose="performance", reason="Final-system endpoint under the stated conditions.")],
            source_index=[ResearchSourceIndex(id="index", source_id="source", block_id="page",
                locator=EvidenceLocator(physical_page=1), content_types=["figure"], reading_state="read",
                summary="Endpoint and overall decline understood; the ordinary sampling grid was not digitized.",
                material_relation="The final sample in the coupled experiment.", retained_observation_ids=["endpoint"],
                quantitative_coverage_ids=["series"], deferred_topics=["dense_time_series"],
                reason="Ordinary intermediate samples do not determine the present design comparison.")],
            eligibility=[ResearchEligibility(use=use, target_kind="paper", target_id="paper", status="not_assessed",
                reason="Explicit downstream-use assessment remains pending.") for use in sorted(RESEARCH_USES)]))


def codes(record):
    return {issue.code for issue in scope_issues(record)}


def documents():
    return [{"source": {"source_id": "source", "page_count": 2},
             "blocks": [{"block_id": "page", "source_id": "source", "location": {"physical_page": 1}}]}]


def test_native_contract_keeps_reading_and_quantification_distinct():
    record = design_record()
    assert not codes(record)
    assert scoped_projection(record)
    assert not design_source_index_issues(record, documents())
    assert record.design_scope.source_index[0].reading_state == "read"
    assert record.quantitative_coverage[0].state == "partial"
    assert len(record.observations) == 1  # No numeric records for deferred regular samples.
    assert record == ExtractionRecord.model_validate_json(record.model_dump_json())


@pytest.mark.parametrize("rule,schema", [("0.5", "0.1.0"), ("0.6", "0.2.0"), ("0.7", "0.3.0")])
def test_legacy_new_default_fields_do_not_change_any_serialized_fingerprint(rule, schema):
    record = ExtractionRecord(rule_version=rule, schema_version=schema, paper=Paper(id="paper"),
        comparisons=[DesignComparison(id="comparison")])
    archived = record.model_dump(mode="json")
    assert "design_scope" not in archived
    assert set(archived["comparisons"][0]) == {
        "id", "material_ids", "observation_ids", "operation", "held_conditions", "changed_conditions", "outcomes",
        "causal_status", "limitations", "evidence_ids"}
    assert digest_json(ExtractionRecord.model_validate(archived).model_dump(mode="json")) == digest_json(archived)
    assert not design_scope_issues(record)


def test_legacy_cannot_opt_into_three_layer_scientific_semantics_by_adding_flags():
    record = design_record()
    record.rule_version, record.schema_version = "0.7", "0.3.0"
    assert "DESIGN_SCOPE_VERSION" in codes(record)


@pytest.mark.parametrize("mutation,expected", [
    (lambda r: setattr(r, "design_scope", None), "DESIGN_SCOPE_REQUIRED"),
    (lambda r: setattr(r, "schema_version", "0.3.0"), "DESIGN_SCOPE_VERSION"),
    (lambda r: r.design_scope.core_checks.pop(), "DESIGN_CORE_CHECK_REQUIRED"),
    (lambda r: r.design_scope.core_checks.append(r.design_scope.core_checks[0]), "DESIGN_CORE_CHECK_REQUIRED"),
    (lambda r: r.design_scope.observation_assignments.clear(), "DESIGN_ASSIGNMENT_REQUIRED"),
    (lambda r: r.design_scope.observation_assignments.append(r.design_scope.observation_assignments[0]), "DESIGN_ASSIGNMENT_REQUIRED"),
    (lambda r: setattr(r.design_scope.observation_assignments[0], "reason", " "), "DESIGN_EMPTY_REASON"),
    (lambda r: setattr(r.design_scope.observation_assignments[0], "observation_id", "invented"), "DESIGN_MISSING_REFERENCE"),
    (lambda r: r.design_scope.source_index.clear(), "DESIGN_SOURCE_INDEX_REQUIRED"),
    (lambda r: r.paper.source_ids.append("si"), "DESIGN_SOURCE_INDEX_INCOMPLETE"),
    (lambda r: r.design_scope.source_index[0].quantitative_coverage_ids.clear(), "DESIGN_SERIES_NOT_INDEXED"),
    (lambda r: setattr(r.design_scope.source_index[0], "block_id", "unrelated"), "DESIGN_INDEX_SOURCE_MISMATCH"),
    (lambda r: setattr(r.design_scope.source_index[0], "reading_state", "unread"), "DESIGN_DEFERRED_NOT_READ"),
    (lambda r: r.design_scope.eligibility.pop(), "DESIGN_USE_ASSESSMENT_REQUIRED"),
    (lambda r: setattr(r.design_scope.eligibility[0], "status", "eligible"), "DESIGN_ELIGIBILITY_EVIDENCE_REQUIRED"),
    (lambda r: setattr(r.design_scope.eligibility[0], "status", "limited"), "DESIGN_ELIGIBILITY_LIMITATION_REQUIRED"),
])
def test_missing_or_contradictory_scope_contracts_are_rejected(mutation, expected):
    record = design_record()
    mutation(record)
    assert expected in codes(record)


def test_core_check_missing_source_is_not_forced_to_report_or_have_evidence():
    record = design_record()
    check = record.design_scope.core_checks[0]
    check.state = "not_available"
    check.reason = "One necessary attachment is unavailable; the paper remains useful within limits."
    assert "DESIGN_CORE_LIMITATION_REQUIRED" in codes(record)
    check.limitations = ["No exact composition fraction comparison can be made from the available sources."]
    assert not codes(record)


def test_source_index_unread_content_can_stay_unread_without_becoming_deliberate_deferral():
    record = design_record()
    record.design_scope.source_index[0].reading_state = "unreadable"
    record.design_scope.source_index[0].deferred_topics = []
    record.design_scope.source_index[0].reason = "Image remains unreadable; do not treat this as a policy-driven omission."
    assert not codes(record)


def test_native_core_cannot_emit_detail_and_then_merely_hide_it_in_web():
    record = design_record()
    assignment = record.design_scope.observation_assignments[0]
    assignment.layer, assignment.purpose = "detail", "requested_detail"
    record.observations[0].analysis_role = "dense_series_point"
    record.observations[0].role_reason = "Regular interior point."
    assert "DESIGN_UNREQUESTED_DETAIL" in codes(record)
    assert not archived_dense_ids(record)  # Invalid diagnostic projection also fails closed.
    record.extraction_scope = "series_digitization"
    record.digitization_targets = ["series"]
    assert not codes(record)
    assert archived_dense_ids(record) == {"endpoint"}
    record.design_scope.observation_assignments[0].layer = "core"
    assert "DESIGN_DETAIL_MISCLASSIFIED" in codes(record)
    assert not archived_dense_ids(record)


def test_mixed_source_panel_retains_core_result_and_defers_detail_without_conflicting_read_claim():
    record = design_record()
    record.coverage = [CoverageEntry(source_id="source", block_ids=["page"], state="read_unquantified", visual_checked=True)]
    assert not codes(record)
    record.coverage[0].state = "parsed_unread"
    assert "DESIGN_INDEX_READING_CONFLICT" in codes(record)


def test_native_scope_survives_compact_candidate_navigation_without_becoming_complete_record():
    from watertreatment.candidate_view import candidate_view
    record = design_record()
    original = record.model_dump(mode="json")
    rendered = candidate_view(record, {"path": "immutable-candidate.json", "sha256": "a" * 64,
        "content_hash": digest_json(original), "scope": "test-turn"})
    view = json.loads(rendered.split("\n", 1)[1])
    assert view["complete_record_inlined"] is False
    assert view["design_scope_navigation"]["layer_counts"] == {"core": 1}
    assert view["design_scope_navigation"]["source_index_entries"] == 1
    assert "design_scope" in view["query"]["sections"]
    assert record.model_dump(mode="json") == original


@pytest.mark.parametrize("purpose", ["performance", "condition_endpoint", "stability", "material_feature", "design_comparison", "mechanism_claim", "adverse_result"])
def test_all_relevant_numeric_kinds_can_stay_core_without_count_caps(purpose):
    record = design_record()
    record.design_scope.observation_assignments[0].purpose = purpose
    for index in range(25):
        point = copy.deepcopy(record.observations[0])
        point.id = f"condition-{index}"
        point.analysis_role = "condition_comparison"
        record.observations.append(point)
        record.quantitative_coverage[0].observation_ids.append(point.id)
        record.design_scope.observation_assignments.append(ResearchObservationAssignment(
            observation_id=point.id, layer="core", purpose="condition_endpoint", reason="Each actually tested condition has its own endpoint."))
    assert not codes(record)


def test_module_requires_real_source_activation_and_correct_owner():
    record = design_record()
    assignment = record.design_scope.observation_assignments[0]
    assignment.layer, assignment.module_id = "module", "outdoor"
    assert "DESIGN_MODULE_REQUIRED" in codes(record)
    record.design_scope.modules.append(ResearchModuleActivation(id="outdoor", kind="outdoor_application",
        reason="Outdoor operation affects the claim."))
    assert {"DESIGN_MODULE_EVIDENCE_REQUIRED", "DESIGN_MODULE_OWNER_REQUIRED"} <= codes(record)
    module = record.design_scope.modules[0]
    module.evidence_ids = ["evidence"]
    module.experiment_ids = ["experiment"]
    assert not codes(record)
    module.experiment_ids = ["unrelated"]
    assert {"DESIGN_MISSING_REFERENCE", "DESIGN_MODULE_OWNER_MISMATCH"} <= codes(record)


def test_unavailable_core_facts_and_limited_uses_do_not_invalidate_entire_paper():
    record = design_record()
    record.observations[0].value = Fact(status="not_reported", note="The source does not report this endpoint.")
    record.design_scope.eligibility[0].status = "limited"
    record.design_scope.eligibility[0].limitations = ["Exact numerical ranking is unsupported."]
    assert not codes(record)


def test_direct_design_pairing_never_becomes_optional_numeric_detail():
    record = design_record()
    record.extraction_scope, record.digitization_targets = "series_digitization", ["series"]
    record.observations[0].analysis_role, record.observations[0].role_reason = "dense_series_point", "Claimed optional point."
    assignment = record.design_scope.observation_assignments[0]
    assignment.layer, assignment.purpose = "detail", "requested_detail"
    record.comparisons = [DesignComparison(id="comparison", observation_ids=["endpoint"])]
    assert "DESIGN_PROTECTED_DETAIL" in codes(record)


def test_source_index_cannot_launder_an_unrelated_evidence_block():
    record = design_record()
    record.evidence[0].block_id = "other-page"
    assert "DESIGN_INDEX_OBSERVATION_SOURCE" in codes(record)


@pytest.mark.parametrize("change,expected", [
    (lambda r: setattr(r.design_scope.source_index[0], "block_id", "missing"), "DESIGN_INDEX_BLOCK_MISSING"),
    (lambda r: setattr(r.design_scope.source_index[0].locator, "physical_page", 2), "DESIGN_INDEX_LOCATOR_MISMATCH"),
    (lambda r: setattr(r.design_scope.source_index[0].locator, "physical_page", 10), "DESIGN_INDEX_PAGE_RANGE"),
])
def test_source_locations_are_checked_when_real_document_representations_are_available(change, expected):
    record = design_record()
    change(record)
    assert expected in {issue.code for issue in design_source_index_issues(record, documents())}
    assert design_source_index_issues(record, ()) == []  # No false claim of source checking.


def test_explicit_design_tradeoffs_and_counterevidence_roundtrip_without_new_causal_claims():
    record = design_record()
    record.comparisons = [DesignComparison(id="comparison", material_ids=["material"], observation_ids=["endpoint"],
        causal_status="author_hypothesis", author_interpretation=Fact(value="A proposed structure effect", status="reported", evidence_ids=["evidence"]),
        counterevidence=[Fact(value="One response worsened", status="reported", evidence_ids=["evidence"])],
        confounders=[Fact(status="unread", note="Independent changes have not been resolved.")])]
    restored = ExtractionRecord.model_validate_json(record.model_dump_json())
    assert restored.comparisons == record.comparisons
    assert restored.comparisons[0].causal_status == "author_hypothesis"
    assert not codes(restored)


def add_unavailable_si(record, state="unreadable"):
    record.paper.source_ids.append("si-source")
    record.design_scope.source_index.append(ResearchSourceIndex(
        id="si-availability", source_id="si-source", block_id=None,
        content_types=["supporting_information"], reading_state=state,
        summary="The registered SI yielded no parsed blocks; its contents were not read.",
        material_relation="Its possible details about the final material remain unavailable.",
        reason="Record an availability limitation without inventing a source location.",
        limitations=["SI-only details cannot support composition or exact-condition comparisons."]))
    record.design_scope.eligibility[0].status = "limited"
    record.design_scope.eligibility[0].limitations = ["Registered SI content could not be inspected."]
    return documents() + [{"source": {"source_id": "si-source", "page_count": None}, "blocks": [], "phase": "unreadable"}]


@pytest.mark.parametrize("state", ["unreadable", "not_available"])
def test_registered_unavailable_si_without_blocks_keeps_otherwise_useful_record(state):
    record = design_record()
    sources = add_unavailable_si(record, state)
    assert not codes(record)
    assert not design_source_index_issues(record, sources)
    restored = ExtractionRecord.model_validate_json(record.model_dump_json())
    assert restored.design_scope.source_index[-1].block_id is None
    assert restored.design_scope.source_index[-1].reading_state == state
    assert len(restored.observations) == 1
    assert restored.observations[0].value.value == 72


@pytest.mark.parametrize("mutation", [
    lambda e: setattr(e, "reading_state", "read"),
    lambda e: setattr(e, "reading_state", "unread"),
    lambda e: e.limitations.clear(),
    lambda e: setattr(e, "limitations", [" "]),
    lambda e: e.retained_observation_ids.append("endpoint"),
    lambda e: e.quantitative_coverage_ids.append("series"),
    lambda e: e.deferred_topics.append("toxicity_matrix"),
    lambda e: setattr(e.locator, "physical_page", 1),
    lambda e: setattr(e.locator, "description", "Invented figure position"),
])
def test_blockless_source_disposition_cannot_launder_reading_or_scientific_content(mutation):
    record = design_record()
    sources = add_unavailable_si(record)
    mutation(record.design_scope.source_index[-1])
    assert "DESIGN_SOURCE_LEVEL_LIMITATION_REQUIRED" in codes(record)
    assert "DESIGN_SOURCE_LEVEL_LIMITATION_REQUIRED" in {
        issue.code for issue in design_source_index_issues(record, sources)}


def test_existing_registered_blocks_cannot_be_hidden_as_whole_source_unavailable():
    record = design_record()
    sources = add_unavailable_si(record)
    sources[-1]["blocks"] = [{"block_id": "si-block", "source_id": "si-source", "location": {"paragraph_index": 1}}]
    assert not codes(record)  # Without documents, no false claim that block existence was checked.
    assert "DESIGN_INDEX_BLOCK_REQUIRED" in {
        issue.code for issue in design_source_index_issues(record, sources)}
    entry = record.design_scope.source_index[-1]
    entry.block_id = "si-block"
    entry.locator.paragraph_index = 1
    assert not design_source_index_issues(record, sources)


def test_blockless_availability_still_requires_an_actual_registered_source():
    record = design_record()
    add_unavailable_si(record)
    assert "DESIGN_INDEX_BLOCK_MISSING" in {
        issue.code for issue in design_source_index_issues(record, documents())}
