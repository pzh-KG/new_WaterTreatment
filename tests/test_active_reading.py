"""Offline scientific-workflow and source-access regression tests."""
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from test_workflow import workspace, read_json
from watertreatment.domain import CoverageEntry, ExtractionRecord, Paper
from watertreatment.reading import AUDIT_DIMENSIONS, AuditFinding, ReadingAudit, ReadingDossier
from watertreatment.reading_tools import ReadingTools, ToolBudgetExceeded
from watertreatment.settings import ReadingSettings
from watertreatment.storage import DataStore, hash_file
from watertreatment.workflow import execute_plan, import_paper, prepare


def test_audit_dimension_schema_is_enum_and_legacy_notes_are_preserved():
    from pydantic import ValidationError
    values = {"findings": [], "potential_omissions": [], "coverage_limitations": [],
              "checked_dimensions": [name + " was checked against its source." for name in AUDIT_DIMENSIONS]}
    audit = ReadingAudit.model_validate(values)
    assert audit.checked_dimensions == list(AUDIT_DIMENSIONS)
    assert audit.dimension_notes == values["checked_dimensions"]
    schema = ReadingAudit.model_json_schema()
    assert schema["properties"]["checked_dimensions"]["items"]["enum"] == list(AUDIT_DIMENSIONS)
    values["checked_dimensions"] = ["stability_unchecked should not be silently mapped"]
    with pytest.raises(ValidationError):
        ReadingAudit.model_validate(values)


@pytest.fixture
def active(workspace):
    settings = workspace.settings
    settings.codex.model = "gpt-6.2-sol"
    settings.codex.max_calls_per_run = 7
    settings.reading = ReadingSettings(enabled=True)
    for stem in ("understand", "audit-reading", "revise-reading"):
        shutil.copyfile(Path(__file__).resolve().parents[1] / "prompts" / f"{stem}.v0.5.txt",
                        workspace.project / "prompts" / f"{stem}.v0.5.txt")
    DataStore(settings.data_root).initialize()
    imported = import_paper(settings, workspace.folder, paper_id="paper-test", render=True,
                           source_roles={"main.pdf": "main_article", "support.docx": "supporting_information"})
    run = prepare(settings, imported / "sources.json", cohort_role="development")
    return workspace, run, read_json(run / "sources.json"), read_json(run / "plan.json")


def toolset(active, budget=80):
    _, run, bundle, plan = active
    return ReadingTools(bundle, run, max_calls=budget, expected_inputs=plan["input_paths"])


def test_pdf_zoom_and_alternate_text_have_original_locator(active):
    _, run, bundle, _ = active
    tools = toolset(active)
    doc = next(d for d in bundle["documents"] if d["source"]["format"] == "pdf")
    source_id = doc["source"]["source_id"]
    result = tools.handle("wt_view", {"source_id": source_id, "physical_page": 1,
                        "rectangle": [0.25, 0.25, 0.75, 0.75], "scale": 4})
    assert result["success"] and result["contentItems"][1]["type"] == "inputImage"
    data = json.loads(result["contentItems"][0]["text"])
    assert data["block_ids"] == [doc["blocks"][0]["block_id"]]
    assert data["size_pixels"] == [400, 240]
    assert hash_file(Path(doc["source"]["path"])) == doc["source"]["sha256"]
    text = tools.handle("wt_page_text", {"source_id": source_id, "physical_page": 1})
    assert text["success"] and "Synthetic final composite" in text["contentItems"][0]["text"]


def test_word_zoom_preserves_occurrence_and_does_not_invent_pages(active):
    _, _, bundle, _ = active
    tools = toolset(active)
    doc = next(d for d in bundle["documents"] if d["source"]["format"] == "docx")
    block = next(b for b in doc["blocks"] if b["kind"] == "image")
    response = tools.handle("wt_view", {"source_id": doc["source"]["source_id"], "block_id": block["block_id"], "scale": 2})
    assert response["success"]
    data = json.loads(response["contentItems"][0]["text"])
    assert data["physical_page"] is None
    assert data["block_ids"] == [block["block_id"]]
    assert any("adds no source detail" in s for s in data["limitations"])


def test_native_word_table_is_explicitly_text_only_not_a_wrong_image(active):
    tools = toolset(active)
    doc = next(d for d in active[2]["documents"] if d["source"]["format"] == "docx")
    block = next(b for b in doc["blocks"] if b["kind"] == "table")
    response = tools.handle("wt_view", {"source_id": doc["source"]["source_id"], "block_id": block["block_id"]})
    assert not response["success"]
    text = response["contentItems"][0]["text"]
    assert "kind=table" in text and "wt_read_blocks" in text and "Do not substitute" in text
    inventory = next(d for d in tools.inventory()["sources"] if d["source_id"] == doc["source"]["source_id"])
    assert inventory["viewable_word_image_block_ids"]
    assert block["block_id"] not in inventory["viewable_word_image_block_ids"]


def test_word_zoom_composites_legacy_alpha_and_keeps_original_hash(tmp_path):
    from watertreatment.sources import SourceDocument, SourceFile
    from watertreatment.codex import source_snapshot
    original = tmp_path / "source.docx"
    original.write_bytes(b"synthetic registered source")
    preview = tmp_path / "legacy.png"
    picture = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    picture.putpixel((1, 1), (0, 0, 0, 255))
    picture.save(preview)
    doc = SourceDocument(source=SourceFile(source_id="s", path=str(original), sha256=hash_file(original), size_bytes=original.stat().st_size, format="docx"))
    doc.add_block("image")
    run = tmp_path / "run"
    run.mkdir()
    before = hash_file(preview)
    tools = ReadingTools({"documents": [doc.model_dump(mode="json")], "renderings": [{"source_id": "s", "images": [{"block_id": doc.blocks[0].block_id, "path": str(preview)}]}]}, run,
                         max_calls=1, expected_inputs=source_snapshot([original, preview]))
    response = tools.handle("wt_view", {"source_id": "s", "block_id": doc.blocks[0].block_id, "scale": 1})
    assert response["success"]
    data = json.loads(response["contentItems"][0]["text"])
    assert data["display_adjustment"]["alpha_composited"] and data["physical_page"] is None
    with Image.open(data["image_path"]) as image:
        assert image.getpixel((0, 0)) == (255, 255, 255)
        assert image.getpixel((1, 1)) == (0, 0, 0)
    assert hash_file(preview) == before


@pytest.mark.parametrize("arguments", [
    {"source_id": "../../secrets/password.txt", "physical_page": 1},
    {"source_id": "unknown", "physical_page": 1},
    {"source_id": "PLACEHOLDER", "physical_page": 999},
    {"source_id": "PLACEHOLDER", "physical_page": 1, "rectangle": [0.8, 0, 0.2, 1]},
    {"source_id": "PLACEHOLDER", "physical_page": 1, "path": "C:/private.txt"},
])
def test_reading_tools_reject_unknown_paths_invalid_regions(active, arguments):
    tools = toolset(active)
    if arguments["source_id"] == "PLACEHOLDER":
        arguments = {**arguments, "source_id": active[2]["documents"][0]["source"]["source_id"]}
    assert not tools.handle("wt_view", arguments)["success"]


def test_changed_source_and_tool_exhaustion_are_explicit(active):
    tools = toolset(active, budget=1)
    doc = active[2]["documents"][0]
    Path(doc["source"]["path"]).write_bytes(b"changed")
    result = tools.handle("wt_read_blocks", {"source_id": doc["source"]["source_id"], "start_order": 1, "end_order": 1})
    assert not result["success"] and "changed" in result["contentItems"][0]["text"]
    with pytest.raises(ToolBudgetExceeded):
        tools.handle("wt_inventory", {})


def test_search_paginates_without_dropping_remaining_hits(active):
    tools = toolset(active)
    first = tools.handle("wt_search", {"query": "synthetic", "limit": 1})
    data = json.loads(first["contentItems"][0]["text"])
    assert len(data["matches"]) == 1 and data["next_offset"] == 1
    assert data["total_matches"] > 1
    second = tools.handle("wt_search", {"query": "synthetic", "offset": 1, "limit": 1})
    assert json.loads(second["contentItems"][0]["text"])["matches"] != data["matches"]


class FakeReader:
    def __init__(self, settings, bundle, *, persistent_error=False, fail_revision=False):
        self.settings = settings.codex
        self.live = True
        self.calls_started = 0
        self.closed = False
        self.stages = []
        self.persistent_error = persistent_error
        self.fail_revision = fail_revision
        self.coverage = [CoverageEntry(source_id=d["source"]["source_id"], block_ids=[b["block_id"] for b in d["blocks"]], state="read_unquantified", text_read=True, visual_checked=False) for d in bundle["documents"]]
        self.candidate = ExtractionRecord(paper=Paper(id="paper-test", dataset_role="development", source_ids=[d["source"]["source_id"] for d in bundle["documents"]]), coverage=self.coverage)

    def bind_tools(self, tools):
        self.tools = tools

    def close(self):
        self.closed = True

    def complete(self, model, **kwargs):
        self.calls_started += 1
        stage = kwargs["identity"]["stage"]
        self.stages.append((stage, kwargs["conversation_key"]))
        if model is ReadingDossier:
            return ReadingDossier(paper_id="paper-test", article_question_and_design="Synthetic fixture", material_map=[], experiment_map=[], coverage=self.coverage, cross_source_conflicts=[], unresolved_questions=[], schema_gaps=[])
        if model is ExtractionRecord:
            if stage.startswith("revision") and self.fail_revision:
                raise TimeoutError("synthetic model timeout")
            result = self.candidate.model_copy(deep=True)
            if stage == "extraction" or self.persistent_error:
                result.paper.id = "wrong-paper"
            return result
        assert model is ReadingAudit
        return ReadingAudit(findings=[], potential_omissions=[], coverage_limitations=[], checked_dimensions=list(AUDIT_DIMENSIONS))


def test_real_state_machine_repairs_and_reaudits_without_auto_publishing(active):
    workspace, run, bundle, _ = active
    client = FakeReader(workspace.settings, bundle)
    execute_plan(workspace.settings, run / "plan.json", client=client)
    assert client.stages == [("comprehension", "reader"), ("extraction", "reader"), ("audit_0", "audit-0"), ("revision_1", "revision-1"), ("audit_1", "audit-1")]
    assert read_json(run / "candidate.v00.json")["paper"]["id"] == "wrong-paper"
    assert read_json(run / "candidate.json")["paper"]["id"] == "paper-test"
    result = read_json(run / "result.json")
    assert result["revisions"] == 1 and result["validation_error_count"] == 0
    assert not result["human_validated"] and not result["graph_published"]
    assert (run / "human_spot_check.txt").exists() and client.closed


def test_unresolved_error_hits_revision_limit_and_keeps_every_candidate(active):
    workspace, run, bundle, _ = active
    client = FakeReader(workspace.settings, bundle, persistent_error=True)
    execute_plan(workspace.settings, run / "plan.json", client=client)
    result = read_json(run / "result.json")
    assert result["status"] == "candidate_incomplete_budget"
    assert result["stop_reason"] == "revision_budget_exhausted"
    assert client.calls_started == 7
    assert len(list(run.glob("candidate.v*.json"))) == 3


def test_mid_revision_failure_keeps_original_candidate_and_closes_session(active):
    workspace, run, bundle, _ = active
    client = FakeReader(workspace.settings, bundle, fail_revision=True)
    with pytest.raises(TimeoutError):
        execute_plan(workspace.settings, run / "plan.json", client=client)
    assert (run / "candidate.v00.json").exists() and (run / "failure.json").exists()
    assert client.closed


def test_changed_reading_prompt_rejected_before_model_call(active):
    workspace, run, bundle, _ = active
    (run / "understand_prompt.txt").write_text("modified", encoding="utf-8")
    client = FakeReader(workspace.settings, bundle)
    with pytest.raises(ValueError, match="artifact changed"):
        execute_plan(workspace.settings, run / "plan.json", client=client)
    assert client.calls_started == 0


def test_active_reading_cannot_skip_audit(active):
    workspace, run, bundle, _ = active
    client = FakeReader(workspace.settings, bundle)
    with pytest.raises(ValueError, match="requires"):
        execute_plan(workspace.settings, run / "plan.json", client=client, verify=False)
    assert client.calls_started == 0


def test_pdf_crop_coordinates_select_top_not_bottom(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    from watertreatment.sources import parse_source
    from watertreatment.codex import source_snapshot
    source = tmp_path / "quadrants.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    stream = DecodedStreamObject()
    stream.set_data(b"1 0 0 rg 0 0 100 50 re f 0 1 0 rg 0 50 100 50 re f")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(source)
    doc = parse_source(source, source_id="synthetic-quadrants")
    run = tmp_path / "run"
    run.mkdir()
    tools = ReadingTools({"documents": [doc.model_dump(mode="json")], "renderings": []}, run,
                         max_calls=2, expected_inputs=source_snapshot([source]))
    result = tools.handle("wt_view", {"source_id": "synthetic-quadrants", "physical_page": 1,
                        "rectangle": [0.1, 0.1, 0.9, 0.4], "scale": 3})
    data = json.loads(result["contentItems"][0]["text"])
    with Image.open(data["image_path"]) as image:
        assert image.convert("RGB").getpixel((50, 20)) == (0, 255, 0)


def test_word_changed_preview_is_not_delivered(active):
    tools = toolset(active)
    key, item = next(iter(tools.word_images.items()))
    Path(item["path"]).write_bytes(b"tampered-preview")
    result = tools.handle("wt_view", {"source_id": key[0], "block_id": key[1]})
    assert not result["success"]
    assert "changed" in result["contentItems"][0]["text"]


def test_compact_submission_expands_only_defaults_and_is_turn_bound(active):
    from watertreatment.reading_tools import SubmissionReceipt
    tools = toolset(active)
    payload = {"record": {"paper": {"id": "synthetic-compact", "publication_year": {"value": 2024, "status": "reported", "raw": "2024"}}}}
    assert not tools.handle("wt_submit_record", payload)["success"]
    tools.begin_submission(True)
    result = tools.handle("wt_submit_record", payload)
    assert result["success"]
    data = json.loads(result["contentItems"][0]["text"])
    receipt = SubmissionReceipt(submission_id=data["submission_id"])
    record = tools.resolve_submission(receipt)
    assert record.paper.publication_year.value == 2024
    assert record.paper.doi.status.value == "unread" and record.paper.doi.value is None
    assert not record.materials and not data["published"]
    tools.begin_submission(True)
    with pytest.raises(ValueError, match="this model turn"):
        tools.resolve_submission(receipt)


def test_compact_submission_rejects_unknown_fields_and_file_tampering(active):
    from watertreatment.reading_tools import SubmissionReceipt
    tools = toolset(active)
    tools.begin_submission(True)
    assert not tools.handle("wt_submit_record", {"record": {"paper": {"id": "p"}, "invented_schema_field": 2}})["success"]
    result = tools.handle("wt_submit_record", {"record": {"paper": {"id": "p"}}})
    receipt = SubmissionReceipt(submission_id=json.loads(result["contentItems"][0]["text"])["submission_id"])
    tools.submissions[receipt.submission_id]["path"].write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        tools.resolve_submission(receipt)


def test_reusing_comprehension_checks_lineage_and_keeps_development_role(active):
    workspace, run, bundle, _ = active
    client = FakeReader(workspace.settings, bundle)
    execute_plan(workspace.settings, run / "plan.json", client=client)
    call = run / "calls" / "synthetic-comprehension"
    call.mkdir(parents=True)
    (call / "request.json").write_text(json.dumps({"request": {"stage": "comprehension", "model": workspace.settings.codex.model, "reasoning_effort": "max"}}), encoding="utf-8")
    (call / "result.json").write_text(json.dumps({"status": "schema_valid_candidate", "server_selected_model": "gpt-6.2-sol", "server_selected_effort": "max"}), encoding="utf-8")
    shutil.copyfile(run / "reading_dossier.json", call / "answer.json")
    bundle_path = Path(read_json(run / "manifest.json")["source_bundle"])
    resumed = prepare(workspace.settings, bundle_path, cohort_role="development", reuse_dossier=run / "reading_dossier.json")
    plan = read_json(resumed / "plan.json")
    assert plan["reused_dossier"]["parent_run"] == run.name
    assert plan["cohort_role"] == "development" and plan["record_delivery"] == "compact_tool_submission"
    result = read_json(run / "reading_dossier.json")
    result["article_question_and_design"] = "tampered notes"
    (run / "reading_dossier.json").write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError, match="completed Sol MAX"):
        prepare(workspace.settings, bundle_path, cohort_role="development", reuse_dossier=run / "reading_dossier.json")


def test_resume_checkpoint_repairs_then_freshly_audits_with_full_sources(active):
    workspace, run, bundle, _ = active

    class SeedReader(FakeReader):
        def complete(self, model, **kwargs):
            output = super().complete(model, **kwargs)
            if model is ExtractionRecord:
                output.paper.id = "paper-test"
            if model is ReadingAudit and kwargs["identity"]["stage"] == "audit_0":
                output.findings = [AuditFinding(record_path="paper.title", severity="minor", verdict="unsupported",
                    needs_revision=True, explanation="Synthetic repair fixture", evidence=[])]
            return output

    execute_plan(workspace.settings, run / "plan.json", client=SeedReader(workspace.settings, bundle))
    checkpoint = run / "candidate.v00.json"
    candidate = ExtractionRecord.model_validate_json(checkpoint.read_bytes())
    for stage, answer_name, record_name in [("comprehension", "reading_dossier.json", None),
                                            ("extraction", None, "candidate.v00.json"),
                                            ("audit_0", "audit.v00.json", None)]:
        call = run / "calls" / ("proof-" + stage)
        call.mkdir(parents=True)
        (call / "request.json").write_text(json.dumps({"request": {"stage": stage, "model": workspace.settings.codex.model, "reasoning_effort": "max",
            "prompt": candidate.model_dump_json(exclude_defaults=True)}}), encoding="utf-8")
        (call / "result.json").write_text(json.dumps({"status": "schema_valid_candidate", "server_selected_model": "gpt-6.2-sol",
            "server_selected_effort": "max"}), encoding="utf-8")
        if answer_name:
            shutil.copyfile(run / answer_name, call / "answer.json")
        if record_name:
            shutil.copyfile(run / record_name, call / "record.json")
    bundle_path = Path(read_json(run / "manifest.json")["source_bundle"])
    resumed = prepare(workspace.settings, bundle_path, cohort_role="development",
                      reuse_dossier=run / "reading_dossier.json", reuse_candidate=checkpoint)
    assert read_json(resumed / "plan.json")["reused_candidate"]["repair_audit"]

    class CheckingReader(FakeReader):
        def complete(self, model, **kwargs):
            if kwargs["identity"]["stage"] == "revision_1":
                assert not kwargs["image_paths"]
                assert "REGISTERED SOURCE NAVIGATION" in kwargs["prompt"] and "SCIENTIFIC RULES" in kwargs["prompt"]
                assert "INHERITED DOSSIER" in kwargs["prompt"]
                assert kwargs["conversation_key"] == "revision-1"
            return super().complete(model, **kwargs)

    client = CheckingReader(workspace.settings, bundle)
    execute_plan(workspace.settings, resumed / "plan.json", client=client)
    assert client.stages == [("revision_1", "revision-1"), ("audit_1", "audit-1")]
    result = read_json(resumed / "result.json")
    assert result["history"][0]["audit_origin"] == "reused_repair_guidance"
    assert result["history"][-1]["audit_origin"] == "fresh_model_audit"
    assert result["resumed_from"]["parent_run"] == run.name
    assert result["validation_error_count"] == 0 and not result["human_validated"]
    changed = candidate.model_copy(deep=True)
    changed.paper.title.value = "tampered scientific draft"
    checkpoint.write_text(changed.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="completed Sol MAX record"):
        prepare(workspace.settings, bundle_path, cohort_role="development",
                reuse_dossier=run / "reading_dossier.json", reuse_candidate=checkpoint)
