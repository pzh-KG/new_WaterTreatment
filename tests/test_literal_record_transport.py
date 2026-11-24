"""Offline lossless wire compaction; no provider/database or paper-specific answers."""
from __future__ import annotations

import copy
import json

import pytest

from test_record_patch import patch_case, data, start, receipt
from watertreatment.domain import ExtractionRecord
from watertreatment.literal_record_transport import (
    SHARED_MARKER, expand_record_wire, apply_record_edits, expand_record_edits,
    select_schema_definitions,
)
from watertreatment.storage import hash_file


def marker(name):
    return {SHARED_MARKER: name}


def test_invalid_initial_draft_can_be_corrected_exactly_and_replayed(patch_case):
    from watertreatment.staged_source_resume import _replay_stage
    tools = patch_case.tools
    tools.begin_submission(True)
    raw = patch_case.record.model_dump(mode="json", exclude_defaults=True)
    raw["observations"][0].update(origin="invalid_label", unexpected_auxiliary="extra")
    rejected = tools.handle("wt_submit_record", {"record":raw})
    assert not rejected["success"] and not tools.submissions
    draft = json.loads(rejected["contentItems"][-1]["text"])["rejected_draft_id"]
    base = {"draft_id":draft,"edits":[{"path":["observations",0,"origin"], "operation":"set",
                                      "value":"reported_measurement"},
                                     {"path":["observations",0,"unexpected_auxiliary"], "operation":"remove_extra"}]}
    for invalid in ([{"path":["observations",0,"value"],"operation":"set","value":5}],
                    [{"path":["observations",0,"origin"],"operation":"remove_extra"}],
                    [{"path":["observations",0,"origin"],"operation":"set"}]):
        assert not tools.handle("wt_submit_record", {"repair_draft":dict(base, edits=invalid)})["success"]
        assert not tools.submissions
    result = data(tools.handle("wt_submit_record", {"repair_draft":base}))
    assert tools.resolve_submission(receipt(result)) == patch_case.record
    path = tools.submissions[result["submission_id"]]["path"]
    item = {"path":path, "request":json.loads((path.parent/"request.json").read_bytes()), "data":result}
    assert _replay_stage(None, item, patch_case.record.paper.id) == patch_case.record
    assert not tools.handle("wt_submit_record", {"repair_draft":base})["success"]
    tools.begin_submission(True)
    assert not tools.handle("wt_submit_record", {"repair_draft":base})["success"]
    first_request = path.parent.parent / draft.rsplit("-", 1)[0] / "request.json"
    first_request.write_bytes(first_request.read_bytes() + b" ")
    with pytest.raises(ValueError, match="changed"):
        _replay_stage(None, item, patch_case.record.paper.id)


def test_many_distinct_literal_definitions_are_still_bounded():
    from watertreatment.literal_record_transport import MAX_SHARED_VALUES
    entries = {f"a{i}": i for i in range(MAX_SHARED_VALUES)}
    record = {"items":[marker(k) for k in entries]}
    expanded, _ = expand_record_wire(record, entries)
    assert expanded["items"] == list(entries.values())
    with pytest.raises(ValueError, match="at most"):
        expand_record_wire(record, {**entries, "extra":1})


def test_nested_literal_field_rows_preserve_exact_candidate_and_can_replay_staging(patch_case):
    from watertreatment.staged_source_resume import _replay
    original = patch_case.record.model_dump(mode="json")
    body = copy.deepcopy(original)
    fields = list(body["observations"][0]["value"])
    reference = body["observations"][0]["value"]
    body["observations"][0]["value"] = ["@wt:fact", *(marker("amount") if k == "value" else reference[k] for k in fields)]
    args = {"record": body, "field_layouts": {"fact": fields}, "shared_values": {"amount": reference["value"]}}
    decoded, proof = expand_record_wire(**args)
    assert decoded == original and proof["expanded_rows"] == 1 and proof["expanded_reference_count"] == 1
    assert _replay(None, {"tool": "wt_submit_record", "arguments": args}) == patch_case.record
    literal = {"x": ["@wt:fact", "literal scientific label"]}
    assert expand_record_wire(literal)[0] == literal


@pytest.mark.parametrize("row,layouts", [
    (["@wt:F", 1], {"F": ["value", "status"]}),
    (["@wt:X", 1], {"F": ["value"]}),
    (["@wt:F", 1, 2], {"F": ["value", "value"]}),
    (["@wt:F", 1], {"F": ["value"], "unused": ["status"]}),
])
def test_literal_field_rows_reject_incomplete_or_ambiguous_encodings(row, layouts):
    with pytest.raises(ValueError):
        expand_record_wire({"fact": row}, field_layouts=layouts)


def test_literal_shared_values_preserve_exact_status_source_and_independent_objects():
    fact = {"value": None, "status": "not_reported", "evidence_ids": ["source-a"],
            "note": "Retained amount absent; nominal precursor charge is a different quantity."}
    raw = {"one": marker("missing"), "two": marker("alias"),
           "distinct": {"value": None, "status": "unread"}}
    definitions = {"missing": fact, "alias": marker("missing")}
    before = copy.deepcopy((raw, definitions))
    expanded, proof = expand_record_wire(raw, definitions)
    assert expanded == {"one": fact, "two": fact, "distinct": {"value": None, "status": "unread"}}
    assert proof["automatic_scientific_transformations"] == 0 and proof["expanded_reference_count"] == 3
    expanded["one"]["evidence_ids"].append("changed")
    assert expanded["two"] == fact and (raw, definitions) == before


def test_legacy_literal_marker_shaped_measurement_is_not_interpreted_without_dictionary():
    literal = {"value": {SHARED_MARKER: "a literal scientific label"}}
    expanded, proof = expand_record_wire(literal)
    assert expanded == literal and proof["mode"] == "literal"


@pytest.mark.parametrize("record,values,match", [
    ({"a": marker("unknown")}, {"known": 3}, "unknown"),
    ({"a": {SHARED_MARKER: "known", "note": "override"}}, {"known": 3}, "sibling"),
    ({"a": marker("loop")}, {"loop": marker("loop")}, "cyclic"),
    ({"a": marker("first")}, {"first": marker("second"), "second": marker("first")}, "cyclic"),
    ({"a": 3}, {"unused": 9}, "unused"),
    ({"a": marker("../file")}, {"../file": 3}, "identifiers"),
])
def test_shared_protocol_rejects_ambiguous_or_implicit_meanings(record, values, match):
    with pytest.raises(ValueError, match=match):
        expand_record_wire(record, values)


def test_shared_expansion_fails_before_exponential_copy_exhaustion(monkeypatch):
    import watertreatment.literal_record_transport as transport
    monkeypatch.setattr(transport, "MAX_EXPANDED_NODES", 50)
    definitions = {"base": ["data"]}
    for i in range(8):
        previous = "base" if i == 0 else f"n{i-1}"
        definitions[f"n{i}"] = [marker(previous), marker(previous)]
    with pytest.raises(ValueError, match="node budget"):
        expand_record_wire({"a": marker("n7")}, definitions)


def test_shared_depth_and_byte_limits_are_independent(monkeypatch):
    import watertreatment.literal_record_transport as transport
    monkeypatch.setattr(transport, "MAX_DEPTH", 4)
    with pytest.raises(ValueError, match="depth"):
        expand_record_wire({"a": marker("deep")}, {"deep": [[[[[7]]]]]})
    monkeypatch.setattr(transport, "MAX_EXPANDED_BYTES", 30)
    with pytest.raises(ValueError, match="byte budget"):
        expand_record_wire({"a": marker("big")}, {"big": "界" * 30})


def test_grouped_set_is_exactly_equal_to_sequential_explicit_sets_and_keeps_nulls():
    original = {"a": {"status": "unread", "value": 4}, "b": {"status": "unread", "value": 5}}
    grouped = [{"operation": "set", "paths": [["a", "value"], ["b", "value"]], "value": None}]
    expected = [{"operation": "set", "path": [key, "value"], "value": None} for key in ["a", "b"]]
    changed, expanded = apply_record_edits(original, grouped)
    assert expanded == expected and changed == apply_record_edits(original, expected)[0]
    assert original["a"]["value"] == 4 and changed["a"]["status"] == "unread"


@pytest.mark.parametrize("edits", [
    [{"operation": "set", "paths": [["a"], ["a", "b"]], "value": 1}],
    [{"operation": "set", "paths": [["a"], ["a"]], "value": 1}],
    [{"operation": "set", "path": ["a"], "paths": [["b"]], "value": 1}],
    [{"operation": "append", "paths": [["a"]], "value": 1}],
    [{"operation": "set", "paths": [["a", True]], "value": 1}],
    [{"operation": "set", "paths": [["a", -1]], "value": 1}],
    [{"operation": "set", "paths": [[str(i)] for i in range(101)], "value": 1}],
])
def test_grouped_edits_reject_overlaps_ambiguity_and_budget_bypass(edits):
    with pytest.raises(ValueError):
        expand_record_edits(edits)


def test_legacy_sequential_overwrites_remain_replayable():
    edits = [{"operation": "set", "path": ["value"], "value": n} for n in [1, 2]]
    changed, expanded = apply_record_edits({"value": 0}, edits)
    assert changed == {"value": 2} and expanded == edits


def test_grouped_edits_to_original_rows_can_share_a_batch_with_ordered_appends():
    original={"points":[{"n":1},{"n":2}]}
    append=lambda n: {"operation":"append","path":["points"],"value":{"n":n}}
    group={"operation":"set","paths":[["points",1,"n"],["points",0,"n"]],"value":0}
    edits=[append(3),group,append(4)]
    amended,literal=apply_record_edits(original,edits)
    assert amended=={"points":[{"n":0},{"n":0},{"n":3},{"n":4}]}
    assert amended==apply_record_edits(original,literal)[0]
    assert original=={"points":[{"n":1},{"n":2}]}
    with pytest.raises(ValueError,match="existing list indices"):
        apply_record_edits(original,[append(3),dict(group,paths=[["points",2,"n"]])])
    with pytest.raises(ValueError,match="overlap"):
        apply_record_edits(original,[append(3),dict(group,paths=[["points"]])])


def test_shared_staging_retains_raw_expanded_and_canonical_inputs(patch_case):
    tools = patch_case.tools
    tools.begin_submission(True)
    compact = patch_case.record.model_dump(mode="json", exclude_defaults=True)
    observed = copy.deepcopy(compact["observations"][0]["value"])
    compact["observations"][0]["value"] = marker("value")
    result = data(tools.handle("wt_submit_record", {"record": compact, "shared_values": {"value": observed}}))
    recovered = tools.resolve_submission(receipt(result))
    assert recovered == patch_case.record
    folder = tools.submissions[result["submission_id"]]["path"].parent
    raw = json.loads((folder / "request.json").read_bytes())
    expanded = json.loads((folder / "expanded_record_input.json").read_bytes())
    assert raw["arguments"]["record"]["observations"][0]["value"] == marker("value")
    assert expanded["observations"][0]["value"] == observed
    assert result["transport"]["expanded_record_input_sha256"] == hash_file(folder / "expanded_record_input.json")
    assert result["transport"]["raw_request_sha256"] == hash_file(folder / "request.json")
    assert not result["published"] and hash_file(patch_case.original_path) == patch_case.original_hash


def test_grouped_patch_stages_atomic_complete_record_and_audits_each_target(patch_case):
    state = start(patch_case)
    before = patch_case.record.model_dump(mode="json")
    changed = data(patch_case.tools.handle("wt_patch_record", {"base_hash": state["base_hash"],
        "edits": [{"operation": "set", "paths": [["observations", 0, "limitations"],
                  ["observations", 1, "limitations"]], "value": ["Synthetic shared limitation."]}]}))
    final = patch_case.tools.resolve_submission(receipt(changed)).model_dump(mode="json")
    assert final["observations"][0]["value"] == before["observations"][0]["value"]
    assert final["observations"][1]["value"]["status"] == "not_reported"
    assert final["observations"][0]["limitations"] == final["observations"][1]["limitations"]
    folder = patch_case.tools.submissions[changed["submission_id"]]["path"].parent
    assert len(json.loads((folder / "expanded_edits.json").read_bytes())) == 2
    assert changed["transport"]["expanded_operations"] == 2
    # Schema/identity checks are applied after literal expansion, never bypassed.
    denied = patch_case.tools.handle("wt_patch_record", {"base_hash": changed["base_hash"],
        "edits": [{"operation": "set", "paths": [["paper", "id"]], "value": "foreign-paper"}]})
    assert not denied["success"]
    assert patch_case.tools.latest_submission_id == changed["submission_id"]


def test_selected_schema_has_complete_local_reference_closure():
    schema = ExtractionRecord.model_json_schema()
    selected = select_schema_definitions(schema, ["Component"])
    assert set(selected["$defs"]) < set(schema["$defs"])
    assert selected["$defs"]["Component"] == schema["$defs"]["Component"]
    assert selected["$defs"]["Fact"] == schema["$defs"]["Fact"]
    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].split("/")[-1] in selected["$defs"]
            for item in value.values(): visit(item)
        elif isinstance(value, list):
            for item in value: visit(item)
    visit(selected)
    with pytest.raises(ValueError, match=r"unknown schema definition: .*Available definitions: .*Component"):
        select_schema_definitions(schema, ["../not-a-definition"])


def test_schema_selection_cache_never_substitutes_other_definition_or_whole_schema(patch_case):
    tools = patch_case.tools
    component = data(tools.handle("wt_record_schema", {"definitions": ["Component"]}))
    again = data(tools.handle("wt_record_schema", {"definitions": ["Component"]}))
    full = data(tools.handle("wt_record_schema", {}))
    fact = data(tools.handle("wt_record_schema", {"definitions": ["Fact"]}))
    assert again["mode"] == "cached_schema_reference"
    assert not component["complete_record_schema"] and full["complete_record_schema"]
    assert component["schema"] != fact["schema"] != full["schema"]
    refreshed = data(tools.handle("wt_record_schema", {"definitions": ["Component"], "refresh": True}))
    assert refreshed["schema"] == component["schema"]


def test_reference_association_section_is_queryable_without_reassigning_primary_owner(patch_case):
    from watertreatment.domain import ReferenceAssociation
    association = ReferenceAssociation(id="reference-context", target_kind="experiment", target_id="joint",
        observation_ids=["rate"], relationship="contextual_comparison", applicability="ambiguous",
        rationale="Synthetic navigation fixture; applicability still requires scientific review.",
        evidence_ids=["value-source"], limitations=["Not a repeated measurement."])
    patch_case.record.reference_associations = [association]
    start(patch_case)
    result = data(patch_case.tools.handle("wt_query_candidate", {"section": "reference_associations"}))
    assert result["items"] == [association.model_dump(mode="json")]
    assert patch_case.tools.candidate_for_prompt().observations[0].experiment_id == "joint"
    selected = data(patch_case.tools.handle("wt_record_schema", {"definitions": ["ReferenceAssociation"]}))
    assert selected["schema"]["$defs"]["ReferenceAssociation"] == ExtractionRecord.model_json_schema()["$defs"]["ReferenceAssociation"]
