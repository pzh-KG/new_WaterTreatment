"""Coverage recovery is an explicitly enabled, separate model operation."""
import json

from watertreatment import cli
from watertreatment.settings import Settings


def test_coverage_correction_requires_live_before_loading_plan(tmp_path, monkeypatch, capsys):
    from watertreatment.app_server import AppServerClient
    from watertreatment import coverage_correction_workflow

    def forbidden(*args, **kwargs):
        raise AssertionError("unenabled coverage correction cannot create a provider client")

    monkeypatch.setattr(AppServerClient, "__init__", forbidden)
    monkeypatch.setattr(coverage_correction_workflow, "execute", forbidden)
    settings = Settings(project_root=tmp_path / "project", data_root=tmp_path / "data")
    config = tmp_path / "settings.json"
    config.write_text(settings.model_dump_json(), encoding="utf-8")
    assert cli.main(["--config", str(config), "complete-coverage-correction", str(tmp_path / "missing.json")]) == 2
    assert "specify --live" in json.loads(capsys.readouterr().err)["error"]
    assert not settings.data_root.exists()


def test_partial_completion_requires_live_before_loading_plan(tmp_path, monkeypatch, capsys):
    from watertreatment.app_server import AppServerClient
    from watertreatment import partial_audit_completion_workflow

    def forbidden(*args, **kwargs):
        raise AssertionError("unenabled partial completion cannot create a provider client")

    monkeypatch.setattr(AppServerClient, "__init__", forbidden)
    monkeypatch.setattr(partial_audit_completion_workflow, "execute", forbidden)
    settings = Settings(project_root=tmp_path / "project", data_root=tmp_path / "data")
    config = tmp_path / "settings.json"
    config.write_text(settings.model_dump_json(), encoding="utf-8")
    assert cli.main(["--config", str(config), "complete-partial-audit", str(tmp_path / "missing.json")]) == 2
    assert "specify --live" in json.loads(capsys.readouterr().err)["error"]
    assert not settings.data_root.exists()
