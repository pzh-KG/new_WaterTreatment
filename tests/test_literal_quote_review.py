from types import SimpleNamespace

import pytest

from watertreatment.focused_review_packet import build
from watertreatment.retained_review import _packet_version
from watertreatment.scientific_revision_audit import ScientificScopeRejected
from watertreatment.sources import SourceDocument, SourceFile, SourceLocation
from test_scientific_revision_audit import fixture


def example(monkeypatch):
    before, _, _ = fixture()
    before.rule_version = "0.9"
    source = SourceDocument(source=SourceFile(source_id="source", path="study.docx",
        sha256="b" * 64, size_bytes=1, format="docx"))
    for i, text in enumerate(("Measured signal in the control arm was 5.5 units.",
                               "A comparison signal.", "A separate experiment."), 1):
        source.add_block("paragraph", text=text, location=SourceLocation(part="word/document.xml", paragraph_index=i))
    for evidence, block in zip(before.evidence, source.blocks):
        evidence.block_id = block.block_id
    before.evidence[0].quote = "Measured signal"
    current = before.model_copy(deep=True)
    current.evidence[0].quote = source.blocks[0].text
    current.evidence[0].locator.panel = "b"
    parent = SimpleNamespace(before=before, candidate=current, documents=[source], findings=[],
        receipt={"mode":"native-repair-targets-v1"}, bundle={"renderings":[]}, plan={})
    monkeypatch.setattr("watertreatment.focused_review_packet.reading_groups", lambda parent: ({}, []))
    return parent, current


def test_literal_extension_requires_original_and_all_consumers(monkeypatch):
    parent, proposed = example(monkeypatch)
    assert _packet_version(parent) == 9
    with pytest.raises(ScientificScopeRejected):
        build(parent, proposed, version=4)
    packet, _, _ = build(parent, proposed, version=9)
    row = packet["changes"][0]
    assert row["changes"][0][0] == "evidence[0]"
    assert row["changes"][0][1]["quote"] == "Measured signal"
    assert row["changes"][0][2]["quote"] == parent.documents[0].blocks[0].text
    consumers = {p for group in packet["findings"] for p in group["paths"]}
    assert "observations[0]" in consumers and "observations[2]" not in consumers
    for group in [row, *packet["findings"]]:
        assert packet["required_group_sources"][group["id"]] == ["S1"]
    assert packet["sources"][0]["text"] == parent.documents[0].blocks[0].text


def test_unlocated_extension_or_changed_physical_reference_fails_closed(monkeypatch):
    parent, proposed = example(monkeypatch)
    proposed.evidence[0].quote += " Added without source."
    with pytest.raises(ValueError, match="original block"):
        build(parent, proposed, version=9)
    proposed.evidence[0].quote = "Entirely different claim."
    with pytest.raises(ScientificScopeRejected):
        build(parent, proposed, version=9)
    proposed.evidence[0].quote = parent.documents[0].blocks[0].text
    proposed.evidence[0].locator.paragraph_index = 12
    with pytest.raises(ScientificScopeRejected):
        build(parent, proposed, version=9)
