"""Offline workflow guardrails; fake clients never execute a model or network call."""

import json
import zipfile
from pathlib import Path

import pytest

from watertreatment.codex import ModelCallError
from watertreatment.domain import DatasetRole, ExtractionRecord, Paper
from watertreatment.settings import CodexSettings, Settings
from watertreatment.storage import DataStore, hash_file
from watertreatment.workflow import execute_plan, import_paper, prepare


@pytest.fixture
def planned(tmp_path, request):
    cohort_role = getattr(request, "param", "development")
    project = tmp_path / "project"
    prompts = project / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "extract.v0.5.txt").write_text("Extract supplied evidence only.", encoding="utf-8")
    (prompts / "verify.v0.5.txt").write_text("Verify supplied evidence only.", encoding="utf-8")
    config = project / "config"
    config.mkdir()
    (config / "taxonomy.v0.5.json").write_text('{"version":"0.5"}', encoding="utf-8")
    originals = tmp_path / "input"
    paper_folder = originals / "1_fixture"
    paper_folder.mkdir(parents=True)
    source = paper_folder / "main.docx"
    with zipfile.ZipFile(source, "w") as package:
        package.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Experimental source text.</w:t></w:r></w:p></w:body></w:document>')
    settings = Settings(project_root=project, data_root=tmp_path / "data", source_roots=[originals],
                        codex=CodexSettings(model="gpt-6.2-sol", max_calls_per_run=2))
    DataStore(settings.data_root).initialize()
    imported = import_paper(settings, paper_folder, paper_id="paper-fixture", source_roles={"main.docx": "main_article"})
    plan_run = prepare(settings, imported / "sources.json", cohort_role=cohort_role)
    bundle = json.loads((plan_run / "sources.json").read_text(encoding="utf-8"))
    source_ids = [d["source"]["source_id"] for d in bundle["documents"]]
    dataset_role = DatasetRole.VALIDATION_RESERVED if cohort_role == "heldout" else DatasetRole.DEVELOPMENT
    candidate = ExtractionRecord(paper=Paper(id="paper-fixture", source_ids=source_ids, dataset_role=dataset_role))
    return settings, plan_run, candidate, source


class FakeClient:
    def __init__(self, settings, candidate, *, live=True, fail=False):
        self.settings = settings
        self.candidate = candidate
        self.live = live
        self.fail = fail
        self.calls_started = 0

    def complete(self, model, **kwargs):
        self.calls_started += 1
        if self.fail:
            raise ModelCallError("offline simulated failure")
        assert model is ExtractionRecord
        return self.candidate


def test_changed_input_rejected_before_any_model_call(planned):
    settings, run, candidate, _ = planned
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    staged_bundle = next(Path(item["path"]) for item in plan["input_paths"] if Path(item["path"]).name == "sources.json")
    staged_bundle.write_text(staged_bundle.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    client = FakeClient(settings.codex, candidate)
    with pytest.raises(ValueError):
        execute_plan(settings, run / "plan.json", client=client, verify=False)
    assert client.calls_started == 0
    assert not (run / "execution_started.json").exists()


def test_two_stage_budget_checked_before_spending_one_call(planned):
    settings, run, candidate, _ = planned
    client = FakeClient(settings.codex, candidate)
    client.calls_started = 1  # One of this client's two allowed calls is already spent.
    with pytest.raises(ValueError):
        execute_plan(settings, run / "plan.json", client=client, verify=True)
    assert client.calls_started == 1
    assert not (run / "execution_started.json").exists()


def test_live_opt_in_required_before_execution_marker(planned):
    settings, run, candidate, _ = planned
    client = FakeClient(settings.codex, candidate, live=False)
    with pytest.raises(ValueError):
        execute_plan(settings, run / "plan.json", client=client, verify=False)
    assert client.calls_started == 0
    assert not (run / "execution_started.json").exists()


def test_changed_prompt_rejected_without_call(planned):
    settings, run, candidate, _ = planned
    (run / "extract_prompt.txt").write_text("changed", encoding="utf-8")
    client = FakeClient(settings.codex, candidate)
    with pytest.raises(ValueError):
        execute_plan(settings, run / "plan.json", client=client, verify=False)
    assert client.calls_started == 0


def test_model_failure_kept_and_plan_not_silently_reused(planned):
    settings, run, candidate, original = planned
    before = original.read_bytes()
    client = FakeClient(settings.codex, candidate, fail=True)
    with pytest.raises(ModelCallError, match="simulated"):
        execute_plan(settings, run / "plan.json", client=client, verify=False)
    assert json.loads((run / "failure.json").read_text(encoding="utf-8"))["model_calls"] == 1
    assert not (run / "result.json").exists()
    assert original.read_bytes() == before
    with pytest.raises(ValueError):
        execute_plan(settings, run / "plan.json", client=client, verify=False)
    assert client.calls_started == 1


def test_candidate_success_is_not_scientific_validation(planned):
    settings, run, candidate, _ = planned
    client = FakeClient(settings.codex, candidate)
    execute_plan(settings, run / "plan.json", client=client, verify=False)
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    assert result["human_validated"] is False
    assert result["graph_published"] is False
    assert (run / "candidate.json").is_file()
    assert client.calls_started == 1


def test_wrong_candidate_paper_identity_is_a_validation_error(planned):
    settings, run, candidate, _ = planned
    candidate.paper.id = "different-paper"
    client = FakeClient(settings.codex, candidate)
    execute_plan(settings, run / "plan.json", client=client, verify=False)
    validation = json.loads((run / "validation.json").read_text(encoding="utf-8"))
    assert any(issue["severity"] == "error" for issue in validation["issues"])
    assert (run / "candidate.json").is_file(), "Keep failed candidates for audit instead of hiding them."
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    assert result["human_validated"] is False


def test_development_candidate_cannot_promote_itself_to_validation(planned):
    settings, run, candidate, _ = planned
    candidate.paper.dataset_role = DatasetRole.VALIDATION
    client = FakeClient(settings.codex, candidate)
    execute_plan(settings, run / "plan.json", client=client, verify=False)
    validation = json.loads((run / "validation.json").read_text(encoding="utf-8"))
    assert any(issue["severity"] == "error" for issue in validation["issues"])
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    assert result["human_validated"] is False


@pytest.mark.parametrize("changes", [
    {"model": "different-offline-model"},
    {"reasoning_effort": "low"},
    {"timeout_seconds": 5},
])
def test_changed_model_configuration_rejected_without_spending_calls(planned, changes):
    settings, run, candidate, _ = planned
    client = FakeClient(settings.codex.model_copy(update=changes), candidate)
    with pytest.raises(ValueError):
        execute_plan(settings, run / "plan.json", client=client, verify=False)
    assert client.calls_started == 0
    assert not (run / "execution_started.json").exists()


def test_taxonomy_is_frozen_hashed_and_supplied_to_model(planned):
    settings, run, candidate, _ = planned
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    frozen = run / "taxonomy.json"
    original = settings.taxonomy_path.read_bytes()
    assert frozen.read_bytes() == original
    planned_file = next(item for item in plan["input_paths"] if Path(item["path"]) == frozen)
    assert planned_file["sha256"] == hash_file(frozen)
    prompt = (run / "extract_prompt.txt").read_text(encoding="utf-8")
    manifest = json.loads(prompt.split("INPUT MANIFEST (metadata, not scientific evidence):\n", 1)[1])
    assert Path(manifest["taxonomy"]) == frozen
    assert str(frozen) in manifest["allowed_input_paths"]
    frozen.write_text('{"version":"changed"}', encoding="utf-8")
    client = FakeClient(settings.codex, candidate)
    with pytest.raises(ValueError):
        execute_plan(settings, run / "plan.json", client=client, verify=False)
    assert client.calls_started == 0
    assert settings.taxonomy_path.read_bytes() == original


@pytest.mark.parametrize("field, value", [("rule_version", "unplanned"), ("schema_version", "unplanned")])
def test_candidate_cannot_change_frozen_versions(planned, field, value):
    settings, run, candidate, _ = planned
    setattr(candidate, field, value)
    execute_plan(settings, run / "plan.json", client=FakeClient(settings.codex, candidate), verify=False)
    validation = json.loads((run / "validation.json").read_text(encoding="utf-8"))
    assert any(issue["severity"] == "error" for issue in validation["issues"])


def test_candidate_cannot_omit_supplied_source_inventory(planned):
    settings, run, candidate, _ = planned
    candidate.paper.source_ids = []
    execute_plan(settings, run / "plan.json", client=FakeClient(settings.codex, candidate), verify=False)
    validation = json.loads((run / "validation.json").read_text(encoding="utf-8"))
    assert any(issue["severity"] == "error" for issue in validation["issues"])


@pytest.mark.parametrize("planned", ["heldout"], indirect=True)
@pytest.mark.parametrize("exposure_match", ["same_paper_different_hash", "same_hash_different_paper"])
def test_stale_heldout_plan_rejected_after_later_development_exposure(planned, exposure_match):
    settings, heldout_run, candidate, original = planned
    plan_path = heldout_run / "plan.json"
    original_plan_hash = hash_file(plan_path)
    original_source_hash = hash_file(original)
    assert json.loads(plan_path.read_text(encoding="utf-8"))["cohort_role"] == "heldout"

    later_folder = original.parent.parent / "2_later_development"
    later_folder.mkdir()
    later_source = later_folder / "main.docx"
    if exposure_match == "same_paper_different_hash":
        with zipfile.ZipFile(original) as package:
            xml = package.read("word/document.xml").decode("utf-8")
        with zipfile.ZipFile(later_source, "w") as package:
            package.writestr("word/document.xml", xml.replace("Experimental source text.", "Revised source text."))
        later_paper_id = candidate.paper.id
        assert hash_file(later_source) != original_source_hash
    else:
        later_source.write_bytes(original.read_bytes())
        later_paper_id = "paper-renamed-alias"
        assert hash_file(later_source) == original_source_hash
        assert later_paper_id != candidate.paper.id

    imported = import_paper(settings, later_folder, paper_id=later_paper_id,
                            source_roles={"main.docx": "main_article"})
    development_run = prepare(settings, imported / "sources.json", cohort_role="development")
    assert json.loads((development_run / "plan.json").read_text(encoding="utf-8"))["cohort_role"] == "development"
    assert hash_file(plan_path) == original_plan_hash, "Exposure changed, not the frozen heldout plan."
    assert hash_file(original) == original_source_hash

    client = FakeClient(settings.codex, candidate)
    with pytest.raises(ValueError, match="development exposure"):
        execute_plan(settings, plan_path, client=client, verify=False)
    assert client.calls_started == 0
    assert not (heldout_run / "execution_started.json").exists()


@pytest.mark.parametrize("templates_available", [False, True])
def test_rule_version_selects_matching_prompts_without_old_version_fallback(planned, templates_available):
    settings, old_run, _, _ = planned
    manifest = json.loads((old_run / "manifest.json").read_text(encoding="utf-8"))
    settings.rules_version = "9.9"
    if not templates_available:
        with pytest.raises(FileNotFoundError):
            prepare(settings, Path(manifest["source_bundle"]), cohort_role="development")
        return
    (settings.project_root / "prompts/extract.v9.9.txt").write_text("New extraction rules.", encoding="utf-8")
    (settings.project_root / "prompts/verify.v9.9.txt").write_text("New verification rules.", encoding="utf-8")
    run = prepare(settings, Path(manifest["source_bundle"]), cohort_role="development")
    assert (run / "extract_prompt.txt").read_text(encoding="utf-8").startswith("New extraction rules.")
    assert (run / "verify_prompt.txt").read_text(encoding="utf-8").startswith("New verification rules.")
    assert json.loads((run / "plan.json").read_text(encoding="utf-8"))["rule_version"] == "9.9"


def test_complete_source_text_is_delivered_to_extraction_and_review_without_file_tools(planned):
    _, run, _, _ = planned
    bundle = json.loads((run / "sources.json").read_text(encoding="utf-8"))
    for filename in ("extract_prompt.txt", "verify_prompt.txt"):
        prompt = (run / filename).read_text(encoding="utf-8")
        manifest = json.loads(prompt.split("INPUT MANIFEST (metadata, not scientific evidence):\n", 1)[1])
        assert manifest["source_representation"] == bundle["documents"]
        assert "Experimental source text." in prompt
        assert "do not run shell commands" in prompt
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    assert plan["source_delivery"] == "inline_full_text_and_images"


def test_image_index_preserves_pdf_pages_and_each_word_occurrence():
    from watertreatment.workflow import _image_manifest
    bundle = {"renderings": [{"source_id":"pdf", "pages":[{"path":"page1.png","physical_page":1},{"path":"page2.png","physical_page":2}]},
        {"source_id":"word", "images":[{"path":"crop1.png","block_id":"b1"},{"path":"crop2.png","block_id":"b2"},{"block_id":"b3","status":"unavailable"}]}]}
    images = _image_manifest(bundle)
    assert [i["attachment_index"] for i in images] == [1,2,3,4]
    assert [i["source_id"] for i in images] == ["pdf","pdf","word","word"]
    assert [i["physical_page"] for i in images] == [1,2,None,None]
    assert [i["block_id"] for i in images] == [None,None,"b1","b2"]
