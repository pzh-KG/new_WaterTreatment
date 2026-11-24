"""Offline candidate access is bound, complete, paginated and auditable."""
import copy
import json
from pathlib import Path

import pytest

from test_record_patch import patch_case
from watertreatment.storage import digest_json, hash_file, write_new_json


def payload(result):
    assert result["success"], result
    return json.loads(result["contentItems"][0]["text"])


@pytest.fixture
def candidate_case(patch_case):
    record = patch_case.record.model_copy(deep=True)
    for index in range(2, 25):
        observation = record.observations[0].model_copy(deep=True)
        observation.id = f"point-{index:02d}"
        record.observations.append(observation)
    record.observations[0].value.raw = "Complete scientific wording: " + "原文事实与条件;" * 1000
    path = patch_case.tools.run / "candidate-input.json"
    write_new_json(path, record.model_dump(mode="json"))
    patch_case.candidate = record
    patch_case.candidate_path = path
    patch_case.binding = patch_case.tools.bind_candidate(record, path)
    return patch_case


def test_query_requires_controller_binding_and_never_accepts_an_arbitrary_file(patch_case):
    assert not patch_case.tools.handle("wt_query_candidate", {})["success"]
    assert not patch_case.tools.handle("wt_query_candidate", {"file": str(patch_case.original_path)})["success"]
    assert not patch_case.tools.handle("wt_query_candidate", {"path": str(patch_case.original_path)})["success"]
    with pytest.raises(ValueError, match="controller-bound"):
        patch_case.tools.candidate_for_prompt()


def test_binding_requires_in_run_file_and_exact_record_without_displacing_existing_binding(candidate_case, tmp_path):
    tools = candidate_case.tools
    outside = tmp_path / "outside.json"
    write_new_json(outside, candidate_case.candidate.model_dump(mode="json"))
    with pytest.raises(ValueError, match="within this run"):
        tools.bind_candidate(candidate_case.candidate, outside)
    mismatch = tools.run / "mismatch.json"
    changed = candidate_case.candidate.model_dump(mode="json")
    changed["observations"][0]["value"]["value"] = 999
    write_new_json(mismatch, changed)
    with pytest.raises(ValueError, match="exactly match"):
        tools.bind_candidate(candidate_case.candidate, mismatch)
    assert tools.candidate_binding == candidate_case.binding


def test_binding_is_an_independent_frozen_copy_with_fresh_audit_scope(candidate_case):
    tools = candidate_case.tools
    original = candidate_case.candidate.model_dump(mode="json")
    binding = candidate_case.binding
    assert set(binding) == {"path", "sha256", "content_hash", "scope"}
    snapshot = Path(binding["path"])
    assert snapshot.parent == tools.run / "candidate_snapshots"
    assert snapshot != candidate_case.candidate_path
    assert hash_file(snapshot) == binding["sha256"] and digest_json(original) == binding["content_hash"]
    candidate_case.candidate_path.write_text("{}", encoding="utf-8")
    candidate_case.candidate.observations[0].value.value = 999
    exposed = tools.candidate_for_prompt()
    exposed.observations[0].value.value = 888
    property_copy = tools.candidate_binding
    property_copy["scope"] = "not-controller-authorized"
    assert tools.candidate_for_prompt().model_dump(mode="json") == original
    assert tools.candidate_binding == binding
    tools.begin_submission(False)
    assert tools.candidate_binding == binding
    second = tools.bind_candidate(tools.candidate_for_prompt(), snapshot)
    assert second["scope"] != binding["scope"] and second["path"] != binding["path"]
    assert second["content_hash"] == binding["content_hash"]
    assert hash_file(snapshot) == binding["sha256"]


def test_candidate_sections_paginate_complete_entities_without_truncating_facts(candidate_case):
    tools = candidate_case.tools
    first = payload(tools.handle("wt_query_candidate", {"section": "observations", "limit": 20}))
    second = payload(tools.handle("wt_query_candidate", {"section": "observations", "limit": 20, "offset": first["next_offset"]}))
    expected = candidate_case.candidate.model_dump(mode="json")["observations"]
    assert first["mode"] == "section" and first["returned_count"] == 20 and first["total"] == 25
    assert second["returned_count"] == 5 and second["next_offset"] is None
    assert first["items"] + second["items"] == expected
    assert first["items"][0]["value"]["raw"] == expected[0]["value"]["raw"]
    assert first["candidate_binding"] == second["candidate_binding"] == candidate_case.binding
    logged = json.loads((tools.audit_root / "tool-0001" / "result.json").read_text(encoding="utf-8"))
    assert logged["success"] and logged["data"] == first


def test_candidate_filter_matches_ids_and_literal_text_without_hiding_other_fields(candidate_case):
    tools = candidate_case.tools
    selected = payload(tools.handle("wt_query_candidate", {
        "section": "observations", "ids": ["rate", "point-02"], "query": "COMPLETE SCIENTIFIC WORDING"}))
    assert selected["returned_count"] == selected["total"] == 1 and selected["items"][0]["id"] == "rate"
    assert selected["items"][0] == candidate_case.candidate.model_dump(mode="json")["observations"][0]
    absent = payload(tools.handle("wt_query_candidate", {"section": "observations", "query": ".*"}))
    assert absent["total"] == absent["returned_count"] == 0 and absent["next_offset"] is None
    empty = payload(tools.handle("wt_query_candidate", {"section": "observations", "offset": 500}))
    assert empty["returned_count"] == 0 and empty["next_offset"] is None


@pytest.mark.parametrize("section", ["paper", "materials", "substances", "configurations", "experiments",
    "evidence", "coverage", "quantitative_coverage", "comparisons", "extensions"])
def test_all_candidate_sections_are_accessible_including_empty_sections(candidate_case, section):
    data = payload(candidate_case.tools.handle("wt_query_candidate", {"section": section}))
    value = candidate_case.candidate.model_dump(mode="json").get(section, [])
    expected = value if isinstance(value, list) else [value]
    assert data["mode"] == "section" and data["items"] == expected
    assert data["returned_count"] == len(expected)


def test_candidate_summary_does_not_claim_scientific_review(candidate_case):
    data = payload(candidate_case.tools.handle("wt_query_candidate", {}))
    assert data["mode"] == "summary" and data["returned_count"] == 1
    assert data["items"][0]["counts"]["observations"] == 25
    assert data["items"][0]["review_status"] == "not_established_by_retrieval"
    assert data["scientific_validation"] == "not_established"
    assert data["items"][0]["quantitative_coverage_summary"]["limitations"]
    assert not candidate_case.tools.has_candidate_read(candidate_case.binding)


@pytest.mark.parametrize("path", [["observations", 0, "value"], ["observations", 1, "value", "value"]])
def test_path_reads_single_complete_field_even_when_null(candidate_case, path):
    data = payload(candidate_case.tools.handle("wt_query_candidate", {"path": path}))
    expected = candidate_case.candidate.model_dump(mode="json")
    for part in path:
        expected = expected[part]
    assert data["mode"] == "path" and data["value"] == expected
    assert data["returned_count"] == 1 and data["next_offset"] is None
    assert data["candidate_binding"] == candidate_case.binding
    assert candidate_case.tools.has_candidate_read(candidate_case.binding)


@pytest.mark.parametrize("arguments", [
    {"path": []}, {"path": ["observations", -1]}, {"path": ["observations", True]},
    {"path": ["observations", 0.0]}, {"path": ["observations", "0"]},
    {"path": ["observations", 999]}, {"path": ["not-a-field"]},
    {"path": ["paper"] * 17}, {"path": ["paper"], "query": "anything"},
    {"offset": -1}, {"limit": 21}, {"section": "credentials"},
    {"source_id": "../private.json"}, {"candidate_binding": {"path": "../private.json"}},
])
def test_candidate_query_rejects_non_field_paths_and_unbounded_or_foreign_scope(candidate_case, arguments):
    assert not candidate_case.tools.handle("wt_query_candidate", arguments)["success"]
    assert candidate_case.tools.candidate_binding == candidate_case.binding


def test_tampered_snapshot_cannot_be_returned_or_used_for_prompt(candidate_case):
    snapshot = Path(candidate_case.binding["path"])
    changed = candidate_case.candidate.model_dump(mode="json")
    changed["observations"][0]["value"]["value"] = 999
    snapshot.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="changed after binding"):
        candidate_case.tools.candidate_for_prompt()
    result = candidate_case.tools.handle("wt_query_candidate", {"section": "observations"})
    assert not result["success"] and "changed after binding" in result["contentItems"][0]["text"]


def test_revision_queries_follow_actual_stages_with_one_scope_and_preserve_old_snapshots(candidate_case):
    tools = candidate_case.tools
    audit_binding = candidate_case.binding
    tools.prepare_revision(candidate_case.candidate)
    tools.begin_submission(True)
    initial = payload(tools.handle("wt_query_candidate", {"ids": ["rate"], "section": "observations"}))
    initial_binding = initial["candidate_binding"]
    assert Path(initial_binding["path"]).parent == tools.run / "revision_bases"
    assert initial_binding["scope"] == tools.submission_scope != audit_binding["scope"]
    assert tools.has_candidate_read(initial_binding)
    state = payload(tools.handle("wt_revision_state", {}))
    staged = payload(tools.handle("wt_patch_record", {"base_hash": state["base_hash"], "edits": [{
        "operation": "set", "path": ["observations", 0, "value", "value"], "value": 2.5}]}))
    assert not tools.has_candidate_read(tools.candidate_binding)
    current = payload(tools.handle("wt_query_candidate", {"path": ["observations", 0, "value"]}))
    binding = current["candidate_binding"]
    assert current["value"]["value"] == 2.5
    assert binding["scope"] == initial_binding["scope"] and binding["content_hash"] == staged["base_hash"]
    assert Path(binding["path"]) == tools.submissions[staged["submission_id"]]["path"]
    assert binding["sha256"] == staged["canonical_record_sha256"]
    for older in (audit_binding, initial_binding):
        assert hash_file(Path(older["path"])) == older["sha256"]
    tools.begin_submission(False)
    assert tools.candidate_binding == binding
    assert tools.candidate_for_prompt().observations[0].value.value == 2.5


def test_candidate_read_proof_requires_successful_nonempty_read_of_exact_binding(candidate_case):
    tools = candidate_case.tools
    binding = tools.candidate_binding
    payload(tools.handle("wt_query_candidate", {}))
    payload(tools.handle("wt_query_candidate", {"section": "extensions"}))
    payload(tools.handle("wt_query_candidate", {"section": "observations", "ids": ["missing-id"]}))
    assert not tools.handle("wt_query_candidate", {"path": ["missing-field"]})["success"]
    assert not tools.has_candidate_read(binding)
    payload(tools.handle("wt_query_candidate", {"section": "paper"}))
    assert tools.has_candidate_read(binding)
    for key in binding:
        altered = {**binding, key: "different"}
        assert not tools.has_candidate_read(altered)
    assert not tools.has_candidate_read({"content_hash": binding["content_hash"]})
    rebound = tools.bind_candidate(tools.candidate_for_prompt(), Path(binding["path"]))
    assert not tools.has_candidate_read(rebound)
