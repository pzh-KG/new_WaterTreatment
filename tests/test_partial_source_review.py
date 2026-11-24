"""Incomplete delivery stays incomplete; separate calls may prove a real union."""
import copy
import json
from pathlib import Path

import pytest

from test_source_review import source_case, scope_for, begin, read_blocks, view
from test_active_reading import active
from test_workflow import workspace
from test_app_server import Answer, adapter
from watertreatment.reading_tools import ReadingTools
from watertreatment.source_review import (
    build_source_review_proof, partial_source_review_progress,
    verify_partial_source_review_progress, build_source_review_union_proof,
    verify_source_review_union, verify_source_review,
)
from watertreatment.storage import digest_json, hash_file, write_new_json


def test_partial_replays_complete_receipts_without_claiming_completion(source_case):
    c = source_case
    scope = scope_for(c.docs, visual=True)
    begin(c, scope=scope)
    empty = c.tools.source_review_progress()
    assert not empty["complete"] and len(empty["missing_blocks"]) == 2
    read_blocks(c, "main")
    view(c, rectangle=[0, 0, .5, .5])
    partial = c.tools.source_review_progress()
    assert partial["delivered"] == [{"source_id": "main", "block_id": c.docs[0].blocks[0].block_id}]
    assert partial["full_views"] == [] and len(partial["missing_full_views"]) == 2
    assert partial["missing_blocks"][0]["source_id"] == "si"
    assert verify_partial_source_review_progress(c.run, scope, partial, c.call_id)
    with pytest.raises(ValueError, match="delivery incomplete"):
        build_source_review_proof(c.run, scope, partial["tool_receipts"], c.call_id)
    assert not verify_source_review(c.run, scope, partial, c.call_id)
    changed = copy.deepcopy(partial)
    changed["complete"] = True
    changed["missing_blocks"] = changed["missing_full_views"] = []
    changed["progress_sha256"] = digest_json({k:v for k,v in changed.items() if k != "progress_sha256"})
    assert not verify_partial_source_review_progress(c.run, scope, changed, c.call_id)


def parts_for(c):
    scope = scope_for(c.docs, visual=True)
    begin(c, scope=scope)
    view(c, "main")
    first = c.tools.source_review_progress()
    supplement = c.run.parent / "supplement"
    supplement.mkdir()
    bundle = json.loads((c.run / "sources.json").read_text(encoding="utf-8"))
    write_new_json(supplement / "sources.json", bundle)
    tools = ReadingTools(bundle, supplement, max_calls=10,
        expected_inputs=[{"path":d.source.path,"sha256":d.source.sha256} for d in c.docs])
    missing_scope = scope_for(c.docs[1:], visual=True)
    missing_scope["policy"] = "bounded-partial-audit-completion-v1"
    missing_scope["scope_sha256"] = digest_json({k:v for k,v in missing_scope.items() if k != "scope_sha256"})
    tools.begin_query_turn()
    tools.begin_source_review(missing_scope, call_id="call-supplement")
    response = tools.handle("wt_view", {"source_id":"si", "physical_page":1, "scale":1})
    assert response["success"]
    second = tools.source_review_progress()
    parts = [{"run":str(c.run),"scope":scope,"call_id":c.call_id,"progress":first},
             {"run":str(supplement),"scope":missing_scope,"call_id":"call-supplement","progress":second}]
    return scope, parts


def test_union_replays_separate_runs_and_call_ids_and_all_original_requirements(source_case):
    c = source_case
    scope, parts = parts_for(c)
    assert not parts[0]["progress"]["complete"] and parts[1]["progress"]["complete"]
    proof = build_source_review_union_proof(c.run, scope, parts)
    assert verify_source_review_union(c.run, scope, proof)
    assert proof["scientific_validation"] == "not_established"
    assert [p["call_id"] for p in proof["parts"]] == [c.call_id,"call-supplement"]
    assert not verify_source_review(c.run, scope, proof, "fake-combined-call")
    with pytest.raises(ValueError, match="remains incomplete"):
        build_source_review_union_proof(c.run, scope, parts[:1])


def test_protocol_only_completion_has_no_new_source_claim_and_cannot_waive_original_scope(source_case):
    c = source_case
    scope = {"policy":"bounded-partial-audit-completion-v1", "source_anchors":[]}
    scope["scope_sha256"] = digest_json(scope)
    progress = partial_source_review_progress(c.run, scope, [], "protocol-only")
    assert progress["complete"] and progress["requirements"] == []
    assert progress["delivered"] == progress["full_views"] == []
    proof = build_source_review_proof(c.run, scope, [], "protocol-only")
    assert verify_source_review(c.run, scope, proof, "protocol-only")
    with pytest.raises(ValueError, match="remains incomplete"):
        build_source_review_union_proof(c.run, c.scope,
            [{"run":str(c.run),"scope":scope,"call_id":"protocol-only","progress":progress}])


@pytest.mark.parametrize("tamper", ["call", "duplicate", "progress", "result", "source_bundle", "scope"])
def test_union_rejects_reassignment_tampering_and_scope_drift(source_case, tamper):
    c = source_case
    scope, parts = parts_for(c)
    if tamper == "call":
        parts[1]["call_id"] = c.call_id
    elif tamper == "duplicate":
        parts.append(parts[0])
    elif tamper == "progress":
        parts[0]["progress"]["complete"] = True
    elif tamper == "result":
        path = Path(parts[0]["progress"]["tool_receipts"][0]["result_path"])
        path.write_bytes(path.read_bytes()+b" ")
    elif tamper == "source_bundle":
        path = Path(parts[1]["run"])/"sources.json"
        bundle = json.loads(path.read_text(encoding="utf-8"))
        bundle["documents"][0]["blocks"][0]["text"] = "Different source representation"
        path.write_text(json.dumps(bundle),encoding="utf-8")
    else:
        parts[1]["scope"]["scope_sha256"] = "0"*64
    with pytest.raises((ValueError, KeyError)):
        build_source_review_union_proof(c.run, scope, parts)


def test_adapter_retains_typed_source_gap_after_completed_structured_answer(active):
    client, _, kwargs = adapter(active)
    run = active[1]
    pdf = next(d for d in client.tools.documents.values() if d.source.format == "pdf")
    scope = scope_for([pdf], visual=True)
    path = run / "synthetic-source-scope.json"
    write_new_json(path, scope)
    kwargs["identity"]["citation_review"] = {"scope_path":str(path),"scope_file_sha256":hash_file(path)}
    try:
        with pytest.raises(ValueError, match="did not re-read"):
            client.complete(Answer, **kwargs)
    finally:
        client.close()
    folder = next((run/"calls").glob("call-*"))
    result = json.loads((folder/"result.json").read_text(encoding="utf-8"))
    progress = json.loads((folder/"source_review_progress.json").read_text(encoding="utf-8"))
    assert result["status"] == "failed" and result["public_turn_completed"] and result["answer_schema_valid"]
    assert result["source_review_progress"] == progress and not progress["complete"]
    assert result["source_review_completion_required"]["missing_full_views"] == progress["requirements"]
    assert (folder/"answer.json").exists()
    assert "source_review_proof" not in result
