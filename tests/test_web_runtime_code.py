"""Offline startup identity and stale-backend gates; no provider or Neo4j."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from watertreatment import runtime_code
from watertreatment.runtime_code import RuntimeCodeGuard, RuntimeRestartRequired
from watertreatment.settings import Settings, CodexSettings, ReadingSettings
from watertreatment.web_api import create_app
from watertreatment.web_service import Workbench


@pytest.fixture
def code_tree(tmp_path):
    root = tmp_path / "code"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("answer = 1\n", encoding="utf-8")
    return root


def change(root):
    (root / "src" / "app.py").write_text("answer = 222\n", encoding="utf-8")


def test_startup_and_disk_digests_are_distinct_and_restart_is_latched(code_tree):
    guard = RuntimeCodeGuard(code_tree)
    initial = guard.complete_startup()
    assert initial["startup_tree_digest"] == initial["current_disk_digest"]
    assert initial["memory_attestation"] is False
    change(code_tree)
    changed = guard.status(force=True)
    assert changed["restart_required"] and not changed["model_jobs_allowed"]
    assert changed["startup_tree_digest"] == initial["startup_tree_digest"]
    assert changed["current_disk_digest"] != initial["startup_tree_digest"]
    (code_tree / "src" / "app.py").write_text("answer = 1\n", encoding="utf-8")
    assert guard.status(force=True)["restart_required"]
    with pytest.raises(RuntimeRestartRequired, match="没有启动模型"):
        guard.require_model_ready()
    assert not RuntimeCodeGuard(code_tree).complete_startup()["restart_required"]


def test_edits_during_package_import_are_detected_even_if_content_is_restored(code_tree):
    guard = RuntimeCodeGuard(code_tree)
    change(code_tree)
    (code_tree / "src" / "app.py").write_text("answer = 1\n", encoding="utf-8")
    assert guard.complete_startup()["restart_required"]
    assert "导入期间" in guard.reason


def test_observed_metadata_change_after_startup_requires_restart_even_with_same_bytes(code_tree):
    guard = RuntimeCodeGuard(code_tree)
    guard.complete_startup()
    change(code_tree)
    (code_tree / "src" / "app.py").write_text("answer = 1\n", encoding="utf-8")
    state = guard.status(force=True)
    assert state["restart_required"]
    assert state["current_disk_digest"] == state["startup_tree_digest"]
    assert "内容已恢复" in state["reason"]


def test_unstable_initial_snapshot_is_not_claimed_as_loaded_code(code_tree, monkeypatch):
    digest = runtime_code._digest
    def racing(root, signature):
        value = digest(root, signature)
        change(root)
        return value
    monkeypatch.setattr(runtime_code, "_digest", racing)
    state = RuntimeCodeGuard(code_tree).status()
    assert state["restart_required"] and state["startup_tree_digest"] is None


def test_unchanged_poll_and_dispatch_do_not_rehash_and_local_config_is_excluded(code_tree, monkeypatch):
    guard = RuntimeCodeGuard(code_tree)
    guard.complete_startup()
    def unexpected(*args):
        raise AssertionError("unchanged source must not be hashed repeatedly")
    monkeypatch.setattr(runtime_code, "_digest", unexpected)
    (code_tree / "config").mkdir()
    (code_tree / "config" / "project.local.json").write_text('{"synthetic_secret":"do-not-read"}')
    for _ in range(3):
        assert not guard.status(force=True)["restart_required"]
        guard.require_model_ready()


@pytest.fixture
def service(tmp_path, code_tree):
    sources = tmp_path / "papers"
    sources.mkdir()
    configured_project = tmp_path / "separate-configured-project"
    configured_project.mkdir()
    settings = Settings(project_root=configured_project, data_root=tmp_path / "data", source_roots=[sources],
        codex=CodexSettings(model="gpt-6.2-sol", reasoning_effort="max"), reading=ReadingSettings(enabled=True))
    guard = RuntimeCodeGuard(code_tree)
    instance = Workbench(settings, runtime_code_guard=guard)
    instance.jobs.handler = instance.handle_job
    yield instance
    instance.close()


def test_configured_project_does_not_masquerade_as_running_package(service, code_tree):
    assert service.runtime_code.root == code_tree
    assert service.settings.project_root != code_tree
    assert not service.require_model_ready()["restart_required"]
    assert runtime_code.PROCESS_CODE_GUARD.root == Path(runtime_code.__file__).resolve().parents[2]


@pytest.mark.parametrize("kind", ["extract", "recover", "recheck", "ask"])
def test_submission_and_dispatch_stop_all_model_job_kinds(service, code_tree, kind):
    pending = service.jobs.enqueue(kind, {"synthetic": "retained"})
    following = service.jobs.enqueue(kind, {"synthetic": "also-retained"})
    change(code_tree)
    with pytest.raises(RuntimeRestartRequired):
        service.enqueue_model_job(kind, {"synthetic": "never-enqueued"})
    assert len(service.jobs.list()) == 2
    assert not service.jobs.run_one()
    assert service.jobs.paused
    deferred = service.jobs.get(pending)
    assert deferred["status"] == "queued" and "重启" in deferred["stage"]
    assert deferred["error"] is None and deferred["result"] is None
    assert deferred["payload"] == {"synthetic": "retained"}
    assert service.jobs.get(following)["status"] == "queued"
    assert not service.jobs.run_one()
    assert service.store.runs() == []


def test_recovery_and_auto_grants_are_blocked_before_any_reservation(service, code_tree, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("stale backend must not inspect or reserve model work")
    monkeypatch.setattr(service.recovery, "queue", unexpected)
    monkeypatch.setattr(service.recovery, "queue_extension", unexpected)
    monkeypatch.setattr(service.recovery, "automatic_budget_extension", unexpected)
    change(code_tree)
    with pytest.raises(RuntimeRestartRequired):
        service.queue_recovery("old", "continue_candidate", "a" * 64)
    with pytest.raises(RuntimeRestartRequired):
        service.queue_budget_extension("old", "a" * 64)
    with pytest.raises(RuntimeRestartRequired):
        service.queue_extractions(["unregistered"])
    with pytest.raises(RuntimeRestartRequired):
        service.queue_digitization("unregistered", ["series"])
    assert service._after_terminal_job({"id": "old"})["action"] == "restart_required"
    assert service.jobs.list() == []


def test_api_reports_stale_version_but_keeps_read_only_access_and_old_queue(service, code_tree, monkeypatch):
    job_id = service.jobs.enqueue("ask", {"question": "retained", "scope": "published"})
    app = create_app(service.settings, start_worker=False, service=service)
    with TestClient(app) as client:
        original = client.get("/api/status").json()
        client.headers["X-WT-Token"] = original["csrf_token"]
        change(code_tree)
        service.runtime_code.status(force=True)
        for path in ("/api/status", "/api/runtime", "/api/jobs", "/api/papers", "/api/records"):
            assert client.get(path).status_code == 200
        runtime = client.get("/api/runtime").json()
        assert runtime["startup_tree_digest"] == original["runtime_code"]["startup_tree_digest"]
        assert runtime["restart_required"]
        monkeypatch.setattr(service.records, "get", lambda key: {"summary": {"current": True, "source_change": False}, "record": {"observations": []}})
        calls = [
            ("/api/ask", {"question": "test", "scope": "published"}),
            ("/api/recheck", {"record_id": "synthetic", "question": "test"}),
            ("/api/extractions", {"paper_ids": ["synthetic"]}),
            ("/api/digitizations", {"record_id": "synthetic", "series_ids": ["synthetic"]}),
            (f"/api/jobs/{job_id}/recover", {"action": "continue_candidate", "base_fingerprint": "a" * 64}),
            (f"/api/jobs/{job_id}/budget-extension", {"base_fingerprint": "a" * 64}),
            (f"/api/jobs/{job_id}/retry", {}),
        ]
        for path, body in calls:
            response = client.post(path, json=body)
            assert response.status_code == 409, (path, response.text)
            assert "重启" in response.json()["detail"]
        assert len(service.jobs.list()) == 1
        assert service.jobs.get(job_id)["status"] == "queued"
    assert service.store.runs() == []


def test_runtime_ui_labels_startup_snapshot_and_disk_separately():
    root = Path(__file__).resolve().parents[1] / "src" / "watertreatment" / "web_static"
    script = (root / "app.js").read_text(encoding="utf-8")
    html = (root / "index.html").read_text(encoding="utf-8")
    assert 'id="runtime-banner"' in html
    assert 'api("/api/runtime")' in script
    assert "startup_tree_digest" in script and "current_disk_digest" in script
    assert "刷新浏览器不会重启后台" in script
    assert "不是 Python 内存代码的逐字节认证" in script
    assert 'control.disabled=needsRestart' in script
    assert "先重启后台再继续" in script


@pytest.mark.parametrize("kind", ["extract", "recover", "recheck", "ask"])
def test_stale_resume_api_preserves_model_queue_and_still_allows_pause(service, code_tree, kind):
    identifier = service.jobs.enqueue(kind, {"synthetic": "unchanged"})
    service.jobs.pause(True)
    app = create_app(service.settings, start_worker=False, service=service)
    with TestClient(app) as client:
        client.headers["X-WT-Token"] = client.get("/api/status").json()["csrf_token"]
        change(code_tree)
        response = client.post("/api/queue", json={"paused": False})
        assert response.status_code == 409 and "重启" in response.json()["detail"]
        assert service.jobs.paused
        assert client.post("/api/queue", json={"paused": True}).json() == {"paused": True}
        assert service.jobs.get(identifier)["status"] == "queued"
        assert service.jobs.get(identifier)["payload"] == {"synthetic": "unchanged"}


@pytest.mark.parametrize("kind", [None, "scan", "publish"])
def test_stale_gate_does_not_prevent_resuming_non_model_queue(service, code_tree, kind):
    if kind:
        service.jobs.enqueue(kind, {"synthetic": "local-only-test"})
    service.jobs.pause(True)
    change(code_tree)
    assert service.set_queue_paused(False) is False
    assert not service.jobs.paused
    # No worker is started and no queued operation is executed by this test.
