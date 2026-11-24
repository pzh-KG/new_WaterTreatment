"""Initial extraction repairs use immutable local drafts, not repeated full output."""
from __future__ import annotations

import copy
import json

import pytest

from test_record_patch import data, edit, patch_case, receipt, stage
from watertreatment.domain import ExtractionRecord
from watertreatment.storage import hash_file
from watertreatment.validation import validate_record


def submit(case, record=None):
    payload = record if record is not None else case.record.model_dump(mode="json")
    return data(case.tools.handle("wt_submit_record", {"record": payload}))


def test_first_schema_valid_draft_with_binding_errors_can_be_repaired_locally(patch_case):
    tools = patch_case.tools
    tools.begin_submission(True)
    broken = patch_case.record.model_dump(mode="json")
    broken["observations"][0]["experiment_id"] = "experiment-not-in-record"
    before = validate_record(ExtractionRecord.model_validate(broken), tools.documents.values())
    assert any(i.severity == "error" and i.path == "observations[0].experiment_id" for i in before)

    first = submit(patch_case, broken)
    assert first["schema_valid"] and first["patch_available"]
    assert "wt_patch_record" in first["next_step"]
    assert tools.revision_base.observations[0].experiment_id == "experiment-not-in-record"
    assert data(tools.handle("wt_revision_state", {}))["base_hash"] == first["base_hash"]
    original_stage = tools.submissions[first["submission_id"]]["path"]

    repaired = stage(patch_case, first["base_hash"], [
        edit(["observations", 0, "experiment_id"], "joint")])
    final = tools.resolve_submission(receipt(repaired))
    assert final.model_dump(mode="json") == patch_case.record.model_dump(mode="json")
    after = validate_record(final, tools.documents.values())
    assert not any(i.severity == "error" and i.path == "observations[0].experiment_id" for i in after)
    assert hash_file(original_stage) == first["canonical_record_sha256"]
    assert json.loads(original_stage.read_bytes())["observations"][0]["experiment_id"] == "experiment-not-in-record"
    assert hash_file(patch_case.original_path) == patch_case.original_hash
    requests = [json.loads(p.read_bytes())["tool"] for p in sorted(tools.audit_root.glob("*/request.json"))]
    assert requests.count("wt_submit_record") == 1 and requests.count("wt_patch_record") == 1
    with pytest.raises(ValueError, match="stale"):
        tools.resolve_submission(receipt(first))


def test_rejected_initial_schema_does_not_create_a_patch_base(patch_case):
    tools = patch_case.tools
    tools.begin_submission(True)
    invalid = patch_case.record.model_dump(mode="json")
    invalid["invented_field"] = "not permitted"
    assert not tools.handle("wt_submit_record", {"record": invalid})["success"]
    assert tools.revision_base is None and tools.latest_submission_id is None
    assert not tools.submissions
    assert not tools.handle("wt_revision_state", {})["success"]
    valid = submit(patch_case)
    assert tools.resolve_submission(receipt(valid)) == patch_case.record


@pytest.mark.parametrize("operation", ["patch", "submit"])
@pytest.mark.parametrize("field,value", [
    ("id", "another-paper"), ("source_ids", ["another-source"]),
    ("dataset_role", "validation"),
])
def test_first_stage_locks_paper_source_and_cohort_identity(patch_case, operation, field, value):
    tools = patch_case.tools
    tools.begin_submission(True)
    first = submit(patch_case)
    binding = tools.candidate_binding
    if operation == "patch":
        response = tools.handle("wt_patch_record", {"base_hash": first["base_hash"],
            "edits": [edit(["paper", field], value)]})
    else:
        changed = patch_case.record.model_dump(mode="json")
        changed["paper"][field] = value
        response = tools.handle("wt_submit_record", {"record": changed})
    assert not response["success"]
    assert tools.candidate_binding == binding and tools.latest_submission_id == first["submission_id"]
    assert tools.resolve_submission(receipt(first)) == patch_case.record


def test_initial_full_replacement_cannot_restore_a_stale_receipt_or_base(patch_case):
    tools = patch_case.tools
    tools.begin_submission(True)
    initial = submit(patch_case)
    changed = patch_case.record.model_dump(mode="json")
    changed["observations"][0]["value"]["value"] = 2.5
    second = submit(patch_case, changed)
    with pytest.raises(ValueError, match="stale"):
        tools.resolve_submission(receipt(initial))
    assert not tools.handle("wt_patch_record", {"base_hash": initial["base_hash"],
        "edits": [edit(["observations", 0, "value", "value"], 999)]})["success"]
    assert tools.latest_submission_id == second["submission_id"]
    assert tools.resolve_submission(receipt(second)).observations[0].value.value == 2.5


@pytest.mark.parametrize("next_turn_enabled", [True, False])
def test_initial_stage_does_not_authorize_patches_or_receipts_in_later_turns(patch_case, next_turn_enabled):
    tools = patch_case.tools
    tools.begin_submission(True)
    initial = submit(patch_case)
    tools.begin_submission(next_turn_enabled)
    with pytest.raises(ValueError, match="this model turn"):
        tools.resolve_submission(receipt(initial))
    assert tools.revision_base is None and tools.latest_submission_id is None
    assert not tools.handle("wt_revision_state", {})["success"]
    assert not tools.handle("wt_patch_record", {"base_hash": initial["base_hash"],
        "edits": [edit(["observations", 0, "value", "value"], 999)]})["success"]


@pytest.mark.parametrize("mutation", ["disk", "memory", "scope"])
def test_modified_initial_stage_cannot_be_hidden_by_later_patch_or_full_submit(patch_case, mutation):
    tools = patch_case.tools
    tools.begin_submission(True)
    initial = submit(patch_case)
    old_submissions = copy.deepcopy(tools.submissions)
    if mutation == "disk":
        path = tools.submissions[initial["submission_id"]]["path"]
        changed = json.loads(path.read_bytes())
        changed["observations"][0]["value"]["value"] = 999
        path.write_text(json.dumps(changed), encoding="utf-8")
    elif mutation == "memory":
        tools.revision_base.observations[0].value.value = 999
    else:
        tools._candidate_binding["scope"] = "foreign-turn"
    assert not tools.handle("wt_revision_state", {})["success"]
    assert not tools.handle("wt_patch_record", {"base_hash": initial["base_hash"],
        "edits": [edit(["observations", 0, "value", "value"], 2.6)]})["success"]
    assert not tools.handle("wt_submit_record", {"record": patch_case.record.model_dump(mode="json")})["success"]
    with pytest.raises(ValueError):
        tools.resolve_submission(receipt(initial))
    assert tools.submissions == old_submissions and tools.latest_submission_id == initial["submission_id"]
    assert len(list(tools.audit_root.glob("*/submitted_record.json"))) == 1
    assert hash_file(patch_case.original_path) == patch_case.original_hash


def test_initial_stage_is_frozen_from_the_callers_mutable_payload(patch_case):
    tools = patch_case.tools
    tools.begin_submission(True)
    payload = patch_case.record.model_dump(mode="json")
    first = submit(patch_case, payload)
    payload["observations"][0]["value"]["value"] = 999
    assert tools.resolve_submission(receipt(first)).observations[0].value.value == 2.4
    repaired = stage(patch_case, first["base_hash"], [
        edit(["observations", 0, "limitations"], "Synthetic bounded estimate.", "append")])
    final = tools.resolve_submission(receipt(repaired))
    assert final.observations[0].value.value == 2.4
    assert final.observations[1] == patch_case.record.observations[1]
