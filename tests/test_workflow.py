"""Offline end-to-end checks using synthetic documents and an injected CLI runner."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from watertreatment import cli
from watertreatment.codex import CodexClient, ModelCallError
from watertreatment.domain import DatasetRole, ExtractionRecord, Paper
from watertreatment.settings import CodexSettings, Settings
from watertreatment.storage import DataStore, hash_file
from watertreatment.workflow import discover, execute_plan, import_paper, prepare


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def synthetic_pdf(path: Path):
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=120)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 10 Tf 10 80 Td (Synthetic final composite. No scientific claim.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def synthetic_docx(path: Path):
    picture = io.BytesIO()
    Image.new("RGB", (8, 8), color="white").save(picture, format="PNG")
    xml = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
        xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><w:body>
        <w:p><w:r><w:t>Synthetic supporting information</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Unmeasured synthetic field</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        <w:p><w:r><w:drawing><pic:pic><pic:blipFill><a:blip r:embed="rId1"/>
        <a:srcRect t="1000"/></pic:blipFill></pic:pic></w:drawing></w:r></w:p>
        <w:p><w:r><w:t>Fig. S1. Synthetic placeholder.</w:t></w:r></w:p>
        </w:body></w:document>'''
    relationships = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="media/image1.png" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"/></Relationships>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("word/_rels/document.xml.rels", relationships)
        archive.writestr("word/media/image1.png", picture.getvalue())


@pytest.fixture
def workspace(tmp_path):
    root = Path(__file__).resolve().parents[1]
    project, data, source = (tmp_path / name for name in ("code", "data", "sources"))
    project.mkdir()
    (project / "prompts").mkdir()
    (project / "config").mkdir()
    (project / "src").mkdir()
    (project / "src" / "fixture.py").write_text("# Stable synthetic code identity\n", encoding="utf-8")
    for name in ("extract.v0.5.txt", "verify.v0.5.txt"):
        shutil.copyfile(root / "prompts" / name, project / "prompts" / name)
    shutil.copyfile(root / "config" / "taxonomy.v0.5.json", project / "config" / "taxonomy.v0.5.json")
    folder = source / "9001_synthetic_article"
    folder.mkdir(parents=True)
    synthetic_pdf(folder / "main.pdf")
    synthetic_docx(folder / "support.docx")
    executable = tmp_path / "fake-codex.exe"
    executable.write_bytes(b"not executable; only used by injected runner")
    settings = Settings(
        project_root=project, data_root=data, source_roots=[source],
        codex=CodexSettings(executable=str(executable), model="gpt-6.2-sol"),
    )
    config = project / "config" / "project.local.json"
    config.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    original_hashes = {p: hash_file(p) for p in folder.iterdir()}
    return SimpleNamespace(settings=settings, project=project, data=data, source=source,
                           folder=folder, config=config, original_hashes=original_hashes)


def import_and_prepare(workspace, *, role="development"):
    DataStore(workspace.data).initialize()
    imported = import_paper(
        workspace.settings, workspace.folder, paper_id="paper-test",
        source_roles={"main.pdf": "main_article", "support.docx": "supporting_information"},
    )
    run = prepare(workspace.settings, imported / "sources.json", cohort_role=role)
    return imported, run


class FakeRunner:
    def __init__(self, *, fail_at=None, mutate_at=None, candidate_mutator=None):
        self.commands = []
        self.fail_at = fail_at
        self.mutate_at = mutate_at
        self.candidate_mutator = candidate_mutator

    def __call__(self, command, *, prompt, cwd, timeout):
        self.commands.append(command)
        call = len(self.commands)
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert isinstance(prompt, bytes)
        if self.mutate_at == call:
            (cwd / "sources.json").write_text("{}", encoding="utf-8")
        if self.fail_at == call:
            return subprocess.CompletedProcess(command, 9, b'{"type":"turn.failed"}\n', b"synthetic failure")
        output = Path(command[command.index("--output-last-message") + 1])
        if call == 1:
            bundle = read_json(cwd / "sources.json")
            plan = read_json(cwd / "plan.json")
            role = {"development": DatasetRole.DEVELOPMENT,
                    "heldout": DatasetRole.VALIDATION_RESERVED,
                    "unassigned": DatasetRole.UNSPECIFIED}[plan["cohort_role"]]
            candidate = ExtractionRecord(paper=Paper(
                id=plan["paper_id"], dataset_role=role,
                source_ids=[d["source"]["source_id"] for d in bundle["documents"]],
            ))
            payload = candidate.model_dump(mode="json")
            if self.candidate_mutator:
                self.candidate_mutator(payload)
            output.write_text(json.dumps(payload), encoding="utf-8")
        else:
            output.write_text(json.dumps({
                "findings": [], "potential_omissions": ["Synthetic fixture has no adjudicated scientific values"],
                "unread_source_ids": [], "coverage_limitations": ["Fake runner is not scientific verification"],
                "human_validated": False,
            }), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b'{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":4}}\n', b"")


def client_for(workspace, runner, *, live=True):
    return CodexClient(workspace.settings.codex, live=live, runner=runner)


def assert_originals_unchanged(workspace):
    assert {p: hash_file(p) for p in workspace.folder.iterdir()} == workspace.original_hashes


def test_cli_init_import_prepare_are_local_and_keep_original_hashes(workspace, monkeypatch, capsys):
    def forbid_call(*args, **kwargs):
        pytest.fail("prepare/import must not call a model")
    monkeypatch.setattr(CodexClient, "complete", forbid_call)
    base = ["--config", str(workspace.config)]
    assert cli.main(base + ["init"]) == 0
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0
    assert cli.main(base + ["import", str(workspace.folder), "--paper-id", "paper-test"]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["model_calls"] == 0
    assert cli.main(base + ["prepare", imported["source_bundle"], "--cohort-role", "heldout"]) == 0
    planned = json.loads(capsys.readouterr().out)
    plan = read_json(Path(planned["plan"]))
    assert plan["cohort_role"] == "heldout"
    assert plan["status"] == "prepared_no_model_calls"
    assert not (Path(planned["run"]) / "execution_started.json").exists()
    bundle = read_json(Path(imported["source_bundle"]))
    assert bundle["scientific_reading_status"] == "not_started"
    assert {d["source"]["format"] for d in bundle["documents"]} == {"pdf", "docx"}
    assert any("Synthetic final composite" in b.get("text", "") for d in bundle["documents"] for b in d["blocks"])
    for saved in bundle["imports"]:
        assert saved["sha256"] == workspace.original_hashes[Path(saved["original_path"])]
        assert hash_file(Path(saved["stored_path"])) == saved["sha256"]
    assert_originals_unchanged(workspace)
    assert cli.main(base + ["extract", planned["plan"]]) == 2
    assert "no model invoked" in capsys.readouterr().err


def test_fake_extraction_and_review_remain_candidates_with_full_audit(workspace):
    imported, run = import_and_prepare(workspace)
    runner = FakeRunner()
    client = client_for(workspace, runner)
    assert execute_plan(workspace.settings, run / "plan.json", client=client) == run
    assert len(runner.commands) == client.calls_started == 2
    result = read_json(run / "result.json")
    assert result["status"] == "candidate_with_model_review"
    assert result["model_calls"] == 2
    assert result["human_validated"] is False and result["graph_published"] is False
    assert read_json(run / "candidate.json")["paper"]["id"] == "paper-test"
    assert read_json(run / "model_review.json")["human_validated"] is False
    assert read_json(run / "validation.json")["semantic_accuracy"] == "not_established"
    calls = sorted((run / "calls").iterdir())
    assert len(calls) == 2
    for audit in calls:
        assert (audit / "request.json").is_file()
        assert (audit / "events.jsonl").is_file()
        metadata = read_json(audit / "result.json")
        assert metadata["effective_model"] is None
        assert metadata["usage"] == [{"input_tokens": 3, "output_tokens": 4}]
    assert read_json(imported / "sources.json")["scientific_reading_status"] == "not_started"
    assert_originals_unchanged(workspace)
    with pytest.raises(ValueError, match="already been executed"):
        execute_plan(workspace.settings, run / "plan.json", client=client)
    assert len(runner.commands) == 2


@pytest.mark.parametrize("artifact", ["extract_prompt.txt", "verify_prompt.txt", "response_schema.json"])
def test_prepared_artifact_mutation_stops_before_any_call(workspace, artifact):
    _, run = import_and_prepare(workspace)
    (run / artifact).write_text("altered", encoding="utf-8")
    runner = FakeRunner()
    with pytest.raises(ValueError, match="prepared artifact changed"):
        execute_plan(workspace.settings, run / "plan.json", client=client_for(workspace, runner))
    assert runner.commands == []
    assert not (run / "execution_started.json").exists()


@pytest.mark.parametrize("input_kind", ["bundle", "stored_original", "embedded_image", "taxonomy_snapshot"])
def test_prepared_input_mutation_stops_before_any_call(workspace, input_kind):
    imported, run = import_and_prepare(workspace)
    if input_kind == "bundle":
        target = run / "sources.json"
    elif input_kind == "taxonomy_snapshot":
        target = run / "taxonomy.json"
    elif input_kind == "stored_original":
        target = Path(read_json(imported / "sources.json")["imports"][0]["stored_path"])
    else:
        candidates = [Path(p["path"]) for p in read_json(run / "plan.json")["input_paths"] if Path(p["path"]).suffix == ".png"]
        assert candidates, "embedded SI image must be part of the frozen inputs"
        target = candidates[0]
    target.write_bytes(target.read_bytes() + b" changed")
    runner = FakeRunner()
    with pytest.raises(ValueError, match="input changed"):
        execute_plan(workspace.settings, run / "plan.json", client=client_for(workspace, runner))
    assert runner.commands == []
    assert_originals_unchanged(workspace)


def test_changed_code_or_disabled_client_stops_before_call(workspace):
    _, run = import_and_prepare(workspace)
    runner = FakeRunner()
    with pytest.raises(ValueError, match="live execution is disabled"):
        execute_plan(workspace.settings, run / "plan.json", client=client_for(workspace, runner, live=False))
    (workspace.project / "src" / "fixture.py").write_text("# changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="code or runtime changed"):
        execute_plan(workspace.settings, run / "plan.json", client=client_for(workspace, runner))
    assert runner.commands == []


def test_budget_covers_both_stages_before_extraction_starts(workspace):
    workspace.settings.codex.max_calls_per_run = 1
    _, run = import_and_prepare(workspace)
    runner = FakeRunner()
    with pytest.raises(ValueError, match="call budget"):
        execute_plan(workspace.settings, run / "plan.json", client=client_for(workspace, runner))
    assert runner.commands == []
    assert not (run / "execution_started.json").exists()
    execute_plan(workspace.settings, run / "plan.json", client=client_for(workspace, runner), verify=False)
    assert len(runner.commands) == 1
    assert read_json(run / "result.json")["status"] == "candidate_unreviewed"
    assert not (run / "model_review.json").exists()


@pytest.mark.parametrize("fail_at", [1, 2])
def test_failure_retains_audit_and_completed_stages_without_retry(workspace, fail_at):
    _, run = import_and_prepare(workspace)
    runner = FakeRunner(fail_at=fail_at)
    client = client_for(workspace, runner)
    with pytest.raises(ModelCallError, match="exited"):
        execute_plan(workspace.settings, run / "plan.json", client=client)
    assert len(runner.commands) == fail_at
    assert read_json(run / "failure.json")["model_calls"] == fail_at
    assert (run / "candidate.json").exists() is (fail_at == 2)
    assert not (run / "model_review.json").exists()
    assert not (run / "result.json").exists()
    assert len(list((run / "calls").glob("*/stderr.txt"))) == fail_at
    with pytest.raises(ValueError, match="already been executed"):
        execute_plan(workspace.settings, run / "plan.json", client=client)
    assert len(runner.commands) == fail_at
    assert_originals_unchanged(workspace)


def test_mutation_during_model_call_is_not_accepted(workspace):
    _, run = import_and_prepare(workspace)
    runner = FakeRunner(mutate_at=2)
    with pytest.raises(ModelCallError, match="input changed during model run"):
        execute_plan(workspace.settings, run / "plan.json", client=client_for(workspace, runner))
    assert (run / "candidate.json").exists()
    assert not (run / "model_review.json").exists()
    assert read_json(run / "failure.json")["model_calls"] == 2
    assert_originals_unchanged(workspace)


def test_reimport_preserves_old_run_and_reuses_content_snapshot(workspace):
    DataStore(workspace.data).initialize()
    first = import_paper(workspace.settings, workspace.folder, paper_id="paper-test")
    original_bundle = (first / "sources.json").read_bytes()
    second = import_paper(workspace.settings, workspace.folder, paper_id="paper-test")
    assert first != second
    assert (first / "sources.json").read_bytes() == original_bundle
    first_sources = read_json(first / "sources.json")["imports"]
    second_sources = read_json(second / "sources.json")["imports"]
    assert [s["stored_path"] for s in first_sources] == [s["stored_path"] for s in second_sources]
    assert [s["source_id"] for s in first_sources] == [s["source_id"] for s in second_sources]
    assert_originals_unchanged(workspace)


def test_discovery_and_import_scope_do_not_escape_source_root(workspace, tmp_path):
    entries = discover(workspace.settings)
    assert len(entries) == 1 and entries[0]["file_count"] == 2
    assert entries[0]["scientific_eligibility"] == "not_assessed"
    outside = tmp_path / "outside"
    outside.mkdir()
    synthetic_pdf(outside / "other.pdf")
    DataStore(workspace.data).initialize()
    with pytest.raises(ValueError, match="outside configured source_roots"):
        import_paper(workspace.settings, outside)


@pytest.mark.parametrize("field,value", [
    ("model", "different-synthetic-model"),
    ("reasoning_effort", "low"),
    ("timeout_seconds", 120),
    ("max_calls_per_run", 3),
    ("executable", "different-fake-codex.exe"),
])
def test_model_configuration_change_requires_new_plan(workspace, field, value):
    _, run = import_and_prepare(workspace)
    runner = FakeRunner()
    settings = workspace.settings.codex.model_copy(update={field: value})
    client = CodexClient(settings, live=True, runner=runner)
    with pytest.raises(ValueError, match="model settings changed"):
        execute_plan(workspace.settings, run / "plan.json", client=client)
    assert runner.commands == []
    assert not (run / "execution_started.json").exists()


@pytest.mark.parametrize("role,expected", [
    ("development", "development"),
    ("heldout", "validation_reserved"),
    ("unassigned", "unspecified"),
])
def test_dataset_role_is_declared_in_plan_and_checked_in_candidate(workspace, role, expected):
    _, run = import_and_prepare(workspace, role=role)
    prompt = (run / "extract_prompt.txt").read_text(encoding="utf-8")
    manifest = json.loads(prompt.split("INPUT MANIFEST (metadata, not scientific evidence):\n", 1)[1])
    assert manifest["dataset_role"] == expected
    assert manifest["paper_id"] == "paper-test"
    assert Path(manifest["taxonomy"]) == run / "taxonomy.json"
    assert (run / "taxonomy.json").read_bytes() == workspace.settings.taxonomy_path.read_bytes()
    execute_plan(workspace.settings, run / "plan.json", client=client_for(workspace, FakeRunner()))
    assert read_json(run / "candidate.json")["paper"]["dataset_role"] == expected
    issues = read_json(run / "validation.json")["issues"]
    assert not [item for item in issues if item["code"].startswith("PLAN_")]
    assert read_json(run / "result.json")["human_validated"] is False


@pytest.mark.parametrize("field,value,code", [
    ("paper.id", "wrong-paper", "PLAN_PAPER_MISMATCH"),
    ("paper.dataset_role", "validation_reserved", "PLAN_ROLE_MISMATCH"),
    ("paper.source_ids", [], "PLAN_SOURCE_INVENTORY_MISMATCH"),
    ("paper.source_ids", ["invented-source"], "PLAN_SOURCE_INVENTORY_MISMATCH"),
    ("rule_version", "synthetic-other-rule", "PLAN_VERSION_MISMATCH"),
    ("schema_version", "synthetic-other-schema", "PLAN_VERSION_MISMATCH"),
])
def test_candidate_metadata_mismatch_is_retained_and_flagged(workspace, field, value, code):
    _, run = import_and_prepare(workspace)

    def change_metadata(payload):
        if field.startswith("paper."):
            payload["paper"][field.split(".")[1]] = value
        else:
            payload[field] = value

    runner = FakeRunner(candidate_mutator=change_metadata)
    execute_plan(workspace.settings, run / "plan.json", client=client_for(workspace, runner))
    assert (run / "candidate.json").is_file()
    assert any(item["code"] == code and item["severity"] == "error"
               for item in read_json(run / "validation.json")["issues"])
    result = read_json(run / "result.json")
    assert result["validation_error_count"] >= 1
    assert result["human_validated"] is False and result["graph_published"] is False
    assert_originals_unchanged(workspace)


@pytest.mark.parametrize("field,value", [
    ("cohort_role", "heldout"),
    ("paper_id", "altered-paper"),
    ("input_paths", []),
    ("max_calls", 10),
])
def test_plan_mutation_is_rejected_before_any_model_call(workspace, field, value):
    _, run = import_and_prepare(workspace)
    path = run / "plan.json"
    plan = read_json(path)
    plan[field] = value
    path.write_text(json.dumps(plan), encoding="utf-8")
    runner = FakeRunner()
    with pytest.raises(ValueError):
        execute_plan(workspace.settings, path, client=client_for(workspace, runner))
    assert runner.commands == []
    assert not (run / "execution_started.json").exists()
