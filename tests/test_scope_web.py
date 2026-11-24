"""Offline scope/queue boundaries; synthetic records are not scientific validation."""
from __future__ import annotations

import copy
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from test_candidate_view import binding, sample, unpack
from test_web_graph import Driver, FIRST, graph_context, graph_settings
from watertreatment.candidate_view import candidate_view
from watertreatment.settings import CodexSettings, Settings
from watertreatment.web_api import create_app
from watertreatment.web_graph import read_graph
from watertreatment.web_service import Workbench


@pytest.fixture
def scope_service(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path / "code", data_root=tmp_path / "data",
                        source_roots=[], rules_version="0.7",
                        codex=CodexSettings(model="gpt-6.2-sol", reasoning_effort="max"))
    service = Workbench(settings)
    # This boundary fake does not replace production admission/source validation.
    detail = {"id": "record-current", "summary": {
        "current": True, "admissible": True, "source_change": None},
        "record": {"schema_version": "0.3.0", "rule_version": "0.7", "extraction_scope": "research_core",
            "quantitative_coverage": [
                {"id": "curve-a", "data_kind": "time_series", "scope": "final_material", "state": "partial"},
                {"id": "curve-b", "data_kind": "time_series", "scope": "necessary_control", "state": "complete"},
                {"id": "scalar-a", "data_kind": "scalar", "scope": "final_material", "state": "complete"},
                {"id": "external-a", "data_kind": "time_series", "scope": "external_benchmark", "state": "partial"},
                {"id": "excluded-a", "data_kind": "time_series", "scope": "other", "state": "out_of_scope"},
            ]}}

    def get(identifier):
        if identifier != detail["id"]:
            raise KeyError(identifier)
        return copy.deepcopy(detail)

    monkeypatch.setattr(service.records, "get", get)
    monkeypatch.setattr(service.records, "publish", Mock(side_effect=AssertionError("must not publish")))
    fake = Mock(return_value={"run_id": "new-synthetic-run", "revision": {"accepted": True}})
    monkeypatch.setattr("watertreatment.web_model.reaudit", fake)
    yield service, detail, fake
    service.close()


@pytest.fixture
def scope_web(scope_service):
    service, detail, fake = scope_service
    app = create_app(service.settings, start_worker=False, service=service)
    with TestClient(app) as client:
        client.headers["X-WT-Token"] = client.get("/api/status").json()["csrf_token"]
        yield client, service, detail, fake


def test_scope_web_digitization_only_queues_selected_series_without_provider_or_publish(scope_web):
    client, service, detail, fake = scope_web
    before = copy.deepcopy(detail)
    response = client.post("/api/digitizations", json={"record_id": detail["id"], "series_ids": ["curve-b", "curve-a"]})
    assert response.status_code == 200
    job = service.jobs.get(response.json()["job_id"])
    assert job["kind"] == "recheck"
    assert job["payload"]["digitization_targets"] == ["curve-b", "curve-a"]
    assert job["payload"]["record_id"] == detail["id"]
    assert job["payload"]["question"]
    assert "auto_publish" not in job["payload"]
    assert detail == before
    fake.assert_not_called()
    service.records.publish.assert_not_called()


@pytest.mark.parametrize("series", [[], ["curve-a"] * 2, ["unknown"], ["scalar-a"], ["external-a"],
                                     ["excluded-a"], [" "], ["curve-a"] * 13])
def test_scope_web_rejects_unbounded_or_ineligible_series_before_queue(scope_web, series):
    client, service, detail, fake = scope_web
    response = client.post("/api/digitizations", json={"record_id": detail["id"], "series_ids": series})
    assert response.status_code in {409, 422}
    assert service.jobs.list() == []
    fake.assert_not_called()


@pytest.mark.parametrize("section,key,value", [
    ("summary", "current", False), ("summary", "admissible", False),
    ("summary", "source_change", "changed"), ("record", "rule_version", "0.6"),
    ("record", "extraction_scope", None),
])
def test_scope_web_only_accepts_current_admissible_new_scope(scope_web, section, key, value):
    client, service, detail, fake = scope_web
    detail[section][key] = value
    response = client.post("/api/digitizations", json={"record_id": detail["id"], "series_ids": ["curve-a"]})
    assert response.status_code == 409
    assert service.jobs.list() == []
    fake.assert_not_called()


def test_scope_web_requires_loaded_rules_and_exposes_active_scope(scope_web):
    client, service, detail, _ = scope_web
    assert client.get("/api/status").json()["default_extraction_scope"] == "research_core"
    service.settings.rules_version = "0.6"
    status = client.get("/api/status").json()
    assert status["rules_version"] == "0.6" and status["default_extraction_scope"] is None
    response = client.post("/api/digitizations", json={"record_id": detail["id"], "series_ids": ["curve-a"]})
    assert response.status_code == 409
    assert service.jobs.list() == []


def test_scope_web_csrf_and_no_automatic_publication_switch(scope_web):
    client, service, detail, fake = scope_web
    payload = {"record_id": detail["id"], "series_ids": ["curve-a"]}
    token = client.headers.pop("X-WT-Token")
    assert client.post("/api/digitizations", json=payload).status_code == 403
    client.headers["X-WT-Token"] = token
    assert client.post("/api/digitizations", json={**payload, "auto_publish": True}).status_code == 422
    assert client.post("/api/digitizations", json={**payload, "question": "x" * 8001}).status_code == 422
    assert service.jobs.list() == []
    fake.assert_not_called()


def test_scope_web_execution_forwards_only_explicit_scope_and_preserves_question(scope_service):
    service, detail, fake = scope_service
    identifier = service.queue_digitization(detail["id"], ["curve-a"], "  Read the first 20 minutes.  ")
    result = service.handle_job(service.jobs.get(identifier), lambda *a, **k: None)
    assert result["run_id"] == "new-synthetic-run"
    assert fake.call_args.args[2:] == (detail["id"], "Read the first 20 minutes.")
    assert fake.call_args.kwargs["digitization_targets"] == ["curve-a"]
    service.records.publish.assert_not_called()


def test_scope_web_execution_revalidates_queued_source_and_scope(scope_service):
    service, detail, fake = scope_service
    identifier = service.queue_digitization(detail["id"], ["curve-a"])
    detail["summary"]["source_change"] = "Source changed after queueing"
    with pytest.raises(ValueError, match="当前版本"):
        service.handle_job(service.jobs.get(identifier), lambda *a, **k: None)
    fake.assert_not_called()
    service.records.publish.assert_not_called()


def test_scope_web_ordinary_recheck_does_not_gain_digitization_authority(scope_service):
    service, detail, fake = scope_service
    identifier = service.jobs.enqueue("recheck", {"record_id": detail["id"], "question": "Check the endpoint"})
    service.handle_job(service.jobs.get(identifier), lambda *a, **k: None)
    assert set(fake.call_args.kwargs) == {"progress"}


def test_scope_web_retry_preserves_and_binds_exact_selected_series(scope_service):
    service, detail, fake = scope_service
    identifier = service.queue_digitization(detail["id"], ["curve-a"])
    before = copy.deepcopy(service.jobs.get(identifier)["payload"])
    with service.jobs.db() as db:
        db.execute("UPDATE jobs SET status='failed' WHERE id=?", (identifier,))
    job = service.jobs.get(service.jobs.retry(identifier))
    service.handle_job(job, lambda *a, **k: None)
    assert fake.call_args.kwargs["digitization_targets"] == ["curve-a"]
    fake.reset_mock()
    job["payload"]["digitization_targets"] = ["curve-b"]
    with pytest.raises(ValueError, match="不一致"):
        service.handle_job(job, lambda *a, **k: None)
    fake.assert_not_called()
    assert service.jobs.get(identifier)["payload"] == before


def test_scope_web_series_queries_remain_record_bound_archive_navigation(graph_settings, graph_context):
    row = {"snapshot_id": FIRST, "node_id": "series-node", "labels": ["WTEntity", "ObservationSeries"],
           "properties": {"id": "series-node", "wt_snapshot": FIRST,
                          "source_series_id": "curve-a", "label": "Synthetic mass-change curve"}}
    driver = Driver([row])
    result = read_graph(graph_settings, graph_context, {"entity": "series", "record_id": "record-a",
        "keyword": "curve-a"}, live=True, driver_factory=driver.factory)
    assert driver.queries[1][1]["entity_labels"] == ["ObservationSeries"]
    assert result["items"][0]["record_id"] == "record-a"
    assert result["items"][0]["properties"]["source_series_id"] == "curve-a"
    assert result["persistent_graph_write"] is False
    instruction = result["normalization_instruction"]
    assert "quantitative_coverage" in instruction and "not an independent experiment" in instruction


def test_scope_web_candidate_view_labels_scope_without_reclassifying_history():
    legacy = sample()
    _, legacy_view = unpack(candidate_view(legacy, binding(legacy)))
    assert "extraction_scope" not in legacy_view and "analysis_role_counts" not in legacy_view
    current = legacy.model_copy(deep=True)
    current.schema_version = "0.3.0"
    current.rule_version = "0.7"
    current.extraction_scope = "research_core"
    current.observations[0].analysis_role = "dense_series_point"
    current.observations[0].role_reason = "Synthetic previously archived interior point."
    before = current.model_dump(mode="json")
    _, view = unpack(candidate_view(current, binding(current)))
    assert view["extraction_scope"] == "research_core"
    assert view["analysis_role_counts"] == {"dense_series_point": 1}
    assert view["observation_analysis_roles"]["rows"] == [[0, "point-0", "dense_series_point"]]
    assert len(view["observations"]["rows"]) == 1
    assert current.model_dump(mode="json") == before
