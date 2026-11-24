"""Synthetic candidate-snapshot audit receipts; no provider or scientific claims."""
from __future__ import annotations

import copy
import json

import pytest

from test_web_records import fixture, read
from watertreatment.storage import digest_json, hash_file


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def verify(fixture):
    return fixture.records._audit_provenance(fixture.run, fixture.record,
        read(fixture.run / "result.json"), read(fixture.run / "model_review.json"))


def snapshot_call(fixture, *, mode="section", returned_count=1):
    run = fixture.run
    snapshot = run / "candidate_snapshots" / "audit-scope.json"
    write(snapshot, fixture.record.model_dump(mode="json"))
    binding = {"path": str(snapshot), "sha256": hash_file(snapshot),
               "content_hash": digest_json(fixture.record.model_dump(mode="json")), "scope": "audit-scope"}
    request = read(run / "calls" / "audit" / "request.json")["request"]
    request.update(candidate_binding=copy.deepcopy(binding),
                   prompt="Audit the bound record. CANDIDATE_SNAPSHOT " + binding["content_hash"])
    request["sources"].append({"path": binding["path"], "sha256": binding["sha256"]})
    save_request(fixture, request)
    tool = run / "reading_tools" / "query-001"
    write(tool / "request.json", {"tool": "wt_query_candidate", "arguments": {"section": "observations"}})
    result = {"success": True, "data": {"candidate_binding": copy.deepcopy(binding),
              "mode": mode, "returned_count": returned_count}}
    write(tool / "result.json", result)
    return request, result, snapshot


def save_request(fixture, request):
    write(fixture.run / "calls" / "audit" / "request.json",
          {"request": request, "fingerprint": digest_json(request)})


def save_tool(fixture, result):
    write(fixture.run / "reading_tools" / "query-001" / "result.json", result)


def test_web_candidate_provenance_legacy_literal_remains_supported(fixture):
    assert verify(fixture)


@pytest.mark.parametrize("mode", ["section", "path"])
def test_web_candidate_provenance_accepts_bound_snapshot_and_real_query(fixture, mode):
    request, _, _ = snapshot_call(fixture, mode=mode)
    assert fixture.record.model_dump_json(exclude_defaults=True) not in request["prompt"]
    assert verify(fixture)
    assert fixture.records.get(fixture.run.name)["summary"]["human_validated"] is False


@pytest.mark.parametrize("change", ["scope", "empty_scope", "hash", "content_hash", "marker", "snapshot_source", "extra_field"])
def test_web_candidate_provenance_rejects_mismatched_binding(fixture, change):
    request, result, _ = snapshot_call(fixture)
    binding = request["candidate_binding"]
    if change == "scope":
        binding["scope"] = "another-turn"
    elif change == "empty_scope":
        binding["scope"] = ""
        result["data"]["candidate_binding"] = copy.deepcopy(binding)
    elif change == "hash":
        binding["sha256"] = "0" * 64
        request["sources"][-1]["sha256"] = binding["sha256"]
        result["data"]["candidate_binding"] = copy.deepcopy(binding)
    elif change == "content_hash":
        binding["content_hash"] = "1" * 64
        request["prompt"] = "CANDIDATE_SNAPSHOT " + binding["content_hash"]
        result["data"]["candidate_binding"] = copy.deepcopy(binding)
    elif change == "marker":
        request["prompt"] = "Audit an unnamed record"
    elif change == "snapshot_source":
        request["sources"].pop()
    else:
        binding["extra"] = "unrecognized authority"
        result["data"]["candidate_binding"] = copy.deepcopy(binding)
    save_request(fixture, request)
    save_tool(fixture, result)
    assert not verify(fixture)


@pytest.mark.parametrize("outside_data_root", [False, True])
def test_web_candidate_provenance_rejects_snapshot_outside_current_run(fixture, outside_data_root):
    request, result, snapshot = snapshot_call(fixture)
    destination = ((fixture.settings.data_root.parent / "outside.json") if outside_data_root
                   else (fixture.settings.data_root / "runs" / "other-run" / "snapshot.json"))
    write(destination, read(snapshot))
    binding = request["candidate_binding"]
    binding.update(path=str(destination), sha256=hash_file(destination))
    request["sources"][-1] = {"path": binding["path"], "sha256": binding["sha256"]}
    result["data"]["candidate_binding"] = copy.deepcopy(binding)
    save_request(fixture, request)
    save_tool(fixture, result)
    assert not verify(fixture)


def test_web_candidate_provenance_rejects_different_record_even_with_matching_file_hash(fixture):
    request, result, snapshot = snapshot_call(fixture)
    altered = read(snapshot)
    altered["observations"][0]["value"]["value"] = 999
    write(snapshot, altered)
    binding = request["candidate_binding"]
    binding["sha256"] = hash_file(snapshot)
    request["sources"][-1]["sha256"] = binding["sha256"]
    result["data"]["candidate_binding"] = copy.deepcopy(binding)
    save_request(fixture, request)
    save_tool(fixture, result)
    assert not verify(fixture)


@pytest.mark.parametrize("mode,count", [("summary", 1), ("section", 0), ("path", 0), ("section", -1),
                                       ("section", True), ("section", "1"), ("other", 1)])
def test_web_candidate_provenance_rejects_summary_or_empty_query(fixture, mode, count):
    snapshot_call(fixture, mode=mode, returned_count=count)
    assert not verify(fixture)


@pytest.mark.parametrize("change", ["missing_result", "failed", "wrong_tool", "missing_binding", "malformed_result"])
def test_web_candidate_provenance_requires_completed_candidate_query(fixture, change):
    _, result, _ = snapshot_call(fixture)
    tool = fixture.run / "reading_tools" / "query-001"
    if change == "missing_result":
        (tool / "result.json").unlink()
    elif change == "wrong_tool":
        write(tool / "request.json", {"tool": "wt_inventory", "arguments": {}})
    elif change == "malformed_result":
        write(tool / "result.json", [])
    else:
        if change == "failed":
            result["success"] = False
        else:
            result["data"].pop("candidate_binding")
        save_tool(fixture, result)
    assert not verify(fixture)


@pytest.mark.parametrize("field,value", [("status", "failed"), ("server_selected_model", "another-model"),
                                        ("server_selected_effort", "low")])
def test_web_candidate_provenance_keeps_completed_sol_max_requirement(fixture, field, value):
    snapshot_call(fixture)
    path = fixture.run / "calls" / "audit" / "result.json"
    result = read(path)
    result[field] = value
    write(path, result)
    assert not verify(fixture)


def test_web_candidate_provenance_keeps_original_source_snapshot_requirement(fixture):
    request, _, _ = snapshot_call(fixture)
    request["sources"][0]["sha256"] = "0" * 64
    save_request(fixture, request)
    assert not verify(fixture)


def test_web_candidate_provenance_invalid_new_binding_cannot_fall_back_to_literal(fixture):
    request, _, _ = snapshot_call(fixture)
    request["prompt"] += "\n" + fixture.record.model_dump_json(exclude_defaults=True)
    request["candidate_binding"]["scope"] = "wrong-turn"
    save_request(fixture, request)
    assert not verify(fixture)
