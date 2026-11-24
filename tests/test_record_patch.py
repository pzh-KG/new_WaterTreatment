"""Offline, synthetic checks for immutable, turn-bound incremental revisions."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from watertreatment.domain import ExtractionRecord
from watertreatment.reading_tools import ReadingTools, SubmissionReceipt
from watertreatment.storage import digest_json, hash_file, write_new_json


@pytest.fixture
def patch_case(tmp_path):
    source = tmp_path / "synthetic-source.txt"
    source.write_text("Synthetic final composite: rate 2.4; comparison 80 percent.", encoding="utf-8")
    run = tmp_path / "revision-run"
    run.mkdir()
    bundle = {"documents": [{"source": {"source_id": "main", "role": "main_article",
        "path": str(source), "sha256": hash_file(source), "size_bytes": source.stat().st_size,
        "format": "txt"}, "blocks": [{"block_id": "paragraph", "source_id": "main",
        "kind": "paragraph", "order": 1, "text": source.read_text(encoding="utf-8")}]}]}
    record = ExtractionRecord.model_validate({
        "paper": {"id": "synthetic-paper", "source_ids": ["main"], "dataset_role": "development",
                  "title": {"value": "Synthetic patch case", "status": "reported"}},
        "materials": [{"id": "final", "role": "primary_final", "name": {
            "value": "Synthetic composite", "status": "reported", "evidence_ids": ["value-source"]}}],
        "experiments": [{"id": "joint", "material_ids": ["final"], "purpose": "coupled"}],
        "observations": [{"id": "rate", "experiment_id": "joint", "material_id": "final",
            "metric_kind": "evaporation_rate", "origin": "reported_measurement",
            "value": {"value": 2.4, "status": "reported", "evidence_ids": ["value-source"]},
            "unit": {"value": "kg m-2 h-1", "status": "reported"}},
            {"id": "missing", "experiment_id": "joint", "material_id": "final",
             "metric_kind": "mineralization_efficiency", "value": {"status": "not_reported"}}],
        "evidence": [{"id": "value-source", "source_id": "main", "block_id": "paragraph",
                      "quote": "rate 2.4"}],
        "coverage": [{"source_id": "main", "block_ids": ["paragraph"],
                      "state": "read_structured", "text_read": True}],
    })
    original_path = run / "original-candidate.json"
    write_new_json(original_path, record.model_dump(mode="json"))
    tools = ReadingTools(bundle, run, max_calls=40,
                         expected_inputs=[{"path": str(source), "sha256": hash_file(source)}])
    return SimpleNamespace(tools=tools, record=record, original_path=original_path,
                           original_hash=hash_file(original_path), source=source)


def data(response):
    assert response["success"], response
    return json.loads(response["contentItems"][0]["text"])


def start(case):
    case.tools.prepare_revision(case.record)
    case.tools.begin_submission(True)
    return data(case.tools.handle("wt_revision_state", {}))


def stage(case, base_hash, edits):
    return data(case.tools.handle("wt_patch_record", {"base_hash": base_hash, "edits": edits}))


def receipt(result):
    return SubmissionReceipt(submission_id=result["submission_id"])


def edit(path, value, operation="set"):
    return {"operation": operation, "path": path, "value": value}


@pytest.mark.parametrize("enabled", [False, True])
def test_patch_requires_controller_bound_revision_base(patch_case, enabled):
    tools = patch_case.tools
    tools.begin_submission(enabled)
    assert not tools.handle("wt_revision_state", {})["success"]
    assert not tools.handle("wt_patch_record", {
        "base_hash": digest_json(patch_case.record.model_dump(mode="json")),
        "edits": [edit(["observations", 0, "value", "value"], 2.5)]})["success"]
    assert not tools.submissions and tools.latest_submission_id is None


def test_audit_turn_disables_submission_and_discards_pending_revision_base(patch_case):
    tools = patch_case.tools
    tools.prepare_revision(patch_case.record)
    tools.begin_submission(False)
    assert not tools.handle("wt_submit_record", {"record": patch_case.record.model_dump(mode="json")})["success"]
    assert not tools.handle("wt_revision_state", {})["success"]
    assert not tools.handle("wt_patch_record", {
        "base_hash": digest_json(patch_case.record.model_dump(mode="json")),
        "edits": [edit(["rule_version"], "0.6")]})["success"]
    tools.begin_submission(True)
    assert not tools.handle("wt_revision_state", {})["success"]
    assert not tools.submissions


def test_revision_base_is_a_controller_owned_copy(patch_case):
    original = patch_case.record.model_dump(mode="json")
    patch_case.tools.prepare_revision(patch_case.record)
    patch_case.record.observations[0].value.value = 999
    patch_case.tools.begin_submission(True)
    state = data(patch_case.tools.handle("wt_revision_state", {}))
    assert state["base_hash"] == digest_json(original)
    assert patch_case.tools.revision_base.model_dump(mode="json") == original


def test_multiple_patch_batches_preserve_unedited_facts_and_record_each_stage(patch_case):
    original = patch_case.record.model_dump(mode="json")
    initial = start(patch_case)
    assert initial["latest_submission_id"] is None and initial["counts"]["observations"] == 2
    first = stage(patch_case, initial["base_hash"], [
        edit(["schema_version"], "0.2.0"), edit(["rule_version"], "0.6"),
        edit(["observations", 0, "origin"], "plot_estimate"),
        edit(["observations", 0, "value", "status"], "estimated"),
    ])
    first_record = patch_case.tools.resolve_submission(receipt(first)).model_dump(mode="json")
    extra_observation = copy.deepcopy(original["observations"][0])
    extra_observation["id"] = "new-point"
    extra_observation["value"]["value"] = 2.5
    second = stage(patch_case, first["base_hash"], [
        edit(["observations"], extra_observation, "append"),
        edit(["observations", 0, "limitations"], "Synthetic read-off uncertainty remains.", "append"),
        edit(["observations", 0, "coordinates"], {
            "name": "elapsed_time", "fact": {"value": 60, "status": "reported"},
            "unit": {"value": "min", "status": "reported"}}, "append"),
    ])
    final = patch_case.tools.resolve_submission(receipt(second)).model_dump(mode="json")
    assert final["schema_version"] == "0.2.0" and final["rule_version"] == "0.6"
    assert len(final["observations"]) == 3
    assert final["observations"][0]["value"]["value"] == original["observations"][0]["value"]["value"]
    assert final["observations"][1] == original["observations"][1]
    for name in ("paper", "materials", "experiments", "evidence", "coverage"):
        assert final[name] == original[name]
    assert len(first_record["observations"]) == 2 and len(final["observations"]) == 3
    for result in (first, second):
        saved = patch_case.tools.submissions[result["submission_id"]]["path"]
        assert hash_file(saved) == result["canonical_record_sha256"]
        assert digest_json(json.loads(saved.read_text(encoding="utf-8"))) == result["base_hash"]
        assert result["schema_valid"] and not result["published"]
        assert result["scientific_accuracy"] == "not_established" and isinstance(result["errors"], list)
        assert result["all_errors_inlined"] and result["validation_artifact"]["validation_id"]
    assert hash_file(patch_case.original_path) == patch_case.original_hash
    assert patch_case.record.model_dump(mode="json") == original


def test_stale_base_hash_and_receipt_cannot_replace_latest_revision(patch_case):
    initial = start(patch_case)
    first = stage(patch_case, initial["base_hash"], [edit(["observations", 0, "value", "value"], 2.5)])
    assert not patch_case.tools.handle("wt_patch_record", {"base_hash": initial["base_hash"],
        "edits": [edit(["observations", 0, "value", "value"], 999)]})["success"]
    assert patch_case.tools.latest_submission_id == first["submission_id"]
    second = stage(patch_case, first["base_hash"], [edit(["observations", 0, "value", "value"], 2.6)])
    with pytest.raises(ValueError, match="stale"):
        patch_case.tools.resolve_submission(receipt(first))
    assert patch_case.tools.resolve_submission(receipt(second)).observations[0].value.value == 2.6


@pytest.mark.parametrize("invalid_edit", [
    edit(["observations", 99, "value", "value"], 7),
    edit(["observations", -1, "value", "value"], 7),
    edit(["observations", -1], {}),
    edit(["observations", "0", "value", "value"], 7),
    edit(["invented_root_field"], 7),
    edit(["observations", 0, "invented_observation_field"], 7),
    edit(["observations", 0, "value", "value", "nested"], 7),
    edit(["observations", 0, "value", "value"], 7, "append"),
    edit(["observations", 0], None, "delete"),
    edit([], 7),
    edit(["paper", "id"], "different-paper"),
    edit(["paper", "dataset_role"], "validation"),
])
def test_invalid_patch_batch_is_rejected_atomically(patch_case, invalid_edit):
    initial = start(patch_case)
    first = stage(patch_case, initial["base_hash"], [edit(["observations", 0, "value", "value"], 2.5)])
    before = patch_case.tools.revision_base.model_dump(mode="json")
    submissions = copy.deepcopy(patch_case.tools.submissions)
    response = patch_case.tools.handle("wt_patch_record", {"base_hash": first["base_hash"], "edits": [
        edit(["observations", 0, "value", "value"], 999), invalid_edit]})
    assert not response["success"]
    assert patch_case.tools.revision_base.model_dump(mode="json") == before
    assert patch_case.tools.submissions == submissions
    assert patch_case.tools.latest_submission_id == first["submission_id"]
    assert not (patch_case.tools.audit_root / f"tool-{patch_case.tools.calls_started:04d}" / "submitted_record.json").exists()
    assert patch_case.tools.resolve_submission(receipt(first)).model_dump(mode="json") == before
    assert hash_file(patch_case.original_path) == patch_case.original_hash


@pytest.mark.parametrize("index", [True, False, 0.0])
def test_patch_array_index_requires_an_integer_without_coercion(patch_case, index):
    state = start(patch_case)
    response = patch_case.tools.handle("wt_patch_record", {"base_hash": state["base_hash"],
        "edits": [edit(["observations", index, "value", "value"], 999)]})
    assert not response["success"]
    assert patch_case.tools.revision_base.model_dump(mode="json") == patch_case.record.model_dump(mode="json")
    assert not patch_case.tools.submissions


@pytest.mark.parametrize("path,value", [(["paper", "id"], "different-paper"),
                                        (["paper", "dataset_role"], "validation")])
def test_whole_record_submission_cannot_bypass_bound_revision_identity(patch_case, path, value):
    start(patch_case)
    changed = patch_case.record.model_dump(mode="json")
    changed[path[0]][path[1]] = value
    response = patch_case.tools.handle("wt_submit_record", {"record": changed})
    assert not response["success"]
    assert not patch_case.tools.submissions
    assert patch_case.tools.revision_base.model_dump(mode="json") == patch_case.record.model_dump(mode="json")


@pytest.mark.parametrize("next_turn_enabled", [True, False])
def test_submission_receipt_is_bound_to_one_model_turn(patch_case, next_turn_enabled):
    initial = start(patch_case)
    submitted = stage(patch_case, initial["base_hash"], [edit(["observations", 0, "value", "value"], 2.5)])
    patch_case.tools.prepare_revision(patch_case.record)
    patch_case.tools.begin_submission(next_turn_enabled)
    with pytest.raises(ValueError, match="this model turn"):
        patch_case.tools.resolve_submission(receipt(submitted))


def test_resolving_receipt_checks_saved_stage_content_hash(patch_case):
    initial = start(patch_case)
    submitted = stage(patch_case, initial["base_hash"], [edit(["observations", 0, "value", "value"], 2.5)])
    saved = patch_case.tools.submissions[submitted["submission_id"]]["path"]
    changed = json.loads(saved.read_text(encoding="utf-8"))
    changed["observations"][0]["value"]["value"] = 999
    saved.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="changed after submission"):
        patch_case.tools.resolve_submission(receipt(submitted))
    assert hash_file(patch_case.original_path) == patch_case.original_hash
