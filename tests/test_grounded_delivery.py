import copy
import re
from types import SimpleNamespace

from test_record_patch import patch_case, data
from watertreatment.observation_evidence import _quote_location
from watertreatment.source_delivery import compact_source_documents, project_source_scope
from watertreatment.source_tables import table_rows
from watertreatment.sources import SourceDocument, W


def test_oversize_word_blocks_keep_every_locator_metadata_value_and_literal_id(monkeypatch):
    import json
    from watertreatment import source_delivery
    raw = {"source": {"source_id": "si", "format": "docx"}, "blocks": []}
    for i in range(1, 501):
        raw["blocks"].append({"block_id": f"verbatim-alias-{i}", "kind": "paragraph", "order": i,
            "text": f"实测 {i} µg/L" if i % 2 else "", "metadata": {"uncertain_symbol": i % 3},
            "location": {"part": "word/document.xml", "row_index": i, "cell_index": 2},
            "phase": "unknown" if i % 4 else "parsed_unread"})
    scope = {"source_representation": [raw]}
    original = copy.deepcopy(scope)
    # Pick a ceiling between the literal and row-encoded full-audit envelopes.
    monkeypatch.setattr(source_delivery, "MAX_PROMPT_BYTES", 400_000)
    projected, receipt, context, preflight = source_delivery.prepare_active_source(
        scope, comprehension_rules="read", extraction_rules="extract", audit_rules="audit")
    assert scope == original and preflight["planned_bytes_before_word_block_rows"]["full_audit"] > 400_000
    assert preflight["within_budget"] and receipt["word_block_rows"]["source_ids"] == ["si"]
    assert json.loads(context.split("\nINPUT MANIFEST (metadata, not scientific evidence):\n")[-1]) == projected
    doc = projected["source_representation"][0]
    restored = []
    for idx, order, ident, text, meta, *values in doc["word_block_rows"]:
        layout = doc["word_block_layouts"][idx]
        block = {"order": order, "block_id": doc["block_id_prefix"] + ident, "text": text, "kind": layout["kind"]}
        loc = dict(zip(layout["location_fields"], values, strict=True))
        if "part" in layout:
            loc["part"] = layout["part"]
        if loc:
            block["location"] = loc
        if meta is not None:
            block["metadata"] = doc["word_block_metadata"][meta]
        if "phase" in layout:
            block["phase"] = layout["phase"]
        restored.append(block)
    assert restored == compact_source_documents([raw])[0]["blocks"]


def test_word_markup_roundtrip_preserves_scientific_xml_and_reserved_source_tokens():
    style = '<w:rPr><w:rFonts ascii="Cambria Math"/><w:sz val="24"/></w:rPr>'
    xml = f"<m:f><m:num>{style}<m:t>α</m:t></m:num><m:den>{style}<m:t>2</m:t></m:den></m:f>"
    raw = {"source": {"format": "docx", "source_id": "si"}, "blocks": [
        {"block_id": f"b{i}", "kind": "equation", "order": i, "text": "α2", "metadata": {"xml": xml}}
        for i in range(1, 21)]}
    scope = {"source_representation": [raw]}
    original = copy.deepcopy(scope)
    view, receipt = project_source_scope(scope)
    word = view["source_representation"][0]
    literals = word.pop("word_xml_literals")
    assert receipt["block_count"] == 20 and scope == original
    for block in word["blocks"]:
        encoded = block["metadata"]["xml"]
        assert "<m:num>" in encoded and "<m:den>" in encoded
        block["metadata"]["xml"] = re.sub(r"&wtLiteral[0-9]+;", lambda m: literals[m.group()], encoded)
    assert [word] == compact_source_documents([raw])
    raw["blocks"][0]["metadata"]["xml"] += "&wtLiteral0;"
    assert project_source_scope(scope)[0]["source_representation"] == compact_source_documents([raw])


def test_table_rows_keep_physical_ownership_merges_and_context():
    doc = SourceDocument.model_validate({"source": {"source_id": "si", "format": "docx", "path": "unused",
        "size_bytes": 0, "sha256": "0" * 64}})
    from watertreatment.sources import SourceLocation
    loc = SourceLocation(part="word/document.xml")
    doc.add_block("paragraph", text="Table A", location=loc)
    tableloc = loc.model_copy(update={"table_index": 1})
    table = doc.add_block("table", location=tableloc, metadata={"row_count": 3})
    for row, text in enumerate(["Header", "Other study", "This study"], 1):
        cell = tableloc.model_copy(update={"row_index": row, "cell_index": 1})
        props = f'<w:tcPr xmlns:w="{W}"><w:gridSpan w:val="2"/></w:tcPr>'
        doc.add_block("table_cell", location=cell, metadata={"properties_xml": props})
        doc.add_block("paragraph", text=text, location=cell)
    doc.add_block("paragraph", text="Footnote", location=loc)
    result = table_rows(doc, table.block_id, 3, 1)
    assert result["next_row"] is None and result["row_count"] == 3
    assert [(r["row"], r["cells"][0]["content"][0]["text"]) for r in result["rows"]] == [(1, "Header"), (3, "This study")]
    assert result["rows"][1]["cells"][0]["layout"] == {"gridSpan": "2"}
    assert result["nearby_before"][0]["text"] == "Table A" and result["nearby_after"][0]["text"] == "Footnote"


def test_context_query_binds_owner_and_source_and_rechecks_bytes(patch_case):
    tools = patch_case.tools
    binding = tools.bind_candidate(patch_case.record, patch_case.original_path)
    args = {"section": "observations", "ids": ["rate"], "with_source_context": True}
    result = data(tools.handle("wt_query_candidate", args))
    assert tools.has_candidate_read(binding) and not tools.has_candidate_paths_read(binding, [["observations", 0, "value"]])
    assert result["items"][0] == patch_case.record.observations[0].model_dump(mode="json")
    context = result["source_context"]
    assert [e["id"] for e in context["candidate_experiments"]] == ["joint"]
    anchor = context["source_anchors"][0]
    a, b = anchor["quote_span"]
    assert patch_case.source.read_text("utf-8")[a:b] == "rate 2.4"
    assert anchor["quote_status"] == "unique_literal_match"
    assert not tools.handle("wt_query_candidate", {**args, "ids": ["absent"]})["success"]
    assert not tools.handle("wt_query_candidate", {**args, "fields": ["value"]})["success"]
    patch_case.source.write_text("different", encoding="utf-8")
    assert not tools.handle("wt_query_candidate", args)["success"]


def test_unaligned_and_ambiguous_quotes_do_not_get_a_false_span():
    block = SimpleNamespace(text="aaaa")
    assert _quote_location("aaa", block) == {"quote_status": "multiple_literal_matches"}
    assert _quote_location("b", block) == {"quote_status": "no_literal_match"}
    assert _quote_location(None, block) == {"quote_status": "no_quote"}


def test_no_quote_delivers_original_block_once_and_preserves_read_requirements(patch_case):
    from watertreatment.observation_evidence import observation_context
    rec = patch_case.record.model_copy(deep=True)
    obs = rec.observations[0]
    ref = rec.evidence[0]
    ref.quote = None
    duplicate = ref.model_copy(update={"id": "other-citation"})
    rec.evidence.append(duplicate)
    obs.evidence_ids.append(duplicate.id)
    doc = patch_case.tools.documents[ref.source_id]
    block = next(b for b in doc.blocks if b.block_id == ref.block_id)
    items = [obs.model_dump(mode="json")]
    context = observation_context(rec, items, {ref.source_id: doc}, lambda sid: doc)
    assert context["cited_block_texts"] == {ref.source_id: {ref.block_id: block.text}}
    assert all("quote_span" not in anchor for anchor in context["source_anchors"])
    block.text = "水" * (24 * 1024)
    large = observation_context(rec, items, {ref.source_id: doc}, lambda sid: doc)
    assert large["cited_block_texts"] == {}
    assert {anchor["candidate_evidence"]["id"] for anchor in large["source_anchors"]} == {ref.id, duplicate.id}


def test_web_reaudit_uses_its_frozen_scientific_rule_for_arm_tools(tmp_path):
    from watertreatment.reading_tools import ReadingTools

    (tmp_path / "plan.json").write_text('{"scientific_plan":{"rule_version":"0.9"}}', encoding="utf-8")
    tools = ReadingTools({"documents": []}, tmp_path, max_calls=4, expected_inputs=[])
    assert tools.rule_version == "0.9"
