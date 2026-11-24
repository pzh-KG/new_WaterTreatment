"""Metadata-only configured-folder selection; no literature reading or jobs."""
from pathlib import Path
from types import SimpleNamespace
import stat

import pytest
from fastapi.testclient import TestClient

from watertreatment.settings import CodexSettings, Settings
from watertreatment.web_service import Workbench
from watertreatment.web_api import create_app


@pytest.fixture
def picker(tmp_path):
    root = tmp_path / "papers"
    root.mkdir()
    settings = Settings(project_root=tmp_path / "code", data_root=tmp_path / "data", source_roots=[root],
                        codex=CodexSettings(model="gpt-6.2-sol", reasoning_effort="max"))
    return Workbench(settings)


def paper(picker, name, files=()):
    folder = picker.settings.source_roots[0] / name
    folder.mkdir()
    for filename in files:
        (folder / filename).write_bytes(b"invalid scientific content: directory listing must not parse this")
    return folder


def no_document_reads(monkeypatch, picker):
    def denied(*args, **kwargs):
        pytest.fail("folder picker must not read/hash/parse/register documents or enqueue work")
    from watertreatment import web_library, web_service
    monkeypatch.setattr(web_library, "hash_file", denied)
    monkeypatch.setattr(web_service, "hash_file", denied)
    monkeypatch.setattr(picker.library, "scan", denied)
    monkeypatch.setattr(picker.records, "refresh", denied)
    monkeypatch.setattr(picker.jobs, "enqueue", denied)
    original_open = Path.open
    source_roots = picker.settings.source_roots
    def guarded_open(path, *args, **kwargs):
        if any(root == path or root in path.parents for root in source_roots):
            denied()
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)


def test_folder_picker_lists_only_direct_folders_numerically_without_reading(picker, monkeypatch):
    paper(picker, "10_test", ["main.PDF", "extra.docx", "notes.txt"])
    paper(picker, "2_test", ["si.doc"])
    empty = paper(picker, "1_empty")
    paper(picker, "notes", ["unrelated.txt"])
    (empty / "nested").mkdir()
    (empty / "nested" / "hidden.pdf").write_bytes(b"not a direct attachment")
    (picker.settings.source_roots[0] / "loose.pdf").write_bytes(b"no paper folder")
    no_document_reads(monkeypatch, picker)
    result = picker.scan_folders()
    assert [f["name"] for f in result["folders"]] == ["1_empty", "2_test", "10_test", "notes"]
    assert [f["file_count"] for f in result["folders"]] == [0, 1, 2, 0]
    assert result["folders"][2]["formats"] == ["docx", "pdf"]
    assert all(not f["registered"] and f["paper_id"] is None for f in result["folders"])
    assert result["roots"][0]["available"] and result["errors"] == []
    assert picker.library.list_papers() == [] and picker.jobs.list() == []


def test_folder_picker_merges_registered_and_current_record_summary_only(picker, monkeypatch):
    folder = paper(picker, "3_registered", ["source.pdf"])
    monkeypatch.setattr(picker.library, "list_papers", lambda: [{"id": "known", "folder": str(folder), "attachment_status": "ready"}])
    monkeypatch.setattr(picker.records, "list_records", lambda: [
        {"id": "old", "paper_id": "known", "current": False},
        {"id": "current", "paper_id": "known", "current": True}])
    no_document_reads(monkeypatch, picker)
    item = picker.scan_folders()["folders"][0]
    assert item["registered"] and item["paper_id"] == "known"
    assert item["record_id"] == "current" and item["attachment_status"] == "ready"


def test_folder_picker_missing_root_and_unconfigured_roots_are_explicit(picker):
    root = picker.settings.source_roots[0]
    root.rmdir()
    result = picker.scan_folders()
    assert not result["roots"][0]["available"] and result["folders"] == [] and result["errors"]
    picker.settings.source_roots = []
    result = picker.scan_folders()
    assert result["root_path"] is None and result["roots"] == [] and result["errors"]


@pytest.mark.parametrize("index", [-1, 1, True, "0"])
def test_folder_picker_rejects_invalid_root_choice(picker, index):
    with pytest.raises(ValueError, match="文献根目录"):
        picker.scan_folders(index)


def test_folder_picker_can_select_second_configured_root(picker, tmp_path):
    second = tmp_path / "other_papers"
    second.mkdir()
    (second / "9_second").mkdir()
    picker.settings.source_roots.append(second)
    result = picker.scan_folders(1)
    assert result["root_index"] == 1 and result["root_path"] == str(second)
    assert result["folders"][0]["name"] == "9_second"


def mark_reparse(monkeypatch, paths, *, symlink=False):
    original = Path.lstat
    def flagged(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path in paths:
            return SimpleNamespace(st_mode=stat.S_IFLNK if symlink else result.st_mode,
                                   st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))
        return result
    monkeypatch.setattr(Path, "lstat", flagged)


@pytest.mark.parametrize("symlink", [False, True])
def test_folder_picker_rejects_linked_directory_without_enumerating_target(picker, monkeypatch, symlink):
    linked = paper(picker, "1_junction", ["never.pdf"])
    paper(picker, "2_normal", ["okay.pdf"])
    mark_reparse(monkeypatch, {linked}, symlink=symlink)
    original = Path.iterdir
    def no_target(path):
        if path == linked:
            pytest.fail("must not enumerate junction/symlink target")
        return original(path)
    monkeypatch.setattr(Path, "iterdir", no_target)
    result = picker.scan_folders()
    assert [f["name"] for f in result["folders"]] == ["2_normal"]
    assert any(e["path"] == str(linked) for e in result["errors"])


def test_folder_picker_rejects_reparse_root_before_enumeration(picker, monkeypatch):
    root = picker.settings.source_roots[0]
    mark_reparse(monkeypatch, {root})
    monkeypatch.setattr(Path, "iterdir", lambda _: pytest.fail("reparse root must not be enumerated"))
    result = picker.scan_folders()
    assert not result["roots"][0]["available"] and result["folders"] == []
    assert "目录连接" in result["errors"][0]["message"]


def test_folder_picker_rejects_linked_attachment_and_counts_remaining_files(picker, monkeypatch):
    folder = paper(picker, "1_article", ["main.pdf", "linked.docx"])
    mark_reparse(monkeypatch, {folder / "linked.docx"})
    result = picker.scan_folders()
    assert result["folders"][0]["file_count"] == 1
    assert result["folders"][0]["formats"] == ["pdf"] and result["errors"]


def test_folder_picker_keeps_other_folders_when_one_cannot_be_enumerated(picker, monkeypatch):
    denied = paper(picker, "1_denied", ["main.pdf"])
    paper(picker, "2_readable", ["main.pdf"])
    original = Path.iterdir
    def guarded(path):
        if path == denied:
            raise PermissionError("synthetic inaccessible folder")
        return original(path)
    monkeypatch.setattr(Path, "iterdir", guarded)
    result = picker.scan_folders()
    assert [f["name"] for f in result["folders"]] == ["2_readable"] and result["errors"]


def test_folder_picker_refuses_resolved_root_escape(picker, monkeypatch, tmp_path):
    root = picker.settings.source_roots[0]
    outside = tmp_path / "outside"
    original = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda p, *a, **k: outside if p == root else original(p, *a, **k))
    result = picker.scan_folders()
    assert not result["roots"][0]["available"] and not result["folders"]


def test_folder_picker_http_listing_and_explicit_multi_selection_are_separate(picker, monkeypatch):
    first = paper(picker, "1_first", ["article.pdf"])
    second = paper(picker, "2_second", ["article.pdf"])
    third = paper(picker, "3_not_selected", ["article.pdf"])
    monkeypatch.setattr(picker.library, "scan", lambda *a: pytest.fail("listing/queueing must not scan"))
    with TestClient(create_app(picker.settings, start_worker=False, service=picker)) as client:
        response = client.get("/api/scan-folders")
        assert response.status_code == 200 and len(response.json()["folders"]) == 3
        assert picker.jobs.list() == []
        token = client.get("/api/status").json()["csrf_token"]
        response = client.post("/api/scan", headers={"X-WT-Token": token}, json={"folders": [str(first), str(second)]})
        assert response.status_code == 200
        job = picker.jobs.get(response.json()["job_id"])
        assert job["status"] == "queued" and job["kind"] == "scan"
        assert job["payload"]["folders"] == [str(first), str(second)]
        assert str(third) not in job["payload"]["folders"]
        assert picker.library.list_papers() == []
        assert client.get("/api/scan-folders?root_index=-1").status_code == 409
        assert client.get("/api/scan-folders?root_index=wrong").status_code == 422
