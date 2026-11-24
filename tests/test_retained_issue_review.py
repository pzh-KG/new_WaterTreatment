from types import SimpleNamespace

import pytest

from watertreatment.domain import Component, Configuration, Fact
from watertreatment.ownership_audit import configuration_context_changed, check_contextual_edits
from watertreatment.partial_audit_completion_workflow import delivered_table_captions
from watertreatment.reading import SourceAnchor
from watertreatment.retained_review_packet import LocalReview, finish
from watertreatment.scientific_revision_audit import ScientificScopeRejected
from watertreatment.sources import SourceDocument, SourceFile, SourceLocation
from watertreatment.word_review_links import figure_caption_for_occurrences
from test_scientific_revision_audit import fixture


def native_doc():
    return SourceDocument(source=SourceFile(source_id="si", path="example.docx",
        sha256="c" * 64, size_bytes=1, format="docx"))


def test_figure_context_follows_the_actual_occurrence():
    doc = native_doc()
    part = "word/document.xml"
    def append(kind, index, text="", **metadata):
        doc.add_block(kind, text=text, location=SourceLocation(part=part, paragraph_index=index),
                      metadata=metadata)
        return doc.blocks[-1]
    parent = append("paragraph", 1)
    image = append("image", 1, parent_block_id=parent.block_id)
    cap = append("paragraph", 2, "Figure S2. Control response.")
    assert figure_caption_for_occurrences(doc, [image], cap)
    from watertreatment.word_review_links import linked_structure
    def matched(anchors=(), visible=True, enabled=True):
        return linked_structure(doc, cap, anchors, {image.block_id} if visible else set(),
                                lambda _: None, use_document_order=enabled)
    assert matched()["full_view_block_ids"] == [image.block_id]
    assert matched()["block_ids"] == [cap.block_id]
    assert matched(visible=False) is None and matched(enabled=False) is None
    assert matched([{"block_id": image.block_id, "locator": {"figure": "S3"}}]) is None
    assert not figure_caption_for_occurrences(doc, [image], append("paragraph", 3, "Figure S3. Other panel."))
    assert not figure_caption_for_occurrences(doc, [image], cap.model_copy(update={"text":"Table S2. Different object."}))


def test_apparatus_clarification_cannot_hide_changed_measurement():
    before, _, _ = fixture()
    before.configurations = [Configuration(id="apparatus",
        boundary=Fact(status="reported", value="assembly", evidence_ids=["text"]))]
    corrected = before.model_copy(deep=True)
    corrected.configurations[0].boundary.value = "assembly with external collector"
    old = before.model_dump(mode="json")
    new = corrected.model_dump(mode="json")
    assert configuration_context_changed(old, new)
    check_contextual_edits(old, new)
    corrected.observations[0].value.value = 100
    with pytest.raises(ScientificScopeRejected):
        check_contextual_edits(old, corrected.model_dump(mode="json"))


def test_cumulative_apparatus_review_preserves_existing_typed_role_edits():
    old, _, _ = fixture()
    old.materials[0].components = [Component(substance_id="synthetic", roles=[Fact(value="absorber", status="reported", evidence_ids=["text"])])]
    old.configurations = [Configuration(id="device", boundary=Fact(value="collector", status="reported", evidence_ids=["text"]))]
    new = old.model_copy(deep=True)
    new.materials[0].components[0].roles[0].note = "Author-assigned role with a limited control."
    new.configurations[0].boundary.value = "separate collector"
    a, b = old.model_dump(mode="json"), new.model_dump(mode="json")
    check_contextual_edits(a, b, include_existing_interpretations=True)
    new.materials[0].components[0].roles[0].value = "oxidant"
    with pytest.raises(ScientificScopeRejected):
        check_contextual_edits(a, new.model_dump(mode="json"), include_existing_interpretations=True)


def test_secondary_caption_needs_delivery_quote_and_same_original_table():
    doc = native_doc()
    part = "word/document.xml"
    doc.add_block("paragraph", text="Table S2. Duration in hours.",
                  location=SourceLocation(part=part, paragraph_index=2))
    doc.add_block("table", location=SourceLocation(part=part, table_index=1))
    doc.add_block("table_cell", text="3", location=SourceLocation(part=part, table_index=1, row_index=1, cell_index=1))
    cap, _, cell = doc.blocks
    pair = {"source_id":"si", "block_id":cap.block_id}
    full = {"source_anchors":[{"source_id":"si", "block_id":cell.block_id}]}
    parent = SimpleNamespace(full_scope=full, documents=[doc])
    finding = SimpleNamespace(needs_revision=False, verdict="supported", evidence=[SourceAnchor(**pair, quote="Duration in hours")])
    report = SimpleNamespace(findings=[finding])
    assert delivered_table_captions(parent, report, {"delivered":[pair]}) == {("si",cap.block_id)}
    with pytest.raises(ValueError, match="delivered"):
        delivered_table_captions(parent, report, {"delivered":[]})
    finding.evidence[0].quote = "minutes"
    with pytest.raises(ValueError, match="quote"):
        delivered_table_captions(parent, report, {"delivered":[pair]})
    finding.evidence[0].quote = "Duration in hours"
    full["source_anchors"] = [{"source_id":"si","block_id":"foreign"}]
    with pytest.raises(ValueError, match="frozen review"):
        delivered_table_captions(parent, report, {"delivered":[pair]})


def test_secondary_figure_caption_needs_its_cited_original_drawing_and_full_view():
    doc = native_doc()
    part = "word/document.xml"
    doc.add_block("paragraph", location=SourceLocation(part=part, paragraph_index=4))
    doc.add_block("image", location=SourceLocation(part=part, paragraph_index=4),
                  metadata={"parent_block_id": doc.blocks[0].block_id})
    doc.add_block("paragraph", text="Figure S2. Terminated at one hour.",
                  location=SourceLocation(part=part, paragraph_index=5))
    _, image, caption = doc.blocks
    figure = {"source_id":"si","block_id":image.block_id}
    cap = {"source_id":"si","block_id":caption.block_id}
    parent = SimpleNamespace(full_scope={"source_anchors":[figure]},documents=[doc])
    finding = SimpleNamespace(needs_revision=False,verdict="supported",
        evidence=[SourceAnchor(**figure),SourceAnchor(**cap,quote="one hour")])
    result = SimpleNamespace(findings=[finding])
    progress = {"delivered":[figure,cap],"full_views":[figure]}
    assert delivered_table_captions(parent,result,progress) == {("si",caption.block_id)}
    with pytest.raises(ValueError):
        delivered_table_captions(parent,result,{**progress,"full_views":[]})
    finding.evidence.pop(0)
    with pytest.raises(ValueError):
        delivered_table_captions(parent,result,progress)


@pytest.mark.parametrize("decision,reconciled,expected", [
    (["C1"], [], "explicitly reconcile"),
    (["C1"], ["F1"], "omitted local"),
    (["C1","F1","F1"], ["F1"], "duplicate"),
    (["C1","F1"], ["F1","F1"], "explicitly reconcile"),
])
def test_local_report_never_implies_an_unstated_disposition(decision, reconciled, expected):
    packet = {"packet_hash":"a", "changes":[{"id":"C1","changes":[["observations[0]",{},{}]]}],
              "findings":[{"id":"F1","required_evidence_ids":["e"],"paths":["observations[0]"]}],
              "sources":[{"id":"S1","source":"si","block":"a"}],
              "inherited_references":{"e":{"evidence":{"source_id":"si","block_id":"b"}}}}
    report = LocalReview(packet_hash="a", decisions=[{"groups":decision, "verdict":"supported",
        "source_ids":["S1"], "explanation":"Examined the specific difference in the table."}], reading=[],
        reconciled_source_groups=reconciled, additional_issues=[], restrictions=[])
    with pytest.raises(ValueError, match=expected):
        finish(SimpleNamespace(), None, packet, report)
