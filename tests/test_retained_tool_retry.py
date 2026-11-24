import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from watertreatment.reading_budget import MAX_SOURCE_CALLS
from watertreatment.retained_tool_retry import (checked_proposal, is_source_interruption,
                                                source_tool_limit, _confirm_interrupt)
from watertreatment.storage import digest_json
from watertreatment.retained_delta_lineage import confirm_delta_lineage


def test_source_attempts_scale_with_cited_cards_and_findings():
    assert source_tool_limit({}) == 30
    packet = {"sources": [{}] * 40, "findings": [{}] * 12}
    assert source_tool_limit(packet) == 96
    assert source_tool_limit({"sources": list(range(500))}) == MAX_SOURCE_CALLS


def test_retry_extends_only_the_signed_outstanding_candidate(tmp_path):
    raw = {"tabs": ["original"], "value": 1}
    prev = {"candidate": {**raw, "value": 2}, "source_blocks": [["a", "1"]]}
    (tmp_path / "proposal.json").write_text(json.dumps(prev))
    parent = SimpleNamespace(run=tmp_path, candidate=Mock(model_dump=Mock(return_value=raw)))
    assert checked_proposal(parent, None) == prev
    ext = {**prev, "candidate": {"tabs": ["expanded"], "value": 2},
           "continues_failed_candidate_sha256": digest_json(prev["candidate"])}
    assert checked_proposal(parent, ext) == ext
    for bad in ({"candidate": raw}, {"source_blocks": []}, {"continues_failed_candidate_sha256": "stale"}):
        with pytest.raises(ValueError):
            checked_proposal(parent, {**ext, **bad})


def test_failed_turn_discovery_tolerates_incomplete_file(tmp_path):
    assert not is_source_interruption(tmp_path)
    path = tmp_path / "failure.json"
    path.write_text("{")
    assert not is_source_interruption(tmp_path)
    path.write_text(json.dumps({"error": "ToolBudgetExceeded: reading-tool total attempt budget exhausted"}))
    assert is_source_interruption(tmp_path)


@pytest.mark.parametrize("other_turn,delivered,attempts,valid", [
    (False, False, 3, True), (True, False, 3, False), (False, True, 3, False), (False, False, 2, False)])
def test_provider_interruption_needs_same_unanswered_exhausted_turn(tmp_path, other_turn, delivered, attempts, valid):
    def row(direction, method, **data):
        return {"direction": direction, "message": {"method": method, "params": {"threadId": "paper", "turnId": "source", **data}}}
    rows = [row("receive", "item/tool/call") for _ in range(attempts)]
    if delivered:
        rows.append(row("receive", "item/completed", item={"type": "agentMessage", "phase": "final_answer", "text": "{}"}))
    rows.append(row("send", "turn/interrupt", turnId="other" if other_turn else "source"))
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in rows))
    if valid:
        _confirm_interrupt(path, {"turn_id": "source", "thread_id": "paper"}, 2)
    else:
        with pytest.raises(ValueError):
            _confirm_interrupt(path, {"turn_id": "source", "thread_id": "paper"}, 2)


@pytest.mark.parametrize("scope,index", [("full", 1), ("scientific_delta", 0)])
def test_delta_replay_requires_a_scoped_intermediate_version(tmp_path, scope, index):
    with pytest.raises(ValueError, match="source-scoped"):
        confirm_delta_lineage(None, tmp_path, {}, index, None, SimpleNamespace(review_scope=scope))


def test_deadline_requires_a_real_interrupt_even_if_no_tool_pool_was_exhausted(tmp_path):
    from watertreatment.retained_tool_retry import TURN_DEADLINE
    (tmp_path / "failure.json").write_text(json.dumps({"error": TURN_DEADLINE}))
    assert is_source_interruption(tmp_path)
    path = tmp_path / "events.jsonl"
    outcome = {"turn_id": "s", "thread_id": "t"}
    event = {"direction": "send", "message": {"method": "turn/interrupt", "params": {"turnId": "s", "threadId": "t"}}}
    path.write_text(json.dumps(event))
    assert _confirm_interrupt(path, outcome) == 0
    path.write_text("")
    with pytest.raises(ValueError):
        _confirm_interrupt(path, outcome)
