"""Source fixtures exercise provenance boundaries rather than scientific claims."""

import hashlib
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfWriter

from watertreatment.sources import parse_source, sha256_file

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"


def make_docx(path: Path, body: str, *, extra: dict[str, bytes | str] | None = None) -> Path:
    xml = f'<w:document xmlns:w="{W}" xmlns:r="{R}" xmlns:a="{A}" xmlns:m="{M}" xmlns:pic="{PIC}"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("word/document.xml", xml)
        for name, content in (extra or {}).items():
            package.writestr(name, content)
    return path


def drawing(crop: str) -> str:
    return f'<w:r><w:drawing><pic:pic><pic:blipFill><a:blip r:embed="rId7"/><a:srcRect {crop}/></pic:blipFill></pic:pic></w:drawing></w:r>'


def test_same_media_different_crops_are_distinct_occurrences(tmp_path):
    media = b"test image bytes"
    first_drawing = drawing('t="38000" b="40000"')
    second_drawing = drawing('l="32000" t="19000"')
    path = make_docx(tmp_path / "si.docx", f'<w:p>{first_drawing}</w:p><w:p>{second_drawing}</w:p>', extra={
        "word/_rels/document.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId7" Target="media/image1.png" Type="image"/></Relationships>',
        "word/media/image1.png": media,
    })
    before = path.read_bytes()
    document = parse_source(path, source_id="article:SI", source_family_id="paper1", role="supplement", output_dir=tmp_path / "images")
    images = [b for b in document.blocks if b.kind == "image"]
    assert len(images) == 2
    assert images[0].block_id != images[1].block_id
    assert images[0].location.paragraph_index == 1
    assert images[1].location.paragraph_index == 2
    assert images[0].metadata["crop"]["raw"] == {"t": "38000", "b": "40000"}
    assert images[1].metadata["crop"]["raw"] == {"l": "32000", "t": "19000"}
    assert images[0].metadata["media_sha256"] == images[1].metadata["media_sha256"] == hashlib.sha256(media).hexdigest()
    assert images[0].metadata["image_output_path"] == images[1].metadata["image_output_path"]
    assert Path(images[0].metadata["image_output_path"]).read_bytes() == media
    assert all(b.location.physical_page is None for b in document.blocks)
    assert document.source.page_count is None
    assert path.read_bytes() == before
    assert document.source.sha256 == sha256_file(path)
    assert '"parsed_unread"' in document.model_dump_json()


def test_interleaved_body_table_equation_and_revisions(tmp_path):
    path = make_docx(tmp_path / "complex.docx", '''
      <w:p><w:r><w:t>before</w:t></w:r></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
      <w:p><w:r><w:t>after </w:t></w:r><w:del w:id="4" w:author="Editor"><w:r><w:delText>old</w:delText></w:r></w:del><w:ins w:id="5"><w:r><w:t>new</w:t></w:r></w:ins>
      <m:oMath><m:f><m:num><m:r><m:t>a</m:t></m:r></m:num><m:den><m:r><m:t>b</m:t></m:r></m:den></m:f></m:oMath></w:p>
    ''')
    document = parse_source(path)
    paragraphs = [b for b in document.blocks if b.kind == "paragraph"]
    assert [b.text for b in paragraphs] == ["before", "cell", "after newab"]
    assert [b.kind for b in document.blocks[:5]] == ["paragraph", "table", "table_cell", "paragraph", "paragraph"]
    assert paragraphs[1].location.table_index == paragraphs[1].location.row_index == paragraphs[1].location.cell_index == 1
    assert paragraphs[2].location.paragraph_index == 3
    revisions = [b for b in document.blocks if b.kind == "revision"]
    assert [(b.text, b.metadata["revision_state"]) for b in revisions] == [("old", "del"), ("new", "ins")]
    assert revisions[0].metadata["author"] == "Editor"
    assert not revisions[0].metadata["is_current_text"]
    equation = next(b for b in document.blocks if b.kind == "equation")
    assert "num" in equation.metadata["xml"] and "den" in equation.metadata["xml"]
    assert equation.metadata["text_is_lossy_projection"] is True
    assert any(i.code == "tracked_changes_not_resolved" for i in document.issues)


def test_pdf_all_physical_pages_and_empty_page_unknown(tmp_path):
    path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    for _ in range(5):
        writer.add_blank_page(width=100, height=100)
    with path.open("wb") as stream:
        writer.write(stream)
    before = sha256_file(path)
    document = parse_source(path)
    assert document.source.page_count == 5
    assert [b.location.physical_page for b in document.blocks] == [1, 2, 3, 4, 5]
    assert all(b.phase == "unknown" for b in document.blocks)
    assert len([i for i in document.issues if i.code == "pdf_no_extractable_text"]) == 5
    assert all(c.phase == "unknown" for c in document.coverage if c.scope == "pdf_page_visual")
    assert before == sha256_file(path)


def test_hash_identity_stable_but_explicit_file_identity_allowed(tmp_path):
    first = make_docx(tmp_path / "main.docx", '<w:p><w:r><w:t>same</w:t></w:r></w:p>')
    second = tmp_path / "copy.docx"
    second.write_bytes(first.read_bytes())
    a, b = parse_source(first), parse_source(second)
    assert a.source.sha256 == b.source.sha256
    assert a.source.source_id == b.source.source_id
    explicit = parse_source(second, source_id="separate-attachment", source_family_id="article-7")
    assert explicit.source.source_id == "separate-attachment"
    assert explicit.source.source_family_id == "article-7"


def test_doc_unsupported_is_not_a_successful_read(tmp_path):
    path = tmp_path / "legacy.doc"
    path.write_bytes(b"legacy")
    document = parse_source(path)
    assert document.phase == "not_supported"
    assert document.blocks == []
    assert document.issues[0].code == "legacy_doc_not_supported"


def test_unsafe_xml_does_not_resolve_entities(tmp_path):
    path = tmp_path / "unsafe.docx"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("word/document.xml", f'<!DOCTYPE x [<!ENTITY leak SYSTEM "file:///secret">]><w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&leak;</w:t></w:r></w:p></w:body></w:document>')
    document = parse_source(path)
    assert document.phase == "unreadable"
    assert not document.blocks
    assert any("DTD" in i.message for i in document.issues)


@pytest.mark.parametrize("target, mode, code", [
    ("../../outside.png", "", "image_read_failed"),
    ("https://example.invalid/image.png", ' TargetMode="External"', "external_image_not_fetched"),
])
def test_image_targets_cannot_escape_or_fetch_network(tmp_path, target, mode, code):
    path = make_docx(tmp_path / "links.docx", f'<w:p>{drawing("")}</w:p>', extra={
        "word/_rels/document.xml.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId7" Target="{target}"{mode}/></Relationships>',
    })
    document = parse_source(path, output_dir=tmp_path / "images")
    assert any(i.code == code for i in document.issues)
    assert not (tmp_path / "images").exists()


def test_comments_and_footnotes_have_separate_part_locators(tmp_path):
    path = make_docx(tmp_path / "notes.docx", '<w:p><w:r><w:t>body</w:t><w:footnoteReference w:id="1"/></w:r></w:p>', extra={
        "word/footnotes.xml": f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="1"><w:p><w:r><w:t>note</w:t></w:r></w:p></w:footnote></w:footnotes>',
        "word/comments.xml": f'<w:comments xmlns:w="{W}"><w:comment w:id="2"><w:p><w:r><w:t>editor note</w:t></w:r></w:p></w:comment></w:comments>',
    })
    document = parse_source(path)
    paragraphs = [b for b in document.blocks if b.kind == "paragraph"]
    assert {b.location.part for b in paragraphs} == {"word/document.xml", "word/footnotes.xml", "word/comments.xml"}
    assert next(b for b in document.blocks if b.kind == "reference").metadata["reference_id"] == "1"
    comment = next(b for b in paragraphs if b.location.part == "word/comments.xml")
    assert comment.metadata["content_role"] == "editorial_comment"
    assert comment.metadata["container_id"] == "2"


def test_whole_deleted_paragraph_is_not_current_body_text(tmp_path):
    path = make_docx(tmp_path / "deleted.docx", '''
      <w:del w:id="8"><w:p><w:r><w:delText>deleted paragraph</w:delText></w:r></w:p></w:del>
      <w:p><w:r><w:t>current paragraph</w:t></w:r></w:p>
    ''')
    document = parse_source(path)
    paragraphs = [b for b in document.blocks if b.kind == "paragraph"]
    assert [b.text for b in paragraphs] == ["", "current paragraph"]
    assert paragraphs[0].metadata["revision_states"] == ["del"]
    deleted = next(b for b in document.blocks if b.kind == "revision")
    assert deleted.text == "deleted paragraph"
    assert deleted.metadata["inherited_from_container"]


def test_arbitrary_xml_is_not_silently_accepted_as_docx_body(tmp_path):
    path = tmp_path / "wrong-root.docx"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("word/document.xml", "<unrelated>not a Word document</unrelated>")
    document = parse_source(path)
    assert document.phase == "unreadable"
    assert any(issue.code == "docx_part_failed" for issue in document.issues)


def test_vml_crop_is_preserved_without_assuming_drawingml_units(tmp_path):
    path = make_docx(tmp_path / "vml.docx", '''
      <w:p><w:r><w:pict xmlns:v="urn:schemas-microsoft-com:vml"><v:shape>
      <v:imagedata r:id="rId7" cropleft="6553f" croptop="0.25"/>
      </v:shape></w:pict></w:r></w:p>
    ''', extra={
        "word/_rels/document.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId7" Target="media/image1.png" Type="image"/></Relationships>',
        "word/media/image1.png": b"image",
    })
    document = parse_source(path)
    image = next(block for block in document.blocks if block.kind == "image")
    crop = image.metadata["crop"]
    assert crop["specified"] is True
    assert crop["format"] == "vml"
    assert crop["unit"] == "vml_raw"
    assert crop["raw"] == {"cropleft": "6553f", "croptop": "0.25"}
    assert crop["left"] is None
    assert any(issue.code == "vml_crop_not_normalized" for issue in document.issues)
