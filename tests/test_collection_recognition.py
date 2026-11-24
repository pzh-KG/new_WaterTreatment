"""Collection declarations affect acquisition state, never scientific extraction."""
from __future__ import annotations

import json
import sqlite3

import pytest

from test_web_library import library, folder, pdf, docx, MAIN, SI
from watertreatment.storage import hash_file


def declare(library, paths, *, policy_id="synthetic-acquisition-confirmation"):
    policy = library.settings.data_root / (policy_id + ".json")
    payload = {"version": 1, "policy_id": policy_id,
        "source_root": str(library.settings.source_roots[0]), "confirmed_by": "user",
        "confirmed_at": "2026-11-16", "statement": "Each included paper was checked; absent SI was not provided by its author.",
        "absent_si_meaning": "author_not_provided", "entries": [{"folder": str(path),
        "documents": [{"name": item.name, "sha256": hash_file(item)} for item in sorted(path.iterdir())
                      if item.suffix.casefold() in {".pdf", ".docx", ".doc"} and not item.name.startswith("~$")]}
        for path in paths]}
    policy.write_text(json.dumps(payload), encoding="utf-8")
    library.settings.collection_policy_path = policy
    return policy


def test_verified_single_main_is_ready_and_si_absence_has_user_provenance(library):
    path = folder(library)
    source = pdf(path / "unconventional-name.pdf", ["A sparse main document whose role the user has confirmed."])
    before = source.read_bytes()
    policy = declare(library, [path])
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] == "ready"
    assert paper["files"][0]["role"] == "main_article"
    assert paper["files"][0]["role_origin"] == "single_document_rule"
    assert paper["supporting_information_state"] == "author_not_provided"
    proof = paper["supporting_information_verification"]
    assert proof["confirmed_by"] == "user" and proof["manifest_sha256"] == hash_file(policy)
    assert paper["scientific_eligibility"] == "not_assessed"
    assert paper["identification_scope"] == "limited_content_sample_not_full_reading"
    assert library.roles_for_import(paper["id"]) == {source.name: "main_article"}
    assert source.read_bytes() == before
    assert not list((library.settings.data_root / "runs").iterdir())
    assert not list((library.settings.data_root / "records").iterdir())


def test_another_single_main_keeps_generic_unknown_si_state(library):
    verified = folder(library)
    pdf(verified / "main.pdf", [MAIN])
    outside = folder(library, "136_not_verified")
    pdf(outside / "main.pdf", [MAIN])
    declare(library, [verified])
    paper = library.scan([str(outside)])[0]
    assert paper["attachment_status"] == "ready"
    assert paper["files"][0]["role"] == "main_article"
    assert paper["supporting_information_state"] == "not_obtained_or_not_identified"
    assert paper["collection_verification"] is None


def test_actual_identified_si_remains_present_in_verified_collection(library):
    path = folder(library)
    pdf(path / "main.pdf", [MAIN])
    docx(path / "additional.docx", SI)
    declare(library, [path])
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] == "ready"
    assert paper["supporting_information_state"] == "present"
    assert paper["supporting_information_verification"] is None
    assert paper["collection_verification"]["confirmed_by"] == "user"


def test_single_combined_document_keeps_main_role_but_not_false_si_absence(library):
    path = folder(library)
    pdf(path / "combined.pdf", [MAIN, "Discussion of results", SI])
    declare(library, [path])
    paper = library.scan([str(path)])[0]
    assert paper["files"][0]["role"] == "main_article"
    assert paper["files"][0]["status"] == "mixed"
    assert paper["attachment_status"] == "needs_confirmation"
    assert paper["supporting_information_state"] != "author_not_provided"
    assert paper["supporting_information_verification"] is None
    with pytest.raises(ValueError):
        library.roles_for_import(paper["id"])
    confirmed = library.set_role(paper["id"], paper["files"][0]["id"],
        "main_article_with_supporting_information", "reviewer", "The last section contains SI.")
    assert confirmed["supporting_information_state"] == "present"


@pytest.mark.parametrize("kind", ["malformed_pdf", "unsupported_doc"])
def test_single_unreadable_document_role_does_not_claim_usable_sources_or_absence(library, kind):
    path = folder(library)
    source = path / ("main.pdf" if kind == "malformed_pdf" else "main.doc")
    source.write_bytes(b"Unusable or legacy file, retained as supplied.")
    declare(library, [path])
    paper = library.scan([str(path)])[0]
    assert paper["files"][0]["role"] == "main_article"
    assert paper["attachment_status"] == "source_unreadable"
    assert paper["readability_issue_count"] == 1
    assert paper["supporting_information_state"] != "author_not_provided"
    with pytest.raises(ValueError):
        library.roles_for_import(paper["id"])


@pytest.mark.parametrize("kind", ["unknown", "conflicting_si"])
def test_unresolved_multiple_files_never_become_author_not_provided(library, kind):
    path = folder(library)
    pdf(path / "main.pdf", [MAIN])
    text = "An attachment whose role cannot be established." if kind == "unknown" else SI.replace(
        "Supporting Information\n", "Supporting Information\nhttps://doi.org/10.8888/different\n")
    docx(path / "attachment.docx", text)
    declare(library, [path])
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] in {"needs_confirmation", "identity_conflict"}
    assert paper["supporting_information_state"] != "author_not_provided"
    assert paper["supporting_information_verification"] is None


@pytest.mark.parametrize("change", ["add", "remove", "change"])
def test_document_changes_expire_absence_assertion_after_rescan(library, change):
    path = folder(library)
    source = pdf(path / "main.pdf", [MAIN])
    duplicate = None
    if change == "remove":
        duplicate = path / "duplicate.pdf"
        duplicate.write_bytes(source.read_bytes())
    declare(library, [path])
    first = library.scan([str(path)])[0]
    assert first["supporting_information_state"] == "author_not_provided"
    if change == "add":
        docx(path / "new.docx", "A newly obtained unknown attachment.")
    elif change == "remove":
        duplicate.unlink()
    else:
        pdf(source, [MAIN + "\nA changed source version."])
    latest = library.scan([str(path)])[0]
    assert latest["collection_verification"] is None
    assert latest["supporting_information_state"] != "author_not_provided"
    assert latest["source_version"] > first["source_version"]


def test_new_declaration_versions_acquisition_without_modifying_sources_or_overrides(library):
    path = folder(library)
    source = pdf(path / "article.pdf", [MAIN])
    before = source.read_bytes()
    unverified = library.scan([str(path)])[0]
    confirmed = library.set_role(unverified["id"], unverified["files"][0]["id"],
        "main_article", "researcher", "Role confirmed before collection declaration.")
    assert confirmed["files"][0]["role_origin"] == "human"
    declare(library, [path])
    verified = library.scan([str(path)])[0]
    assert verified["source_version"] == confirmed["source_version"] + 1
    assert verified["files"][0]["role_origin"] == "human"
    assert verified["files"][0]["confirmed_by"] == "researcher"
    assert verified["supporting_information_state"] == "author_not_provided"
    again = library.scan([str(path)])[0]
    assert again["source_version"] == verified["source_version"]
    assert source.read_bytes() == before
    with sqlite3.connect(library.database) as db:
        old_rows = db.execute("SELECT payload FROM paper_events WHERE paper_id=? AND event='source_version_changed'",
                             (verified["id"],)).fetchall()
    assert any(json.loads(row[0])["supporting_information_state"] == "not_obtained_or_not_identified" for row in old_rows)


def test_manual_exclusion_is_required_for_other_attachment_before_absence_can_apply(library):
    path = folder(library)
    pdf(path / "article.pdf", [MAIN])
    docx(path / "cover.docx", "An ambiguous document, not classified from its name.")
    declare(library, [path])
    pending = library.scan([str(path)])[0]
    assert pending["supporting_information_state"] != "author_not_provided"
    extra = next(f for f in pending["files"] if f["format"] == "docx")
    confirmed = library.set_role(pending["id"], extra["id"], "other", "researcher", "A cover note, not SI.")
    assert confirmed["attachment_status"] == "ready"
    assert confirmed["supporting_information_state"] == "author_not_provided"
