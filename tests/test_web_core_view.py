"""Offline immutable research-view publication, without graph or model work."""
from __future__ import annotations

import json

import pytest

from test_web_records import fixture
from test_web_api import web
from watertreatment.core_view import write_core_view_artifact
from watertreatment.storage import digest_json, hash_file


def register_view(f):
    source = f.run / "candidate.json"
    original = json.loads(source.read_text(encoding="utf-8"))
    mapping = {"version": "research-core-mapping-v1", "policy_version": "0.8",
               "basis": "existing_candidate_review_only", "source_record_sha256": hash_file(source),
               "source_record_fingerprint": digest_json(original), "modules": [],
               "decisions": [{"observation_id": "rate", "layer": "A", "purpose": "key_result",
                              "reason": "Synthetic final material result", "module": None},
                             {"observation_id": "conversion", "layer": "C", "purpose": "ordinary_detail",
                              "reason": "Synthetic detail for archive integrity check", "module": None}],
               "limitations": ["Synthetic view; no new extraction or scientific validation"]}
    mapping_path = f.settings.data_root / "synthetic-mapping.json"
    mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
    folder = f.settings.data_root / "workbench" / "core_views" / hash_file(source) / "version-one"
    write_core_view_artifact(source, mapping_path, folder)
    return folder, source.read_bytes()


def test_absent_view_preserves_complete_record_and_does_not_queue_work(web):
    client, service, f = web
    before = (f.run / "candidate.json").read_bytes()
    response = client.get(f"/api/records/{f.run.name}/core-view")
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert not (f.settings.data_root / "workbench" / "core_views").exists()
    assert service.jobs.list() == []
    assert (f.run / "candidate.json").read_bytes() == before
    assert client.get(f"/api/records/{f.run.name}/core-view/download").status_code == 404


def test_registered_view_keeps_archive_download_and_audit_identity(web):
    client, service, f = web
    folder, before = register_view(f)
    base = f"/api/records/{f.run.name}"
    previous = service.records.get(f.run.name)
    result = client.get(base + "/core-view").json()
    assert result["available"] is True
    view = result["view"]
    assert view["core_observation_ids"] == ["rate"]
    assert view["deferred_observation_ids"] == ["conversion"]
    assert view["provenance"]["new_source_audit_performed"] is False
    assert view["provenance"]["human_validated"] is False
    assert client.get(base + "/download").content == before
    assert client.get(base + "/core-view/download").json() == view
    assert service.records.get(f.run.name)["summary"] == previous["summary"]
    assert service.records.get(f.run.name)["audit"] == previous["audit"]
    assert service.jobs.list() == []
    assert service.records.publication_scope() == []
    assert len(list(folder.iterdir())) == 3


@pytest.mark.parametrize("target", ["core-view.json", "mapping.json"])
def test_changed_artifact_rejected_instead_of_silently_displayed(web, target):
    client, _, f = web
    folder, before = register_view(f)
    path = folder / target
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert client.get(f"/api/records/{f.run.name}/core-view").status_code == 409
    assert (f.run / "candidate.json").read_bytes() == before


def test_self_consistent_edited_view_does_not_override_mapping(web):
    client, _, f = web
    folder, _ = register_view(f)
    path = folder / "core-view.json"
    view = json.loads(path.read_text(encoding="utf-8"))
    view["observations"][0]["value"]["value"] = 999
    path.write_text(json.dumps(view), encoding="utf-8")
    manifest_path = folder / "core-view-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["view_sha256"] = hash_file(path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert client.get(f"/api/records/{f.run.name}/core-view").status_code == 409


def test_registry_manifest_cannot_read_artifacts_outside_version(web):
    client, _, f = web
    folder, _ = register_view(f)
    copied = folder.parent / "outside.json"
    copied.write_bytes((folder / "mapping.json").read_bytes())
    path = folder / "core-view-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["mapping_path"] = "../outside.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert client.get(f"/api/records/{f.run.name}/core-view").status_code == 409


def test_other_candidate_hash_or_version_cannot_be_substituted(web):
    client, _, f = web
    folder, _ = register_view(f)
    base = f"/api/records/{f.run.name}/core-view"
    assert client.get(base, params={"view_id": "unregistered-version"}).status_code == 404
    assert client.get(base, params={"view_id": "../candidate"}).status_code == 409
    path = folder / "core-view-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["source_record_sha256"] = "f" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert client.get(base).status_code == 409


def test_native_rule08_projects_extracted_scope_without_legacy_registry(web, monkeypatch):
    from test_design_scope import design_record
    client, service, f = web
    detail = service.records.get(f.run.name)
    detail["record"] = design_record().model_dump(mode="json")
    monkeypatch.setattr(service.records, "get", lambda identifier: detail)
    result = client.get(f"/api/records/{f.run.name}/core-view").json()
    assert result["available"] is True
    view = result["view"]
    assert view["provenance"]["kind"] == "projection_of_extracted_scope"
    assert view["counts"]["deferred_observations"] == 0
    assert view["counts"]["deferred_topics"] == 1
    assert view["source_index"][0]["deferred_topics"] == ["dense_time_series"]
    assert view["source_index"][0]["state"] == "read"
    assert not (f.settings.data_root / "workbench" / "core_views").exists()
    assert service.jobs.list() == []


def test_explicit_view_selection_keeps_previous_view_downloadable(web):
    client, _, f = web
    latest, before = register_view(f)
    source = f.run / "candidate.json"
    older = latest.parent / "older-presentation"
    write_core_view_artifact(source, f.settings.data_root / "synthetic-mapping.json", older,
                             view_version="research-core-view-v1")
    latest_manifest = json.loads((latest / "core-view-manifest.json").read_text(encoding="utf-8"))
    older_manifest = json.loads((older / "core-view-manifest.json").read_text(encoding="utf-8"))
    base = f"/api/records/{f.run.name}"
    # No arbitrary lexicographic hash order chooses a scientific presentation.
    assert client.get(base + "/core-view").status_code == 409
    selection = {"version": "research-core-view-selection-v1", "source_record_sha256": hash_file(source),
                 "view_id": latest_manifest["view_id"],
                 "manifest_sha256": hash_file(latest / "core-view-manifest.json")}
    (latest.parent / "current.json").write_text(json.dumps(selection), encoding="utf-8")
    current = client.get(base + "/core-view").json()
    assert current["view"]["version"] == "research-core-view-v2"
    assert len(current["versions"]) == 2
    old = client.get(base + "/core-view", params={"view_id": older_manifest["view_id"]}).json()
    assert old["view"] == json.loads((older / "core-view.json").read_text(encoding="utf-8"))
    assert old["view"]["version"] == "research-core-view-v1"
    assert client.get(base + "/download").content == before
    selection["manifest_sha256"] = "f" * 64
    (latest.parent / "current.json").write_text(json.dumps(selection), encoding="utf-8")
    assert client.get(base + "/core-view").status_code == 409
    # Explicit old view remains recoverable even with a damaged default pointer.
    assert client.get(base + "/core-view", params={"view_id": older_manifest["view_id"]}).status_code == 200
