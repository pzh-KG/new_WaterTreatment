import json

import pytest

from watertreatment import model_check
from watertreatment.codex import ModelCallError
from watertreatment.settings import Settings, CodexSettings
from watertreatment.storage import DataStore


@pytest.mark.parametrize("correct", [True, False])
def test_synthetic_check_does_not_claim_scientific_validation(tmp_path, monkeypatch, correct):
    settings = Settings(project_root=tmp_path / "project", data_root=tmp_path / "data",
                        codex=CodexSettings(model="gpt-6.2-sol", reasoning_effort="max"))
    DataStore(settings.data_root).initialize()
    monkeypatch.setattr(model_check.secrets, "token_hex", lambda n: "ABCD1234")
    monkeypatch.setattr(model_check.secrets, "randbelow", lambda n: 4)
    class FakeClient:
        live = True
        calls_started = 0
        def __init__(self):
            self.settings = settings.codex
        def complete(self, cls, **kwargs):
            self.calls_started += 1
            assert "Final material: synthetic-composite-A" in kwargs["prompt"]
            assert "Do not call tools" in kwargs["prompt"]
            assert kwargs["image_paths"][0] in kwargs["input_paths"]
            return cls(passage_code="ABCD1234", material_name="synthetic-composite-A", oxidant="PMS",
                       evaporation_rate=2.34 if correct else None, image_code="ABCD1234", cycle_count=9)
    if correct:
        run = model_check.check_model(settings, live=True, client=FakeClient())
    else:
        with pytest.raises(ModelCallError, match="failed"):
            model_check.check_model(settings, live=True, client=FakeClient())
        run = next((settings.data_root / "runs").iterdir())
        assert (run / "failure.json").is_file()
    result = json.loads((run / "result.json").read_text())
    assert result["status"] == ("passed" if correct else "failed")
    assert result["model_calls"] == 1 and result["graph_writes"] == 0
    assert result["synthetic"] and result["scientific_accuracy"] == "not_established"


def test_model_check_without_live_creates_no_run(tmp_path):
    settings = Settings(project_root=tmp_path / "project", data_root=tmp_path / "data")
    with pytest.raises(ModelCallError, match="no model invoked"):
        model_check.check_model(settings)
    assert not settings.data_root.exists()
