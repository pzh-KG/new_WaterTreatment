"""Same-turn source receipts save duplicate delivery, never fabricate new reading."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from test_source_review import source_case, begin, scope_for
from watertreatment.source_review import verify_source_review
from watertreatment.storage import digest_json, hash_file


def payload(response):
    assert response["success"], response
    return json.loads(response["contentItems"][0]["text"])


REQUESTS = [
    ("wt_read_blocks", {"source_id": "main", "start_order": 1, "end_order": 1}),
    ("wt_page_text", {"source_id": "main", "physical_page": 1, "mode": "layout"}),
    ("wt_view", {"source_id": "main", "physical_page": 1, "scale": 1}),
]


@pytest.mark.parametrize("tool,args", REQUESTS)
def test_exact_repeat_is_receipt_refresh_redelivers_and_new_turn_is_fresh(source_case, tool, args):
    c = source_case
    scope = scope_for(c.docs[:1], visual=tool == "wt_view")
    begin(c, scope=scope)
    first_response = c.tools.handle(tool, args)
    first = payload(first_response)
    original_proof = c.tools.source_review_proof()
    repeated_response = c.tools.handle(tool, args)
    repeated = payload(repeated_response)
    assert repeated["mode"] == "cached_source_reference" and repeated["source_content_delivered"] is False
    assert not any(key in repeated for key in ("blocks", "text", "image_path", "source_delivery"))
    assert len(repeated_response["contentItems"]) == 1
    assert repeated["prior_delivery"]["tool_call_number"] == first["tool_call_number"]
    assert c.tools.source_review_proof() == original_proof
    assert verify_source_review(c.run, scope, original_proof, c.call_id)
    refreshed_response = c.tools.handle(tool, {**args, "refresh": True})
    refreshed = payload(refreshed_response)
    assert refreshed.get("mode") != "cached_source_reference"
    assert len(c.tools.source_review_proof()["tool_receipts"]) == 2
    if tool == "wt_view":
        assert len(first_response["contentItems"]) == len(refreshed_response["contentItems"]) == 2
        assert first["image_sha256"] == refreshed["image_sha256"]
        assert first["image_path"] != refreshed["image_path"]
    begin(c, scope=scope, call_id="call-source-next")
    again = payload(c.tools.handle(tool, args))
    assert again.get("mode") != "cached_source_reference"
    assert verify_source_review(c.run, scope, c.tools.source_review_proof(), "call-source-next")


def test_different_crop_scale_or_text_mode_is_never_merged(source_case):
    c = source_case
    view = {"source_id": "main", "physical_page": 1, "scale": 1}
    responses = [payload(c.tools.handle("wt_view", args)) for args in (
        view, {**view, "scale": 2}, {**view, "rectangle": [0, 0, 0.5, 0.5]})]
    assert all("image_path" in item for item in responses)
    assert len({item["image_path"] for item in responses}) == 3
    layout = payload(c.tools.handle("wt_page_text", {"source_id": "main", "physical_page": 1, "mode": "layout"}))
    plain = payload(c.tools.handle("wt_page_text", {"source_id": "main", "physical_page": 1, "mode": "plain"}))
    assert layout["mode"] == "layout" and plain["mode"] == "plain"
    payload(c.tools.handle("wt_read_blocks", {"source_id": "main", "start_order": 1, "end_order": 1}))
    invalid_range = c.tools.handle("wt_read_blocks", {"source_id": "main", "start_order": 1, "end_order": 20})
    assert not invalid_range["success"]  # A different range cannot reuse a valid shorter result.


@pytest.mark.parametrize("tamper", ["source", "request", "result", "image"])
def test_cached_source_never_trusts_changed_original_or_delivery_artifacts(source_case, tamper):
    c = source_case
    args = {"source_id": "main", "physical_page": 1, "scale": 1}
    first = payload(c.tools.handle("wt_view", args))
    cached = payload(c.tools.handle("wt_view", args))["prior_delivery"]
    path = Path(c.docs[0].source.path) if tamper == "source" else Path(cached[tamper + "_path"])
    path.write_bytes(path.read_bytes() + b" changed")
    response = c.tools.handle("wt_view", args)
    assert not response["success"]
    assert "changed" in response["contentItems"][0]["text"]
    assert first["image_sha256"] != hash_file(path) or tamper != "image"


def test_cache_receipt_cannot_be_forged_into_new_source_review_proof(source_case):
    c = source_case
    scope = scope_for(c.docs[:1])
    begin(c, scope=scope)
    args = {"source_id": "main", "start_order": 1, "end_order": 1}
    payload(c.tools.handle("wt_read_blocks", args))
    repeated = payload(c.tools.handle("wt_read_blocks", args))
    proof = c.tools.source_review_proof()
    folder = c.run / "reading_tools" / f"tool-{repeated['tool_call_number']:04d}"
    forged = copy.deepcopy(proof)
    forged["tool_receipts"] = [{"request_path": str(folder / "request.json"),
        "request_sha256": hash_file(folder / "request.json"), "result_path": str(folder / "result.json"),
        "result_sha256": hash_file(folder / "result.json")}]
    forged["proof_sha256"] = digest_json({k: v for k, v in forged.items() if k != "proof_sha256"})
    assert not verify_source_review(c.run, scope, forged, c.call_id)
    # The original pre-refresh API shape (no refresh key) still replays exactly.
    assert verify_source_review(c.run, scope, proof, c.call_id)


def test_source_reference_can_cross_a_contiguous_reader_handoff_only(source_case):
    c = source_case
    tool, args = REQUESTS[0]
    before = payload(c.tools.handle(tool, args))
    c.tools.begin_query_turn(keep_source_deliveries=True)
    repeated = payload(c.tools.handle(tool, args))
    assert repeated["prior_delivery"]["tool_call_number"] == before["tool_call_number"]
    assert repeated["source_content_delivered"] is False
    begin(c, scope=scope_for(c.docs[:1]))
    fresh = payload(c.tools.handle(tool, args))
    assert fresh["blocks"] == before["blocks"]
    assert len(c.tools.source_review_proof()["tool_receipts"]) == 1
