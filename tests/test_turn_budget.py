"""Offline monotonic deadline replay; never call a provider or a database."""
import json
from pathlib import Path

import pytest

from test_active_reading import active, toolset
from test_app_server import Answer, ReplayTransport
from test_record_patch import patch_case
from test_workflow import workspace
from watertreatment.app_server import AppServerClient


def result_data(response):
    assert response["success"]
    return json.loads(response["contentItems"][0]["text"])


def test_tool_budget_is_unknown_unbound_and_uses_clock_after_tool_work(patch_case, monkeypatch):
    tools = patch_case.tools
    clock = [100.2]
    monkeypatch.setattr("watertreatment.reading_tools.time.monotonic", lambda: clock[0])
    assert result_data(tools.handle("wt_inventory", {}))["remaining_turn_seconds"] is None
    tools.bind_turn_deadline(120.9)
    assert result_data(tools.handle("wt_inventory", {}))["remaining_turn_seconds"] == 20
    clock[0] = 105.1
    execute = tools._execute
    def slow_execute(*args):
        clock[0] += 7
        return execute(*args)
    monkeypatch.setattr(tools, "_execute", slow_execute)
    assert result_data(tools.handle("wt_inventory", {}))["remaining_turn_seconds"] == 8
    clock[0] = 130
    failure = tools.handle("not-authorized", {})
    assert not failure["success"]
    assert json.loads(failure["contentItems"][1]["text"])["remaining_turn_seconds"] == 0
    tools.bind_turn_deadline(None)
    assert tools.remaining_turn_seconds() is None
    tools.bind_turn_deadline(500)
    tools.begin_submission(False)
    assert tools.remaining_turn_seconds() is None


class TimedReplay(ReplayTransport):
    def __init__(self, clock, *, turn_delay=7):
        super().__init__()
        self.clock = clock
        self.turn_delay = turn_delay

    def send(self, message):
        if message.get("method") == "turn/start":
            self.clock[0] += self.turn_delay
        super().send(message)


@pytest.mark.parametrize("per_call,total,expected_deadlines,expected_remaining", [
    (120, 300, [1120, 1150], [113, 113]),
    (120, 50, [1050, 1050], [43, 13]),
])
@pytest.mark.parametrize("custom_handle", [False, True])
def test_client_binds_enforced_minimum_and_returns_true_budget_for_each_turn(
        active, monkeypatch, per_call, total, expected_deadlines, expected_remaining, custom_handle):
    project, run, _, plan = active
    project.settings.codex.timeout_seconds = per_call
    clock = [1000]
    monkeypatch.setattr("watertreatment.app_server.time.monotonic", lambda: clock[0])
    transport = TimedReplay(clock)
    tools = toolset(active)
    if custom_handle:
        handle = tools.handle
        def handle_without_budget(*args):
            result = handle(*args)
            payload = json.loads(result["contentItems"][0]["text"])
            payload.pop("remaining_turn_seconds", None)
            result["contentItems"][0]["text"] = json.dumps(payload)
            return result
        monkeypatch.setattr(tools, "handle", handle_without_budget)
    bound = []
    bind = tools.bind_turn_deadline
    def observe_deadline(deadline):
        if deadline is not None:
            bound.append(deadline)
        bind(deadline)
    monkeypatch.setattr(tools, "bind_turn_deadline", observe_deadline)
    client = AppServerClient(project.settings.codex, live=True, total_timeout_seconds=total,
        transport_factory=lambda *args: transport, config_factory=lambda: {})
    client.bind_tools(tools)
    kwargs = {"prompt": "Synthetic audit", "input_paths": [Path(item["path"]) for item in plan["input_paths"]],
              "workdir": run, "audit_root": run / "calls", "identity": {"stage": "synthetic-budget"}}
    try:
        client.complete(Answer, **kwargs)
        assert tools.remaining_turn_seconds() is None
        clock[0] = 1030
        client.complete(Answer, **kwargs)
        assert tools.remaining_turn_seconds() is None
    finally:
        client.close()
    assert bound == expected_deadlines
    replies = [json.loads(message["result"]["contentItems"][-1]["text"])
               for message in transport.sent if message.get("id") == 900 and "result" in message]
    assert [reply["remaining_turn_seconds"] for reply in replies] == expected_remaining
    assert all(reply["budget_source"] == "controller_monotonic_deadline" for reply in replies)
    calls = sorted((run / "calls").glob("call-*"))
    for call, deadline, start in zip(calls, expected_deadlines, (1000, 1030)):
        envelope = json.loads((call / "request.json").read_text(encoding="utf-8"))
        prompt = envelope["request"]["prompt"]
        assert "SINGLE-TURN TIME CONTRACT" in prompt and "remaining_turn_seconds" in prompt
        assert "actual latest submission_id" in prompt and "partial/unread/unquantified" in prompt
        assert "not_checked/unresolved" in prompt and "never presented as passing" in prompt
        result = json.loads((call / "result.json").read_text(encoding="utf-8"))
        assert result["turn_budget_seconds_at_binding"] == deadline - start
        assert result["server_selected_model"] == "gpt-6.2-sol" and result["server_selected_effort"] == "max"


def test_exhausted_transport_deadline_is_not_extended_by_budget_reporting(active, monkeypatch):
    project, run, _, plan = active
    project.settings.codex.timeout_seconds = 1
    clock = [1000]
    monkeypatch.setattr("watertreatment.app_server.time.monotonic", lambda: clock[0])
    tools = toolset(active)
    transport = TimedReplay(clock, turn_delay=2)
    client = AppServerClient(project.settings.codex, live=True, total_timeout_seconds=300,
        transport_factory=lambda *args: transport, config_factory=lambda: {})
    client.bind_tools(tools)
    try:
        with pytest.raises(TimeoutError, match="budget exceeded"):
            client.complete(Answer, prompt="Synthetic timeout", input_paths=[Path(i["path"]) for i in plan["input_paths"]],
                workdir=run, audit_root=run / "calls", identity={"stage": "synthetic-budget-timeout"})
    finally:
        client.close()
    assert tools.remaining_turn_seconds() is None and tools.calls_started == 0
    result = json.loads(next((run / "calls").glob("*/result.json")).read_text(encoding="utf-8"))
    assert result["status"] == "failed" and result["turn_budget_seconds_at_binding"] == 1
