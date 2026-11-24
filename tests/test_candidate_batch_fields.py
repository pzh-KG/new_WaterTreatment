"""Offline checks for lossless, version-bound field batches and exact filters."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from test_record_patch import patch_case
from watertreatment.codex import strict_schema
from watertreatment.reading_tools import CandidateQueryArgs, ReadingTools
from watertreatment.storage import digest_json, hash_file, write_new_json


def payload(response):
    assert response["success"], response
    return json.loads(response["contentItems"][0]["text"])


@pytest.fixture
def batch_case(patch_case):
    record = patch_case.record.model_copy(deep=True)
    other = record.experiments[0].model_copy(deep=True)
    other.id = "other-experiment"
    record.experiments.append(other)
    record.observations = []
    for index in range(107):
        point = patch_case.record.observations[0].model_copy(deep=True)
        point.id = f"p-{index:03d}"
        point.experiment_id = "joint" if index % 2 == 0 else "other-experiment"
        point.series_id = "s-joint" if index % 2 == 0 else "s-other"
        point.value.value = -index / 10
        point.value.status = "estimated"
        point.value.raw = f"Signed source marker {index}; " + "原文注释" * 20
        point.value.note = "Estimate; not author raw data."
        point.uncertainty.value = 0.1
        point.uncertainty.status = "estimated"
        point.uncertainty.raw = "Vertical digitization tolerance, not an experimental error bar."
        point.reference.value = "Source plotted baseline"
        point.reference.status = "reported"
        point.reference.raw = "Independent baseline wording " * 50
        record.observations.append(point)
    path = patch_case.tools.run / "batch-candidate.json"
    write_new_json(path, record.model_dump(mode="json"))
    patch_case.binding = patch_case.tools.bind_candidate(record, path)
    patch_case.candidate = record
    patch_case.snapshot_bytes = Path(patch_case.binding["path"]).read_bytes()
    return patch_case


def test_projected_batches_return_complete_facts_and_keep_full_record_intact(batch_case):
    fields = ["value", "unit", "uncertainty", "coordinates", "evidence_links"]
    first = payload(batch_case.tools.handle("wt_query_candidate", {
        "section": "observations", "fields": fields, "limit": 100}))
    second = payload(batch_case.tools.handle("wt_query_candidate", {
        "section": "observations", "fields": fields, "limit": 100, "offset": first["next_offset"]}))
    original = batch_case.candidate.model_dump(mode="json")["observations"]
    assert first["mode"] == "fields" and first["complete_entities"] is False
    assert first["omitted_fields_not_missing"] is True and first["field_selection"] == fields
    assert first["returned_count"] == 100 and second["returned_count"] == 7
    assert first["next_offset"] == 100 and second["next_offset"] is None
    assert first["total"] == second["total"] == len(original)
    assert first["source_indices"] + second["source_indices"] == list(range(len(original)))
    assert first["candidate_binding"] == second["candidate_binding"] == batch_case.binding
    for expected, actual in zip(original, first["items"] + second["items"], strict=True):
        assert set(actual) == {"id", "experiment_id", "material_id", "series_id", "value", "unit", "uncertainty"}
        for field in actual:
            assert actual[field] == expected[field]
        assert actual["value"]["raw"].endswith("原文注释" * 20)
        assert actual["uncertainty"]["raw"] == expected["uncertainty"]["raw"]
        assert "reference" not in actual  # Omitted transport field, still present in snapshot.
    assert Path(batch_case.binding["path"]).read_bytes() == batch_case.snapshot_bytes
    assert hash_file(Path(batch_case.binding["path"])) == batch_case.binding["sha256"]
    assert digest_json(batch_case.tools.candidate_for_prompt().model_dump(mode="json")) == batch_case.binding["content_hash"]
    assert not batch_case.tools.has_candidate_read(batch_case.binding)


def test_exact_filters_intersect_and_indices_remain_global(batch_case):
    fields = ["metric_kind", "value"]
    request = {"section": "observations", "fields": fields, "limit": 20,
               "experiment_ids": ["other-experiment"], "series_ids": ["s-other"]}
    result = payload(batch_case.tools.handle("wt_query_candidate", request))
    assert result["source_indices"] == list(range(1, 40, 2))
    assert result["total"] == 53 and result["next_offset"] == 20
    next_page = payload(batch_case.tools.handle("wt_query_candidate", {**request, "offset": 20}))
    assert next_page["source_indices"] == list(range(41, 80, 2))
    assert next_page["offset"] == 20
    assert all(item["experiment_id"] == "other-experiment" for item in next_page["items"])
    empty = payload(batch_case.tools.handle("wt_query_candidate", {**request, "series_ids": ["s-joint"]}))
    assert empty["returned_count"] == empty["total"] == 0 and empty["next_offset"] is None
    selected = payload(batch_case.tools.handle("wt_query_candidate", {
        **request, "ids": ["p-001", "p-003", "p-005"], "query": "marker 3;"}))
    assert selected["source_indices"] == [3] and selected["items"][0]["id"] == "p-003"
    wrong_case = payload(batch_case.tools.handle("wt_query_candidate", {**request, "series_ids": ["S-OTHER"]}))
    assert wrong_case["returned_count"] == 0  # IDs are exact; scientific identities are not normalized.


def test_filtered_full_entities_keep_old_limit_and_complete_read_proof(batch_case):
    response = payload(batch_case.tools.handle("wt_query_candidate", {
        "section": "observations", "experiment_ids": ["other-experiment"], "limit": 20}))
    original = batch_case.candidate.model_dump(mode="json")["observations"]
    assert response["mode"] == "section" and "field_selection" not in response
    assert response["items"] == original[1:40:2]
    assert batch_case.tools.has_candidate_read(batch_case.binding)
    assert not batch_case.tools.handle("wt_query_candidate", {
        "section": "observations", "experiment_ids": ["joint"], "limit": 21})["success"]


def test_legacy_serialized_defaults_are_explicit_not_invented_null_or_missing_source(batch_case):
    result = payload(batch_case.tools.handle("wt_query_candidate", {
        "section": "observations", "ids": ["p-000"], "fields": ["coordinates", "value"]}))
    assert "coordinates" not in result["items"][0]
    assert result["serialized_default_omissions"] == [{"source_index": 0, "fields": ["coordinates"]}]
    assert result["items"][0]["value"]["status"] == "estimated"
    assert "not author-not-reported" in result["field_notice"]


@pytest.mark.parametrize("arguments", [
    {"section": "observations", "fields": ["value.value"]},
    {"section": "observations", "fields": ["value", "value"]},
    {"section": "observations", "fields": [" value"]},
    {"section": "observations", "fields": [""]},
    {"section": "observations", "fields": ["value"], "limit": 101},
    {"section": "observations", "fields": ["value"], "limit": True},
    {"section": "observations", "fields": ["value"], "offset": 1.0},
    {"section": "observations", "fields": ["value"], "series_ids": ["s-joint", "s-joint"]},
    {"section": "observations", "experiment_ids": [""]},
    {"section": "summary", "fields": ["paper_id"]},
    {"section": "summary", "experiment_ids": ["joint"]},
    {"section": "experiments", "series_ids": ["s-joint"]},
    {"section": "evidence", "experiment_ids": ["joint"]},
    {"path": ["paper"], "fields": ["title"]},
    {"path": ["paper"], "series_ids": ["s-joint"]},
    {"path": ["paper"], "experiment_ids": ["joint"]},
    {"section": "extensions", "fields": ["credentials"]},  # Unknown fields rejected even when there are no rows.
])
def test_projection_cannot_change_scope_or_hide_invalid_field_names(batch_case, arguments):
    assert not batch_case.tools.handle("wt_query_candidate", arguments)["success"]
    assert not batch_case.tools.has_candidate_read(batch_case.binding)
    assert Path(batch_case.binding["path"]).read_bytes() == batch_case.snapshot_bytes


def test_other_entity_types_support_whole_fields_without_observation_filtering(batch_case):
    result = payload(batch_case.tools.handle("wt_query_candidate", {
        "section": "experiments", "fields": ["conditions", "pairs", "time_window"], "limit": 100}))
    original = batch_case.candidate.model_dump(mode="json")["experiments"]
    assert result["source_indices"] == [0, 1]
    assert result["items"] == [{name: row[name] for name in ["id", "conditions", "pairs", "time_window"]} for row in original]
    paper = payload(batch_case.tools.handle("wt_query_candidate", {
        "section": "paper", "fields": ["title"], "limit": 100}))
    assert paper["items"] == [{"id": batch_case.candidate.paper.id,
                               "title": batch_case.candidate.paper.title.model_dump(mode="json")}]


def test_legacy_query_shape_and_strict_tool_schema_are_compatible(batch_case):
    args = {"section": "observations", "ids": [], "query": "", "offset": 0, "limit": 10, "path": None}
    parsed = CandidateQueryArgs.model_validate(args)
    assert parsed.fields == parsed.series_ids == parsed.experiment_ids == []
    result = payload(batch_case.tools.handle("wt_query_candidate", args))
    assert not any(key in result for key in ("field_selection", "source_indices", "complete_entities", "filters"))
    assert result["items"] == batch_case.candidate.model_dump(mode="json")["observations"][:10]
    spec = next(s for s in ReadingTools.specifications() if s["name"] == "wt_query_candidate")
    schema = strict_schema(CandidateQueryArgs.model_json_schema())
    assert spec["inputSchema"] == schema
    assert set(schema["properties"]) == set(schema["required"])
    assert schema["additionalProperties"] is False


def test_projected_batch_still_verifies_snapshot_and_consumes_one_query_attempt(batch_case):
    tools = batch_case.tools
    first = payload(tools.handle("wt_query_candidate", {"section": "observations", "fields": ["value"], "limit": 100}))
    assert tools.calls_started == tools.candidate_calls_started == 1
    assert first["tool_call_number"] == 1 and first["returned_count"] == 100
    assert not tools.has_candidate_read(batch_case.binding)
    snapshot = Path(batch_case.binding["path"])
    modified = copy.deepcopy(batch_case.candidate.model_dump(mode="json"))
    modified["observations"][0]["value"]["value"] = 999
    snapshot.write_text(json.dumps(modified), encoding="utf-8")
    response = tools.handle("wt_query_candidate", {"section": "observations", "fields": ["value"], "limit": 100})
    assert not response["success"] and "changed after binding" in response["contentItems"][0]["text"]
    assert tools.calls_started == tools.candidate_calls_started == 2
