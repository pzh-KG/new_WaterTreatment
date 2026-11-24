import json
from pathlib import Path

import pytest

from watertreatment.settings import Settings
from watertreatment.storage import DataStore, hash_file, write_new_json


def test_uninitialized_import_leaves_root_untouched(tmp_path):
    source = tmp_path / "input.pdf"
    source.write_bytes(b"original")
    root = tmp_path / "data"
    root.mkdir()
    with pytest.raises(ValueError, match="initialized"):
        DataStore(root).import_file(source)
    assert list(root.iterdir()) == []
    DataStore(root).initialize()


def test_import_deduplicates_bytes_but_preserves_occurrences(tmp_path):
    root = tmp_path / "data"
    store = DataStore(root)
    store.initialize()
    first = tmp_path / "a.pdf"
    second = tmp_path / "b.pdf"
    first.write_bytes(b"unchanged scientific original")
    second.write_bytes(first.read_bytes())
    before = hash_file(first)
    a, b = store.import_file(first), store.import_file(second)
    assert a["stored_path"] == b["stored_path"]
    assert a["source_id"] != b["source_id"]
    assert store.import_file(first) == a
    assert hash_file(first) == before
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM sources").fetchone()[0] == 2


def test_changed_original_creates_new_snapshot(tmp_path):
    store = DataStore(tmp_path / "data")
    store.initialize()
    source = tmp_path / "a.pdf"
    source.write_bytes(b"version one")
    first = store.import_file(source)
    source.write_bytes(b"version two")
    second = store.import_file(source)
    assert first["stored_path"] != second["stored_path"]
    assert Path(first["stored_path"]).read_bytes() == b"version one"


def test_artifact_publish_never_overwrites(tmp_path):
    target = tmp_path / "record.json"
    write_new_json(target, {"value": 1})
    with pytest.raises(FileExistsError):
        write_new_json(target, {"value": 2})
    assert json.loads(target.read_text()) == {"value": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_unrecognized_store_is_not_adopted(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "previous-result.txt").write_text("keep")
    with pytest.raises(ValueError, match="nonempty"):
        DataStore(root).initialize()
    assert (root / "previous-result.txt").read_text() == "keep"


def test_marker_change_blocks_writes(tmp_path):
    store = DataStore(tmp_path / "data")
    store.initialize()
    (store.root / "store.json").write_text('{"format":"different","version":1}')
    with pytest.raises(ValueError, match="marker"):
        store.new_run("import", {})
    assert not list((store.root / "runs").iterdir())


def test_development_exposure_survives_renaming_and_unassigned(tmp_path):
    store = DataStore(tmp_path / "data")
    store.initialize()
    store.register_cohort("paper-A", ["source-digest"], "development")
    assert store.register_cohort("paper-A", ["changed-source-digest"], "unassigned") == "development"
    with pytest.raises(ValueError, match="development"):
        store.register_cohort("new-paper-label", ["source-digest"], "heldout")


def test_settings_roots_and_source_escape(tmp_path):
    with pytest.raises(ValueError, match="separate"):
        Settings(project_root=tmp_path, data_root=tmp_path / "data")
    source_root = tmp_path / "sources"
    source_root.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"secret unrelated to papers")
    settings = Settings(project_root=tmp_path / "code", data_root=tmp_path / "data", source_roots=[source_root])
    with pytest.raises(ValueError, match="outside"):
        settings.assert_source_allowed(outside)

