"""Offline tests for provenance, source expiry and review authority boundaries."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from watertreatment import attachment_reviews as reviews
from watertreatment.settings import Settings
from watertreatment.storage import hash_file


@pytest.fixture
def case(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    source = tmp_path / "sources"
    folder = source / "17_synthetic"
    folder.mkdir(parents=True)
    files = []
    for identifier, name in [("file-main", "main.pdf"), ("file-si", "si.docx")]:
        path = folder / name
        path.write_bytes(("Synthetic identity bytes: " + identifier).encode())
        files.append({"id": identifier, "name": name, "path": str(path), "sha256": hash_file(path),
                      "role": "unknown", "status": "unknown"})
    artifact = data / "sample.txt"
    artifact.write_text("Bounded synthetic source sample", encoding="utf-8")
    raw = {"version": 1, "review_id": "synthetic-review", "paper_id": "synthetic-paper",
        "reviewed_at": "2026-11-16T00:00:00Z", "origin": "codex_attachment_review",
        "reviewer": "Codex desktop assistant", "human_validated": False, "scientific_validated": False,
        "input_inventory": [{"file_id": f["id"], "path": f["path"], "sha256": f["sha256"]} for f in files],
        "decisions": [{"file_id": f["id"], "role": "main_article" if i == 0 else "supporting_information",
                       "main_file_id": None if i == 0 else "file-main", "title": "Synthetic title",
                       "reason": "A bounded identity check; not a scientific claim.",
                       "evidence": [{"source_file_id": f["id"], "location": {"physical_page": 1},
                                     "quote": "Synthetic source title", "method": "visual_header"}]}
                      for i, f in enumerate(files)],
        "limitations": ["No scientific facts were validated."],
        "evidence_artifacts": [{"path": str(artifact), "sha256": hash_file(artifact)}],
        "effective_model": None}
    path = data / "review.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    settings = Settings(project_root=tmp_path / "project", data_root=data, source_roots=[source])
    return SimpleNamespace(settings=settings, source=source, folder=folder, data=data, files=files,
                           artifact=artifact, path=path, raw=raw)


def write(case):
    case.path.write_text(json.dumps(case.raw), encoding="utf-8")


def load(case, pointer=None, files=None):
    return reviews.load_attachment_review(case.settings, pointer or reviews.make_review_pointer(case.settings, case.path),
                                          "synthetic-paper", case.files if files is None else files)


def test_absent_pointer_does_not_read_anything(case, monkeypatch):
    monkeypatch.setattr(Path, "open", lambda *a, **k: pytest.fail("no implicit review lookup"))
    assert reviews.load_attachment_review(case.settings, None, "synthetic-paper", case.files) is None


def test_exact_review_returns_original_fields_and_separates_scientific_authority(case):
    pointer = reviews.make_review_pointer(case.settings, case.path)
    assert pointer == {"path": str(case.path), "sha256": hash_file(case.path), "review_id": "synthetic-review"}
    result = load(case, pointer)
    assert result == case.raw
    assert result["origin"] == "codex_attachment_review"
    assert result["human_validated"] is False and result["scientific_validated"] is False
    assert result["effective_model"] is None
    assert case.files[0]["role"] == "unknown"


@pytest.mark.parametrize("change", ["added", "deleted", "replaced", "renamed", "moved_folder", "directory"])
def test_disk_inventory_change_expires_review_despite_cached_files(case, change):
    pointer = reviews.make_review_pointer(case.settings, case.path)
    path = Path(case.files[0]["path"])
    if change == "added":
        (case.folder / "another.pdf").write_bytes(b"additional source")
    elif change == "deleted":
        path.unlink()
    elif change == "replaced":
        path.write_bytes(b"changed source")
    elif change == "renamed":
        path.rename(case.folder / "renamed.pdf")
    elif change == "moved_folder":
        case.folder.rename(case.source / "renamed_folder")
    else:
        path.unlink()
        path.mkdir()
    assert load(case, pointer) is None


@pytest.mark.parametrize("extra", ["archive.zip", "note.txt", "nested", "Thumbs.db"])
def test_unregistered_substantive_entries_invalidate_review(case, extra):
    pointer = reviews.make_review_pointer(case.settings, case.path)
    path = case.folder / extra
    if extra in {"nested", "Thumbs.db"}:
        path.mkdir()
    else:
        path.write_bytes(b"unregistered")
    assert load(case, pointer) is None


@pytest.mark.parametrize("name", ["~$working.docx", "Thumbs.db", "desktop.ini", ".DS_Store"])
def test_ordinary_inventory_noise_is_ignored(case, name):
    pointer = reviews.make_review_pointer(case.settings, case.path)
    (case.folder / name).write_bytes(b"temporary metadata")
    assert load(case, pointer) == case.raw


@pytest.mark.parametrize("change", ["hash", "path", "file_id", "name", "missing", "duplicate", "bad_type"])
def test_caller_inventory_cannot_reuse_different_sources(case, change):
    pointer = reviews.make_review_pointer(case.settings, case.path)
    files = copy.deepcopy(case.files)
    if change in {"hash", "path", "file_id", "name"}:
        field = {"hash": "sha256", "file_id": "id"}.get(change, change)
        files[0][field] = "0" * 64 if change == "hash" else "different"
    elif change == "missing":
        files.pop()
    elif change == "duplicate":
        files[1] = dict(files[0])
    else:
        files[0] = None
    assert load(case, pointer, files) is None


@pytest.mark.parametrize("change", ["id", "hash", "path", "paper_id", "extra", "blank"])
def test_bad_pointer_is_explicit_error(case, change):
    pointer = reviews.make_review_pointer(case.settings, case.path)
    if change == "id":
        pointer["review_id"] = "different-review"
    elif change == "hash":
        pointer["sha256"] = "0" * 64
    elif change == "path":
        other = case.data / "different.json"
        other.write_text(json.dumps({**case.raw, "review_id": "different-review"}), encoding="utf-8")
        pointer["path"] = str(other)
    elif change == "extra":
        pointer["trust_me"] = True
    elif change == "blank":
        pointer["review_id"] = " "
    else:
        with pytest.raises(ValueError, match="paper identity"):
            reviews.load_attachment_review(case.settings, pointer, "another-paper", case.files)
        return
    with pytest.raises(ValueError):
        load(case, pointer)


@pytest.mark.parametrize("change", ["human_true", "scientific_true", "human_zero", "version_bool",
    "wrong_origin", "wrong_reviewer", "date", "extra", "missing_decision", "duplicate_decision",
    "two_mains", "no_main", "wrong_main_link", "main_link_on_other", "unknown_evidence", "empty_evidence",
    "blank_quote", "blank_reason", "blank_title", "blank_method", "duplicate_input", "duplicate_path"])
def test_invalid_review_fields_cannot_grant_authority(case, change):
    if change == "human_true": case.raw["human_validated"] = True
    elif change == "scientific_true": case.raw["scientific_validated"] = True
    elif change == "human_zero": case.raw["human_validated"] = 0
    elif change == "version_bool": case.raw["version"] = True
    elif change == "wrong_origin": case.raw["origin"] = "human_review"
    elif change == "wrong_reviewer": case.raw["reviewer"] = "user"
    elif change == "date": case.raw["reviewed_at"] = "yesterday"
    elif change == "extra": case.raw["override_science"] = True
    elif change == "missing_decision": case.raw["decisions"].pop()
    elif change == "duplicate_decision": case.raw["decisions"].append(case.raw["decisions"][0])
    elif change == "two_mains": case.raw["decisions"][1].update(role="main_article", main_file_id=None)
    elif change == "no_main": case.raw["decisions"][0]["role"] = "other"
    elif change == "wrong_main_link": case.raw["decisions"][1]["main_file_id"] = "file-si"
    elif change == "main_link_on_other": case.raw["decisions"][1]["role"] = "other"
    elif change == "unknown_evidence": case.raw["decisions"][0]["evidence"][0]["source_file_id"] = "outside"
    elif change == "empty_evidence": case.raw["decisions"][0]["evidence"] = []
    elif change == "blank_quote": case.raw["decisions"][0]["evidence"][0]["quote"] = " "
    elif change == "blank_reason": case.raw["decisions"][0]["reason"] = " "
    elif change == "blank_title": case.raw["decisions"][0]["title"] = " "
    elif change == "blank_method": case.raw["decisions"][0]["evidence"][0]["method"] = " "
    elif change == "duplicate_input": case.raw["input_inventory"].append(case.raw["input_inventory"][0])
    else: case.raw["input_inventory"][1]["path"] = case.raw["input_inventory"][0]["path"]
    write(case)
    with pytest.raises(ValueError, match="invalid attachment review"):
        reviews.make_review_pointer(case.settings, case.path)


@pytest.mark.parametrize("path_kind", ["relative", "outside", "traversal", "extension", "directory"])
def test_manifest_path_boundary(case, path_kind):
    if path_kind == "relative": path = Path("review.json")
    elif path_kind == "outside":
        path = case.source / "review.json"
        path.write_text(json.dumps(case.raw), encoding="utf-8")
    elif path_kind == "traversal": path = case.data / ".." / "data" / "review.json"
    elif path_kind == "extension":
        path = case.data / "review.txt"
        path.write_text(json.dumps(case.raw), encoding="utf-8")
    else:
        path = case.data / "directory.json"
        path.mkdir()
    with pytest.raises(ValueError):
        reviews.make_review_pointer(case.settings, path)


@pytest.mark.parametrize("scope", ["outside", "nested_folder", "mixed_folders", "traversal"])
def test_input_manifest_cannot_expand_source_authority(case, scope):
    if scope == "outside": value = case.data / "main.pdf"
    elif scope == "nested_folder": value = case.folder / "nested" / "main.pdf"
    elif scope == "mixed_folders": value = case.source / "other" / "main.pdf"
    else: value = case.folder / ".." / case.folder.name / "main.pdf"
    case.raw["input_inventory"][0]["path"] = str(value)
    write(case)
    with pytest.raises(ValueError):
        reviews.make_review_pointer(case.settings, case.path)


@pytest.mark.parametrize("change", ["missing", "changed", "directory", "outside", "traversal"])
def test_evidence_artifact_provenance_must_remain_intact(case, change):
    pointer = reviews.make_review_pointer(case.settings, case.path)
    if change == "missing": case.artifact.unlink()
    elif change == "changed": case.artifact.write_text("changed", encoding="utf-8")
    elif change == "directory":
        case.artifact.unlink()
        case.artifact.mkdir()
    else:
        value = case.source / "source.txt" if change == "outside" else case.data / ".." / "data" / case.artifact.name
        case.raw["evidence_artifacts"][0]["path"] = str(value)
        write(case)
    with pytest.raises((ValueError, OSError)):
        load(case, pointer)


@pytest.mark.parametrize("target", ["manifest", "data_parent", "artifact"])
def test_linked_provenance_is_rejected_before_use(case, monkeypatch, target):
    selected = {"manifest": case.path, "data_parent": case.data, "artifact": case.artifact}[target]
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == selected or original(p))
    with pytest.raises(ValueError, match="symbolic links"):
        reviews.make_review_pointer(case.settings, case.path)


@pytest.mark.parametrize("target", ["folder", "document"])
def test_linked_source_expires_review(case, monkeypatch, target):
    pointer = reviews.make_review_pointer(case.settings, case.path)
    selected = case.folder if target == "folder" else Path(case.files[0]["path"])
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == selected or original(p))
    assert load(case, pointer) is None


def test_overlarge_and_invalid_json_are_explicit_errors(case, monkeypatch):
    monkeypatch.setattr(reviews, "MAX_REVIEW_BYTES", case.path.stat().st_size - 1)
    with pytest.raises(ValueError, match="8 MiB"):
        reviews.make_review_pointer(case.settings, case.path)
    monkeypatch.setattr(reviews, "MAX_REVIEW_BYTES", 8 * 1024 * 1024)
    case.path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid attachment review"):
        reviews.make_review_pointer(case.settings, case.path)


def test_manifest_mutation_while_loading_is_not_silently_accepted(case, monkeypatch):
    original = reviews.hash_file
    def changed(path):
        if path == case.path:
            return "0" * 64
        return original(path)
    monkeypatch.setattr(reviews, "hash_file", changed)
    with pytest.raises(ValueError, match="changed during loading"):
        reviews.make_review_pointer(case.settings, case.path)


def test_review_pointer_can_reference_history_but_expired_sources_cannot_be_reused(case):
    Path(case.files[0]["path"]).write_bytes(b"new original version")
    pointer = reviews.make_review_pointer(case.settings, case.path)
    assert load(case, pointer) is None


@pytest.mark.parametrize("target", ["manifest", "artifact", "source"])
def test_windows_reparse_points_do_not_bypass_path_boundary(case, monkeypatch, target):
    pointer = reviews.make_review_pointer(case.settings, case.path)
    selected = {"manifest": case.path, "artifact": case.artifact,
                "source": Path(case.files[0]["path"])}[target]
    original = Path.lstat
    class ReparseStat:
        st_file_attributes = 1024
        def __init__(self, real):
            self.real = real
        def __getattr__(self, key):
            return getattr(self.real, key)
    monkeypatch.setattr(Path, "lstat", lambda p: ReparseStat(original(p)) if p == selected else original(p))
    if target == "source":
        assert load(case, pointer) is None
    else:
        with pytest.raises(ValueError, match="symbolic links or junctions"):
            load(case, pointer)


def test_manifest_changed_while_hashing_sources_is_explicit_error(case, monkeypatch):
    pointer = reviews.make_review_pointer(case.settings, case.path)
    original = reviews.hash_file
    def changed(path):
        result = original(path)
        if path == Path(case.files[0]["path"]):
            case.path.write_text(json.dumps({**case.raw, "review_id": "replaced-mid-load"}), encoding="utf-8")
        return result
    monkeypatch.setattr(reviews, "hash_file", changed)
    with pytest.raises(ValueError, match="changed during applicability"):
        load(case, pointer)
