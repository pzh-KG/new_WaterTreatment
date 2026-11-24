"""Offline exact path batches: no projection, scientific rewriting or partial delivery."""
import copy
import json

import pytest

from test_record_patch import patch_case, data
from watertreatment.reading_tools import (CandidateQueryArgs, CANDIDATE_BATCH_MAX_BYTES,
    candidate_path_batch, candidate_path_batch_matches, candidate_batch_response_bytes)
from watertreatment.storage import digest_json, write_new_json


def bind(case):
    case.tools.bind_candidate(case.record, case.original_path)
    case.tools.begin_query_turn()
    case.tools.begin_submission(False)
    return case.tools.candidate_binding


def test_batch_serializes_one_record_and_preserves_both_facts(patch_case, monkeypatch):
    record = patch_case.record
    paths = [["observations", i, "value"] for i in (0, 1)]
    expected = [record.observations[i].value.model_dump(mode="json") for i in (0, 1)]
    original_dump = type(record).model_dump
    calls = []

    def counted_dump(self, **kwargs):
        calls.append(self is record)
        return original_dump(self, **kwargs)

    monkeypatch.setattr(type(record), "model_dump", counted_dump)
    rows = candidate_path_batch(record, {}, paths)["items"]
    assert [row["value"] for row in rows] == expected
    assert calls == [True]


def test_exact_batches_keep_full_facts_indices_and_current_turn_proof(patch_case):
    c = patch_case
    binding = bind(c)
    paths = [["observations", 0], ["observations", 1, "value"], ["paper", "title"]]
    result = data(c.tools.handle("wt_query_candidate", {"paths": paths}))
    assert result["returned_count"] == 3 and result["complete_requested_values"]
    assert result["items"][0]["value"] == c.record.observations[0].model_dump(mode="json")
    assert result["items"][0]["source_indices"] == [{"path_position": 1, "index": 0}]
    assert result["items"][0]["entity_id"] == "rate"
    assert result["items"][1]["value"]["status"] == "not_reported"
    assert candidate_path_batch_matches(c.record, binding, {"paths": paths}, result)
    assert c.tools.has_candidate_paths_read(binding, paths)
    cached = data(c.tools.handle("wt_query_candidate", {"paths": paths}))
    assert cached["mode"] == "cached_reference"
    assert not candidate_path_batch_matches(c.record, binding, {"paths": paths}, cached)
    refreshed = data(c.tools.handle("wt_query_candidate", {"paths": paths, "refresh": True}))
    assert refreshed["items"] == result["items"]
    c.tools.begin_query_turn()
    assert not c.tools.has_candidate_paths_read(binding, paths)
    single = data(c.tools.handle("wt_query_candidate", {"path": paths[0], "paths": []}))
    assert single["value"] == result["items"][0]["value"]
    assert c.tools.has_candidate_paths_read(binding, paths[:1])


@pytest.mark.parametrize("arguments", [
    {"paths": [["paper"]] * 2}, {"paths": [["paper"]] * 21},
    {"paths": [[]]}, {"paths": [["observations", -1]]},
    {"paths": [["observations", True]]}, {"paths": [[""]]},
    {"paths": [["paper"]], "path": ["paper"]},
    {"paths": [["paper"]], "section": "paper"},
    {"paths": [["paper"]], "limit": 20},
    {"paths": [["paper"]], "fields": ["id"]},
    {"paths": [["paper"]], "ids": ["id"]},
    {"paths": [["paper"]], "offset": 1},
])
def test_invalid_batches_are_rejected(arguments):
    with pytest.raises(ValueError):
        CandidateQueryArgs.model_validate(arguments)


def test_batch_failure_is_atomic_and_does_not_prove_reading(patch_case):
    c = patch_case
    binding = bind(c)
    paths = [["paper", "id"], ["observations", 99]]
    response = c.tools.handle("wt_query_candidate", {"paths": paths})
    assert not response["success"]
    assert not c.tools.has_candidate_read(binding)
    assert not c.tools.has_candidate_paths_read(binding, paths[:1])
    assert not c.tools._query_turn_cache
    assert c.record.paper.id not in response["contentItems"][0]["text"]


def test_native_utf8_batch_bound_rejects_all_values_without_truncation(patch_case):
    c = patch_case
    c.record.paper.title.value = "中文" * 50000
    new_path = c.original_path.with_name("large-candidate.json")
    write_new_json(new_path, c.record.model_dump(mode="json"))
    c.tools.bind_candidate(c.record, new_path)
    c.tools.begin_query_turn()
    binding = c.tools.candidate_binding
    response = c.tools.handle("wt_query_candidate", {"paths": [["paper", "id"], ["paper", "title"]]})
    assert not response["success"] and "CANDIDATE_BATCH_TOO_LARGE" in response["contentItems"][0]["text"]
    assert "中文" not in json.dumps(response, ensure_ascii=False)
    assert not c.tools.has_candidate_read(binding) and not c.tools._query_turn_cache


@pytest.mark.parametrize("change", ["value", "count_type", "value_type", "binding", "indices", "extra", "path"])
def test_provenance_rejects_exact_value_and_receipt_tampering(patch_case, change):
    c = patch_case
    binding = bind(c)
    args = {"paths": [["paper", "id"]]}
    result = data(c.tools.handle("wt_query_candidate", args))
    if change == "value": result["items"][0]["value"] = "forged"
    elif change == "count_type": result["returned_count"] = True
    elif change == "value_type": result["complete_requested_values"] = 1
    elif change == "binding": result["candidate_binding"]["sha256"] = "a" * 64
    elif change == "indices": result["items"][0]["source_indices"] = [{"path_position": 1, "index": 9}]
    elif change == "extra": result["unsupported_extra"] = 1
    else: result["items"][0]["path"] = ["paper", "title"]
    assert not candidate_path_batch_matches(c.record, binding, args, result)


def test_read_only_addendum_boundary_refuses_both_mutation_tools(patch_case):
    c = patch_case
    binding = bind(c)
    before = c.original_path.read_bytes()
    for name, args in (("wt_submit_record", {"record": c.record.model_dump(mode="json")}),
        ("wt_patch_record", {"base_hash": binding["content_hash"],
            "edits": [{"operation": "set", "path": ["observations", 0, "value", "value"], "value": 999}]})):
        assert not c.tools.handle(name, args)["success"]
    assert not c.tools.submissions and before == c.original_path.read_bytes()


def test_scientific_scope_display_removes_only_transport_metadata():
    from watertreatment.audit_execution import scientific_scope_display
    scope = {"scope_sha256": "a" * 64, "old_hash": "root-kept", "changes": [{"old_value_hash": "old",
        "new_value_hash": "new", "old_value": {"old_value_hash": "scientific-key", "value": None,
        "status": "not_reported"}, "new_value": {"value": 0, "status": "reported"}}],
        "entity_refs": [{"old_hash": "a", "new_hash": "b", "record_path": "observations[0]"}],
        "source_anchors": [{"locator": {"description": None, "table_index": 0, "flag": False, "raw": []}}],
        "blocking_obligations": [{"path": "x", "value": None}]}
    frozen = copy.deepcopy(scope)
    shown = scientific_scope_display(scope)
    assert scope == frozen and digest_json(scope) == digest_json(frozen)
    assert shown["scope_sha256"] == scope["scope_sha256"] and shown["old_hash"] == "root-kept"
    assert "old_value_hash" not in shown["changes"][0]
    assert shown["changes"][0]["old_value"] == scope["changes"][0]["old_value"]
    assert shown["blocking_obligations"] == scope["blocking_obligations"]
    assert shown["source_anchors"][0]["locator"] == {"table_index": 0, "flag": False, "raw": []}
    assert candidate_batch_response_bytes({"value": "中文"}) < CANDIDATE_BATCH_MAX_BYTES
