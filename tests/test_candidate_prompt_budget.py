"""Draft offline budget tests. No provider is called; candidate data is synthetic."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from test_app_server import Answer, adapter
from test_active_reading import active
from test_workflow import workspace
from watertreatment.app_server import _bound_candidate_prompt, _prompt_size_metadata, _check_prompt_size
from watertreatment.codex import ModelCallError
from watertreatment.domain import ExtractionRecord, Fact, Observation, Paper
from watertreatment.storage import digest_json


def synthetic():
    record = ExtractionRecord(paper=Paper(id="synthetic"), observations=[Observation(id="point", experiment_id="experiment",
        value=Fact(value=2.5, status="reported"), origin="reported_measurement")])
    binding = {"path": "D:/synthetic/candidate.json", "sha256": "a" * 64,
        "content_hash": digest_json(record.model_dump(mode="json")), "scope": "synthetic-budget-test"}
    return record, binding


@pytest.mark.parametrize("source_chars,expected_budget", [(0, 600000), (100000, 600000), (300000, 540000), (839000, 1000)])
def test_view_allocation_keeps_cap_and_reserves_sixty_thousand_characters(monkeypatch, source_chars, expected_budget):
    record = SimpleNamespace(model_dump_json=lambda **_: "EXACT_CANDIDATE")
    binding = {"content_hash": "b" * 64}
    budgets = []
    def view(candidate, bound, *, max_chars):
        budgets.append(max_chars)
        return "V" * max_chars
    monkeypatch.setattr("watertreatment.candidate_view.candidate_view", view)
    text = _bound_candidate_prompt("S" * source_chars + "EXACT_CANDIDATE", record, binding)
    assert budgets == [expected_budget]
    assert text.startswith("S" * source_chars)
    assert len(text) <= 840000
    _check_prompt_size(_prompt_size_metadata(text + "A" * 60000))


def test_identical_candidates_are_inlined_once_without_multiplying_the_budget(monkeypatch):
    record = SimpleNamespace(model_dump_json=lambda **_: "EXACT_CANDIDATE")
    binding = {"content_hash": "b" * 64}
    calls = []
    def view(candidate, bound, *, max_chars):
        calls.append(max_chars)
        return "CANDIDATE_SNAPSHOT " + bound["content_hash"] + "\n" + "V" * (max_chars - 84)
    monkeypatch.setattr("watertreatment.candidate_view.candidate_view", view)
    parts = ["SOURCE_BEFORE\n", "\nSOURCE_MIDDLE_A\n", "\nSOURCE_MIDDLE_B\n", "\nSOURCE_AFTER"]
    text = _bound_candidate_prompt("EXACT_CANDIDATE".join(parts), record, binding)
    assert len(calls) == 1 and calls[0] <= 600000
    assert text.count("CANDIDATE_SNAPSHOT ") == 1
    assert text.count("SAME_BOUND_CANDIDATE ") == 2
    assert text.index(parts[0]) < text.index(parts[1]) < text.index(parts[2]) < text.index(parts[3])
    assert all(part in text for part in parts)
    assert "EXACT_CANDIDATE" not in text
    _check_prompt_size(_prompt_size_metadata(text + "A" * 60000))


def test_candidate_not_inline_is_appended_without_discarding_source_text():
    record, binding = synthetic()
    source = "Original source text\n原文和单位必须保留。"
    text = _bound_candidate_prompt(source, record, binding)
    assert text.startswith(source + "\nBOUND CANDIDATE VIEW:\n")
    assert "CANDIDATE_SNAPSHOT " + binding["content_hash"] in text
    assert '"complete_record_inlined":false' in text


def test_a_different_unbound_candidate_is_retained_as_counted_base_text():
    record, binding = synthetic()
    other = record.model_copy(deep=True)
    other.paper.id = "other-candidate"
    other_text = other.model_dump_json(exclude_defaults=True)
    prompt = "SOURCE_A\n" + other_text + "\nSOURCE_B\n" + record.model_dump_json(exclude_defaults=True)
    text = _bound_candidate_prompt(prompt, record, binding)
    assert text.startswith("SOURCE_A\n" + other_text + "\nSOURCE_B\n")
    assert text.count("CANDIDATE_SNAPSHOT ") == 1
    assert "SAME_BOUND_CANDIDATE " not in text


def test_large_source_uses_directory_or_rejects_without_truncating_the_source():
    record, binding = synthetic()
    record.observations[0].value.value = "Scientific value cannot be silently shortened. " * 2000
    binding["content_hash"] = digest_json(record.model_dump(mode="json"))
    source = "S" * 835000
    before = copy.deepcopy(record.model_dump(mode="json"))
    text = _bound_candidate_prompt(source + record.model_dump_json(exclude_defaults=True), record, binding)
    assert text.startswith(source)
    view_text = text[len(source):]
    view = json.loads(view_text.split("\n", 1)[1])
    assert view["view_kind"] == "directory"
    assert view["observations_inlined"] is False and len(text) <= 840000
    assert record.model_dump(mode="json") == before
    with pytest.raises((ValueError, ModelCallError), match="budget|space"):
        _bound_candidate_prompt("S" * 839950 + record.model_dump_json(exclude_defaults=True), record, binding)
    with pytest.raises(ModelCallError, match="no.*space|budget"):
        _bound_candidate_prompt("S" * 840000 + record.model_dump_json(exclude_defaults=True), record, binding)


@pytest.mark.parametrize("text,expected_chars,expected_bytes", [("A" * 900000, 900000, 900000), ("汉" * 300000, 300000, 900000)])
def test_final_budget_accepts_exact_utf8_boundary(text, expected_chars, expected_bytes):
    metadata = _prompt_size_metadata(text)
    assert metadata["characters"] == expected_chars and metadata["utf8_bytes"] == expected_bytes
    assert metadata["max_utf8_bytes"] == 900000
    _check_prompt_size(metadata)


@pytest.mark.parametrize("text", ["A" * 900001, "汉" * 300001, "🙂" * 225001])
def test_final_budget_rejects_nonascii_byte_overflow_even_when_character_count_fits(text):
    metadata = _prompt_size_metadata(text)
    with pytest.raises(ModelCallError, match="UTF-8.*900000"):
        _check_prompt_size(metadata)
    assert text.encode("utf-8")  # The input itself was retained, not truncated.


def test_byte_guard_preserves_failure_metadata_without_starting_transport(active):
    client, transport, kwargs = adapter(active)
    kwargs["prompt"] = "汉" * 300001
    try:
        with pytest.raises(ModelCallError, match="UTF-8"):
            client.complete(Answer, **kwargs)
    finally:
        client.close()
    assert transport.sent == [] and client.calls_started == 0
    call = next((active[1] / "calls").glob("call-*"))
    request = json.loads((call / "request.json").read_text(encoding="utf-8"))["request"]
    result = json.loads((call / "result.json").read_text(encoding="utf-8"))
    assert request["prompt"].startswith(kwargs["prompt"])
    assert request["prompt_size"] == result["prompt_size"]
    assert request["prompt_size"]["characters"] == len(request["prompt"])
    assert request["prompt_size"]["utf8_bytes"] == len(request["prompt"].encode("utf-8"))
    assert result["status"] == "failed"
    assert result.get("server_selected_model") is None


def test_candidate_and_binding_remain_unchanged_and_small_views_still_work():
    record, binding = synthetic()
    before = copy.deepcopy(record.model_dump(mode="json")), copy.deepcopy(binding)
    text = _bound_candidate_prompt(record.model_dump_json(exclude_defaults=True), record, binding)
    view = json.loads(text.split("\n", 1)[1])
    assert view["view_kind"] == "scientific_overview_and_all_observation_rows"
    assert view["observations"]["rows"][0][1] == "point"
    assert (record.model_dump(mode="json"), binding) == before
