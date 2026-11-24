"""Attachment identity, source boundaries and human role provenance tests."""
from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from watertreatment.settings import Settings
from watertreatment.storage import DataStore, digest_json
from watertreatment.web_library import WebLibrary
from watertreatment.workflow import import_paper


TITLE = "Hierarchical composite architecture for simultaneous treatment and solar evaporation"
MAIN = f"{TITLE}\nResearch Authors\nhttps://doi.org/10.1234/example.01\nABSTRACT\nThis experimental report describes a final material.\n1. Introduction\nA study of the relevant system."
SI = f"Supporting Information\n{TITLE}\nResearch Authors\nExperimental details\nFigure S1. Additional sample characterization."


def pdf(path: Path, pages: list[str], *, title: str | None = None):
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    font_ref = writer._add_object(font)
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
        parts = ["BT /F1 10 Tf 20 750 Td"]
        for index, line in enumerate(text.splitlines()):
            if index:
                parts.append("0 -14 Td")
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            parts.append(f"({safe}) Tj")
        parts.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(parts).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if title:
        writer.add_metadata({"/Title": title})
    with path.open("wb") as output:
        writer.write(output)
    return path


def docx(path: Path, text: str):
    paragraphs = "".join(f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>" for line in text.splitlines())
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + paragraphs + "</w:body></w:document>")
    return path


@pytest.fixture
def library(tmp_path):
    sources = tmp_path / "papers"
    sources.mkdir()
    settings = Settings(project_root=tmp_path / "project", data_root=tmp_path / "data", source_roots=[sources])
    DataStore(settings.data_root).initialize()
    return WebLibrary(settings)


def folder(library, name="135_test_journal"):
    path = library.settings.source_roots[0] / name
    path.mkdir()
    return path


def test_main_and_multiple_si_are_automatically_linked_by_content(library):
    path = folder(library)
    pdf(path / "paper.pdf", [MAIN])
    docx(path / "attachment.docx", SI)
    pdf(path / "extra.pdf", [SI + "\nFigure S2. Different supplementary observation."])
    originals = {p.name: p.read_bytes() for p in path.iterdir()}
    paper = library.scan([str(path)])[0]
    assert paper["id"] == "paper-" + digest_json(str(path.resolve()))[:16]
    assert paper["attachment_status"] == "ready"
    assert paper["title"] == TITLE
    assert paper["doi"] == "10.1234/example.01"
    assert len(library.roles_for_import(paper["id"])) == 3
    assert [f["role"] for f in paper["files"]].count("supporting_information") == 2
    assert all(f["signals"]["sample_scope"] == "identification_sample_only" for f in paper["files"])
    assert originals == {p.name: p.read_bytes() for p in path.iterdir()}


def test_wrapped_title_skips_issn_and_license_without_absorbing_authors(library):
    path = folder(library)
    title_lines = ["Hierarchical composite architecture for simultaneous treatment",
                   "and solar evaporation with improved stability",
                   "in complex wastewater"]
    header = ("Desalination 500 (2024) 123456\nAvailable online 10 June 2024\n"
              "0011-9164/ Copyright 2024 Elsevier B.V. All rights are reserved, including text and data mining.\n"
              + "\n".join(title_lines)
              + "\nAlice Brown a,1, Daniel Green a, Bob White b,*\nCollege of Materials Research, Sample University\nARTICLE INFO\nKeywords:\nWater treatment\n")
    pdf(path / "article.pdf", [header + "ABSTRACT\nSynthetic report\n1. Introduction\nSynthetic background"])
    paper = library.scan([str(path)])[0]
    assert paper["title"] == " ".join(title_lines)
    assert "Brown" not in paper["title"] and "reserved" not in paper["title"]


@pytest.mark.parametrize("license_text", [
    "1234-567X/ Copyright 2025 Publisher. Text and data mining rights reserved.",
    "This is an open access article distributed under the Creative Commons license.",
    "Elsevier B.V. All rights reserved including text and data mining.",
    "Licensed under CC BY 4.0 with attribution required for reuse.",
])
def test_publisher_license_cannot_be_a_title_even_when_in_metadata(library, license_text):
    path = folder(library)
    pdf(path / "article.pdf", [license_text + "\n" + MAIN], title=license_text)
    assert library.scan([str(path)])[0]["title"] == TITLE


def test_verified_metadata_recovers_full_wrapped_title(library):
    path = folder(library)
    full = "Hierarchical composite architecture for simultaneous treatment and solar evaporation"
    wrapped = "Hierarchical composite architecture for\nsimultaneous treatment and\nsolar evaporation"
    pdf(path / "article.pdf", [MAIN.replace(TITLE, wrapped)], title=full)
    assert library.scan([str(path)])[0]["title"] == full


def test_stale_metadata_title_not_found_in_title_region_is_ignored(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN], title="A completely different composite for unrelated battery energy storage applications")
    assert library.scan([str(path)])[0]["title"] == TITLE


def test_si_wrapped_title_is_joined_and_inline_title_is_supported(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN])
    docx(path / "supplement.docx", "Supporting Information for\nHierarchical composite architecture for\nsimultaneous treatment and solar evaporation\nAlice Brown a, Bob White b\nExperimental Methods\nFigure S1. Characterization.")
    docx(path / "extra.docx", "Supplementary Information for " + TITLE + "\nResearch Authors\nExperimental Methods\nFigure S2. Stability.")
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] == "ready"
    assert all(file["signals"]["title"] == TITLE for file in paper["files"])


def test_filename_alone_and_word_extension_do_not_determine_role(library):
    path = folder(library)
    pdf(path / "main.pdf", ["An ordinary cover page without document structure."])
    docx(path / "supporting_information.docx", "A note without a supporting document declaration.")
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] == "needs_confirmation"
    assert all(f["role"] == "unknown" for f in paper["files"])
    with pytest.raises(ValueError, match="尚未确认"):
        library.roles_for_import(paper["id"])


def test_main_article_can_be_native_word(library):
    path = folder(library)
    docx(path / "article.docx", MAIN)
    paper = library.scan([str(path)])[0]
    assert paper["files"][0]["role"] == "main_article"
    assert paper["attachment_status"] == "ready"
    assert "physical_page" not in paper["files"][0]["signals"]["sampled_locations"][0]


def test_single_document_convention_outweighs_filename_and_preserves_human_override(library):
    path = folder(library)
    pdf(path / "si.pdf", [MAIN])
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] == "ready"
    assert paper["files"][0]["role_origin"] == "single_document_rule"
    updated = library.set_role(paper["id"], paper["files"][0]["id"], "main_article", "researcher", "The title and article sections establish this is the main article.")
    assert updated["attachment_status"] == "ready"
    assert updated["files"][0]["role_origin"] == "human"
    assert updated["source_version"] == 1  # same content and role, distinct confirmation provenance
    with sqlite3.connect(library.database) as db:
        assert db.execute("SELECT COUNT(*) FROM role_history").fetchone()[0] == 1


def test_unknown_confirmation_cannot_silently_exclude_file(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN])
    docx(path / "ambiguous.docx", "An attachment whose role remains uncertain.")
    paper = library.scan([str(path)])[0]
    ambiguous = next(f for f in paper["files"] if f["format"] == "docx")
    result = library.set_role(paper["id"], ambiguous["id"], "unknown", "researcher")
    assert result["attachment_status"] == "needs_confirmation"
    with pytest.raises(ValueError):
        library.roles_for_import(paper["id"])
    result = library.set_role(paper["id"], ambiguous["id"], "other", "researcher", "Cover note, not scientific SI.")
    assert result["attachment_status"] == "ready"
    assert library.roles_for_import(paper["id"]) == {"article.pdf": "main_article"}


def test_main_without_si_can_complete_source_registration(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN])
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] == "ready"
    assert paper["supporting_information_state"] == "not_obtained_or_not_identified"


def test_si_folder_context_alone_is_not_identity_match(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN])
    docx(path / "attachment.docx", "Supporting Information\nExperimental Methods\nFigure S1. Additional characterization.")
    paper = library.scan([str(path)])[0]
    si = next(f for f in paper["files"] if f["role"] == "supporting_information")
    assert si["identity_status"] == "folder_only"
    assert paper["attachment_status"] == "needs_confirmation"
    updated = library.set_role(paper["id"], si["id"], "supporting_information", "researcher", "Publisher supplied this file for the same DOI.")
    assert updated["attachment_status"] == "ready"


def test_strong_publisher_file_identity_can_link_titleless_si(library):
    path = folder(library)
    pdf(path / "1-s2.0-S9999999999999999-main.pdf", [MAIN])
    docx(path / "1-s2.0-S9999999999999999-mmc1.docx", "Supporting Information\nExperimental Methods\nFigure S1. Characterization.")
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] == "ready"
    assert next(f for f in paper["files"] if f["format"] == "docx")["identity_status"] == "matched"


def test_reference_doi_is_not_assigned_as_main_identity(library):
    path = folder(library)
    main = MAIN.replace("https://doi.org/10.1234/example.01\n", "")
    pdf(path / "article.pdf", [main + "\nReferences\nOther Authors. doi:10.9999/unrelated."])
    paper = library.scan([str(path)])[0]
    assert paper["doi"] is None
    assert paper["files"][0]["signals"]["doi_candidates"] == []


def test_multiple_header_dois_remain_unresolved_without_guessing(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN + "\nRelated DOI: 10.9999/unrelated"])
    paper = library.scan([str(path)])[0]
    assert paper["doi"] is None
    assert paper["files"][0]["identity_status"] == "multiple_dois_unresolved"
    assert paper["attachment_status"] == "ready"  # missing DOI is not missing main role


def test_conflicting_si_doi_is_not_silenced_by_role_confirmation(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN])
    docx(path / "attachment.docx", SI.replace("Supporting Information\n", "Supporting Information\nhttps://doi.org/10.4321/another.03\n"))
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] == "identity_conflict"
    si = next(f for f in paper["files"] if f["role"] == "supporting_information")
    confirmed = library.set_role(paper["id"], si["id"], "supporting_information", "researcher")
    assert confirmed["attachment_status"] == "identity_conflict"
    with pytest.raises(ValueError):
        library.roles_for_import(paper["id"])


def test_supplement_body_reference_doi_is_not_the_attachment_identity(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN])
    docx(path / "attachment.docx", SI + "\nReferences\nAn external method: https://doi.org/10.4321/another.03")
    paper = library.scan([str(path)])[0]
    supplement = next(f for f in paper["files"] if f["format"] == "docx")
    assert paper["attachment_status"] == "ready"
    assert supplement["signals"]["doi_candidates"] == []
    assert "10.4321/another.03" in supplement["signals"]["reference_dois"]


def test_supplement_without_declaration_requires_both_content_and_identity(library):
    path = folder(library)
    pdf(path / "1-s2.0-S1234567891234567-main.pdf", [MAIN])
    docx(path / "1-s2.0-S1234567891234567-mmc1.docx", TITLE + "\nResearch Authors\nText S1. Additional details\nFig. S1. Additional results")
    paper = library.scan([str(path)])[0]
    supplement = next(f for f in paper["files"] if f["format"] == "docx")
    assert paper["attachment_status"] == "ready"
    assert supplement["role"] == "supporting_information"
    assert supplement["role_origin"] == "paired_source_evidence"


def test_supplement_number_cannot_override_a_conflicting_header_identity(library):
    path = folder(library)
    pdf(path / "1-s2.0-S1234567891234567-main.pdf", [MAIN])
    docx(path / "1-s2.0-S1234567891234567-mmc1.docx", TITLE + "\nDOI: 10.4321/another.03\nText S1. Additional details\nFig. S1. Additional results")
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] != "ready"
    assert next(f for f in paper["files"] if f["format"] == "docx")["role"] == "unknown"


def test_auxiliary_archive_prevents_false_single_document_convention(library):
    path = folder(library)
    pdf(path / "unclear.pdf", ["An unclassified document."])
    (path / "sources.zip").write_bytes(b"Additional publisher files")
    paper = library.scan([str(path)])[0]
    assert paper["files"][0]["role"] == "unknown"
    assert paper["attachment_status"] == "needs_confirmation"


def test_pdf_surrogate_metadata_and_text_cannot_abort_identification(library, monkeypatch):
    from types import SimpleNamespace
    import pypdf
    path = folder(library)
    (path / "article.pdf").write_bytes(b"synthetic mocked PDF")
    monkeypatch.setattr(pypdf, "PdfReader", lambda _: SimpleNamespace(is_encrypted=False,
        metadata={"/Title": TITLE + "\ud835"}, pages=[SimpleNamespace(extract_text=lambda: MAIN + "\n\ud835\udc65 and isolated \ud835")]))
    paper = library.scan([str(path)])[0]
    file = paper["files"][0]
    assert paper["attachment_status"] == "ready"
    assert "\U0001d465" in file["preview_text"]
    assert "\ufffd" in file["preview_text"]
    assert file["signals"]["sample_warnings"]
    json.dumps(paper, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("marker", ["Research Article", "PAPER", "Accepted Article"])
def test_unlabelled_abstract_journal_requires_independent_primary_signals(library, marker):
    path = folder(library)
    main = marker + "\n" + MAIN.replace("ABSTRACT\n", "").replace("1. Introduction", "1 | INTRODUCTION")
    pdf(path / "article.pdf", [main])
    docx(path / "attachment.docx", SI)
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] == "ready"
    assert next(f for f in paper["files"] if f["format"] == "pdf")["signals"]["article_type_marker"]


def test_publication_header_at_end_of_physical_first_page_is_used(library):
    path = folder(library)
    first = MAIN.replace("ABSTRACT\n", "") + "\n" + "\n".join(["The first page continues."]*40) + "\nPAPER"
    pdf(path / "article.pdf", [first])
    docx(path / "attachment.docx", SI)
    assert library.scan([str(path)])[0]["attachment_status"] == "ready"


def test_publication_marker_only_on_second_page_is_not_first_page_proof(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN.replace("ABSTRACT\n", ""), "PAPER"])
    docx(path / "attachment.docx", SI)
    assert library.scan([str(path)])[0]["attachment_status"] == "needs_confirmation"


def test_letter_publication_signature_does_not_require_an_introduction(library):
    path = folder(library)
    first = MAIN.replace("1. Introduction", "Discussion") + "\nCite\nThis: Synthetic Journal 2025\nRead OnlineACCESS"
    pdf(path / "article.pdf", [first])
    docx(path / "attachment.docx", SI)
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] == "ready"
    assert next(f for f in paper["files"] if f["format"] == "pdf")["signals"]["publisher_citation_marker"]


def test_numbered_run_in_si_declaration_preserves_type_and_link(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN])
    docx(path / "attachment.docx", SI.replace("Supporting Information", "1SupportInformation"))
    assert library.scan([str(path)])[0]["attachment_status"] == "ready"


def test_html_placeholder_is_reported_as_download_failure_not_si_absence(library):
    from test_collection_recognition import declare
    path = folder(library)
    pdf(path / "main.pdf", [MAIN])
    (path / "supplement.pdf").write_text("<!DOCTYPE html><html>Access denied</html>",encoding="utf-8")
    declare(library, [path])
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] == "source_unreadable"
    assert paper["supporting_information_state"] != "author_not_provided"
    assert next(f for f in paper["files"] if f["name"] == "supplement.pdf")["format_issue"] == "html_saved_as_pdf"


def test_capitalized_wrapped_title_lines_are_not_guessed_to_be_author_names(library):
    path = folder(library)
    full = "Highly Efficient Solar-Driven Photothermal System Based on Composite Materials for Wastewater Purification"
    wrapped = "Highly Efficient Solar-Driven\nPhotothermal System Based\non Composite Materials for\nWastewater Purification"
    pdf(path / "article.pdf", [MAIN.replace(TITLE,wrapped)])
    paper = library.scan([str(path)])[0]
    assert paper["title"] == full


def test_combined_file_requires_explicit_role_and_keeps_one_source(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN, "Discussion of results", SI])
    paper = library.scan([str(path)])[0]
    file = paper["files"][0]
    assert file["status"] == "mixed"
    assert file["segments"][0]["location"]["physical_page"] == 3
    confirmed = library.set_role(paper["id"], file["id"], "main_article_with_supporting_information", "researcher", "The final section contains SI.")
    assert confirmed["attachment_status"] == "ready"
    assert library.roles_for_import(paper["id"]) == {"article.pdf": "main_article_with_supporting_information"}
    assert len(confirmed["files"]) == 1


@pytest.mark.parametrize("notice_page", [0, 2])
def test_main_si_availability_notice_is_not_mistaken_for_combined_content(library, notice_page):
    path = folder(library)
    notice = "Supporting Information\nSupporting files are available free of charge at the publisher website."
    pages = [MAIN, "Results and discussion", "References"]
    pages[notice_page] += "\n" + notice
    pdf(path / "article.pdf", pages)
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] == "ready"
    assert paper["files"][0]["role"] == "main_article"
    assert paper["files"][0]["segments"] == []


def test_exact_duplicates_not_imported_twice_and_files_kept(library):
    path = folder(library)
    first = pdf(path / "main.pdf", [MAIN])
    (path / "copy.pdf").write_bytes(first.read_bytes())
    paper = library.scan([str(path)])[0]
    assert len(paper["files"]) == 2
    assert len(library.roles_for_import(paper["id"])) == 1
    assert next(f for f in paper["files"] if f["status"] == "duplicate")["name"] == "copy.pdf"
    assert len(list(path.iterdir())) == 2


def test_excluding_preferred_copy_recomputes_duplicate_identity(library):
    path = folder(library)
    first = pdf(path / "main.pdf", [MAIN])
    (path / "copy.pdf").write_bytes(first.read_bytes())
    paper = library.scan([str(path)])[0]
    original = next(f for f in paper["files"] if f["name"] == "main.pdf")
    updated = library.set_role(paper["id"], original["id"], "other", "researcher", "Use the other identical copy.")
    assert updated["attachment_status"] == "ready"
    assert library.roles_for_import(paper["id"]) == {"copy.pdf": "main_article"}
    chosen = next(f for f in updated["files"] if f["name"] == "copy.pdf")
    assert "duplicate_of" not in chosen


def test_changed_file_does_not_inherit_human_role(library):
    path = folder(library)
    first = pdf(path / "si.pdf", [MAIN])
    paper = library.scan([str(path)])[0]
    file_id = paper["files"][0]["id"]
    library.set_role(paper["id"], file_id, "main_article", "researcher")
    pdf(first, ["Different unclassified contents after download replacement."])
    with pytest.raises(ValueError, match="内容已变化"):
        library.set_role(paper["id"], file_id, "main_article", "researcher")
    with pytest.raises(ValueError, match="内容已变化"):
        library.roles_for_import(paper["id"])
    updated = library.scan([str(path)])[0]
    assert updated["files"][0]["role"] == "main_article"
    assert updated["files"][0]["role_origin"] == "single_document_rule"
    assert updated["source_version"] == 2
    assert "confirmed_by" not in updated["files"][0]


def test_new_attachment_invalidates_import_inventory(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN])
    paper = library.scan([str(path)])[0]
    docx(path / "added.docx", SI)
    with pytest.raises(ValueError, match="清单已变化"):
        library.roles_for_import(paper["id"])


def test_scan_is_root_scoped_and_rejects_nested_or_traversal(library, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    pdf(outside / "article.pdf", [MAIN])
    with pytest.raises(ValueError):
        library.scan([str(outside)])
    path = folder(library)
    nested = path / "nested"
    nested.mkdir()
    pdf(nested / "article.pdf", [MAIN])
    with pytest.raises(ValueError, match="直接文献文件夹"):
        library.scan([str(nested)])
    with pytest.raises(ValueError, match="路径跳转"):
        library.scan([str(path / ".." / path.name)])


def test_symbolic_attachment_rejected_even_inside_allowed_root(library, monkeypatch):
    path = folder(library)
    linked = pdf(path / "article.pdf", [MAIN])
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == linked or original(p))
    with pytest.raises(ValueError, match="符号链接"):
        library.scan([str(path)])


def test_unreadable_and_legacy_files_not_accepted_from_filename(library):
    path = folder(library)
    pdf(path / "main.pdf", [""])
    (path / "si.doc").write_bytes(b"opaque legacy word")
    paper = library.scan([str(path)])[0]
    assert {f["status"] for f in paper["files"]} == {"unreadable", "unsupported"}
    assert paper["attachment_status"] == "source_unreadable"


def test_registered_import_seed_preserves_ids_and_ignores_other_papers(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN])
    docx(path / "si.docx", "Supporting Information\nExperimental Methods\nFigure S1. Characterization.")
    another = folder(library, "999_unread")
    pdf(another / "main.pdf", [MAIN.replace(TITLE, "An entirely different final material for chemical energy generation")])
    run = import_paper(library.settings, path, paper_id="registered-study", source_roles={"article.pdf": "main_article", "si.docx": "supporting_information"})
    bundle = json.loads((run / "sources.json").read_text(encoding="utf-8"))
    DataStore(library.settings.data_root).register_cohort("registered-study", [d["source"]["sha256"] for d in bundle["documents"]], "development")
    library.sync_registered_runs()
    paper = library.get("registered-study")
    assert paper["attachment_status"] == "ready"
    assert paper["cohort_role"] == "development"
    assert paper["registered_run"] == run.name
    assert len(library.list_papers()) == 1
    library.sync_registered_runs()
    assert len(library.list_papers()) == 1
    assert library.get("registered-study")["source_version"] == 1


def test_scan_preserves_registered_custom_id(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN])
    import_paper(library.settings, path, paper_id="custom-paper-id", source_roles={"article.pdf": "main_article"})
    assert library.scan([str(path)])[0]["id"] == "custom-paper-id"


def test_rescan_preserves_content_bound_registered_roles(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN])
    docx(path / "si.docx", "Supporting Information\nExperimental Methods\nFigure S1. Characterization.")
    import_paper(library.settings, path, paper_id="registered-study", source_roles={"article.pdf": "main_article", "si.docx": "supporting_information"})
    library.sync_registered_runs()
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] == "ready"
    assert library.roles_for_import(paper["id"])["si.docx"] == "supporting_information"
    docx(path / "si.docx", "Unclassified replacement document.")
    changed = library.scan([str(path)])[0]
    assert changed["attachment_status"] == "needs_confirmation"
    assert next(f for f in changed["files"] if f["name"] == "si.docx")["role"] == "unknown"


def test_year_in_experimental_narrative_is_not_publication_metadata(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN + "\nThe equipment was first constructed in 2024."])
    assert library.scan([str(path)])[0]["year"] is None


def test_sync_never_trusts_changed_original_as_registered_source(library):
    path = folder(library)
    original = pdf(path / "article.pdf", [MAIN])
    import_paper(library.settings, path, paper_id="old-version", source_roles={"article.pdf": "main_article"})
    pdf(original, ["Replaced file contents"])
    library.sync_registered_runs()
    paper = library.get("old-version")
    assert paper["attachment_status"] == "source_unavailable"
    assert paper["files"][0]["status"] == "stale_source"
    with pytest.raises(ValueError):
        library.roles_for_import(paper["id"])


def test_no_side_effects_when_listing_and_empty_scan(library):
    assert library.list_papers() == []
    assert library.scan([]) == []
    with pytest.raises(KeyError):
        library.get("missing-study")


def test_role_confirmation_requires_identity_and_scoped_file(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN])
    paper = library.scan([str(path)])[0]
    with pytest.raises(ValueError):
        library.set_role(paper["id"], paper["files"][0]["id"], "main_article", " ")
    with pytest.raises(KeyError):
        library.set_role(paper["id"], "file-unrelated", "main_article", "researcher")
    with pytest.raises(ValueError):
        library.set_role(paper["id"], paper["files"][0]["id"], "arbitrary", "researcher")
