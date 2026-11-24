"""An audit recovery command cannot implicitly enable provider execution."""
import json

from watertreatment import cli
from watertreatment.settings import Settings


def test_addendum_execution_requires_live_before_opening_or_running_plan(tmp_path, monkeypatch, capsys):
    from watertreatment.app_server import AppServerClient
    from watertreatment import audit_addendum_workflow

    def forbidden(*args, **kwargs):
        raise AssertionError("an unenabled command must not create a model client or execute a plan")

    monkeypatch.setattr(AppServerClient, "__init__", forbidden)
    monkeypatch.setattr(audit_addendum_workflow, "execute_audit_addendum", forbidden)
    settings = Settings(project_root=tmp_path / "project", data_root=tmp_path / "data")
    config = tmp_path / "settings.json"
    config.write_text(settings.model_dump_json(), encoding="utf-8")
    assert cli.main(["--config", str(config), "complete-audit-addendum", str(tmp_path / "missing-plan.json")]) == 2
    assert "specify --live" in json.loads(capsys.readouterr().err)["error"]
    assert not settings.data_root.exists()
