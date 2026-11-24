"""A historical receipt can be reused, but cannot authorize a new turn under another model."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from watertreatment.model_policy import manifest_matches_plan, receipt_matches_plan, require_active_model
from watertreatment.scope_policy import scientific_prompt
from watertreatment.settings import CodexSettings


ROOT = Path(__file__).resolve().parents[1]
OLD = "gpt-6.1-sol"
NEW = "gpt-6.2-sol"
CONFIGS = [ROOT / "config" / "project.example.json"] + [
    path for path in (ROOT / "config").glob("*.local.json")
    if path.name in {"project.local.json", "continuation-pilot.local.json"}
]


@pytest.mark.parametrize("path", CONFIGS, ids=lambda path: path.name)
def test_project_profiles_select_new_model_and_max(path):
    profile = CodexSettings.model_validate(json.loads(path.read_text(encoding="utf-8-sig"))["codex"])
    require_active_model(profile)
    assert (profile.model, profile.reasoning_effort) == (NEW, "max")


@pytest.mark.parametrize(("model", "effort"), [(OLD, "max"), (NEW, "xhigh"), (None, "max")])
def test_new_calls_require_current_model_and_effort(model, effort):
    with pytest.raises(ValueError):
        require_active_model(CodexSettings(model=model, reasoning_effort=effort))


@pytest.mark.parametrize(("selected", "other"), [(OLD, NEW), (NEW, OLD)])
@pytest.mark.parametrize("web", [True, False])
def test_each_receipt_is_bound_to_its_own_plan_request_and_server_selection(selected, other, web):
    manifest = {"model": selected, "effort": "max"}
    plan = manifest if web else {"model_settings": {"model": selected, "reasoning_effort": "max"}}
    request = {"model": selected, "reasoning_effort": "max"}
    receipt = {"requested_model": selected, "server_selected_model": selected, "server_selected_effort": "max"}
    assert manifest_matches_plan(plan, manifest)
    assert not manifest_matches_plan(plan, {**manifest, "model": other})
    assert receipt_matches_plan(plan, request, receipt)
    assert not receipt_matches_plan(plan, {**request, "model": other}, receipt, legacy_sparse=True)
    for field, value in (("server_selected_model", other), ("requested_model", other), ("server_selected_effort", "high")):
        assert not receipt_matches_plan(plan, request, {**receipt, field: value}, legacy_sparse=True)
    assert not receipt_matches_plan(plan, {"model": selected}, receipt, legacy_sparse=True)
    assert not receipt_matches_plan({}, {}, receipt)
    assert receipt_matches_plan({}, {}, receipt, legacy_sparse=True) == (selected == OLD)


@pytest.mark.parametrize("version", ["0.5", "0.6", "0.8"])
def test_frozen_scientific_prompt_binds_to_original_reader(version):
    settings = SimpleNamespace(project_root=ROOT, codex=CodexSettings(model=NEW))
    current = scientific_prompt(settings, "understand", version)
    original = scientific_prompt(settings, "understand", version, reader_model=OLD)
    if version == "0.8":
        assert current == original
    else:
        assert "6.1 Sol MAX" in original
        assert current == original.replace("6.1 Sol MAX", "6.2 Sol MAX")
