"""Offline tests for lossless scoped reads and immutable machine-feedback archives."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_record_patch import patch_case
from watertreatment.storage import digest_json, hash_file, write_new_json


def data(response):
    assert response["success"], response
    return json.loads(response["contentItems"][0]["text"])


def bind(case):
    return case.tools.bind_candidate(case.record, case.original_path)


def test_identical_queries_have_explicit_receipts_with_unrestricted_refresh(patch_case):
    bound = bind(patch_case)
    args = {"section": "observations", "fields": ["value", "unit", "time_window"], "limit": 100}
    first = data(patch_case.tools.handle("wt_query_candidate", args))
    repeated = data(patch_case.tools.handle("wt_query_candidate", args))
    assert repeated["mode"] == "cached_reference" and "items" not in repeated
    assert repeated["candidate_binding"] == first["candidate_binding"] == bound
    assert repeated["prior_tool_call_number"] == first["tool_call_number"]
    assert repeated["returned_count"] == 0 and repeated["original_returned_count"] == 2
    assert not patch_case.tools.has_candidate_read(bound)
    refreshed = data(patch_case.tools.handle("wt_query_candidate", {**args, "refresh": True}))
    assert refreshed["mode"] == "fields" and refreshed["items"] == first["items"]
    assert refreshed["candidate_binding"] == bound
    patch_case.tools.begin_query_turn()
    new_turn = data(patch_case.tools.handle("wt_query_candidate", args))
    assert new_turn["mode"] == "fields" and new_turn["items"] == first["items"]
    assert hash_file(patch_case.original_path) == patch_case.original_hash


def test_receipts_never_bypass_snapshot_verification_or_new_binding(patch_case):
    bound = bind(patch_case)
    args = {"section": "paper"}
    first = data(patch_case.tools.handle("wt_query_candidate", args))
    rebound = patch_case.tools.bind_candidate(patch_case.record, patch_case.original_path)
    assert rebound != bound
    again = data(patch_case.tools.handle("wt_query_candidate", args))
    assert again["mode"] == "section" and again["items"] == first["items"]
    Path(rebound["path"]).write_text("{}", encoding="utf-8")
    denied = patch_case.tools.handle("wt_query_candidate", args)
    assert not denied["success"] and "changed after binding" in denied["contentItems"][0]["text"]


def test_default_path_returns_typed_value_without_rewriting_old_json(patch_case):
    bound = bind(patch_case)
    before = Path(bound["path"]).read_bytes()
    assert "digitization_targets" not in json.loads(before)
    result = data(patch_case.tools.handle("wt_query_candidate", {"path": ["digitization_targets"]}))
    assert result["value"] == [] and result["candidate_binding"] == bound
    assert result["serialized_default_paths"] == [["digitization_targets"]]
    assert result["value_origin"] == "schema_default_omitted_by_compatibility_serialization"
    assert not patch_case.tools.has_candidate_read(bound)
    coordinate = data(patch_case.tools.handle("wt_query_candidate", {"path": ["observations", 0, "coordinates"]}))
    assert coordinate["value"] == []
    assert coordinate["serialized_default_paths"] == [["observations", 0, "coordinates"]]
    assert Path(bound["path"]).read_bytes() == before
    assert not patch_case.tools.handle("wt_query_candidate", {"path": ["not_in_schema"]})["success"]
    assert not patch_case.tools.handle("wt_query_candidate", {"path": ["model_fields"]})["success"]
    assert not patch_case.tools.handle("wt_query_candidate", {"path": ["digitization_targets", 0]})["success"]


def test_schema_repeat_is_per_turn_and_refresh_keeps_full_schema(patch_case):
    first = data(patch_case.tools.handle("wt_record_schema", {}))
    second = data(patch_case.tools.handle("wt_record_schema", {}))
    assert second["mode"] == "cached_schema_reference" and "schema" not in second
    assert second["schema_sha256"] == digest_json(first["schema"])
    fresh = data(patch_case.tools.handle("wt_record_schema", {"refresh": True}))
    assert fresh["schema"] == first["schema"]
    patch_case.tools.begin_query_turn()
    assert data(patch_case.tools.handle("wt_record_schema", {}))["schema"] == first["schema"]


def test_warning_feedback_keeps_all_errors_and_archives_every_full_warning(patch_case):
    warnings = [{"severity": "warning", "code": "SYNTHETIC_PARTIAL", "path": f"observations[{i}].value",
                 "message": f"Marker {i} is a partial series endpoint. " + "保留完整原文限制。" * 50} for i in range(237)]
    error = {"severity": "error", "code": "SYNTHETIC_BINDING", "path": "observations[0].experiment_id",
             "message": "Unresolved binding must be fixed; grouping must never hide this error."}
    issues = [error, *warnings]
    feedback = patch_case.tools.validation_feedback(issues, candidate=patch_case.record)
    assert feedback["errors"] == [error] and feedback["all_errors_inlined"]
    assert feedback["issue_counts"] == {"error": 1, "warning": 237}
    group = feedback["warning_groups"][0]
    assert group["count"] == 237 and len(group["examples"]) == 2
    assert group["affected_paths"] == [x["path"] for x in warnings]
    assert len(json.dumps(feedback)) < len(json.dumps(issues)) / 3
    archive = feedback["validation_artifact"]
    assert archive["candidate_content_hash"] == digest_json(patch_case.record.model_dump(mode="json"))
    assert hash_file(Path(archive["path"])) == archive["sha256"]
    fetched, offset = [], 0
    while True:
        page = data(patch_case.tools.handle("wt_validation_issues", {
            "validation_id": archive["validation_id"], "offset": offset, "limit": 100}))
        assert page["validation_artifact"] == archive
        fetched += page["items"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert fetched == issues
    selected = data(patch_case.tools.handle("wt_validation_issues", {
        "validation_id": archive["validation_id"], "severity": "error", "codes": ["SYNTHETIC_BINDING"]}))
    assert selected["items"] == [error] and selected["source_indices"] == [0]
    assert patch_case.tools.validation_feedback(issues, candidate=patch_case.record) == feedback
    assert hash_file(patch_case.original_path) == patch_case.original_hash


def test_validation_retrieval_rejects_foreign_or_modified_artifacts(patch_case):
    feedback = patch_case.tools.validation_feedback([], candidate=patch_case.record)
    artifact = feedback["validation_artifact"]
    assert not patch_case.tools.handle("wt_validation_issues", {"validation_id": "a" * 64})["success"]
    assert not patch_case.tools.handle("wt_validation_issues", {"validation_id": "../credentials"})["success"]
    Path(artifact["path"]).write_text("{}", encoding="utf-8")
    denied = patch_case.tools.handle("wt_validation_issues", {"validation_id": artifact["validation_id"]})
    assert not denied["success"] and "artifact changed" in denied["contentItems"][0]["text"]


def test_validation_feedback_reuse_cannot_rebless_modified_artifact_bytes(patch_case):
    feedback = patch_case.tools.validation_feedback([], candidate=patch_case.record)
    path = Path(feedback["validation_artifact"]["path"])
    same_content = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(same_content, separators=(",", ":")), encoding="utf-8")
    with pytest.raises(ValueError, match="artifact changed"):
        patch_case.tools.validation_feedback([], candidate=patch_case.record)


def test_repeated_original_source_reads_can_be_explicitly_refreshed(patch_case):
    args = {"source_id": "main", "start_order": 1, "end_order": 1}
    first = data(patch_case.tools.handle("wt_read_blocks", args))
    repeated = data(patch_case.tools.handle("wt_read_blocks", args))
    assert repeated["mode"] == "cached_source_reference" and "blocks" not in repeated
    assert repeated["tool_call_number"] == first["tool_call_number"] + 1
    refreshed = data(patch_case.tools.handle("wt_read_blocks", {**args, "refresh": True}))
    assert first["blocks"] == refreshed["blocks"]
