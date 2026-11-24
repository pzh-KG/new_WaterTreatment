"""Offline checks for registered-candidate provenance at CLI read/export boundaries."""

import json
import shutil
import zipfile
from types import SimpleNamespace

import pytest

from watertreatment import cli
from watertreatment.codex import CodexClient
from watertreatment.domain import ExtractionRecord, Paper
from watertreatment.settings import Settings
from watertreatment.storage import DataStore
from watertreatment.workflow import import_paper, prepare


@pytest.fixture
def registered(tmp_path, monkeypatch):
    def no_model(*args, **kwargs):
        pytest.fail("CLI validation/projection must remain offline")
    monkeypatch.setattr(CodexClient, "complete", no_model)
    project = tmp_path / "project"
    (project / "prompts").mkdir(parents=True)
    (project / "config").mkdir()
    for name in ("extract.v0.5.txt", "verify.v0.5.txt"):
        (project / "prompts" / name).write_text("Synthetic offline instructions.", encoding="utf-8")
    (project / "config" / "taxonomy.v0.5.json").write_text('{"version":"0.5"}', encoding="utf-8")
    source_root = tmp_path / "sources"
    folder = source_root / "1_synthetic"
    folder.mkdir(parents=True)
    with zipfile.ZipFile(folder / "main.docx", "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Synthetic source.</w:t></w:r></w:p></w:body></w:document>')
    settings = Settings(project_root=project, data_root=tmp_path / "data", source_roots=[source_root])
    config = project / "config" / "project.local.json"
    config.write_text(settings.model_dump_json(), encoding="utf-8")
    store = DataStore(settings.data_root)
    store.initialize()
    imported = import_paper(settings, folder, paper_id="synthetic-paper")
    run = prepare(settings, imported / "sources.json", cohort_role="heldout")
    bundle = json.loads((run / "sources.json").read_text(encoding="utf-8"))
    candidate = ExtractionRecord(paper=Paper(id="synthetic-paper", dataset_role="validation_reserved",
                                            source_ids=[d["source"]["source_id"] for d in bundle["documents"]]))
    path = run / "candidate.json"
    path.write_text(candidate.model_dump_json(), encoding="utf-8")
    return SimpleNamespace(settings=settings, store=store, config=config, run=run,
                           candidate=candidate, path=path, bundle=bundle)


def invoke(case, command, *extra, record=None):
    return cli.main(["--config", str(case.config), command, str(record or case.path), *map(str, extra)])


def test_registered_candidate_automatically_uses_its_source_bundle(registered, capsys):
    assert invoke(registered, "project") == 0
    output = json.loads(capsys.readouterr().out)
    assert output["scientific_validation"] == "not_established"
    from pathlib import Path
    graph = json.loads((Path(output["export"]) / "graph.json").read_text(encoding="utf-8"))
    assert graph["source_check"] == "checked_with_issues"
    assert registered.candidate.paper.source_ids


@pytest.mark.parametrize("command", ["validate", "project"])
@pytest.mark.parametrize("field,value,code", [
    ("paper.id", "other-paper", "PLAN_PAPER_MISMATCH"),
    ("paper.dataset_role", "development", "PLAN_ROLE_MISMATCH"),
    ("paper.source_ids", [], "PLAN_SOURCE_INVENTORY_MISMATCH"),
    ("rule_version", "other-version", "PLAN_VERSION_MISMATCH"),
])
def test_registered_candidate_cannot_bypass_plan_metadata(registered, capsys, command, field, value, code):
    payload = registered.candidate.model_dump(mode="json")
    owner = payload["paper"] if field.startswith("paper.") else payload
    owner[field.split(".")[-1]] = value
    registered.path.write_text(json.dumps(payload), encoding="utf-8")
    # No validation.json is required or trusted: constraints are recomputed.
    assert invoke(registered, command) == 1
    output = json.loads(capsys.readouterr().out)
    assert any(issue["code"] == code for issue in output["issues"])
    assert not list((registered.settings.data_root / "exports").iterdir())


@pytest.mark.parametrize("damage", ["missing_plan", "changed_plan", "changed_bundle", "changed_source"])
def test_registered_candidate_fails_closed_on_broken_provenance(registered, capsys, damage):
    from pathlib import Path
    if damage == "missing_plan":
        (registered.run / "plan.json").unlink()
    elif damage == "changed_plan":
        path = registered.run / "plan.json"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    elif damage == "changed_bundle":
        path = registered.run / "sources.json"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    else:
        Path(registered.bundle["documents"][0]["source"]["path"]).write_bytes(b"changed snapshot")
    assert invoke(registered, "project") == 2
    assert "error" in json.loads(capsys.readouterr().err)
    assert not list((registered.settings.data_root / "exports").iterdir())


def test_registered_candidate_rejects_even_identical_bundle_from_another_path(registered, tmp_path, capsys):
    other = tmp_path / "other-sources.json"
    shutil.copyfile(registered.run / "sources.json", other)
    assert invoke(registered, "validate", "--bundle", other) == 2
    assert "own run source bundle" in capsys.readouterr().err


def test_registered_candidate_accepts_explicit_own_bundle(registered, capsys):
    assert invoke(registered, "validate", "--bundle", registered.run / "sources.json") == 0
    assert json.loads(capsys.readouterr().out)["scientific_accuracy"] == "not_established"


@pytest.mark.parametrize("command", ["validate", "project"])
def test_later_development_exposure_invalidates_registered_heldout_candidate(registered, capsys, command):
    hashes = [d["source"]["sha256"] for d in registered.bundle["documents"]]
    registered.store.register_cohort("renamed-paper", hashes, "development")
    assert invoke(registered, command) == 1
    output = json.loads(capsys.readouterr().out)
    assert any(issue["code"] == "PLAN_COHORT_STALE" for issue in output["issues"])


def test_manual_record_still_supported_but_provided_bundle_checks_original_hashes(registered, tmp_path, capsys):
    from pathlib import Path
    manual = tmp_path / "manual-record.json"
    manual.write_text(registered.candidate.model_dump_json(), encoding="utf-8")
    assert invoke(registered, "validate", record=manual) == 0
    assert json.loads(capsys.readouterr().out)["scientific_accuracy"] == "not_established"
    Path(registered.bundle["documents"][0]["source"]["path"]).write_bytes(b"changed snapshot")
    assert invoke(registered, "validate", "--bundle", registered.run / "sources.json", record=manual) == 2
    assert "source integrity failure" in capsys.readouterr().err


def test_graph_load_requires_live_and_preserves_frozen_candidate_metadata(registered, monkeypatch, capsys):
    from watertreatment import graph_store
    monkeypatch.setattr(graph_store, "load_graph", lambda *a, **k: pytest.fail("must not connect"))
    assert invoke(registered, "graph-load") == 2
    assert "--live" in capsys.readouterr().err
    registered.candidate.paper.id = "wrong-paper"
    registered.path.write_text(registered.candidate.model_dump_json(), encoding="utf-8")
    assert invoke(registered, "graph-load", "--live") == 1
    assert any(i["code"] == "PLAN_PAPER_MISMATCH" for i in json.loads(capsys.readouterr().out)["issues"])


def test_graph_load_freezes_record_and_marks_only_candidate_status(registered, monkeypatch, capsys):
    from watertreatment import graph_store
    seen = []
    def load(settings, graph, **kwargs):
        seen.append(kwargs)
        assert kwargs["source_record"].read_text(encoding="utf-8")
        assert kwargs["source_record"] != registered.path
        return {"status":"rolled_back", "scientific_accuracy":"not_established", "persistent_graph_write":False}
    monkeypatch.setattr(graph_store,"load_graph",load)
    assert invoke(registered,"graph-load","--live","--rollback") == 0
    output=json.loads(capsys.readouterr().out)
    assert output["status"]=="rolled_back" and not output["persistent_graph_write"]
    assert seen[0]["live"] and seen[0]["rollback"]


def test_manual_record_cannot_be_loaded_without_registered_source_plan(registered,tmp_path,capsys):
    manual=tmp_path/"record.json"
    manual.write_text(registered.candidate.model_dump_json(),encoding="utf-8")
    assert invoke(registered,"graph-load","--live",record=manual) == 2
    assert "registered candidate" in capsys.readouterr().err
