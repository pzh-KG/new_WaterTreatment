import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from test_active_reading import active
from test_workflow import workspace, read_json
from watertreatment.storage import write_new_json


def test_pilot_stops_on_budget_incomplete_and_does_not_count_it_completed(active, monkeypatch, capsys):
    setup, prepared, _, _ = active
    bundle = read_json(prepared / "manifest.json")["source_bundle"]
    script = Path(__file__).resolve().parents[1] / "scripts/quality/run_reading_pilot.py"
    spec = importlib.util.spec_from_file_location("pilot_fixture", script)
    pilot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pilot)
    monkeypatch.setattr(pilot, "load_settings", lambda _: setup.settings)
    monkeypatch.setattr(pilot, "AppServerClient", lambda *args, **kwargs: SimpleNamespace())
    attempted = []

    def incomplete(settings, plan, **kwargs):
        attempted.append(plan)
        write_new_json(plan.parent / "result.json", {"status": "candidate_incomplete_budget", "human_validated": False})

    monkeypatch.setattr(pilot, "execute_plan", incomplete)
    monkeypatch.setattr(sys, "argv", [str(script), "--live", "--bundle", bundle, "--bundle", bundle])
    assert pilot.main() == 1
    assert len(attempted) == 1
    first = json.loads(capsys.readouterr().out.splitlines()[0])
    summary = read_json(Path(first["pilot"]) / "result.json")
    assert summary["planned_paper_count"] == 2
    assert summary["attempted_paper_count"] == 1 and summary["completed_paper_count"] == 0
