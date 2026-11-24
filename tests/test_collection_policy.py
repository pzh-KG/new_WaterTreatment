"""Offline collection assertions bind user authority to exact source versions."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from watertreatment.collection_policy import load_collection_policy
from watertreatment.settings import Settings
from watertreatment.storage import digest_json, hash_file


@pytest.fixture
def case(tmp_path):
    source = tmp_path / "sources"
    folder = source / "17_synthetic"
    folder.mkdir(parents=True)
    document = folder / "paper.pdf"
    document.write_bytes(b"Synthetic source bytes; no parsing or scientific reading in this test.")
    data = tmp_path / "data"
    data.mkdir()
    path = data / "verified.json"
    raw = {"version": 1, "policy_id": "synthetic-verified-collection", "source_root": str(source),
        "confirmed_by": "user", "confirmed_at": "2026-11-16", "statement": "Each included source was checked for SI.",
        "absent_si_meaning": "author_not_provided", "entries": [{"folder": str(folder),
        "documents": [{"name": document.name, "sha256": hash_file(document)}]}]}
    path.write_text(json.dumps(raw), encoding="utf-8")
    settings = Settings(project_root=tmp_path / "project", data_root=data, source_roots=[source],
        collection_policy_path=path)
    files = [{"name": document.name, "path": str(document), "sha256": hash_file(document),
              "role": "main_article", "status": "auto"}]
    return SimpleNamespace(settings=settings, source=source, folder=folder, document=document,
        path=path, raw=raw, files=files)


def write(case):
    case.path.write_text(json.dumps(case.raw), encoding="utf-8")


def test_opt_out_never_searches_for_or_loads_a_collection_policy(case, monkeypatch):
    settings = case.settings.model_copy(update={"collection_policy_path": None})
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("must not search default policy files"))
    assert load_collection_policy(settings) is None


def test_exact_inventory_returns_user_provenance_without_scientific_field_judgment(case):
    policy = load_collection_policy(case.settings)
    proof = policy.match(case.folder, case.files)
    assert proof["origin"] == "user_verified_collection"
    assert proof["manifest_sha256"] == hash_file(case.path)
    assert proof["entry_sha256"] == digest_json(case.raw["entries"][0])
    assert proof["confirmed_by"] == "user"
    assert proof["absent_si_meaning"] == "author_not_provided"
    assert "not_scientific_field_validation" in proof["verification_scope"]
    assert set(case.folder.iterdir()) == {case.document}


@pytest.mark.parametrize("change", ["add", "remove", "rename", "modify"])
def test_inventory_changes_expire_confirmation_even_with_old_classification(case, change):
    policy = load_collection_policy(case.settings)
    if change == "add":
        (case.folder / "new_si.docx").write_bytes(b"new supplement")
    elif change == "remove":
        case.document.unlink()
    elif change == "rename":
        case.document.rename(case.folder / "renamed.pdf")
    else:
        case.document.write_bytes(b"different source contents")
    assert policy.match(case.folder, case.files) is None


def test_equal_file_bytes_in_a_different_folder_do_not_extend_user_authority(case):
    outside = case.source / "18_not_verified"
    outside.mkdir()
    path = outside / case.document.name
    path.write_bytes(case.document.read_bytes())
    assert load_collection_policy(case.settings).match(outside,
        [{"name": path.name, "path": str(path), "sha256": hash_file(path)}]) is None


@pytest.mark.parametrize("tamper", ["hash", "path", "extra", "duplicate"])
def test_classification_inventory_cannot_misrepresent_verified_sources(case, tamper):
    files = [dict(case.files[0])]
    if tamper == "hash":
        files[0]["sha256"] = "0" * 64
    elif tamper == "path":
        files[0]["path"] = str(case.source / "unrelated.pdf")
    elif tamper == "extra":
        files.append({"name": "hidden_si.pdf", "sha256": "1" * 64})
    else:
        files.append(dict(files[0]))
    assert load_collection_policy(case.settings).match(case.folder, files) is None


def test_word_lock_file_does_not_become_a_new_scientific_source(case):
    (case.folder / "~$editing.docx").write_bytes(b"word lock")
    assert load_collection_policy(case.settings).match(case.folder, case.files)


@pytest.mark.parametrize("name", ["Thumbs.db", "desktop.ini", ".DS_Store"])
def test_known_os_metadata_does_not_extend_or_revoke_scientific_inventory(case, name):
    (case.folder / name).write_bytes(b"OS metadata")
    assert load_collection_policy(case.settings).match(case.folder, case.files)


@pytest.mark.parametrize("kind", ["archive", "nested", "note", "directory_named_like_noise"])
def test_unmanifested_attachment_or_unexplained_file_invalidates_absence(case, kind):
    if kind == "nested":
        nested = case.folder / "new_supplement"
        nested.mkdir()
        (nested / "support.pdf").write_bytes(b"new nested SI")
    elif kind == "directory_named_like_noise":
        (case.folder / "Thumbs.db").mkdir()
    else:
        (case.folder / ("support.zip" if kind == "archive" else "note.txt")).write_bytes(b"not yet accounted for")
    assert load_collection_policy(case.settings).match(case.folder, case.files) is None


def test_loaded_policy_cannot_silently_use_a_replaced_user_statement(case):
    policy = load_collection_policy(case.settings)
    case.raw["statement"] = "Changed policy after this scan began."
    write(case)
    with pytest.raises(ValueError, match="changed during use"):
        policy.match(case.folder, case.files)


def test_index_preserves_separate_provenance_and_detects_replacements(case):
    old = load_collection_policy(case.settings).match(case.folder, case.files)
    other_folder = case.source / "later"
    other_folder.mkdir()
    new_doc = other_folder / "other.pdf"
    new_doc.write_bytes(b"second source")
    other_files = [{"name": new_doc.name, "sha256": hash_file(new_doc)}]
    second = case.settings.data_root / "later.json"
    raw = {**case.raw, "policy_id": "later", "confirmed_at": "2026-11-20", "statement": "Separate confirmation.",
           "entries": [{"folder": str(other_folder), "documents": other_files}]}
    second.write_text(json.dumps(raw), encoding="utf-8")
    index = case.settings.data_root / "index.json"
    refs = [{"path": str(path), "sha256": hash_file(path)} for path in [case.path, second]]
    index.write_text(json.dumps({"version": 2, "manifests": refs}), encoding="utf-8")
    settings = case.settings.model_copy(update={"collection_policy_path": index})
    policy = load_collection_policy(settings)
    assert policy.manifest_sha256 == hash_file(index)
    assert policy.match(case.folder, case.files) == old
    match = policy.match(other_folder, other_files)
    assert match["manifest_sha256"] == hash_file(second) and match["statement"] == raw["statement"]
    (other_folder / "new-si.pdf").write_bytes(b"new source")
    assert policy.match(other_folder, other_files) is None
    second.write_text(json.dumps({**raw, "statement": "Replaced."}), encoding="utf-8")
    with pytest.raises(ValueError, match="changed during use"):
        policy.match(other_folder, other_files)
    with pytest.raises(ValueError, match="pinned hash"):
        load_collection_policy(settings)
    index.write_text(json.dumps({"version": 2, "manifests": [refs[0], refs[0]]}), encoding="utf-8")
    with pytest.raises(ValueError, match="changed during use"):
        policy.match(case.folder, case.files)
    with pytest.raises(ValueError, match="ambiguous folder"):
        load_collection_policy(settings)


@pytest.mark.parametrize("tamper", ["schema", "unknown_policy", "source_root", "folder_escape",
    "nested_folder", "duplicate_folder", "duplicate_name", "traversal_name", "bad_hash", "empty_confirmation", "date"])
def test_bad_manifest_is_visible_error_not_an_absence_fallback(case, tamper):
    if tamper == "schema":
        case.raw["unexpected"] = "must not be ignored"
    elif tamper == "unknown_policy":
        case.raw["absent_si_meaning"] = "all_fields_not_reported"
    elif tamper == "source_root":
        case.raw["source_root"] = str(case.source.parent)
    elif tamper == "folder_escape":
        case.raw["entries"][0]["folder"] = str(case.source.parent / "other")
    elif tamper == "nested_folder":
        case.raw["entries"][0]["folder"] = str(case.folder / "nested")
    elif tamper == "duplicate_folder":
        case.raw["entries"].append(case.raw["entries"][0])
    elif tamper == "duplicate_name":
        case.raw["entries"][0]["documents"].append(case.raw["entries"][0]["documents"][0])
    elif tamper == "traversal_name":
        case.raw["entries"][0]["documents"][0]["name"] = "../paper.pdf"
    elif tamper == "bad_hash":
        case.raw["entries"][0]["documents"][0]["sha256"] = "bad"
    elif tamper == "empty_confirmation":
        case.raw["confirmed_by"] = " "
    else:
        case.raw["confirmed_at"] = "not dated"
    write(case)
    with pytest.raises(ValueError):
        load_collection_policy(case.settings)


def test_invalid_json_and_missing_configured_file_are_not_silent_opt_out(case):
    case.path.write_text("not JSON", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid verified"):
        load_collection_policy(case.settings)
    case.path.unlink()
    with pytest.raises(OSError):
        load_collection_policy(case.settings)


@pytest.mark.parametrize("location", ["relative", "project", "outside", "traversal", "not_json"])
def test_settings_cannot_point_policy_outside_explicit_data_json(case, location):
    value = {"relative": Path("verified.json"), "project": case.settings.project_root / "verified.json",
        "outside": case.source / "verified.json", "traversal": case.settings.data_root / ".." / "data" / "verified.json",
        "not_json": case.settings.data_root / "a.txt"}[location]
    raw = case.settings.model_dump()
    raw["collection_policy_path"] = value
    with pytest.raises(ValueError, match="collection_policy_path"):
        Settings.model_validate(raw)


def test_settings_accepts_future_data_path_without_opening_any_policy(case, monkeypatch):
    raw = case.settings.model_dump()
    raw["collection_policy_path"] = case.settings.data_root / "future" / "verified.json"
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("settings must not read policy contents"))
    assert Settings.model_validate(raw).collection_policy_path == raw["collection_policy_path"]


def test_missing_verified_folder_loses_applicability_without_extending_to_another(case):
    case.document.unlink()
    case.folder.rmdir()
    policy = load_collection_policy(case.settings)
    assert policy.match(case.folder, case.files) is None


def test_linked_policy_is_rejected_before_reading_its_contents(case, monkeypatch):
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == case.path or original(path))
    with pytest.raises(ValueError, match="symbolic links"):
        load_collection_policy(case.settings)


@pytest.mark.parametrize("linked", ["folder", "document"])
def test_linked_source_cannot_reuse_collection_confirmation(case, monkeypatch, linked):
    policy = load_collection_policy(case.settings)
    target = getattr(case, linked)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == target or original(path))
    assert policy.match(case.folder, case.files) is None


def test_document_replaced_by_a_directory_expires_confirmation(case):
    policy = load_collection_policy(case.settings)
    case.document.unlink()
    case.document.mkdir()
    assert policy.match(case.folder, case.files) is None
