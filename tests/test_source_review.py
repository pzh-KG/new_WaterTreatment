"""Fresh source delivery is replayable but never treated as scientific truth."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_workflow import synthetic_pdf
from watertreatment.bounded_revision_audit import POLICY as BOUNDED_POLICY
from watertreatment.reading_tools import ReadingTools
from watertreatment.source_review import verify_source_review
from watertreatment.sources import parse_source
from watertreatment.storage import digest_json, hash_file, write_new_json


def scope_for(documents, *, visual=False):
    scope = {"policy": BOUNDED_POLICY, "source_anchors": [
        {"evidence_id": f"e-{d.source.source_id}", "source_id": d.source.source_id,
         "block_id": d.blocks[0].block_id, "locator": {"physical_page": 1, "figure": "Fig. 1" if visual else None}}
        for d in documents]}
    scope["scope_sha256"] = digest_json(scope)
    return scope


@pytest.fixture
def source_case(tmp_path):
    root = tmp_path / "data"
    originals = root / "originals"
    originals.mkdir(parents=True)
    docs = []
    for sid in ("main", "si"):
        path = originals / (sid + ".pdf")
        synthetic_pdf(path)
        docs.append(parse_source(path, source_id=sid))
    run = root / "runs" / "source-test"
    run.mkdir(parents=True)
    bundle = {"documents": [d.model_dump(mode="json") for d in docs]}
    write_new_json(run / "sources.json", bundle)
    tools = ReadingTools(bundle, run, max_calls=30,
        expected_inputs=[{"path": d.source.path, "sha256": d.source.sha256} for d in docs])
    return SimpleNamespace(root=root, run=run, docs=docs, tools=tools,
                           scope=scope_for(docs), call_id="call-source-01")


def begin(case, *, scope=None, call_id=None):
    case.tools.begin_query_turn()
    case.tools.begin_source_review(scope or case.scope, call_id=call_id or case.call_id)


def test_reader_and_model_input_inventories_must_both_authorize_each_original(source_case):
    from watertreatment.source_review import review_requirements
    req = review_requirements(source_case.run,source_case.scope,"offline")
    inputs = [{"path":d.source.path, "sha256":d.source.sha256} for d in source_case.docs]
    source_case.tools.verify_review_inputs(req,inputs)
    with pytest.raises(ValueError,match="exact frozen call inputs"):
        source_case.tools.verify_review_inputs(req,inputs[:1])
    source_case.tools.expected.pop(str(Path(inputs[1]["path"]).resolve()))
    with pytest.raises(ValueError,match="frozen input inventory"):
        source_case.tools.verify_review_inputs(req,inputs)


def test_missing_original_stops_before_a_provider_turn(source_case):
    from watertreatment.app_server import AppServerClient
    from watertreatment.codex import ModelCallError
    from watertreatment.reading import ReadingAudit
    from watertreatment.settings import CodexSettings
    path = source_case.run / "bounded-scope.json"
    write_new_json(path,source_case.scope)
    model = CodexSettings(model="gpt-6.2-sol", reasoning_effort="max")
    client = AppServerClient(model,live=True,transport_factory=lambda *args: pytest.fail("provider started"))
    client.bind_tools(source_case.tools)
    with pytest.raises(ModelCallError,match="preflight failed"):
        client.complete(ReadingAudit,prompt="Check frozen synthetic claims",input_paths=[path],workdir=source_case.run,
            audit_root=source_case.run/"calls",identity={"citation_review":{"scope_path":str(path),"scope_file_sha256":hash_file(path)},
            "stage":"audit_0"})
    assert client.calls_started == 0 and client.transport is None
    diagnostic = json.loads(next(source_case.run.glob("source_review_preflight-*.json")).read_bytes())
    assert diagnostic["new_turn_started"] is False


def read_blocks(case, sid):
    result = case.tools.handle("wt_read_blocks", {"source_id": sid, "start_order": 1, "end_order": 1})
    assert result["success"], result


def view(case, sid="main", rectangle=None):
    result = case.tools.handle("wt_view", {"source_id": sid, "physical_page": 1,
        "scale": 1, "rectangle": rectangle})
    assert result["success"], result
    return json.loads(result["contentItems"][0]["text"])


def test_every_required_source_block_must_be_delivered_in_this_call(source_case):
    c = source_case
    begin(c)
    with pytest.raises(ValueError, match="did not re-read"):
        c.tools.source_review_proof()
    c.tools.handle("wt_inventory", {})
    c.tools.handle("wt_search", {"query": "Synthetic"})
    with pytest.raises(ValueError, match="did not re-read"):
        c.tools.source_review_proof()
    read_blocks(c, "main")
    with pytest.raises(ValueError, match="si"):
        c.tools.source_review_proof()
    read_blocks(c, "si")
    proof = c.tools.source_review_proof()
    assert verify_source_review(c.run, c.scope, proof, c.call_id)
    assert len(proof["tool_receipts"]) == len(proof["requirements"]) == 2
    assert proof["scientific_validation"] == "not_established"


def test_visual_repair_requires_full_page_not_text_crop_or_failed_view(source_case):
    c = source_case
    scope = scope_for(c.docs[:1], visual=True)
    begin(c, scope=scope)
    read_blocks(c, "main")
    bad = c.tools.handle("wt_view", {"source_id": "main", "physical_page": 1, "scale": 10})
    assert not bad["success"]
    view(c, rectangle=[0, 0, 0.2, 0.2])
    with pytest.raises(ValueError, match="missing full-page"):
        c.tools.source_review_proof()
    view(c, "si")  # A full view of the wrong source cannot satisfy the figure.
    with pytest.raises(ValueError, match="missing full-page"):
        c.tools.source_review_proof()
    data = view(c)
    proof = c.tools.source_review_proof()
    assert verify_source_review(c.run, scope, proof, c.call_id)
    assert data["source_delivery"]["image_sha256"] == hash_file(Path(data["image_path"]))
    assert len(proof["tool_receipts"]) == 4  # The failed scale request is absent.


def test_page_text_is_valid_text_delivery_but_never_visual_delivery(source_case):
    c = source_case
    scope = scope_for(c.docs[:1])
    begin(c, scope=scope)
    response = c.tools.handle("wt_page_text", {"source_id": "main", "physical_page": 1})
    assert response["success"]
    proof = c.tools.source_review_proof()
    assert verify_source_review(c.run, scope, proof, c.call_id)
    visual = scope_for(c.docs[:1], visual=True)
    assert not verify_source_review(c.run, visual, proof, c.call_id)


def test_source_receipts_cannot_be_reused_in_another_model_turn(source_case):
    c = source_case
    scope = scope_for(c.docs[:1])
    begin(c, scope=scope)
    read_blocks(c, "main")
    first = c.tools.source_review_proof()
    begin(c, scope=scope, call_id="call-source-02")
    with pytest.raises(ValueError, match="did not re-read"):
        c.tools.source_review_proof()
    assert not verify_source_review(c.run, scope, first, "call-source-02")
    read_blocks(c, "main")
    second = c.tools.source_review_proof()
    assert verify_source_review(c.run, scope, second, "call-source-02")
    c.tools.begin_query_turn()
    with pytest.raises(ValueError, match="no controller-bound"):
        c.tools.source_review_proof()


@pytest.mark.parametrize("tamper", ["request", "result", "image", "source", "bundle", "call", "scope", "proof"])
def test_registration_replays_hashes_source_identity_and_actual_delivery(source_case, tamper):
    c = source_case
    scope = scope_for(c.docs[:1], visual=True)
    begin(c, scope=scope)
    data = view(c)
    proof = c.tools.source_review_proof()
    assert verify_source_review(c.run, scope, proof, c.call_id)
    receipt = proof["tool_receipts"][0]
    if tamper in {"request", "result"}:
        from pathlib import Path
        path = Path(receipt[tamper + "_path"])
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    elif tamper == "image":
        from pathlib import Path
        Path(data["image_path"]).write_bytes(b"changed retained image")
    elif tamper == "source":
        from pathlib import Path
        Path(c.docs[0].source.path).write_bytes(b"changed original PDF")
    elif tamper == "bundle":
        path = c.run / "sources.json"
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    elif tamper == "call":
        proof["call_id"] = "call-foreign"
    elif tamper == "scope":
        scope["scope_sha256"] = "0" * 64
    else:
        proof["requirements"] = []
        proof["proof_sha256"] = digest_json({k: v for k, v in proof.items() if k != "proof_sha256"})
    assert not verify_source_review(c.run, scope, proof, c.call_id)


def test_rehashed_log_cannot_change_the_delivered_source_block(source_case):
    from pathlib import Path
    c = source_case
    begin(c)
    read_blocks(c, "main")
    read_blocks(c, "si")
    proof = c.tools.source_review_proof()
    receipt = proof["tool_receipts"][0]
    path = Path(receipt["result_path"])
    result = json.loads(path.read_bytes())
    result["data"]["blocks"][0]["text"] = "Invented source text"
    path.write_text(json.dumps(result), encoding="utf-8")
    receipt["result_sha256"] = hash_file(path)
    proof["proof_sha256"] = digest_json({k: v for k, v in proof.items() if k != "proof_sha256"})
    assert not verify_source_review(c.run, c.scope, proof, c.call_id)


def test_duplicate_or_foreign_receipts_do_not_establish_source_review(source_case):
    c = source_case
    begin(c)
    read_blocks(c, "main")
    read_blocks(c, "si")
    proof = c.tools.source_review_proof()
    repeated = copy.deepcopy(proof)
    repeated["tool_receipts"].append(repeated["tool_receipts"][0])
    assert not verify_source_review(c.run, c.scope, repeated, c.call_id)
    scope = copy.deepcopy(c.scope)
    scope["source_anchors"][0]["block_id"] = c.docs[1].blocks[0].block_id
    scope["scope_sha256"] = digest_json({k: v for k, v in scope.items() if k != "scope_sha256"})
    with pytest.raises(ValueError, match="registered source block"):
        c.tools.begin_source_review(scope, call_id="call-other")


def test_word_visual_receipt_binds_the_exact_occurrence_without_inventing_pages(tmp_path):
    from PIL import Image
    from test_workflow import synthetic_docx
    root = tmp_path / "data"
    original = root / "originals" / "support.docx"
    original.parent.mkdir(parents=True)
    synthetic_docx(original)
    doc = parse_source(original, source_id="support")
    block = next(b for b in doc.blocks if b.kind == "image")
    preview = root / "derived" / "word-image.png"
    preview.parent.mkdir()
    Image.new("RGB", (20, 15), "white").save(preview)
    run = root / "runs" / "word-source-test"
    run.mkdir(parents=True)
    bundle = {"documents": [doc.model_dump(mode="json")], "renderings": [
        {"source_id": "support", "images": [{"block_id": block.block_id, "path": str(preview)}]}]}
    write_new_json(run / "sources.json", bundle)
    tools = ReadingTools(bundle, run, max_calls=10, expected_inputs=[
        {"path": str(path), "sha256": hash_file(path)} for path in (original, preview)])
    scope = {"policy": BOUNDED_POLICY, "source_anchors": [{"evidence_id": "word-figure",
        "source_id": "support", "block_id": block.block_id, "locator": {"figure": "Fig. S1"}}]}
    scope["scope_sha256"] = digest_json(scope)
    tools.begin_query_turn()
    tools.begin_source_review(scope, call_id="call-word")
    response = tools.handle("wt_view", {"source_id": "support", "block_id": block.block_id, "scale": 1})
    assert response["success"], response
    proof = tools.source_review_proof()
    assert proof["requirements"][0]["physical_page"] is None
    assert proof["requirements"][0]["requires_full_view"]
    assert verify_source_review(run, scope, proof, "call-word")
    repeated = tools.handle("wt_view", {"source_id": "support", "block_id": block.block_id, "scale": 1})
    assert repeated["success"]
    assert json.loads(repeated["contentItems"][0]["text"])["mode"] == "cached_source_reference"
    assert tools.source_review_proof() == proof
    preview.write_bytes(b"changed frozen Word occurrence preview")
    denied = tools.handle("wt_view", {"source_id": "support", "block_id": block.block_id, "scale": 1})
    assert not denied["success"] and "changed" in denied["contentItems"][0]["text"]
