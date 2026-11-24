"""Frozen workflow budget integration, using synthetic local clients only."""
import copy
import json

import pytest

from test_active_reading import active, FakeReader
from test_workflow import workspace
from test_web_model import model_fixture, fixture, ReplayClient, audit
from test_repair_guidance import failed_repair, rewrite
from watertreatment.domain import ExtractionRecord
from watertreatment.reader_workflow import execute_reader
from watertreatment.storage import DataStore, digest_json, hash_file, write_new_json
from watertreatment.web_model import reaudit, _repair_guidance
from watertreatment.workflow import _read_bundle


def test_fresh_active_plan_freezes_unknown_candidate_reserve(active):
    workspace, run, bundle, plan = active
    budget = plan["reading_tool_budget"]
    assert budget["candidate_known"] is False
    assert budget["candidate_query_limit"] == 800
    assert budget["source_limit"] == plan["reading_settings"]["max_tool_calls"]
    assert budget["max_model_turns"] == plan["max_calls"]
    assert budget["total_attempt_limit"] == budget["source_limit"] + 800


def test_web_reaudit_uses_frozen_candidate_size_and_separate_pools(model_fixture):
    f = model_fixture
    replay = ReplayClient([audit()])
    result = reaudit(f.settings, f.records, f.run.name, "Check synthetic source binding", client_factory=replay.factory)
    plan = json.loads((f.settings.data_root / "runs" / result["run_id"] / "plan.json").read_text(encoding="utf-8"))
    budget = plan["reading_tool_budget"]
    assert budget["candidate_content_hash"] == digest_json(f.record.model_dump(mode="json"))
    assert budget["candidate_counts"]["observations"] == len(f.record.observations)
    assert budget["source_limit"] == replay.tools.max_calls == 160
    assert budget["candidate_query_limit"] == replay.tools.candidate_query_limit
    assert budget["max_model_turns"] == 5
    assert replay.calls_started == 1 and not result["result"]["graph_published"]


def test_legacy_frozen_plan_keeps_shared_tool_budget(active):
    workspace, run, bundle, original = active
    plan = copy.deepcopy(original)
    plan.pop("reading_tool_budget")
    client = FakeReader(workspace.settings, bundle)
    _, documents = _read_bundle(run / "sources.json")
    execute_reader(workspace.settings, plan, run, [run / "sources.json"], documents,
                   DataStore(workspace.settings.data_root), client)
    assert client.tools.candidate_query_limit is None
    assert client.tools.max_calls == plan["reading_settings"]["max_tool_calls"]


@pytest.mark.parametrize("field", ["max_model_turns", "source_limit"])
def test_reader_rejects_budget_inconsistent_with_frozen_limits(active, field):
    workspace, run, bundle, original = active
    from watertreatment.reading_budget import reading_tool_budget
    plan = copy.deepcopy(original)
    turns, sources = plan["max_calls"], plan["reading_settings"]["max_tool_calls"]
    plan["reading_tool_budget"] = reading_tool_budget(None,
        max_model_turns=turns + (field == "max_model_turns"), source_limit=sources + (field == "source_limit"))
    client = FakeReader(workspace.settings, bundle)
    _, documents = _read_bundle(run / "sources.json")
    with pytest.raises(ValueError, match="frozen workflow limits"):
        execute_reader(workspace.settings, plan, run, [], documents, DataStore(workspace.settings.data_root), client)
    assert client.calls_started == 0


def bound_guidance(case):
    record = ExtractionRecord.model_validate(case.detail["record"])
    snapshot = case.prior / "candidate_snapshots" / "scope-guidance.json"
    write_new_json(snapshot, record.model_dump(mode="json"))
    binding = {"path": str(snapshot), "sha256": hash_file(snapshot),
               "content_hash": digest_json(record.model_dump(mode="json")), "scope": "scope-guidance"}
    query = case.prior / "reading_tools" / "query-guidance"
    write_new_json(query / "request.json", {"tool": "wt_query_candidate", "arguments": {"section": "observations"}})
    write_new_json(query / "result.json", {"success": True, "data": {
        "candidate_binding": binding, "mode": "section", "returned_count": 1}})
    envelope = json.loads((case.call / "request.json").read_text(encoding="utf-8"))
    request = envelope["request"]
    request.update(candidate_binding=binding, prompt="CANDIDATE_SNAPSHOT " + binding["content_hash"])
    request["sources"].append({"path": str(snapshot), "sha256": binding["sha256"]})
    rewrite(case.call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    return snapshot, query, request


def test_bound_audit_guidance_preserves_snapshot_and_query_proofs(failed_repair):
    case = failed_repair
    snapshot, query, request = bound_guidance(case)
    result, paths = _repair_guidance(case.f.settings, case.f.records, case.detail, case.prior.name)
    assert result == audit(revision=True)
    assert {snapshot, query / "request.json", query / "result.json"}.issubset(set(paths))
    assert case.f.record.model_dump_json(exclude_defaults=True) not in request["prompt"]


def test_invalid_bound_guidance_cannot_fall_back_to_literal(failed_repair):
    case = failed_repair
    _, _, request = bound_guidance(case)
    request["candidate_binding"]["scope"] = "wrong-scope"
    request["prompt"] += case.f.record.model_dump_json(exclude_defaults=True)
    rewrite(case.call / "request.json", {"request": request, "fingerprint": digest_json(request)})
    with pytest.raises(ValueError, match="matching completed"):
        _repair_guidance(case.f.settings, case.f.records, case.detail, case.prior.name)


def test_app_server_freezes_actual_split_budget_in_request(active):
    from pathlib import Path
    from test_app_server import CandidateAuditReplay
    from watertreatment.app_server import AppServerClient
    from watertreatment.reading import ReadingAudit
    from watertreatment.reading_tools import ReadingTools
    workspace, run, bundle, plan = active
    candidate = ExtractionRecord.model_validate({"paper": {"id": "synthetic-budget",
        "source_ids": [d["source"]["source_id"] for d in bundle["documents"]]}})
    path = run / "budget-candidate.json"
    write_new_json(path, candidate.model_dump(mode="json"))
    tools = ReadingTools(bundle, run, max_calls=2, candidate_query_limit=3, expected_inputs=plan["input_paths"])
    tools.bind_candidate(candidate, path)
    assert tools.handle("wt_inventory", {})["success"]
    transport = CandidateAuditReplay("wt_query_candidate", {"section": "paper"})
    client = AppServerClient(workspace.settings.codex, live=True,
        transport_factory=lambda *args: transport, config_factory=lambda: {})
    client.bind_tools(tools)
    try:
        client.complete(ReadingAudit, prompt=candidate.model_dump_json(exclude_defaults=True),
            input_paths=[Path(i["path"]) for i in plan["input_paths"]], workdir=run,
            audit_root=run / "calls", identity={"stage": "audit_budget"})
    finally:
        client.close()
    envelope = json.loads(next((run / "calls").glob("*/request.json")).read_text(encoding="utf-8"))
    request = envelope["request"]
    assert envelope["fingerprint"] == digest_json(request)
    assert request["tool_budget_at_start"]["remaining"] == {
        "total_attempts": 4, "source_and_revision": 1, "candidate_queries": 3}
    assert "FROZEN TOOL WORK BUDGET" in request["prompt"]
    assert tools.tool_budget_state()["attempts"] == {"total": 2, "source_and_revision": 1, "candidate_queries": 1}
