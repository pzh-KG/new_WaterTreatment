"""Fake-client integration for bounded source re-audit and scoped questions."""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from test_web_records import fixture, read, entry, submit, completed_call
from watertreatment.codex import source_snapshot
from watertreatment.domain import ExtractionRecord
from watertreatment.quantitative import quantitative_summary
from watertreatment.reading import AUDIT_DIMENSIONS, ReadingAudit
from watertreatment.settings import CodexSettings
from watertreatment.storage import digest_json, hash_file, write_new_json
from watertreatment.web_model import QAAnswer, QuestionTools, _freeze_sources, ask, reaudit, validate_answer


@pytest.fixture
def model_fixture(fixture):
    fixture.settings.codex = CodexSettings(model="gpt-6.2-sol", reasoning_effort="max", max_calls_per_run=7)
    prompts = fixture.settings.project_root / "prompts"
    prompts.mkdir(parents=True)
    repository = Path(__file__).resolve().parents[1]
    for name in ("extract.v0.5.txt", "audit-reading.v0.5.txt"):
        shutil.copyfile(repository / "prompts" / name, prompts / name)
    return fixture


def audit(*, revision=False):
    return ReadingAudit.model_validate({"findings": [{"record_path": "observations[0].value", "severity": "major",
        "verdict": "unsupported", "needs_revision": True, "explanation": "Synthetic recheck requested",
        "evidence": [{"source_id": "main", "block_id": "paragraph"}]}] if revision else [],
        "potential_omissions": [], "coverage_limitations": ["Synthetic replay is not scientific verification"],
        "checked_dimensions": list(AUDIT_DIMENSIONS)})


def answer(identifier, *, citation="source-value"):
    return QAAnswer.model_validate({"answer": "该记录在明确实验下报告速率，仍是候选数据。[1]",
        "citations": [{"record_id": identifier, "evidence_id": citation, "observation_id": "rate", "label": "原文段落"}],
        "limitations": ["仅供离线协议验证"], "insufficient_evidence": False})


class ReplayClient:
    """Protocol-shaped artifacts, no provider, process, credentials or network."""
    def __init__(self, responses, *, change_after=None, metadata=True):
        self.responses = list(responses)
        self.calls_started = 0
        self.calls = []
        self.closed = False
        self.change_after = change_after
        self.metadata = metadata

    def factory(self, settings, *, live, total_timeout_seconds):
        assert live and settings.model == "gpt-6.2-sol" and settings.reasoning_effort == "max"
        self.settings = settings
        self.timeout = total_timeout_seconds
        return self

    def bind_tools(self, tools):
        self.tools = tools

    def complete(self, model, *, prompt, input_paths, workdir, audit_root, identity, image_paths=None, conversation_key=None):
        assert self.calls_started < self.settings.max_calls_per_run
        self.calls_started += 1
        response = self.responses.pop(0)
        if callable(response):
            response = response(model, prompt)
        assert isinstance(response, model)
        assert all(p in input_paths for p in image_paths or [])
        self.calls.append({"model": model, "stage": identity["stage"], "prompt": prompt,
                           "conversation_key": conversation_key, "images": image_paths or []})
        folder = audit_root / f"call-{self.calls_started:02d}"
        request = {**identity, "model": self.settings.model, "reasoning_effort": self.settings.reasoning_effort, "prompt": prompt, "sources": source_snapshot(input_paths)}
        write_new_json(folder / "request.json", {"request": request, "fingerprint": digest_json(request)})
        result = {"status": "schema_valid_candidate", "requested_model": "gpt-6.2-sol", "effective_model": None,
                  "usage": [{"input_tokens": 12, "output_tokens": 4}]}
        if self.metadata:
            result.update(server_selected_model="gpt-6.2-sol", server_selected_effort="max")
        write_new_json(folder / "result.json", result)
        if isinstance(response, ExtractionRecord):
            write_new_json(folder / "record.json", response.model_dump(mode="json"))
            write_new_json(folder / "answer.json", {"submission_id": "synthetic-local-submission"})
        else:
            write_new_json(folder / "answer.json", response.model_dump(mode="json"))
        if self.change_after:
            self.change_after(workdir)
        return response

    def close(self):
        self.closed = True


def test_questions_are_frozen_cited_and_record_only_confirmed_model_metadata(model_fixture):
    item = model_fixture.records.get(model_fixture.run.name)
    client = ReplayClient([answer(item["id"])])
    response = ask(model_fixture.settings, [item], "该最终材料的蒸发速率有哪些证据？", scope="selected", client_factory=client.factory)
    assert client.closed and client.calls_started == 1
    assert response["model"] == "gpt-6.2-sol" and response["effort"] == "max"
    assert response["model_metadata"]["effective_model"] is None
    assert response["model_metadata"]["usage"] == [{"input_tokens": 12, "output_tokens": 4}]
    assert response["citations"][0]["quote"] == "rate 2.4"
    assert response["record_versions"][item["id"]] == item["summary"]["record_hash"]
    run = model_fixture.settings.data_root / "runs" / response["id"]
    assert read(run / "scope.json")[0]["record"] == item["record"]
    assert read(run / "plan.json")["runtime"]
    assert not response["human_validated"] and not response["graph_consulted"]


def test_citation_repair_is_bounded_and_uses_exact_reference_fields(model_fixture):
    item = model_fixture.records.get(model_fixture.run.name)
    altered = copy.deepcopy(item)
    altered["record"]["observations"][0]["value"]["evidence_ids"] = ["source-value-extra"]
    altered["record"]["observations"][0]["unit"]["evidence_ids"] = []
    altered["record"]["observations"][0]["limitations"] = ["source-value appears in prose only"]
    with pytest.raises(ValueError, match="观测与引用"):
        validate_answer(answer(item["id"]), [altered])
    client = ReplayClient([answer(item["id"], citation="invented"), answer(item["id"])])
    response = ask(model_fixture.settings, [item], "来源在哪里？", scope="selected", client_factory=client.factory)
    assert client.calls_started == 2 and client.calls[1]["stage"] == "question_citation_repair"
    assert response["model_metadata"]["call_id"] == "call-02"


def test_nonexistent_evidence_cannot_survive_the_only_citation_retry(model_fixture):
    item = model_fixture.records.get(model_fixture.run.name)
    client = ReplayClient([answer(item["id"], citation="invented"), answer(item["id"], citation="invented")])
    with pytest.raises(ValueError, match="证据编号不存在"):
        ask(model_fixture.settings, [item], "来源在哪里？", scope="selected", client_factory=client.factory)
    assert client.closed and client.calls_started == 2
    run = next(r for r in model_fixture.store.runs() if r["kind"] == "web_question")
    assert (Path(run["manifest_path"]).parent / "failure.json").is_file()


@pytest.mark.parametrize("change,metadata", [(None, False), (lambda run: (run / "scope.json").write_text("[]", encoding="utf-8"), True)])
def test_missing_native_metadata_or_changed_inputs_cannot_complete_question(model_fixture, change, metadata):
    item = model_fixture.records.get(model_fixture.run.name)
    client = ReplayClient([answer(item["id"])], change_after=change, metadata=metadata)
    with pytest.raises(ValueError):
        ask(model_fixture.settings, [item], "来源在哪里？", scope="selected", client_factory=client.factory)
    assert client.closed
    run = next(r for r in model_fixture.store.runs() if r["kind"] == "web_question")
    assert not (Path(run["manifest_path"]).parent / "answer.json").exists()


def test_wrong_model_configuration_stops_before_factory(model_fixture):
    model_fixture.settings.codex.model = "unsupported-model"
    with pytest.raises(ValueError, match="固定使用"):
        ask(model_fixture.settings, [model_fixture.records.get(model_fixture.run.name)], "来源？", scope="selected",
            client_factory=lambda *a, **k: pytest.fail("model was called"))


def test_scoped_record_tool_exposes_human_limits_and_paginated_correct_domain_sections(model_fixture, tmp_path):
    submit(model_fixture, [entry()])
    detail = model_fixture.records.get(model_fixture.run.name)
    run = tmp_path / "tool-audit"
    run.mkdir()
    bundle = read(Path(detail["sources_bundle"]))
    tool = QuestionTools(bundle, run, max_calls=6, expected_inputs=[], records=[detail])
    result = tool.handle("wt_query_records", {"section": "observations", "offset": 0, "limit": 1})
    payload = json.loads(result["contentItems"][0]["text"])
    assert payload["total"] == 2 and payload["next_offset"] == 1
    summary = json.loads(tool.handle("wt_query_records", {"section": "summary"})["contentItems"][0]["text"])
    assert summary["items"][0]["human_review_scope"] == "selected_items_only"
    assert summary["items"][0]["review_count"] == 1
    reviews = json.loads(tool.handle("wt_query_records", {"section": "reviews"})["contentItems"][0]["text"])
    assert reviews["total"] == 1
    assert not tool.handle("wt_query_records", {"record_id": "outside"})["success"]
    comparison = tool.handle("wt_query_records", {"section": "comparisons"})
    assert comparison["success"]
    limits = json.loads(tool.handle("wt_query_records", {"section": "audit"})["contentItems"][0]["text"])
    assert limits["items"][0]["data"]["coverage_limitations"]


def test_reaudit_registers_exact_fact_version_with_native_audit_contract(model_fixture):
    original = hash_file(model_fixture.run / "candidate.json")
    client = ReplayClient([audit()])
    result = reaudit(model_fixture.settings, model_fixture.records, model_fixture.run.name, "检查最终材料的证据", client_factory=client.factory)
    assert client.closed and client.calls_started == 1
    assert result["revision"]["accepted"] and result["revision"]["record_id"] != model_fixture.run.name
    assert result["revision"]["summary"]["admissible"]
    assert hash_file(model_fixture.run / "candidate.json") == original
    run = model_fixture.settings.data_root / "runs" / result["run_id"]
    assert result["result"]["history"][-1]["candidate_sha256"] == hash_file(run / "candidate.json")
    assert read(run / "sources.json") == read(model_fixture.run / "sources.json")


def test_revision_has_fresh_audit_and_preserves_explicit_human_judgment(model_fixture):
    submit(model_fixture, [entry()])
    changed = model_fixture.record.model_copy(deep=True)
    changed.observations[0].value.value = 2.5
    client = ReplayClient([audit(revision=True), changed, audit()])
    result = reaudit(model_fixture.settings, model_fixture.records, model_fixture.run.name, "复查速率", client_factory=client.factory)
    assert client.closed and client.calls_started == 3
    assert client.calls[0]["conversation_key"] != client.calls[2]["conversation_key"]
    assert '"action": "confirm"' in client.calls[1]["prompt"]
    assert result["revision"]["status"] == "pending_human_conflict"
    assert not result["revision"]["accepted"]
    assert model_fixture.records.get(model_fixture.run.name)["summary"]["current"]


def test_revision_without_human_conflict_is_registered_only_after_fresh_audit(model_fixture):
    changed = model_fixture.record.model_copy(deep=True)
    changed.observations[0].value.value = 2.5
    client = ReplayClient([audit(revision=True), changed, audit()])
    result = reaudit(model_fixture.settings, model_fixture.records, model_fixture.run.name, "复查速率", client_factory=client.factory)
    assert result["revision"]["accepted"] and result["revision"]["changed"]
    assert result["result"]["model_calls"] == 3 and result["result"]["revisions"] == 1


@pytest.mark.parametrize("violation", ["coverage", "paper_id"])
def test_repeated_coverage_or_identity_violation_exhausts_bounded_loop_without_accepting(model_fixture, violation):
    invalid = model_fixture.record.model_copy(deep=True)
    if violation == "coverage":
        invalid.coverage = []
    else:
        invalid.paper.id = "different-paper"
    client = ReplayClient([audit(revision=True), invalid, audit(), invalid, audit()])
    result = reaudit(model_fixture.settings, model_fixture.records, model_fixture.run.name, "复查来源与实验", client_factory=client.factory)
    assert result["incomplete"] and client.closed and client.calls_started == 5
    assert result["result"]["status"] == "needs_revision"
    assert result["result"]["stop_reason"] == "revision_budget_exhausted"
    assert model_fixture.records.get(model_fixture.run.name)["summary"]["current"]
    assert "READING COVERAGE CHECKS" in client.calls[3]["prompt"]
    if violation == "coverage":
        assert result["result"]["reading_check_issues"]
    else:
        assert result["result"]["validation_error_count"] > 0


def test_reaudit_source_changes_are_rejected_before_any_call(model_fixture):
    model_fixture.records.mark_source_stale("synthetic-paper", "New SI")
    with pytest.raises(ValueError, match="来源未变动"):
        reaudit(model_fixture.settings, model_fixture.records, model_fixture.run.name, "复查来源",
            client_factory=lambda *a, **k: pytest.fail("unprepared sources called model"))


def test_word_zip_member_is_not_opened_as_file_and_extracted_media_hash_is_required(model_fixture):
    detail = model_fixture.records.get(model_fixture.run.name)
    media = model_fixture.settings.data_root / "synthetic-media.tiff"
    Image.new("RGB", (4, 4), "white").save(media)
    bundle = read(Path(detail["sources_bundle"]))
    bundle["documents"][0]["blocks"][0]["metadata"] = {"media_path": "word/media/image1.tiff",
        "image_output_path": str(media), "media_sha256": hash_file(media)}
    frozen_bundle = model_fixture.settings.data_root / "synthetic-word-bundle.json"
    write_new_json(frozen_bundle, bundle)
    detail["sources_bundle"] = str(frozen_bundle)
    run = model_fixture.store.new_run("web_question", {"synthetic": True})
    _, paths = _freeze_sources([detail], run)
    assert media in paths
    assert all(str(p) != "word/media/image1.tiff" for p in paths)
    media.write_bytes(b"modified extracted media")
    with pytest.raises(ValueError, match="Word原始插图快照已变化"):
        _freeze_sources([detail], run)


def test_reaudit_attaches_white_canvas_for_legacy_transparent_image_without_changing_original_bundle(model_fixture):
    transparent = model_fixture.settings.data_root / "legacy-transparent.png"
    image = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    image.putpixel((4, 4), (0, 0, 0, 255))
    image.save(transparent)
    original_image_hash = hash_file(transparent)
    run = model_fixture.store.new_run("extraction", {"paper_id": "synthetic-paper"})
    bundle = read(model_fixture.run / "sources.json")
    bundle["renderings"] = [{"source_id": "main", "images": [{"block_id": "paragraph",
        "path": str(transparent), "sha256": original_image_hash}]}]
    write_new_json(run / "sources.json", bundle)
    original_bundle_hash = hash_file(run / "sources.json")
    original_source = Path(bundle["documents"][0]["source"]["path"])
    plan = {**read(model_fixture.run / "plan.json"), "run_id": run.name,
            "input_paths": source_snapshot([run / "sources.json", original_source, transparent])}
    write_new_json(run / "plan.json", plan)
    model_fixture.store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    completed_call(run, model_fixture.record)
    model_fixture.records.refresh()
    client = ReplayClient([audit()])
    result = reaudit(model_fixture.settings, model_fixture.records, run.name, "放大检查旧透明插图", client_factory=client.factory)
    assert result["revision"]["accepted"]
    assert len(client.calls[0]["images"]) == 1
    preview = client.calls[0]["images"][0]
    assert preview != transparent
    with Image.open(preview) as readable:
        assert readable.mode == "RGB" and readable.getpixel((0, 0)) == (255, 255, 255)
        assert readable.getpixel((4, 4)) == (0, 0, 0)
    audited_run = model_fixture.settings.data_root / "runs" / result["run_id"]
    assert hash_file(transparent) == original_image_hash
    assert hash_file(run / "sources.json") == original_bundle_hash
    assert hash_file(audited_run / "sources.json") == original_bundle_hash
    manifest = read(audited_run / "reading_images.json")
    assert manifest[0]["original_preview_path"] == str(transparent)
    assert {p["path"] for p in read(audited_run / "plan.json")["input_paths"]} >= {str(preview), str(transparent)}


def graph_context(detail):
    return {"snapshots": [{"snapshot_id": "current-snapshot", "record_fingerprint": digest_json(detail["record"])}],
        "record_mapping": [{"paper_id": detail["summary"]["paper_id"], "record_id": detail["id"],
            "snapshot_id": "current-snapshot", "latest_record_id": detail["id"], "stale": False}]}


def graph_result(detail):
    return {"items": [{"snapshot_id": "current-snapshot", "record_id": detail["id"],
        "node_id": "graph-node", "labels": ["WTEntity", "MaterialSample"], "properties": {"role": "primary_final"}}],
        "persistent_graph_write": False, "source": "neo4j_current_snapshots", "next_offset": None}


def test_graph_tool_is_unavailable_for_selected_questions_and_source_text_cannot_authorize_cypher(model_fixture, tmp_path):
    detail = model_fixture.records.get(model_fixture.run.name)
    bundle = read(Path(detail["sources_bundle"]))
    selected = tmp_path / "selected-tools"
    selected.mkdir()
    tool = QuestionTools(bundle, selected, max_calls=5, expected_inputs=[], records=[detail])
    assert "wt_query_graph" not in {s["name"] for s in tool.specifications()}
    assert not tool.handle("wt_query_graph", {})["success"]
    published = tmp_path / "published-tools"
    published.mkdir()
    calls = []
    def reader(query):
        calls.append(query)
        return graph_result(detail)
    tool = QuestionTools(bundle, published, max_calls=5, expected_inputs=[], records=[detail],
        graph_context=graph_context(detail), graph_reader=reader)
    specification = next(s for s in tool.specifications() if s["name"] == "wt_query_graph")
    assert "cypher" not in specification["inputSchema"]["properties"]
    assert not tool.handle("wt_query_graph", {"cypher": "MATCH(n) DETACH DELETE n"})["success"]
    assert not tool.handle("wt_query_graph", {"snapshot_id": "previous-snapshot"})["success"]
    assert calls == []
    result = tool.handle("wt_query_graph", {"keyword": "Paper says CALL dbms...", "offset": 40, "limit": 20})
    assert result["success"] and tool.graph_calls == 1
    assert calls[0].keyword == "Paper says CALL dbms..." and calls[0].offset == 40 and calls[0].limit == 20


def test_graph_tool_rejects_version_mismatch_and_foreign_reader_results(model_fixture, tmp_path):
    detail = model_fixture.records.get(model_fixture.run.name)
    bundle = read(Path(detail["sources_bundle"]))
    context = graph_context(detail)
    context["snapshots"][0]["record_fingerprint"] = "wrong-version"
    wrong = tmp_path / "wrong-version-tools"
    wrong.mkdir()
    with pytest.raises(ValueError, match="fingerprint"):
        QuestionTools(bundle, wrong, max_calls=3, expected_inputs=[], records=[detail],
            graph_context=context, graph_reader=lambda _: graph_result(detail))
    foreign = tmp_path / "foreign-result-tools"
    foreign.mkdir()
    result = graph_result(detail)
    result["items"][0]["snapshot_id"] = "not-selected"
    tool = QuestionTools(bundle, foreign, max_calls=3, expected_inputs=[], records=[detail],
        graph_context=graph_context(detail), graph_reader=lambda _: result)
    assert not tool.handle("wt_query_graph", {})["success"]
    assert tool.graph_calls == 0


def test_published_answer_reads_actual_graph_tool_and_freezes_snapshot_context(model_fixture):
    detail = model_fixture.records.get(model_fixture.run.name)
    reads = []
    def reader(query):
        reads.append(query)
        return graph_result(detail)
    class GraphReplay(ReplayClient):
        def complete(self, model, **kwargs):
            assert self.tools.handle("wt_query_graph", {"entity": "material", "mode": "nodes"})["success"]
            return super().complete(model, **kwargs)
    client = GraphReplay([answer(detail["id"])])
    response = ask(model_fixture.settings, [detail], "图谱中有哪些最终材料？", scope="published",
        graph_context=graph_context(detail), graph_reader=reader, client_factory=client.factory)
    assert response["graph_consulted"] and response["graph_tool_calls"] == 1
    assert len(reads) == 1 and reads[0].entity == "material"
    run = model_fixture.settings.data_root / "runs" / response["id"]
    assert read(run / "graph_context.json") == graph_context(detail)
    assert read(run / "reading_tools" / "tool-0001" / "result.json")["source"] == "neo4j_current_snapshots"


def test_published_answer_cannot_claim_graph_consulted_without_successful_tool_read(model_fixture):
    detail = model_fixture.records.get(model_fixture.run.name)
    client = ReplayClient([answer(detail["id"])])
    with pytest.raises(ValueError, match="没有实际读取"):
        ask(model_fixture.settings, [detail], "图谱中有哪些材料？", scope="published",
            graph_context=graph_context(detail), graph_reader=lambda _: pytest.fail("unused reader"), client_factory=client.factory)
    assert client.closed


@pytest.mark.parametrize("property_field,label_field", [
    ("properties", "labels"),
    ("source_properties", "source_labels"),
    ("target_properties", "target_labels"),
])
@pytest.mark.parametrize("origin,status,expected_status,estimate,author_stated,measurement", [
    ("plot_estimate", "reported", "estimated", True, False, False),
    ("author_simulation", "reported", "reported", False, True, False),
    ("reported_measurement", "reported", "reported", False, True, True),
    ("unresolved", "reported", "reported", False, False, False),
])
def test_graph_tool_normalizes_legacy_value_provenance_without_rewriting_snapshot(
        model_fixture, tmp_path, property_field, label_field, origin, status,
        expected_status, estimate, author_stated, measurement):
    detail = model_fixture.records.get(model_fixture.run.name)
    original_detail = copy.deepcopy(detail)
    candidate_hash = hash_file(model_fixture.run / "candidate.json")
    result = graph_result(detail)
    row = result["items"][0]
    row[property_field] = {"origin": origin, "value_status": status, "value": 80.7}
    row[label_field] = ["WTEntity", "Observation"]
    original_result = copy.deepcopy(result)
    run = tmp_path / "legacy-provenance-tools"
    run.mkdir()
    tool = QuestionTools(read(Path(detail["sources_bundle"])), run, max_calls=1,
        expected_inputs=[], records=[detail], graph_context=graph_context(detail),
        graph_reader=lambda _: result)

    response = tool.handle("wt_query_graph", {
        "mode": "nodes" if property_field == "properties" else "relationships"})
    assert response["success"]
    payload = json.loads(response["contentItems"][0]["text"])
    properties = payload["items"][0][property_field]
    assert properties["origin"] == origin and properties["value"] == 80.7
    assert properties["source_value_status"] == status
    assert properties["value_status"] == expected_status
    assert properties["is_estimate"] is estimate
    assert properties["author_stated"] is author_stated
    assert properties["is_author_measurement"] is measurement
    assert result == original_result and detail == original_detail
    assert hash_file(model_fixture.run / "candidate.json") == candidate_hash


def test_graph_tool_preserves_original_status_from_already_compatible_projection(model_fixture, tmp_path):
    detail = model_fixture.records.get(model_fixture.run.name)
    result = graph_result(detail)
    result["items"][0].update(labels=["WTEntity", "Observation"], properties={
        "origin": "plot_estimate", "value_status": "estimated", "source_value_status": "reported",
        "value": 80.7, "is_estimate": True, "author_stated": False, "is_author_measurement": False})
    original_result = copy.deepcopy(result)
    run = tmp_path / "compatible-provenance-tools"
    run.mkdir()
    tool = QuestionTools(read(Path(detail["sources_bundle"])), run, max_calls=1,
        expected_inputs=[], records=[detail], graph_context=graph_context(detail),
        graph_reader=lambda _: result)
    response = tool.handle("wt_query_graph", {})
    assert response["success"]
    payload = json.loads(response["contentItems"][0]["text"])
    assert payload["items"][0]["properties"] == original_result["items"][0]["properties"]
    assert result == original_result


@pytest.mark.parametrize("has_inventory", [False, True])
@pytest.mark.parametrize("has_graph_matches", [False, True])
def test_graph_tool_attaches_bound_record_coverage_even_for_legacy_or_empty_snapshots(
        model_fixture, tmp_path, has_inventory, has_graph_matches):
    detail = copy.deepcopy(model_fixture.records.get(model_fixture.run.name))
    if has_inventory:
        detail["record"]["quantitative_coverage"] = [{
            "id": "source-series", "source_id": "main", "label": "Synthetic condition series",
            "data_kind": "condition_series", "state": "partial", "expected_points": 4,
            "observation_ids": ["rate"], "reason": "Only one condition was extracted.",
            "limitations": ["No complete cross-condition comparison."]}]
    else:
        detail["record"].pop("quantitative_coverage", None)
    result = graph_result(detail)
    if not has_graph_matches:
        result["items"] = []
    # The graph reader cannot declare a bound record complete on its own.
    result["record_quantitative_summaries"] = [{"record_id": "not-bound", "complete": True}]
    original_result = copy.deepcopy(result)
    original_detail = copy.deepcopy(detail)
    run = tmp_path / "bound-coverage-tools"
    run.mkdir()
    tool = QuestionTools(read(Path(detail["sources_bundle"])), run, max_calls=1,
        expected_inputs=[], records=[detail], graph_context=graph_context(detail),
        graph_reader=lambda _: result)
    response = tool.handle("wt_query_graph", {})
    assert response["success"]
    payload = json.loads(response["contentItems"][0]["text"])
    assert payload["record_quantitative_summaries"] == [{
        "snapshot_id": "current-snapshot", "record_id": detail["id"],
        "quantitative_coverage_summary": quantitative_summary(detail["record"]),
        "quantitative_followups": []}]
    summary = payload["record_quantitative_summaries"][0]["quantitative_coverage_summary"]
    assert summary["has_quantitative_inventory"] is has_inventory
    assert summary["limitations"] and summary["scientific_validation"] == "not_established"
    assert result == original_result and detail == original_detail


@pytest.mark.parametrize("has_graph_matches", [False, True])
def test_scoped_followups_survive_graph_and_record_summary_queries(model_fixture, tmp_path, has_graph_matches):
    detail = copy.deepcopy(model_fixture.records.get(model_fixture.run.name))
    evidence = detail["record"]["evidence"][0]
    followups = [{"kind": "additional_visible_points", "coverage_ids": ["source-series"],
        "evidence": [{"source_id": evidence["source_id"], "block_id": evidence["block_id"]}],
        "affected_query_types": ["pointwise_comparison", "kinetic_recalculation"],
        "recommended_action": "Recover additional visible internal samples; retain the existing partial scope."}]
    detail["audit"]["quantitative_followups"] = followups
    detail["record"]["quantitative_coverage"] = [{"id": "source-series",
        "source_id": evidence["source_id"], "block_id": evidence["block_id"], "label": "Synthetic source curve",
        "data_kind": "time_series", "state": "partial", "expected_points": None,
        "observation_ids": ["rate"], "reason": "Additional visible samples remain.",
        "limitations": ["Do not treat these samples as a complete kinetic dataset."]}]
    result = graph_result(detail)
    if not has_graph_matches:
        result["items"] = []
    result["record_quantitative_summaries"] = [{"record_id": detail["id"], "quantitative_followups": []}]
    before_detail, before_graph = copy.deepcopy(detail), copy.deepcopy(result)
    run = tmp_path / "bound-followup-tools"
    run.mkdir()
    tool = QuestionTools(read(Path(detail["sources_bundle"])), run, max_calls=2,
        expected_inputs=[], records=[detail], graph_context=graph_context(detail), graph_reader=lambda _: result)
    graph_response = tool.handle("wt_query_graph", {})
    assert graph_response["success"]
    payload = json.loads(graph_response["contentItems"][0]["text"])
    context = payload["record_quantitative_summaries"][0]
    assert context["quantitative_followups"] == followups
    assert context["quantitative_coverage_summary"]["coverage_states"] == {"partial": 1}
    response = tool.handle("wt_query_records", {"section": "summary", "record_id": detail["id"]})
    assert response["success"]
    summary = json.loads(response["contentItems"][0]["text"])["items"][0]
    assert summary["quantitative_followups"] == followups
    assert detail == before_detail and result == before_graph
