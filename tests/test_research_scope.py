"""Synthetic research-use boundaries; no provider, graph server or paper answers."""
import json

import pytest

from watertreatment.domain import (
    DesignComparison, EvidenceRef, Experiment, ExtractionRecord, Fact, MaterialSample,
    NamedFact, Observation, ObservationEvidenceLink, ObservationPair, Paper,
    QuantitativeCoverage, ExtensionModule,
)
from watertreatment.graph_store import _rows
from watertreatment.projection import project_record
from watertreatment.research_scope import archived_dense_ids, scope_issues, scoped_projection
from watertreatment.storage import digest_json


def fact(value, status="reported"):
    return Fact(value=value, status=status, evidence_ids=["e"])


def record(scope="research_core"):
    def point(identifier, minute, value, role):
        return Observation(id=identifier, experiment_id="exp", material_id="m",
            metric_kind="concentration_ratio", metric=fact("C/C0"), value=fact(value, "estimated"),
            unit=fact("1"), origin="plot_estimate", series_id="curve", analysis_role=role,
            role_reason="Optional interior marker; no reported transition or comparison depends on it." if role == "dense_series_point" else None,
            coordinates=[NamedFact(name="elapsed_time", fact=fact(minute), unit=fact("min"))],
            evidence_links=[ObservationEvidenceLink(evidence_id="e", supported_fields=["value"],
                explanation="Marker in the named curve.")])
    return ExtractionRecord(schema_version="0.3.0", rule_version="0.7", extraction_scope=scope,
        digitization_targets=["curve"] if scope == "series_digitization" else [],
        paper=Paper(id="synthetic", source_ids=["source"]),
        materials=[MaterialSample(id="m", role="primary_final")],
        experiments=[Experiment(id="exp", material_ids=["m"], purpose="catalysis",
            conditions=[NamedFact(name="pH", fact=fact(6))])],
        observations=[point("end", 60, 0.2, "key_result"), point("inner", 20, 0.7, "dense_series_point")],
        evidence=[EvidenceRef(id="e", source_id="source", block_id="b")],
        quantitative_coverage=[QuantitativeCoverage(id="curve", source_id="source", block_id="b",
            label="Synthetic concentration course", data_kind="time_series", state="partial",
            observation_ids=["end", "inner"], reason="Only identified endpoint and one interior point retained.",
            limitations=["Complete author sampling count unknown; no interpolation."])])


def codes(value):
    return {issue.code for issue in scope_issues(value)}


@pytest.mark.parametrize("rule,schema", [("0.5", "0.1.0"), ("0.6", "0.2.0")])
def test_legacy_defaults_do_not_change_record_fingerprints(rule, schema):
    value = ExtractionRecord(schema_version=schema, rule_version=rule, paper=Paper(id="legacy"),
        observations=[Observation(id="o", experiment_id="x")])
    old = value.model_dump(mode="json")
    assert "extraction_scope" not in old and "digitization_targets" not in old
    assert "analysis_role" not in old["observations"][0] and "role_reason" not in old["observations"][0]
    assert ExtractionRecord.model_validate(old).model_dump(mode="json") == old
    assert not codes(value) and not scoped_projection(value)
    assert digest_json(ExtractionRecord.model_validate(old).model_dump(mode="json")) == digest_json(old)


def test_scope_and_roles_are_round_tripped_and_not_discarded():
    value = record()
    assert ExtractionRecord.model_validate_json(value.model_dump_json()) == value
    assert not codes(value)
    assert archived_dense_ids(value) == {"inner"}
    assert value.observations[1].importance == "core"  # Old importance is not a selection rule.


@pytest.mark.parametrize("scope", ["research_core", "series_digitization"])
def test_scoped_projection_preserves_archive_and_keeps_endpoint(scope):
    value = record(scope)
    before = value.model_dump_json()
    graph = project_record(value)
    observations = [node for node in graph.nodes if node.label == "Observation"]
    assert len(observations) == 1 and observations[0].properties["value"] == 0.2
    series = next(node for node in graph.nodes if node.label == "ObservationSeries")
    assert series.properties["member_observation_ids"] == ["end", "inner"]
    assert series.properties["coverage_state"] == "partial"
    assert series.properties["archived_dense_observation_count"] == 1
    assert series.properties["projected_observation_count"] == 1
    assert series.properties["record_fingerprint"] == graph.record_fingerprint
    assert series.properties["record_pointer"] == "/quantitative_coverage/0"
    assert series.properties["scientific_validation"] == "not_established"
    assert {edge.relationship for edge in graph.edges} >= {"HAS_SERIES", "IN_SERIES"}
    assert value.model_dump_json() == before
    nodes, edges = _rows(graph, "snapshot-fixture")  # Loader whitelist only; never connects.
    assert "ObservationSeries" in nodes and "HAS_SERIES" in edges and "IN_SERIES" in edges


def test_legacy_graph_keeps_points_and_does_not_gain_series_selection():
    value = record()
    value.rule_version, value.schema_version, value.extraction_scope = "0.6", "0.2.0", None
    for observation in value.observations:
        observation.analysis_role, observation.role_reason = "unclassified", None
    graph = project_record(value)
    assert len([node for node in graph.nodes if node.label == "Observation"]) == 2
    assert not any(node.label == "ObservationSeries" for node in graph.nodes)


def test_graph_keeps_uncertainty_time_and_conditions_with_all_statuses():
    value = record()
    observation = value.observations[0]
    observation.uncertainty = Fact(value=0.03, status="estimated", evidence_ids=["e"], note="Visual tolerance, not replicate SD.")
    observation.time_window.reference = Fact(raw="After adsorption", status="inferred", evidence_ids=["e"])
    graph = project_record(value)
    props = next(node.properties for node in graph.nodes if node.label == "Observation")
    assert props["source_observation_id"] == "end" and props["record_pointer"] == "/observations/0"
    assert json.loads(props["uncertainty_json"]) == observation.uncertainty.model_dump(mode="json")
    assert json.loads(props["time_window_json"]) == observation.time_window.model_dump(mode="json")
    experiment = next(node.properties for node in graph.nodes if node.label == "Experiment")
    assert experiment["source_experiment_id"] == "exp" and experiment["record_pointer"] == "/experiments/0"
    assert json.loads(experiment["conditions_json"]) == [x.model_dump(mode="json") for x in value.experiments[0].conditions]
    assert props["source_value_status"] == "estimated" and not props["author_stated"]


def test_scoped_projection_retains_configuration_specific_method_and_sampling_stream():
    from watertreatment.domain import Configuration, MaterialPlacement, Stream

    value = record()
    method = Configuration(
        id="dark-cell", material_ids=["m"], evidence_ids=["e"],
        parameters=[NamedFact(name="relative humidity", fact=fact(39), unit=fact("%"))],
        placements=[MaterialPlacement(material_id="m", position=fact("inside the chamber"))],
        streams=[Stream(id="feed", kind="feed", description=fact("initial water")),
                 Stream(id="out", kind="condensate", upstream_stream_ids=["feed"])],
    )
    value.configurations = [Configuration(id="illuminated-open"), method]
    value.experiments[0].configuration_id = method.id
    graph = project_record(value, allow_invalid=True)
    props = {n.properties["source_configuration_id"]: n.properties for n in graph.nodes if n.label == "Configuration"}
    assert props[method.id]["record_pointer"] == "/configurations/1"
    for name in ("parameters", "placements", "streams"):
        assert json.loads(props[method.id][name + "_json"]) == [x.model_dump(mode="json") for x in getattr(method, name)]
        assert json.loads(props["illuminated-open"][name + "_json"]) == []
    assert len([e for e in graph.edges if e.relationship == "USES_CONFIGURATION" and method.id in e.target]) == 1


@pytest.mark.parametrize("missing", ["scope", "role", "schema", "reason", "series", "membership"])
def test_invalid_new_scope_metadata_is_detected(missing):
    value = record()
    expected = {"scope": "EXTRACTION_SCOPE_REQUIRED", "role": "OBSERVATION_ROLE_REQUIRED",
        "schema": "SCOPE_VERSION_MISMATCH", "reason": "DENSE_ROLE_REASON_REQUIRED",
        "series": "DENSE_SERIES_REQUIRED", "membership": "DENSE_SERIES_REQUIRED"}[missing]
    if missing == "scope": value.extraction_scope = None
    if missing == "role": value.observations[0].analysis_role = "unclassified"
    if missing == "schema": value.schema_version = "0.2.0"
    if missing == "reason": value.observations[1].role_reason = "  "
    if missing == "series": value.quantitative_coverage[0].data_kind = "cycle_series"
    if missing == "membership": value.quantitative_coverage[0].observation_ids = ["end"]
    assert expected in codes(value)


@pytest.mark.parametrize("relation", ["comparison", "pair", "extension"])
def test_dense_role_cannot_hide_key_relations_even_in_diagnostic_projection(relation):
    value = record()
    if relation == "comparison":
        value.comparisons = [DesignComparison(id="comparison", material_ids=["m"], observation_ids=["inner"])]
    elif relation == "pair":
        value.experiments[0].pairs = [ObservationPair(left_observation_id="inner", right_observation_id="end")]
    else:
        value.extensions = [ExtensionModule(module="SyntheticInterpretation", version="1", observation_id="inner")]
    assert "DENSE_KEY_RELATION" in codes(value)
    assert archived_dense_ids(value) == set()
    diagnostic = project_record(value, allow_invalid=True)
    assert len([node for node in diagnostic.nodes if node.label == "Observation"]) == 2


@pytest.mark.parametrize("role", ["key_result", "condition_comparison", "stability", "design_control", "mechanistic_support"])
def test_all_key_roles_retained_in_time_series_without_material_role_or_count_filter(role):
    value = record()
    value.materials[0].role = "necessary_control"
    value.observations[1].analysis_role, value.observations[1].role_reason = role, None
    assert not codes(value)
    graph = project_record(value)
    assert len([node for node in graph.nodes if node.label == "Observation"]) == 2


@pytest.mark.parametrize("targets,expected", [([], "DIGITIZATION_TARGETS_REQUIRED"),
    (["missing"], "INVALID_DIGITIZATION_TARGET"), (["curve", "curve"], "DUPLICATE_DIGITIZATION_TARGET")])
def test_digitization_requires_named_existing_unique_time_series(targets, expected):
    value = record("series_digitization")
    value.digitization_targets = targets
    assert expected in codes(value)


@pytest.mark.parametrize("field,disposition", [("scope", "external_benchmark"), ("state", "out_of_scope")])
def test_digitization_rejects_excluded_and_external_target_series(field, disposition):
    value = record("series_digitization")
    setattr(value.quantitative_coverage[0], field, disposition)
    assert "INVALID_DIGITIZATION_TARGET" in codes(value)
    with pytest.raises(ValueError, match="INVALID_DIGITIZATION_TARGET"):
        project_record(value)


def test_core_does_not_authorize_a_new_digitization_target_and_legacy_cannot_opt_in():
    value = record()
    value.digitization_targets = ["curve"]
    assert "CORE_DIGITIZATION_TARGETS" in codes(value)
    value.rule_version = "0.6"
    assert "SCOPE_VERSION_MISMATCH" in codes(value)
    assert not scoped_projection(value) and not archived_dense_ids(value)
