"""Offline delivery regressions: preserve sources, fail before model execution."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_active_reading import active
from test_workflow import workspace
from watertreatment.app_server import _bound_candidate_prompt
from watertreatment.codex import ModelCallError
from watertreatment.source_delivery import (
    ADAPTER_RESERVE_BYTES, MAX_PROMPT_BYTES, compact_source_documents,
    project_source_scope, prompt_budget_preflight, utf8_size,
)
from watertreatment.storage import hash_file
from watertreatment.workflow import prepare


def test_compact_delivery_keeps_literal_text_table_math_images_and_unknown_metadata():
    document = {"source": {"source_id": "word", "role": "supporting_information", "sha256": "a" * 64,
                            "format": "docx", "page_count": None, "metadata": {}},
                "phase": "parsed_unread", "issues": [{"code": "unparsed_shape", "message": "Cannot reconstruct shape"}],
                "coverage": [{"phase": "unreadable", "details": "Embedded equation could not be decoded"}],
                "blocks": []}
    values = [("paragraph", "浓度 10 mg/L\n", {}), ("table_cell", "", {"properties_xml": "<gridSpan val='2'/>"}),
              ("math", "k=t−1", {"xml": "<math>k=t−1</math>", "text_is_lossy_projection": True}),
              ("image", "", {"occurrence": 2, "crop": {"top": 0.1}, "future_scientific_field": "retained"})]
    for index, (kind, text, metadata) in enumerate(values, 1):
        document["blocks"].append({"block_id": f"word:b{index}", "source_id": "word", "kind": kind,
            "order": index, "text": text, "phase": "parsed_unread",
            "location": {"physical_page": None, "part": "word/document.xml", "table_index": 1,
                         "row_index": 2, "cell_index": index, "xml_path": f"/word/p/{index}"},
            "metadata": {"paragraph_anchor": f"/word/p/{index}", "has_omml": False,
                         "is_current_text": True, **metadata}})
    original = copy.deepcopy(document)
    view = compact_source_documents([document])[0]
    assert document == original
    assert [(b["block_id"], b["text"], b["order"]) for b in view["blocks"]] == [
        (b["block_id"], b["text"], b["order"]) for b in document["blocks"]]
    assert view["blocks"][1]["location"]["row_index"] == 2
    assert view["blocks"][1]["metadata"]["properties_xml"] == "<gridSpan val='2'/>"
    assert view["blocks"][2]["metadata"]["xml"] == "<math>k=t−1</math>"
    assert view["blocks"][3]["metadata"]["future_scientific_field"] == "retained"
    assert view["blocks"][3]["metadata"]["crop"] == {"top": 0.1}
    assert view["parse_coverage_limitations"] == document["coverage"] and view["issues"] == document["issues"]
    assert all("xml_path" not in b["location"] and "physical_page" not in b["location"] for b in view["blocks"])


def test_active_prepare_freezes_projection_and_retains_original_tool_sources(active):
    _, run, bundle, plan = active
    before_hash = hash_file(run / "sources.json")
    context = (run / "reading_context.txt").read_text(encoding="utf-8")
    scope = json.loads(context.split("INPUT MANIFEST (metadata, not scientific evidence):\n", 1)[1])
    raw_blocks = [(d["source"]["source_id"], b["block_id"], b["text"]) for d in bundle["documents"] for b in d["blocks"]]
    delivered = [(d["source"]["source_id"], b["block_id"], b["text"]) for d in scope["source_representation"] for b in d["blocks"]]
    assert raw_blocks == delivered
    assert scope["attached_image_index"] and scope["rendering_inventory"]
    assert plan["source_projection"]["block_count"] == len(raw_blocks)
    assert plan["prompt_budget_preflight"]["within_budget"]
    report = run / "prompt_budget_preflight.json"
    assert {"path": str(report), "sha256": hash_file(report)} in plan["reading_artifacts"]
    assert hash_file(run / "sources.json") == before_hash
    from test_active_reading import toolset
    source = next(d for d in bundle["documents"] if d["source"]["format"] == "docx")
    response = toolset(active).handle("wt_read_blocks", {"source_id": source["source"]["source_id"],
                                                       "start_order": 1, "end_order": 1})
    assert response["success"] and "xml_path" in response["contentItems"][0]["text"]


def test_preflight_reserves_later_audit_before_comprehension_can_spend_time():
    report = prompt_budget_preflight(context="S" * 640000, comprehension_rules="understand",
                                    extraction_rules="extract", audit_rules="audit")
    assert report["stages"]["comprehension"]["within_budget"]
    assert not report["stages"]["full_audit"]["within_budget"]
    assert not report["within_budget"]


def test_oversize_prepare_retains_preflight_failure_without_starting_a_call(active, monkeypatch):
    ws, old_run, _, _ = active
    manifest = json.loads((old_run / "manifest.json").read_text(encoding="utf-8"))
    import watertreatment.workflow as workflow
    original = workflow.scientific_prompt
    monkeypatch.setattr(workflow, "scientific_prompt", lambda settings, name: "汉" * 300000
                        if name == "audit-reading" else original(settings, name))
    with pytest.raises(ValueError, match="before model execution"):
        prepare(ws.settings, Path(manifest["source_bundle"]), cohort_role="development")
    new_runs = [p.parent for p in ws.data.glob("runs/*/prompt_budget_preflight.json") if p.parent != old_run]
    assert len(new_runs) == 1
    failed = new_runs[0]
    assert not json.loads((failed / "prompt_budget_preflight.json").read_text(encoding="utf-8"))["within_budget"]
    assert (failed / "sources.json").exists() and not (failed / "calls").exists()


def test_candidate_allocation_accounts_for_source_and_view_utf8(monkeypatch):
    record = SimpleNamespace(model_dump_json=lambda **_: "EXACT_CANDIDATE")
    caps = []
    def view(_record, _binding, *, max_chars):
        caps.append(max_chars)
        return "界" * max_chars
    monkeypatch.setattr("watertreatment.candidate_view.candidate_view", view)
    source = "汉" * 200000
    result = _bound_candidate_prompt(source + "EXACT_CANDIDATE", record, {"content_hash": "a" * 64})
    assert result.startswith(source) and len(caps) > 1
    assert utf8_size(result) + ADAPTER_RESERVE_BYTES <= MAX_PROMPT_BYTES
    with pytest.raises(ModelCallError, match="base=840003"):
        _bound_candidate_prompt("汉" * 280001 + "EXACT_CANDIDATE", record, {"content_hash": "a" * 64})
