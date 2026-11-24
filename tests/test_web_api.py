"""Offline HTTP boundary, source scope and draft workflow checks."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_web_records import fixture, entry, fake_loader
from test_web_library import pdf, MAIN, SI
from watertreatment.web_api import create_app
from watertreatment.web_service import Workbench
from watertreatment.settings import Settings
from watertreatment.storage import hash_file


@pytest.fixture
def web(fixture):
    settings = fixture.settings.model_copy(update={"codex": fixture.settings.codex.model_copy(update={"model": "gpt-6.2-sol"})})
    service = Workbench(settings)
    app = create_app(settings, start_worker=False, service=service)
    with TestClient(app) as client:
        client.headers["X-WT-Token"] = client.get("/api/status").json()["csrf_token"]
        yield client, service, fixture


def test_page_refresh_revalidates_ui_assets_but_never_caches_api_state(web):
    client, _, _ = web
    for path in ("/", "/static/app.js", "/static/styles.css"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
        if "etag" in response.headers:
            unchanged = client.get(path, headers={"If-None-Match": response.headers["etag"]})
            assert unchanged.status_code in {200, 304}
            assert unchanged.headers["cache-control"] == "no-cache"
    assert client.get("/api/papers").headers["cache-control"] == "no-store"


def test_start_is_local_read_only_and_serves_real_records(web):
    client, service, f = web
    status = client.get("/api/status").json()
    # This synthetic result has no library/import entry. It remains accessible,
    # but must not inflate cards whose drilldowns are registered papers.
    assert status["stats"]["records"] == 0 and status["model"] == "gpt-6.2-sol"
    retained = client.get("/api/records").json()["records"]
    assert len(retained) == 1 and retained[0]["in_library"] is False
    assert service.jobs.list() == []
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    detail = client.get(f"/api/records/{f.run.name}").json()
    assert "sources_bundle" not in detail and "record_path" not in detail
    assert detail["summary"]["human_validated"] is False
    assert client.get(f"/api/records/{f.run.name}/download").json() == detail["record"]


def test_budget_defaults_are_read_only_and_match_strict_batch_schema(web):
    from watertreatment.budget_policy import BudgetPolicy
    client, service, _ = web
    before = service.settings.model_dump(mode="json")
    body = client.get("/api/extraction-budget").json()
    assert BudgetPolicy.model_validate(body["defaults"])
    assert body["limits"]["max_total_calls"]["max"] == 20
    status = client.get("/api/status").json()
    assert status["budget_policy_defaults"] == body["defaults"]
    assert status["budget_policy_limits"] == body["limits"]
    assert service.settings.model_dump(mode="json") == before
    assert service.jobs.list() == []


def test_extraction_http_freezes_explicit_policy_and_rejects_unsupported_limits(web, monkeypatch):
    client, service, _ = web
    policy = client.get("/api/extraction-budget").json()["defaults"]
    captured = []
    monkeypatch.setattr(service, "queue_extractions", lambda **kwargs: captured.append(kwargs) or ["new-job"])
    payload = {"paper_ids": ["synthetic-paper"], "budget_policy": policy}
    assert client.post("/api/extractions", json=payload).json() == {"job_ids": ["new-job"]}
    assert captured[0]["budget_policy"] == policy
    for bad in ({**policy, "initial_calls": True}, {**policy, "max_total_calls": 21},
                {**policy, "max_total_seconds": policy["initial_seconds"] - 1},
                {**policy, "model": "other-model"}):
        assert client.post("/api/extractions", json={**payload, "budget_policy": bad}).status_code == 422
    assert len(captured) == 1 and service.jobs.list() == []


def test_budget_extension_http_preview_is_read_only_and_submit_is_explicit(web, monkeypatch):
    client, service, _ = web
    policy = client.get("/api/extraction-budget").json()["defaults"]
    inspected, queued = [], []
    def preview(identifier, budget_policy=None):
        inspected.append((identifier, budget_policy))
        return {"base_fingerprint": "c" * 64, "available": True}
    monkeypatch.setattr(service, "budget_extension_options", preview)
    monkeypatch.setattr(service, "queue_budget_extension", lambda **kwargs:
        queued.append(kwargs) or "budget-child-job")
    url = "/api/jobs/retained-job/budget-extension"
    assert client.get(url).json()["available"]
    assert client.post(url + "/preview", json={"budget_policy": policy}).status_code == 200
    assert inspected == [("retained-job", None), ("retained-job", policy)]
    assert queued == []
    body = {"base_fingerprint": "c" * 64, "budget_policy": policy}
    # The method receives the immutable request fingerprint; no provider is
    # reachable through this HTTP test and no implicit re-extraction is offered.
    monkeypatch.setattr(service, "queue_budget_extension", lambda job_id, **kwargs:
        queued.append({"job_id": job_id, **kwargs}) or "budget-child-job")
    assert client.post(url, json=body).json() == {"job_id": "budget-child-job"}
    assert queued == [{"job_id": "retained-job", **body}]
    assert client.post(url, json={"budget_policy": policy}).status_code == 422
    assert client.post(url, json={**body, "automatic": True}).status_code == 422
    token = client.headers.pop("X-WT-Token")
    assert client.post(url, json=body).status_code == 403
    client.headers["X-WT-Token"] = token
    assert len(queued) == 1 and service.jobs.list() == []


def test_writes_require_same_origin_token_and_host(web):
    client, _, _ = web
    token = client.headers.pop("X-WT-Token")
    assert client.post("/api/refresh").status_code == 403
    client.headers["X-WT-Token"] = token
    assert client.post("/api/refresh", headers={"Origin": "https://untrusted.test"}).status_code == 403
    assert client.get("/api/status", headers={"Host": "untrusted.test"}).status_code == 403
    assert client.post("/api/refresh", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post("/api/queue", json={"paused": True, "extra": "ignored?"}).status_code == 422


def test_only_registered_evidence_is_served_and_hash_checked(web):
    client, _, f = web
    base = f"/api/records/{f.run.name}"
    evidence = client.get(base+"/evidence/source-value").json()
    assert evidence["quote"] == "rate 2.4" and evidence["context"][0]["block_id"] == "paragraph"
    assert client.get(base+"/sources/unregistered/file").status_code == 409
    original = Path(f.records.get(f.run.name)["sources_bundle"])
    source = Path(json.loads(original.read_text(encoding="utf-8"))["documents"][0]["source"]["path"])
    assert client.get(base+"/sources/main/file").content == source.read_bytes()
    source.write_text("tampered", encoding="utf-8")
    assert client.get(base+"/sources/main/file").status_code == 409


def test_draft_save_restore_submit_preserves_original_and_scope(web):
    client, service, f = web
    base = f"/api/records/{f.run.name}"
    original = hash_file(f.run / "candidate.json")
    body = {"reviewer": "offline reviewer", "base_hash": original, "entries": [entry(scope=["value"])]}
    assert client.post(base+"/draft", json=body).status_code == 200
    assert service.records.get(f.run.name)["reviews"] == []
    assert client.get(base+"/draft", params={"reviewer": body["reviewer"]}).json()["entries"] == body["entries"]
    assert client.post(base+"/review", json=body).status_code == 200
    detail = service.records.get(f.run.name)
    assert not detail["summary"]["human_validated"]
    assert detail["reviews"][0]["payload"]["entries"][0]["scope"] == ["value"]
    assert hash_file(f.run / "candidate.json") == original
    body["base_hash"] = "0" * 64
    assert client.post(base+"/draft", json=body).status_code == 409


def test_explicit_actions_queue_but_do_not_connect_in_http_handler(web):
    client, service, f = web
    assert client.post("/api/publish", json={"record_ids": [f.run.name]}).status_code == 200
    assert service.records.publication_scope() == []
    assert client.post("/api/ask", json={"question": "What is reported?", "record_ids": [f.run.name]}).status_code == 200
    assert client.post("/api/recheck", json={"record_id": f.run.name, "question": "Check rate", "observation_id": "absent"}).status_code == 409
    assert {j["kind"] for j in service.jobs.list()} == {"publish", "ask"}
    assert client.post("/api/ask", json={"question": "any?", "record_ids": []}).status_code == 409


def test_recovery_http_requires_a_specific_frozen_choice_without_running_it(web, monkeypatch):
    client, service, _ = web
    preview = {"job_id": "failed-job", "base_fingerprint": "a" * 64,
               "actions": [{"id": "correct_reading", "available": True}]}
    monkeypatch.setattr(service, "recovery_options", lambda job_id: preview)
    accepted = []
    def queue(job_id, action, base_fingerprint):
        accepted.append((job_id, action, base_fingerprint))
        return "new-recovery-job"
    monkeypatch.setattr(service, "queue_recovery", queue)
    assert client.get("/api/jobs/failed-job/recovery").json() == preview
    assert accepted == []
    body = {"action": "correct_reading", "base_fingerprint": "a" * 64}
    assert client.post("/api/jobs/failed-job/recover", json=body).json() == {"job_id": "new-recovery-job"}
    assert accepted == [("failed-job", "correct_reading", "a" * 64)]
    assert client.post("/api/jobs/failed-job/recover", json={**body, "auto_publish": True}).status_code == 422
    assert client.post("/api/jobs/failed-job/recover", json={**body, "action": "arbitrary"}).status_code == 422
    assert client.post("/api/jobs/failed-job/recover", json={"action": "reextract"}).status_code == 422
    token = client.headers.pop("X-WT-Token")
    assert client.post("/api/jobs/failed-job/recover", json=body).status_code == 403
    client.headers["X-WT-Token"] = token
    assert len(accepted) == 1


@pytest.mark.parametrize("action", ["resume_initial", "resume_extraction", "repair_identity", "resume_audit"])
def test_stage_recovery_api_requires_a_frozen_explicit_choice(web, monkeypatch, action):
    client, service, _ = web
    accepted = []
    def queue(job_id, selected, base_fingerprint):
        accepted.append((job_id, selected, base_fingerprint))
        return "stage-recovery"
    monkeypatch.setattr(service, "queue_recovery", lambda job_id, action, base_fingerprint:
                        queue(job_id, action, base_fingerprint))
    body = {"action": action, "base_fingerprint": "b" * 64}
    response = client.post("/api/jobs/retained-failure/recover", json=body)
    assert response.status_code == 200 and response.json() == {"job_id": "stage-recovery"}
    assert accepted == [("retained-failure", action, "b" * 64)]
    assert client.post("/api/jobs/retained-failure/recover", json={**body, "auto_publish": True}).status_code == 422
    assert len(accepted) == 1


def test_all_recovery_routes_have_http_and_activity_contracts():
    from typing import get_args
    from watertreatment.web_api import Recovery
    from watertreatment.web_recovery import LABELS
    from watertreatment.web_activity import RECOVERY_LABELS

    # A newly added backend route must not become a visible-but-HTTP-422 action.
    assert set(get_args(Recovery.model_fields["action"].annotation)) == set(LABELS)
    assert set(RECOVERY_LABELS) == set(LABELS)
    assert all(isinstance(label, str) and label.strip() for label in RECOVERY_LABELS.values())


def test_legacy_extract_retry_cannot_silently_reextract(web):
    client, service, _ = web
    identifier = service.jobs.enqueue("extract", {"paper_id": "synthetic-paper"})
    with service.jobs.db() as db:
        db.execute("UPDATE jobs SET status='failed',error='retained failure' WHERE id=?", (identifier,))
    before = service.jobs.get(identifier)
    response = client.post(f"/api/jobs/{identifier}/retry")
    assert response.status_code == 409
    assert "恢复方案" in response.json()["detail"]
    assert service.jobs.get(identifier) == before
    assert len(service.jobs.list()) == 1


def test_samples_keep_population_and_never_mark_all_human_validated(web):
    client, service, f = web
    sample = client.post("/api/sample", json={"count": 5, "random_count": 1}).json()["sample"]
    assert sample["random_ids"] == [f.run.name] and sample["targeted_ids"] == []
    assert sample["population"][0]["hash"] == hash_file(f.run / "candidate.json")
    assert client.get("/api/samples").json()["samples"][0]["id"] == sample["id"]
    assert service.records.get(f.run.name)["reviews"] == []


def test_published_scope_refuses_stale_versions_without_database_read(web, monkeypatch):
    _, service, f = web
    service.records.publish(f.run.name, live=True, loader=fake_loader)
    service.records.mark_source_stale("synthetic-paper", "changed original")
    monkeypatch.setattr(service, "_graph_context", lambda _: pytest.fail("must block before DB access"))
    with pytest.raises(ValueError, match="待更新"):
        service.question_scope("published", [])


def test_folder_scan_never_accepts_arbitrary_paths(web):
    client, service, f = web
    assert client.post("/api/scan", json={"folders": [str(f.settings.project_root)]}).status_code in {404, 409}
    assert service.jobs.list() == []


def test_pdf_page_images_are_source_bound_and_bounded(tmp_path):
    from watertreatment.workflow import import_paper
    from watertreatment.storage import DataStore
    from watertreatment.web_evidence import EvidenceView
    root = tmp_path / "papers"
    folder = root / "1_sample"
    folder.mkdir(parents=True)
    pdf(folder / "article.pdf", [MAIN, "Second page"])
    settings = Settings(project_root=tmp_path / "code", data_root=tmp_path / "data", source_roots=[root])
    DataStore(settings.data_root).initialize()
    run = import_paper(settings, folder, source_roles={"article.pdf": "main_article"})
    bundle = json.loads((run / "sources.json").read_text(encoding="utf-8"))
    source_id = bundle["documents"][0]["source"]["source_id"]
    class Registered:
        def get(self, identifier):
            assert identifier == "registered"
            return {"sources_bundle": str(run / "sources.json")}
    view = EvidenceView(settings, Registered())
    image = view.page_image("registered", source_id, 2, scale=1)
    assert image.read_bytes().startswith(b"\x89PNG")
    with pytest.raises(ValueError, match="页码"):
        view.page_image("registered", source_id, 3)
    with pytest.raises(ValueError, match="缩放"):
        view.page_image("registered", source_id, 1, 20)
    with pytest.raises(ValueError, match="不属于"):
        view.file("registered", "outside")
