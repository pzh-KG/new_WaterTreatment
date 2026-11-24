"""Automated acquisition decisions must remain distinct from human validation."""
import copy
import json
import sqlite3

import pytest

from test_web_library import library, folder, pdf, MAIN, SI
from watertreatment.storage import hash_file


def review_for(library, paper, *, title="A source title recovered from the page", limitations=None):
    main, supplement = paper["files"]
    artifact = library.settings.data_root / "bounded-review-evidence.txt"
    artifact.write_text("Synthetic attachment evidence for provenance tests.", encoding="utf-8")
    manifest = {
        "version": 1, "review_id": "synthetic-attachment-review", "paper_id": paper["id"],
        "reviewed_at": "2026-11-16T10:00:00+00:00", "origin": "codex_attachment_review",
        "reviewer": "Codex desktop assistant", "human_validated": False, "scientific_validated": False,
        "input_inventory": [{"file_id": f["id"], "path": f["path"], "sha256": f["sha256"]} for f in paper["files"]],
        "decisions": [{"file_id": f["id"], "role": role,
            "main_file_id": main["id"] if role == "supporting_information" else None,
            "title": title if role == "main_article" else None,
            "reason": "Synthetic evidence-bound role decision; not scientific validation.",
            "evidence": [{"source_file_id": f["id"], "location": {"physical_page": 1},
                          "quote": "Synthetic attachment evidence", "method": "synthetic_test"}]}
            for f, role in ((main, "main_article"), (supplement, "supporting_information"))],
        "limitations": limitations or [],
        "evidence_artifacts": [{"path": str(artifact), "sha256": hash_file(artifact)}],
    }
    path = library.settings.data_root / "review.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def unresolved(library):
    directory = folder(library)
    pdf(directory / "a.pdf", ["A sparsely formatted paper without an abstract label."])
    pdf(directory / "b.pdf", ["An unheaded companion table for this experiment."])
    paper = library.scan([str(directory)])[0]
    assert paper["attachment_status"] == "needs_confirmation"
    return directory, paper


def test_auto_review_persists_on_rescan_without_fabricating_human_confirmation(library):
    directory, before = unresolved(library)
    manifest = review_for(library, before, limitations=["A referenced supplementary image was not located."])
    after = library.apply_attachment_review(before["id"], manifest)
    assert after["attachment_status"] == "ready"
    assert after["title"] == "A source title recovered from the page"
    assert after["supporting_information_state"] == "present"
    assert {f["role_origin"] for f in after["files"]} == {"codex_attachment_review"}
    assert after["files"][1]["identity_status"] == "source_review_matched"
    details = after["attachment_review_details"]
    assert details["state"] == "applied"
    assert details["human_validated"] is False and details["scientific_validated"] is False
    assert "not located" in details["limitations"][0]
    assert after["scientific_eligibility"] == "not_assessed"
    assert after["source_version"] == before["source_version"] + 1
    with sqlite3.connect(library.database) as db:
        assert db.execute("SELECT COUNT(*) FROM role_history").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM paper_events WHERE event='codex_attachment_reviewed'").fetchone()[0] == 1
    again = library.scan([str(directory)])[0]
    assert again["attachment_status"] == "ready"
    assert again["source_fingerprint"] == after["source_fingerprint"]
    assert again["source_version"] == after["source_version"]
    assert library.roles_for_import(after["id"]) == {"a.pdf": "main_article", "b.pdf": "supporting_information"}


@pytest.mark.parametrize("change", ["replace", "rename", "extra_pdf", "extra_zip"])
def test_changed_source_expires_review_and_blocks_cached_import(library, change):
    directory, before = unresolved(library)
    library.apply_attachment_review(before["id"], review_for(library, before))
    if change == "replace":
        pdf(directory / "b.pdf", ["A different document version."])
    elif change == "rename":
        (directory / "b.pdf").rename(directory / "renamed.pdf")
    elif change == "extra_pdf":
        pdf(directory / "extra.pdf", ["An additional attachment whose identity is not established."])
    else:
        (directory / "extra.zip").write_bytes(b"new substantive attachment")
    with pytest.raises(ValueError):
        library.roles_for_import(before["id"])
    after = library.scan([str(directory)])[0]
    assert after["attachment_review_details"]["state"] == "expired"
    assert after["attachment_status"] != "ready"
    assert all(f.get("role_origin") != "codex_attachment_review" for f in after["files"])
    assert after["title"] != "A source title recovered from the page"


def test_human_role_choice_takes_priority_over_automatic_review(library):
    directory, before = unresolved(library)
    after = library.apply_attachment_review(before["id"], review_for(library, before))
    changed = library.set_role(after["id"], after["files"][1]["id"], "other", "researcher", "Explicitly excluded.")
    assert changed["files"][1]["role"] == "other"
    assert changed["files"][1]["role_origin"] == "human"
    again = library.scan([str(directory)])[0]
    assert again["files"][1]["role"] == "other"


def test_expired_review_does_not_block_new_independently_identified_sources(library):
    directory, before = unresolved(library)
    library.apply_attachment_review(before["id"], review_for(library, before))
    pdf(directory / "a.pdf", [MAIN])
    pdf(directory / "b.pdf", [SI])
    refreshed = library.scan([str(directory)])[0]
    assert refreshed["attachment_review_details"]["state"] == "expired"
    assert refreshed["attachment_status"] == "ready"
    assert all(f.get("role_origin") != "codex_attachment_review" for f in refreshed["files"])
    assert library.roles_for_import(before["id"]) == {"a.pdf": "main_article", "b.pdf": "supporting_information"}


def test_conflicting_own_doi_is_not_overridden_by_automated_match(library):
    directory = folder(library)
    pdf(directory / "a.pdf", [MAIN])
    pdf(directory / "b.pdf", [SI.replace("Supporting Information\n", "Supporting Information\n10.8888/wrong-article\n")])
    paper = library.scan([str(directory)])[0]
    after = library.apply_attachment_review(paper["id"], review_for(library, paper))
    assert after["attachment_status"] == "identity_conflict"
    assert after["files"][1]["identity_status"] == "conflict"
    with pytest.raises(ValueError):
        library.roles_for_import(paper["id"])


def test_multiple_main_dois_do_not_hide_disjoint_supplement_identity(library):
    directory = folder(library)
    pdf(directory / "a.pdf", ["10.8888/additional-identity\n" + MAIN])
    pdf(directory / "b.pdf", [SI.replace("Supporting Information\n", "Supporting Information\n10.7777/unrelated-study\n")])
    paper = library.scan([str(directory)])[0]
    after = library.apply_attachment_review(paper["id"], review_for(library, paper))
    assert after["files"][0]["identity_status"] == "multiple_dois_unresolved"
    assert after["attachment_status"] == "identity_conflict"
    assert after["doi"] is None
    with pytest.raises(ValueError):
        library.roles_for_import(paper["id"])


def test_readability_cannot_be_promoted_by_role_review(library):
    directory = folder(library)
    pdf(directory / "a.pdf", [MAIN])
    (directory / "b.pdf").write_bytes(b"An unreadable supplied document.")
    paper = library.scan([str(directory)])[0]
    after = library.apply_attachment_review(paper["id"], review_for(library, paper))
    assert after["attachment_status"] == "source_unreadable"
    assert after["files"][1]["readability"] == "unreadable"
    with pytest.raises(ValueError):
        library.roles_for_import(paper["id"])


def test_tampered_review_cannot_change_saved_catalog(library):
    _, before = unresolved(library)
    manifest = review_for(library, before)
    after = library.apply_attachment_review(before["id"], manifest)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["decisions"][0]["title"] = "Changed after the review was recorded"
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        library.roles_for_import(before["id"])
    assert library.get(before["id"]) == after
