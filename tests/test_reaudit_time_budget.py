"""Offline re-audit deadline and registration tests; no live provider or graph."""
import json
from pathlib import Path

import pytest

from test_active_reading import active, toolset
from test_app_server import Answer, ReplayTransport
from test_web_model import model_fixture, fixture, ReplayClient, audit
from test_workflow import workspace
from watertreatment.app_server import AppServerClient
from watertreatment.codex import ModelCallError
from watertreatment.reaudit_budget import reaudit_time_budget, validate_reaudit_time_budget
from watertreatment.storage import hash_file
from watertreatment.web_model import _fixed_client, reaudit


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("per_turn,total", [(1, 605), (600, 3600), (3600, 18600)])
def test_reaudit_time_budget_reserves_every_turn_and_bounded_controller_time(per_turn, total):
    budget = reaudit_time_budget(per_turn)
    assert budget["max_revisions"] == 2 and budget["max_model_turns"] == 5
    assert budget["per_turn_timeout_seconds"] == per_turn
    assert budget["total_timeout_seconds"] == total <= 18600
    assert total - 5 * per_turn == budget["controller_allowance_seconds"] == 600
    assert validate_reaudit_time_budget(budget) == budget
    assert validate_reaudit_time_budget(budget) is not budget


@pytest.mark.parametrize("value", [0, 3601, True, 3600.0, "3600", None])
def test_reaudit_time_budget_rejects_invalid_or_unbounded_turns(value):
    with pytest.raises(ValueError, match="per-turn timeout"):
        reaudit_time_budget(value)


@pytest.mark.parametrize("field,value", [
    ("policy", "other-policy"), ("max_revisions", 3), ("max_model_turns", 6),
    ("controller_allowance_seconds", 0), ("total_timeout_seconds", 7200),
    ("per_turn_timeout_seconds", 600), ("max_revisions", 2.0),
    ("total_timeout_seconds", 18600.0), ("extra", "unfrozen-input"),
])
def test_reaudit_time_budget_rejects_changed_frozen_limits(field, value):
    budget = reaudit_time_budget(3600)
    budget[field] = value
    with pytest.raises(ValueError, match="frozen re-audit time budget"):
        validate_reaudit_time_budget(budget)


def test_reaudit_time_budget_freezes_client_loop_tools_and_run_metadata(model_fixture):
    f = model_fixture
    f.settings.codex.timeout_seconds = 3600
    original_reading = f.settings.reading.model_dump()
    original_hash = hash_file(f.run / "candidate.json")
    replay = ReplayClient([audit(revision=True), f.record, audit(revision=True), f.record, audit()])

    def change_later_settings(message, **details):
        # Changing mutable settings after the run starts cannot shrink its frozen
        # per-turn limit or change the initial extraction's separate configuration.
        if message == "准备有来源的回查任务":
            f.settings.codex.timeout_seconds = 1

    output = reaudit(f.settings, f.records, f.run.name, "Check synthetic evidence without new extraction",
                     client_factory=replay.factory, progress=change_later_settings)
    run = f.settings.data_root / "runs" / output["run_id"]
    frozen = reaudit_time_budget(3600)
    for artifact in ("manifest.json", "plan.json", "result.json"):
        assert read(run / artifact)["reaudit_time_budget"] == frozen
    assert replay.settings.timeout_seconds == 3600 and replay.timeout == 18600
    assert replay.settings.max_calls_per_run == replay.calls_started == 5
    assert [call["stage"] for call in replay.calls] == [
        "audit_web_0", "revision_web_0", "audit_web_1", "revision_web_1", "audit_web_2"]
    assert read(run / "plan.json")["reading_tool_budget"]["max_model_turns"] == 5
    assert output["result"]["revisions"] == 2 and output["revision"]["accepted"]
    assert f.settings.reading.model_dump() == original_reading
    assert hash_file(f.run / "candidate.json") == original_hash
    assert output["result"]["human_validated"] is False
    assert output["result"]["graph_published"] is False


def test_reaudit_time_budget_keeps_timeout_artifacts_and_current_record(model_fixture, monkeypatch):
    f = model_fixture
    f.settings.codex.timeout_seconds = 3600
    initial_hash = f.records.get(f.run.name)["summary"]["record_hash"]

    def timeout(*args):
        raise TimeoutError("synthetic final-source-audit timeout")

    replay = ReplayClient([timeout])
    for method in ("register_reaudit", "accept_model_revision", "publish"):
        monkeypatch.setattr(f.records, method,
                            lambda *args, **kwargs: pytest.fail("a failed audit cannot register or publish"))
    with pytest.raises(TimeoutError, match="synthetic final-source-audit"):
        reaudit(f.settings, f.records, f.run.name, "Audit an existing candidate",
                client_factory=replay.factory)
    runs = [r for r in f.records.store.runs() if r["kind"] == "web_reaudit"]
    assert len(runs) == 1 and replay.closed
    run = Path(runs[0]["manifest_path"]).parent
    assert "TimeoutError" in read(run / "failure.json")["error"]
    assert (run / "candidate.v00.json").is_file() and not (run / "result.json").exists()
    assert read(run / "plan.json")["reaudit_time_budget"] == reaudit_time_budget(3600)
    current = f.records.get(f.run.name)["summary"]
    assert current["current"] and current["record_hash"] == initial_hash


class SlowAuditReplay(ReplayTransport):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.delay = 0

    def send(self, message):
        if message.get("method") == "initialize":
            self.clock[0] += 50  # Startup is also charged to the session deadline.
        elif message.get("method") == "turn/start":
            self.clock[0] += self.delay
        super().send(message)


@pytest.mark.parametrize("legacy_total", [False, True])
def test_reaudit_time_budget_prevents_global_starvation_of_final_audit(active, monkeypatch, legacy_total):
    project, run, _, plan = active
    project.settings.codex.timeout_seconds = 3600
    frozen = reaudit_time_budget(3600)
    clock = [1000.0]
    monkeypatch.setattr("watertreatment.app_server.time.monotonic", lambda: clock[0])
    transport = SlowAuditReplay(clock)

    def factory(settings, *, live, total_timeout_seconds):
        return AppServerClient(settings, live=live, total_timeout_seconds=total_timeout_seconds,
                               transport_factory=lambda *args: transport, config_factory=lambda: {})

    client = (_fixed_client(project.settings, factory, turns=5, seconds=7200) if legacy_total
              else _fixed_client(project.settings, factory, time_budget=frozen))
    client.bind_tools(toolset(active))
    kwargs = {"prompt": "Synthetic source audit", "input_paths": [Path(p["path"]) for p in plan["input_paths"]],
              "workdir": run, "audit_root": run / "calls", "identity": {"stage": "synthetic-time-budget"}}
    try:
        # The first four legal turns used to leave only 1,080 seconds for the
        # final audit, despite its configured 3,600-second individual allowance.
        for delay in (2500, 760, 2200, 570):
            transport.delay = delay
            assert client.complete(Answer, **kwargs).answer == "source checked"
            clock[0] += 10  # Candidate serialization and checks between turns.
        transport.delay = 1200
        if legacy_total:
            with pytest.raises(TimeoutError, match="budget exceeded"):
                client.complete(Answer, **kwargs)
        else:
            assert client.complete(Answer, **kwargs).answer == "source checked"
            with pytest.raises(ModelCallError, match="turn budget exhausted"):
                client.complete(Answer, **kwargs)
    finally:
        client.close()
    results = [read(p) for p in sorted((run / "calls").glob("*/result.json"))]
    assert len(results) == 5
    if legacy_total:
        assert results[-1]["status"] == "failed" and results[-1]["turn_budget_seconds_at_binding"] == 1080
    else:
        assert results[-1]["status"] == "schema_valid_candidate"
        assert all(r["turn_budget_seconds_at_binding"] == 3600 for r in results)
        assert all(r["server_selected_model"] == "gpt-6.2-sol" and r["server_selected_effort"] == "max" for r in results)
        assert read(run / "app_server" / "session.json")["total_timeout_seconds"] == 18600
