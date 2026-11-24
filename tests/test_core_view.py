"""Offline lossless research views over synthetic records; no providers or graph."""
import copy
import hashlib
import json

import pytest

from watertreatment.core_view import (
    MAPPING_VERSION, build_core_view, build_native_core_view, write_core_view_artifact,
)
from watertreatment.design_scope import CORE_AREAS, RESEARCH_USES
from watertreatment.domain import (
    Component, DesignComparison, EvidenceRef, Experiment, ExtractionRecord, Fact,
    MaterialSample, NamedFact, Observation, ObservationPair, Paper, QuantitativeCoverage,
    ResearchDesignScope, ResearchObservationAssignment, CoreResearchCheck,
    ResearchSourceIndex, ResearchEligibility,
)
from watertreatment.storage import digest_json


SHA = "a" * 64


def synthetic():
    def observation(identifier, value, role, coordinate=None):
        return Observation(id=identifier, experiment_id="experiment", material_id="material",
            metric=Fact(value="synthetic response", status="reported", evidence_ids=["evidence"]),
            value=Fact(value=value, status="estimated", evidence_ids=["evidence"], note="Visual estimate"),
            unit=Fact(value="arbitrary", status="reported", evidence_ids=["evidence"]),
            uncertainty=Fact(status="not_reported"), analysis_role=role, origin="plot_estimate",
            series_id="series", coordinates=[] if coordinate is None else
                [NamedFact(name="cycle_number", fact=Fact(value=coordinate, status="reported"))],
            evidence_ids=["evidence"], limitations=["Synthetic uncertainty limitation"])
    return ExtractionRecord(schema_version="0.3.0", rule_version="0.7", extraction_scope="research_core",
        paper=Paper(id="synthetic", source_ids=["source"]),
        materials=[MaterialSample(id="material", role="primary_final", components=[
            Component(substance_id="retained", retention="retained", amount=Fact(status="not_reported")),
            Component(substance_id="reagent", retention="preloaded_consumable", amount=Fact(value=5, status="reported")),
            Component(substance_id="uncertain", retention="unknown")])],
        experiments=[Experiment(id="experiment", material_ids=["material"], purpose="stability",
            pairs=[ObservationPair(left_observation_id="endpoint", right_observation_id="detail")])],
        observations=[observation("endpoint", 0.8, "key_result"),
            observation("cycle-1", 0.9, "stability", 1), observation("cycle-2", 0.7, "stability", 2),
            observation("detail", 0.4, "condition_comparison")],
        evidence=[EvidenceRef(id="evidence", source_id="source", block_id="block")],
        quantitative_coverage=[QuantitativeCoverage(id="series", source_id="source", block_id="block",
            label="Synthetic cycle and condition measurements", data_kind="cycle_series", state="partial",
            observation_ids=["endpoint", "cycle-1", "cycle-2", "detail"], reason="Source archive is intentionally partial")])


def mapping(record):
    plain = record.model_dump(mode="json")
    return {"version": MAPPING_VERSION, "policy_version": "0.8", "basis": "existing_candidate_review_only",
        "source_record_sha256": SHA, "source_record_fingerprint": digest_json(plain), "modules": [],
        "limitations": ["A pre-existing conflict remains unresolved"],
        "decisions": [{"observation_id": o.id, "layer": "C" if o.id == "detail" else "A",
            "purpose": "requested_detail" if o.id == "detail" else "stability" if o.analysis_role == "stability" else "performance",
            "reason": "Explicit synthetic research-scope decision", "module": None} for o in record.observations]}


def test_view_is_lossless_and_immutable_with_complete_partition():
    record = synthetic()
    before = record.model_dump(mode="json")
    scope = mapping(record)
    scope_before = copy.deepcopy(scope)
    view = build_core_view(record, scope, source_sha256=SHA)
    assert record.model_dump(mode="json") == before and scope == scope_before
    assert view["counts"]["retained_observations"] == 3 and view["counts"]["deferred_observations"] == 1
    assert view["observations"] == before["observations"][:3]
    assert "entries" not in view["series"][0]
    assert [view["observations"][int(ref["view_pointer"].split("/")[-1])]
            for ref in view["series"][0]["entry_refs"]] == before["observations"][:3]
    assert view["source_index"][0]["source_coverage"] == before["quantitative_coverage"][0]
    assert view["source_index"][0]["state"] == "partial"
    assert view["archive_references"][0]["record_pointer"] == "/observations/3"
    assert view["relation_archive_dependencies"][0]["core_pairing_eligible"] is False
    assert view["relation_archive_dependencies"][0]["archived_observation_ids"] == ["detail"]
    assert view["provenance"]["new_extraction_performed"] is False
    assert view["provenance"]["human_validated"] is False
    assert view["provenance"]["source_rule_version"] == "0.7"
    assert view["limitations"] == scope["limitations"]
    # Mutating a returned view never mutates the research archive or the mapping.
    view["observations"][0]["value"]["value"] = 999
    assert record.model_dump(mode="json") == before


def test_missing_mapping_is_conservative_and_explicitly_unassessed():
    record = synthetic()
    view = build_core_view(record, source_sha256=SHA)
    assert view["observations"] == record.model_dump(mode="json")["observations"]
    assert view["counts"]["unclassified_observations"] == 4
    assert view["provenance"]["scope_selection_assessed"] is False


@pytest.mark.parametrize("mutation", ["omit", "duplicate", "extra", "fingerprint", "bytes", "layer", "blank", "module", "protected"])
def test_incomplete_unbound_or_unsafe_mappings_fail_closed(mutation):
    record = synthetic()
    scope = mapping(record)
    if mutation == "omit": scope["decisions"].pop()
    if mutation == "duplicate": scope["decisions"].append(copy.deepcopy(scope["decisions"][0]))
    if mutation == "extra": scope["decisions"][-1]["observation_id"] = "unknown"
    if mutation == "fingerprint": scope["source_record_fingerprint"] = "b" * 64
    if mutation == "bytes": scope["source_record_sha256"] = "b" * 64
    if mutation == "layer": scope["decisions"][-1]["layer"] = "unknown"
    if mutation == "blank": scope["decisions"][-1]["reason"] = "  "
    if mutation == "module": scope["decisions"][-1].update(layer="B", module="not-activated")
    if mutation == "protected": scope["decisions"][1].update(layer="C", purpose="requested_detail")
    with pytest.raises(ValueError):
        build_core_view(record, scope, source_sha256=SHA)


def test_activated_module_retains_source_evidence_without_reclassifying_experiment():
    record = synthetic()
    scope = mapping(record)
    scope["modules"] = [{"id": "module", "kind": "outdoor_application", "reason": "The source reports an outdoor trial",
                         "evidence_ids": ["evidence"]}]
    scope["decisions"][-1].update(layer="B", purpose="application_summary", module="module")
    view = build_core_view(record, scope, source_sha256=SHA)
    assert view["counts"]["module_observations"] == 1
    assert view["source_context"]["experiments"][0]["purpose"] == "stability"
    assert not view["relation_archive_dependencies"]


def test_composition_features_do_not_include_consumables_or_fill_unknown_with_zero():
    view = build_core_view(synthetic(), source_sha256=SHA)
    row = view["analysis_tables"]["materials"][0]
    assert [x["substance_id"] for x in row["component_features"]] == ["retained"]
    assert [x["substance_id"] for x in row["preloaded_consumables"]] == ["reagent"]
    assert [x["substance_id"] for x in row["unresolved_components"]] == ["uncertain"]
    assert row["component_features"][0]["amount"]["value"] is None
    assert not row["performance_is_feature"] and not row["missing_is_zero"] and not row["composition_normalized"]
    assert len(view["analysis_tables"]["experiments"]) == 4  # No implicit condition pooling.


def test_explicit_condition_groups_preserve_native_coordinates_and_do_not_create_pairs():
    record = synthetic()
    scope = mapping(record)
    for entry in scope["decisions"]:
        if entry["observation_id"].startswith("cycle"):
            entry["condition_group"] = "observed-cycles"
    view = build_core_view(record, scope, source_sha256=SHA)
    row = next(x for x in view["analysis_tables"]["experiments"] if len(x["observation_ids"]) == 2)
    assert "native_coordinates" not in row
    assert [view["observations"][int(x["view_pointer"].split("/")[-1])]["coordinates"][0]["fact"]["value"]
            for x in row["observation_refs"]] == [1, 2]
    assert not row["new_pairing_inferred"] and not row["grouping_is_comparability_proof"]


def test_native_projection_uses_actual_extraction_assignments_and_index_only_detail():
    record = synthetic()
    record.rule_version, record.schema_version = "0.8", "0.4.0"
    record.experiments[0].pairs = []
    record.design_scope = ResearchDesignScope(
        core_checks=[CoreResearchCheck(area=area, state="checked", reason="Synthetic source checked") for area in CORE_AREAS],
        observation_assignments=[ResearchObservationAssignment(observation_id=o.id, layer="core", purpose="stability",
            reason="Retained synthetic endpoints") for o in record.observations],
        source_index=[ResearchSourceIndex(id="index", source_id="source", block_id="block", content_types=["figure"],
            reading_state="read", summary="Synthetic series with optional dense interior points", material_relation="Final sample",
            retained_observation_ids=[o.id for o in record.observations], quantitative_coverage_ids=["series"],
            deferred_topics=["dense_time_series"], reason="Endpoint and cycles retained; ordinary interior points indexed")],
        eligibility=[ResearchEligibility(use=use, target_kind="paper", target_id="synthetic", status="not_assessed",
            reason="Synthetic projection does not grant scientific eligibility") for use in RESEARCH_USES])
    view = build_native_core_view(record, source_sha256=SHA)
    assert view["provenance"]["kind"] == "projection_of_extracted_scope"
    assert view["counts"]["deferred_observations"] == 0 and view["counts"]["deferred_topics"] == 1
    assert view["source_index"][0]["deferred_topics"] == ["dense_time_series"]
    native_mapping = {"version": MAPPING_VERSION, "policy_version": "0.8", "basis": "native_extraction_scope",
        "source_record_sha256": SHA, "source_record_fingerprint": digest_json(record.model_dump(mode="json")),
        "modules": view["modules"], "limitations": view["limitations"], "decisions": view["decisions"]}
    assert build_core_view(record, native_mapping, source_sha256=SHA) == view
    native_mapping["decisions"] = copy.deepcopy(view["decisions"])
    native_mapping["decisions"][0]["purpose"] = "mechanism_claim"
    with pytest.raises(ValueError, match="exactly match"):
        build_core_view(record, native_mapping, source_sha256=SHA)
    record.design_scope.observation_assignments.pop()
    with pytest.raises(ValueError, match="structural"):
        build_native_core_view(record, source_sha256=SHA)


def test_external_legacy_mapping_cannot_claim_native_scope_provenance():
    record = synthetic()
    scope = mapping(record)
    scope["basis"] = "native_extraction_scope"
    with pytest.raises(ValueError, match="requires a rule 0.8"):
        build_core_view(record, scope, source_sha256=SHA)


def test_native_requested_detail_is_indexed_as_archival_detail_not_core():
    record = synthetic()
    record.rule_version, record.schema_version = "0.8", "0.4.0"
    record.extraction_scope, record.digitization_targets = "series_digitization", ["series"]
    record.experiments[0].pairs = []
    record.quantitative_coverage[0].data_kind = "time_series"
    record.observations[-1].analysis_role = "dense_series_point"
    record.observations[-1].role_reason = "Explicitly requested optional interior point"
    record.design_scope = ResearchDesignScope(
        core_checks=[CoreResearchCheck(area=area, state="checked", reason="Synthetic source checked") for area in CORE_AREAS],
        observation_assignments=[ResearchObservationAssignment(observation_id=o.id,
            layer="detail" if o.id == "detail" else "core", purpose="requested_detail" if o.id == "detail" else "stability",
            reason="Requested digitization keeps detail separate from endpoints") for o in record.observations],
        source_index=[ResearchSourceIndex(id="index", source_id="source", block_id="block", content_types=["figure"],
            reading_state="read", summary="Synthetic source with requested interior detail", material_relation="Final sample",
            # C is discoverable through the coverage membership even when it is
            # absent from the source index's retained core result list.
            retained_observation_ids=["endpoint", "cycle-1", "cycle-2"], quantitative_coverage_ids=["series"],
            deferred_topics=["dense_time_series"], reason="Core results and requested archival detail have different purposes")],
        eligibility=[ResearchEligibility(use=use, target_kind="paper", target_id="synthetic", status="not_assessed",
            reason="No scientific acceptance from projection") for use in RESEARCH_USES])
    view = build_native_core_view(record, source_sha256=SHA)
    assert view["source_index"][0]["deferred_observation_ids"] == ["detail"]
    assert view["source_index"][0]["retained_observation_ids"] == ["endpoint", "cycle-1", "cycle-2"]
    assert view["research_source_index"] == record.design_scope.model_dump(mode="json")["source_index"]


def test_legacy_view_layout_is_reproducible_and_versioned_separately():
    record = synthetic()
    scope = mapping(record)
    old = build_core_view(record, scope, source_sha256=SHA, view_version="research-core-view-v1")
    new = build_core_view(record, scope, source_sha256=SHA)
    assert old["version"] == "research-core-view-v1" and new["version"] == "research-core-view-v2"
    assert old["view_id"] != new["view_id"]
    assert old["observations"] == new["observations"] and old["counts"] == new["counts"]
    assert old["series"][0]["entries"] == new["observations"]
    assert "native_coordinates" in old["analysis_tables"]["experiments"][0]
    assert len(json.dumps(new)) < len(json.dumps(old))
    with pytest.raises(ValueError, match="Unsupported"):
        build_core_view(record, scope, source_sha256=SHA, view_version="unknown")


def test_artifact_writer_keeps_source_and_rejects_overwrite(tmp_path):
    record = synthetic().model_dump(mode="json")
    source = tmp_path / "source.json"
    source.write_text(json.dumps(record), encoding="utf-8")
    source_before = source.read_bytes()
    scope = mapping(synthetic())
    scope["source_record_sha256"] = hashlib.sha256(source_before).hexdigest()
    mapping_path = tmp_path / "explicit-mapping.json"
    mapping_path.write_text(json.dumps(scope), encoding="utf-8")
    output = tmp_path / "new-version"
    manifest = write_core_view_artifact(source, mapping_path, output)
    assert source.read_bytes() == source_before
    assert hashlib.sha256((output / "core-view.json").read_bytes()).hexdigest() == manifest["view_sha256"]
    assert write_core_view_artifact(source, mapping_path, output) == manifest
    (output / "core-view.json").write_text("different immutable version", encoding="utf-8")
    with pytest.raises(ValueError, match="overwrite"):
        write_core_view_artifact(source, mapping_path, output)
    assert source.read_bytes() == source_before
