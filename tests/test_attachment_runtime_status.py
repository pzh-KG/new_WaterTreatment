"""Runtime identification metadata must never initiate document reads."""
import json

from test_web_service import workbench
from watertreatment import collection_policy
from watertreatment.storage import hash_file
from watertreatment.web_service import CLASSIFIER_VERSION


def test_status_reports_loaded_classifier_and_unconfigured_manifest(workbench):
    status = workbench.service.status()
    assert status["attachment_classifier_version"] == CLASSIFIER_VERSION
    assert status["collection_policy_status"] == "not_configured"
    assert status["collection_policy_path"] is None
    assert status["collection_policy_manifest_sha256"] is None
    assert status["model"] == "gpt-6.2-sol" and status["effort"] == "max"
    assert workbench.clients == [] and workbench.service.jobs.list() == []


def test_status_hashes_selected_manifest_without_reading_its_documents(workbench, monkeypatch):
    folder = workbench.source / "1_synthetic"
    # The declared folder need not exist. Status checks configuration, not
    # availability or correspondence of each scientific source.
    path = workbench.settings.data_root / "collection.json"
    manifest = {
        "version": 1, "policy_id": "synthetic-runtime-status",
        "source_root": str(workbench.source), "confirmed_by": "synthetic user",
        "confirmed_at": "2026-01-01", "statement": "Synthetic acquisition statement",
        "absent_si_meaning": "author_not_provided",
        "entries": [{"folder": str(folder), "documents": [{"name": "main.pdf", "sha256": "0" * 64}]}],
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")
    workbench.settings.collection_policy_path = path
    expected = hash_file(path)
    calls = []

    def manifest_hash_only(target):
        assert target == path
        calls.append(target)
        return hash_file(target)

    monkeypatch.setattr(collection_policy, "hash_file", manifest_hash_only)
    status = workbench.service.status()
    assert calls and set(calls) == {path}
    assert status["collection_policy_path"] == str(path)
    assert status["collection_policy_manifest_sha256"] == expected
    assert status["collection_policy_status"] == "loaded"
    assert not folder.exists()
    assert workbench.clients == [] and workbench.service.jobs.list() == []


def test_status_reports_bad_manifest_without_echoing_its_contents(workbench):
    path = workbench.settings.data_root / "collection.json"
    path.write_text('{"private_test_marker": "do not expose"', encoding="utf-8")
    workbench.settings.collection_policy_path = path
    status = workbench.service.status()
    assert status["collection_policy_status"] == "unavailable"
    assert status["collection_policy_manifest_sha256"] is None
    assert status["attachment_classifier_version"] == CLASSIFIER_VERSION
    assert "private_test_marker" not in json.dumps(status)
    assert "do not expose" not in json.dumps(status)
    assert workbench.clients == [] and workbench.service.jobs.list() == []
