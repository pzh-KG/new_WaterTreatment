"""Offline checks for frozen, bounded candidate/source tool pools."""
import json

import pytest

from test_record_patch import patch_case
from watertreatment.domain import ExtractionRecord
from watertreatment.reading_budget import reading_tool_budget, validate_reading_tool_budget
from watertreatment.reading_tools import ReadingTools, SubmissionReceipt, ToolBudgetExceeded
from watertreatment.storage import digest_json, hash_file, write_new_json


def synthetic_record(points=0):
    return ExtractionRecord.model_validate({"paper": {"id": "synthetic-budget"},
        "experiments": [{"id": "experiment"}],
        "observations": [{"id": f"point-{i}", "experiment_id": "experiment"} for i in range(points)]})


@pytest.mark.parametrize("points,pages,turns,limit,capped", [(0, 2, 5, 160, False), (20, 3, 5, 160, False),
    (21, 4, 5, 160, False), (201, 13, 5, 165, False), (501, 28, 20, 800, True)])
def test_frozen_planner_counts_complete_collections_with_floor_and_cap(points, pages, turns, limit, capped):
    record = synthetic_record(points)
    original = record.model_dump(mode="json")
    budget = reading_tool_budget(record, max_model_turns=turns, source_limit=160)
    assert budget["candidate_known"] and budget["candidate_pages"] == pages
    assert budget["candidate_counts"]["paper"] == 1 and budget["candidate_counts"]["observations"] == points
    assert budget["candidate_counts"]["quantitative_coverage"] == 0
    assert budget["candidate_query_limit"] == limit and budget["total_attempt_limit"] == 160 + limit
    assert budget["candidate_budget_capped"] is capped
    assert budget["candidate_content_hash"] == digest_json(original)
    assert budget["candidate_budget_basis"] == "frozen_candidate_size"
    assert validate_reading_tool_budget(budget, record) == budget
    assert record.model_dump(mode="json") == original


def test_unknown_candidate_reserves_capped_queries_without_claiming_measured_size():
    budget = reading_tool_budget(None, max_model_turns=7, source_limit=160)
    assert budget["candidate_query_limit"] == 800 and budget["total_attempt_limit"] == 960
    assert budget["candidate_known"] is False and budget["candidate_budget_capped"] is False
    assert budget["candidate_counts"] is budget["candidate_content_hash"] is budget["candidate_pages"] is None
    assert budget["candidate_budget_basis"] == "maximum_reserve_unknown_candidate"
    assert validate_reading_tool_budget(budget, synthetic_record(21)) == budget


def test_known_budget_identity_is_frozen_and_returned_policy_is_an_independent_copy():
    record = synthetic_record(21)
    budget = reading_tool_budget(record, max_model_turns=5, source_limit=160)
    checked = validate_reading_tool_budget(budget)
    checked["candidate_counts"]["observations"] = 999
    assert budget["candidate_counts"]["observations"] == 21
    record.paper.title.value = "Later candidate"
    with pytest.raises(ValueError, match="identity differs"):
        validate_reading_tool_budget(budget, record)


@pytest.mark.parametrize("turns,source", [(0, 160), (21, 160), (True, 160), (5.0, 160),
    (5, 0), (5, 301), (5, True), (5, 160.0)])
def test_planner_rejects_invalid_or_coerced_limits(turns, source):
    with pytest.raises(ValueError):
        reading_tool_budget(None, max_model_turns=turns, source_limit=source)


@pytest.mark.parametrize("field,value", [
    ("policy", "unknown-policy"), ("source_limit", 301), ("source_limit", 160.0),
    ("candidate_query_limit", 801), ("candidate_query_limit", 161),
    ("total_attempt_limit", 999), ("max_model_turns", 21), ("page_size", 10),
    ("per_turn_navigation_reserve", 1), ("candidate_known", 1),
    ("candidate_content_hash", "bad-hash"), ("candidate_pages", 99),
    ("candidate_budget_capped", True), ("candidate_budget_basis", "guaranteed_complete"),
    ("candidate_counts", {"paper": True}), ("candidate_counts", {"paper": 1, "observations": -1}),
])
def test_frozen_budget_rejects_changed_policy_formula_and_invalid_counts(field, value):
    budget = reading_tool_budget(synthetic_record(21), max_model_turns=5, source_limit=160)
    budget[field] = value
    with pytest.raises(ValueError):
        validate_reading_tool_budget(budget)


def make_tools(case, *, source_limit=2, candidate_limit=2):
    run = case.tools.run / "pooled-run"
    run.mkdir()
    bundle = {"documents": [document.model_dump(mode="json") for document in case.tools.documents.values()]}
    tools = ReadingTools(bundle, run, max_calls=source_limit, candidate_query_limit=candidate_limit,
                         expected_inputs=[{"path": str(case.source), "sha256": hash_file(case.source)}])
    source = run / "candidate.json"
    write_new_json(source, case.record.model_dump(mode="json"))
    tools.bind_candidate(case.record, source)
    return tools


def data(response):
    assert response["success"], response
    return json.loads(response["contentItems"][0]["text"])


@pytest.mark.parametrize("candidate_first", [True, False])
def test_split_pools_do_not_charge_candidate_queries_to_source_or_revision_work(patch_case, candidate_first):
    tools = make_tools(patch_case)
    queries = [("wt_query_candidate", {"section": "paper"})] * 2
    sources = [("wt_inventory", {})] * 2
    first, second = (queries, sources) if candidate_first else (sources, queries)
    for name, args in first:
        reply = data(tools.handle(name, args))
    other = "source_and_revision" if candidate_first else "candidate_queries"
    assert reply["tool_budget"]["remaining"][other] == 2
    assert reply["remaining_tool_calls"] == 2
    for name, args in second:
        reply = data(tools.handle(name, args))
    assert tools.calls_started == 4 and reply["remaining_tool_calls"] == 0
    assert reply["tool_budget"]["attempts"] == {"total": 4, "source_and_revision": 2, "candidate_queries": 2}
    assert sorted(path.name for path in tools.audit_root.iterdir()) == [f"tool-{i:04d}" for i in range(1, 5)]
    with pytest.raises(ToolBudgetExceeded, match="total attempt"):
        tools.handle("wt_inventory", {})
    assert tools.calls_started == 4


def test_invalid_queries_and_unknown_tools_consume_their_pool_and_total_attempts(patch_case):
    tools = make_tools(patch_case)
    assert not tools.handle("wt_query_candidate", {"limit": 99})["success"]
    assert not tools.handle("invented-tool", {})["success"]
    assert tools.tool_budget_state()["remaining"] == {
        "total_attempts": 2, "source_and_revision": 1, "candidate_queries": 1}
    data(tools.handle("wt_query_candidate", {"section": "paper"}))
    data(tools.handle("wt_inventory", {}))
    with pytest.raises(ToolBudgetExceeded):
        tools.handle("invented-tool", {})


def test_query_pool_exhaustion_is_logged_and_does_not_block_remaining_source_work(patch_case):
    tools = make_tools(patch_case, source_limit=3, candidate_limit=1)
    data(tools.handle("wt_query_candidate", {}))
    failure = tools.handle("wt_query_candidate", {"section": "paper"})
    assert not failure["success"] and "candidate query pool exhausted" in failure["contentItems"][0]["text"]
    status = json.loads(failure["contentItems"][1]["text"])
    assert status["tool_budget"]["attempts"]["candidate_queries"] == 2
    assert status["tool_budget"]["remaining"] == {
        "total_attempts": 2, "source_and_revision": 2, "candidate_queries": 0}
    assert not tools.has_candidate_read(tools.candidate_binding)
    assert not json.loads((tools.audit_root / "tool-0002" / "result.json").read_text())["success"]
    data(tools.handle("wt_inventory", {}))
    data(tools.handle("wt_inventory", {}))
    with pytest.raises(ToolBudgetExceeded):
        tools.handle("wt_query_candidate", {})


def test_revision_pool_exhaustion_preserves_staged_candidate_and_allows_candidate_read(patch_case):
    tools = make_tools(patch_case, source_limit=1, candidate_limit=3)
    tools.prepare_revision(patch_case.record)
    tools.begin_submission(True)
    original_hash = digest_json(patch_case.record.model_dump(mode="json"))
    edits = [{"operation": "set", "path": ["observations", 0, "value", "value"], "value": 2.5}]
    saved = data(tools.handle("wt_patch_record", {"base_hash": original_hash, "edits": edits}))
    entry = tools.submissions[saved["submission_id"]]
    before = hash_file(entry["path"])
    failure = tools.handle("wt_patch_record", {"base_hash": saved["base_hash"], "edits": [
        {"operation": "set", "path": ["observations", 0, "value", "value"], "value": 999}]})
    assert not failure["success"] and "source and revision pool exhausted" in failure["contentItems"][0]["text"]
    assert hash_file(entry["path"]) == before and tools.latest_submission_id == saved["submission_id"]
    current = data(tools.handle("wt_query_candidate", {"path": ["observations", 0, "value"]}))
    assert current["value"]["value"] == 2.5
    assert tools.resolve_submission(SubmissionReceipt(submission_id=saved["submission_id"])).observations[0].value.value == 2.5
    assert not (tools.audit_root / "tool-0002" / "submitted_record.json").exists()


def test_legacy_default_remains_a_single_shared_budget(patch_case):
    tools = make_tools(patch_case, source_limit=2, candidate_limit=None)
    data(tools.handle("wt_query_candidate", {}))
    response = data(tools.handle("wt_inventory", {}))
    assert response["tool_budget"]["mode"] == "shared"
    assert response["tool_budget"]["remaining"] == {
        "total_attempts": 0, "source_and_revision": None, "candidate_queries": None}
    with pytest.raises(ToolBudgetExceeded):
        tools.handle("wt_query_candidate", {})


def test_candidate_pagination_can_exceed_old_shared_ceiling_without_losing_source_allowance(patch_case):
    tools = make_tools(patch_case, source_limit=1, candidate_limit=160)
    for _ in range(160):
        data(tools.handle("wt_query_candidate", {}))
    assert tools.tool_budget_state()["remaining"]["source_and_revision"] == 1
    last = data(tools.handle("wt_inventory", {}))
    assert last["tool_call_number"] == 161 and last["remaining_tool_calls"] == 0
    assert (tools.audit_root / "tool-0161" / "result.json").is_file()
    with pytest.raises(ToolBudgetExceeded):
        tools.handle("wt_inventory", {})


@pytest.mark.parametrize("source,candidate", [(0, 1), (301, 1), (True, 1), (1, 0),
    (1, 801), (1, True), (1, 2.0)])
def test_split_constructor_rejects_unbounded_or_coerced_limits(patch_case, source, candidate):
    with pytest.raises(ValueError):
        make_tools(patch_case, source_limit=source, candidate_limit=candidate)

def test_reaudit_capacity_follows_source_shape_and_later_passes():
    from watertreatment.reading_budget import reaudit_source_allowance
    t = {"max_model_turns": 5, "max_revisions": 2}
    assert reaudit_source_allowance([{"blocks": [1]}], [], t)["source_limit"] == 160
    plan = reaudit_source_allowance([{"blocks": [1] * 201}, {"blocks": [1] * 100}], [1] * 50, t)
    assert plan["text_ranges"] == 16 and plan["source_limit"] == 300 and plan["reserve_per_later_audit"] == 66


def test_refused_early_reads_leave_originals_for_the_next_independent_audit(patch_case):
    tools = make_tools(patch_case, source_limit=3, candidate_limit=5)
    tools.future_source_reserve = 2
    data(tools.handle("wt_inventory", {}))
    assert not tools.handle("wt_inventory", {})["success"]
    assert tools.source_calls_started == 1 and tools.calls_started == 2
    assert tools.tool_budget_state()["remaining"]["source_and_revision"] == 0
    data(tools.handle("wt_query_candidate", {}))
    tools.future_source_reserve = 0
    data(tools.handle("wt_inventory", {}))
    data(tools.handle("wt_inventory", {}))
    assert tools.tool_budget_state()["remaining"]["source_and_revision"] == 0
