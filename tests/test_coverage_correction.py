"""Coverage receipts preserve epistemic states, not merely completed counts."""
import copy

import pytest

from watertreatment.coverage_correction import (CoverageCorrectionReport, CoverageDisposition,
    CoverageScientificConcern, build_coverage_correction_scope, corrected_dossier, coverage_correction_issues,
    apply_structural_correction, verify_structural_correction)
from watertreatment.domain import CoverageEntry, ExtractionRecord, Paper
from watertreatment.reading import ReadingDossier, SourceAnchor
from watertreatment.reader_workflow import reading_checks
from watertreatment.sources import SourceDocument, SourceFile, SourceLocation


def fixture():
    doc = SourceDocument(source=SourceFile(source_id="si", path="synthetic.docx", sha256="a" * 64,
        size_bytes=1, format="docx"))
    doc.add_block("paragraph", text="Final material and a specified operating dose.",
        location=SourceLocation(part="word/document.xml", xml_path="/body/p[1]"))
    doc.add_block("paragraph", location=SourceLocation(part="word/document.xml", xml_path="/body/p[2]"))
    doc.add_block("table_cell", location=SourceLocation(part="word/document.xml", xml_path="/body/tbl/tc"))
    doc.add_block("paragraph", text="External comparison: distinct publication.",
        location=SourceLocation(part="word/document.xml", xml_path="/body/tbl/tc/p"))
    doc.add_block("equation", text="k", metadata={"parent_block_id": doc.blocks[0].block_id, "format": "OMML"})
    doc.add_block("paragraph", metadata={"has_revisions": True})
    dossier = ReadingDossier(paper_id="paper", article_question_and_design="Synthetic design",
        material_map=[], experiment_map=[], coverage=[CoverageEntry(source_id="si",
            block_ids=[doc.blocks[0].block_id, "invalid-placeholder"], state="read_structured", text_read=True)],
        cross_source_conflicts=["Retained source conflict"], unresolved_questions=["Retained source limitation"], schema_gaps=[])
    return dossier, [doc]


def report_for(scope, docs):
    blocks = {b.block_id: b for d in docs for b in d.blocks}
    entries = []
    for item in scope["required_dispositions"]:
        b = blocks[item["block_id"]]
        entries.append(CoverageDisposition(source_id=item["source_id"], block_ids=[b.block_id],
            state="out_of_scope", text_read=bool(b.text), visual_checked=False, category="bibliographic",
            note="The named source content was read and excluded from this study's experiment data.",
            evidence=[SourceAnchor(source_id=item["source_id"], block_id=b.block_id, quote=b.text)]))
    return CoverageCorrectionReport(scope_sha256=scope["scope_sha256"], dispositions=entries)


def test_invalid_identifier_does_not_discard_other_valid_entries():
    dossier, docs = fixture()
    candidate = ExtractionRecord(rule_version="0.5", paper=Paper(id="paper", source_ids=["si"]),
        coverage=[CoverageEntry(source_id="si", block_ids=[b.block_id for b in docs[0].blocks])])
    issues = reading_checks(dossier, candidate, None, docs)
    assert "dossier coverage references unknown source/blocks." in issues
    assert any("for 5 blocks" in item for item in issues)
    assert not any("for 6 blocks" in item for item in issues)


def test_only_proven_empty_structural_blocks_are_automatic():
    dossier, docs = fixture()
    scope = build_coverage_correction_scope(dossier, docs)
    automatic = {x["block_id"]: x for x in scope["structural_dispositions"]}
    assert set(automatic) == {docs[0].blocks[1].block_id, docs[0].blocks[2].block_id}
    assert all(not x["text_read"] and not x["visual_checked"] and x["state"] == "out_of_scope" for x in automatic.values())
    required = {x["block_id"] for x in scope["required_dispositions"]}
    assert docs[0].blocks[3].block_id in required  # Nonempty table text.
    assert docs[0].blocks[4].block_id in required  # A symbol is not auto-read.
    assert docs[0].blocks[5].block_id in required  # Deleted/revised content is not a blank.


def test_empty_image_wrapper_does_not_assert_its_child_image_was_read():
    dossier, docs = fixture()
    parent = docs[0].add_block("paragraph")
    child = docs[0].add_block("image", metadata={"parent_block_id": parent.block_id})
    scope = build_coverage_correction_scope(dossier, docs)
    assert parent.block_id in {x["block_id"] for x in scope["structural_dispositions"]}
    assert child.block_id in {x["block_id"] for x in scope["required_dispositions"]}


def ready():
    dossier, docs = fixture()
    # Keep the tracked-revision placeholder explicitly unavailable in the old
    # dossier; the report still has substantive text and equation targets.
    dossier.coverage.append(CoverageEntry(source_id="si", block_ids=[docs[0].blocks[5].block_id], state="unreadable"))
    scope = build_coverage_correction_scope(dossier, docs)
    return dossier, docs, scope, report_for(scope, docs)


def test_correction_is_new_version_and_preserves_scientific_notes():
    dossier, docs, scope, report = ready()
    original = dossier.model_dump(mode="json")
    assert coverage_correction_issues(scope, report, dossier, docs) == []
    corrected = corrected_dossier(scope, report, dossier, docs)
    assert dossier.model_dump(mode="json") == original
    assert corrected.cross_source_conflicts == dossier.cross_source_conflicts
    assert corrected.unresolved_questions == dossier.unresolved_questions
    assert {bid for e in corrected.coverage for bid in e.block_ids} == {b.block_id for b in docs[0].blocks}
    assert "invalid-placeholder" not in {bid for e in corrected.coverage for bid in e.block_ids}
    assert scope["invalid_identifiers"][0]["block_id"] == "invalid-placeholder"


@pytest.mark.parametrize("defect", ["omitted", "duplicate", "other_source", "quote", "unread", "no_declaration", "science", "scope"])
def test_candidate_or_delivery_claim_cannot_clear_semantic_disposition(defect):
    dossier, docs, scope, report = ready()
    if defect == "omitted": report.dispositions.pop()
    elif defect == "duplicate": report.dispositions.append(report.dispositions[0].model_copy(deep=True))
    elif defect == "other_source": report.dispositions[0].source_id = "invented"
    elif defect == "quote": report.dispositions[0].evidence[0].quote = "Invented statement"
    elif defect == "unread": report.dispositions[0].state = "parsed_unread"
    elif defect == "no_declaration": report.dispositions[0].text_read = False
    elif defect == "science": report.scientific_concerns = [CoverageScientificConcern(source_id="si",
        block_ids=[docs[0].blocks[3].block_id], explanation="The re-read source contradicts an inherited material attribution.")]
    else: report.scope_sha256 = "0" * 64
    assert coverage_correction_issues(scope, report, dossier, docs)


def test_hash_binding_rejects_changed_source_or_old_dossier():
    dossier, docs, scope, report = ready()
    changed = copy.deepcopy(docs)
    changed[0].blocks[0].text += " changed"
    assert coverage_correction_issues(scope, report, dossier, changed)
    with pytest.raises(ValueError, match="scope changed"):
        corrected_dossier(scope, report, dossier, changed)
    dossier.unresolved_questions.append("New inherited limitation")
    assert coverage_correction_issues(scope, report, dossier, docs)


def test_fresh_workflow_structural_version_does_not_complete_substantive_gaps(tmp_path):
    from watertreatment.storage import write_new_json, hash_file
    dossier, docs = fixture()
    write_new_json(tmp_path / "reading_dossier.json", dossier.model_dump(mode="json"))
    write_new_json(tmp_path / "sources.json", {"documents": [d.model_dump(mode="json") for d in docs]})
    original_hash = hash_file(tmp_path / "reading_dossier.json")
    effective, proof = apply_structural_correction(tmp_path, dossier, docs)
    assert proof["remaining_model_dispositions"] == 3
    assert not proof["model_reading_asserted"]
    assert verify_structural_correction(tmp_path, proof, docs)
    assert hash_file(tmp_path / "reading_dossier.json") == original_hash
    covered = {bid for e in effective.coverage for bid in e.block_ids}
    assert docs[0].blocks[3].block_id not in covered
    (tmp_path / "reading_dossier.structural.v01.json").write_text("{}", encoding="utf-8")
    assert not verify_structural_correction(tmp_path, proof, docs)
