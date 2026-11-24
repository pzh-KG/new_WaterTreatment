"""Rule upgrades use frozen versions; replay clients never call a provider."""
import copy
import json
import shutil
from pathlib import Path

from test_workflow import workspace, import_and_prepare
from test_web_model import model_fixture, fixture, ReplayClient, audit
from watertreatment.domain import ExtractionRecord
from watertreatment.web_model import reaudit, RecordQuery


def test_new_rule_plan_freezes_schema_and_quantitative_policy(workspace):
    root = Path(__file__).resolve().parents[1]
    for stem in ("extract", "verify"):
        shutil.copyfile(root / "prompts" / f"{stem}.v0.6.txt", workspace.project / "prompts" / f"{stem}.v0.6.txt")
    workspace.settings.rules_version = "0.6"
    _, run = import_and_prepare(workspace)
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    assert plan["rule_version"] == "0.6" and plan["schema_version"] == "0.2.0"
    prompt = (run / "extract_prompt.txt").read_text(encoding="utf-8")
    assert "QUANTITATIVE COVERAGE" in prompt and '"schema_version": "0.2.0"' in prompt


def test_model_reaudit_upgrades_versions_without_rewriting_source_candidate(model_fixture):
    data = model_fixture
    root = Path(__file__).resolve().parents[1]
    for stem in ("extract", "audit-reading"):
        shutil.copyfile(root / "prompts" / f"{stem}.v0.6.txt", data.settings.project_root / "prompts" / f"{stem}.v0.6.txt")
    data.settings.rules_version = "0.6"
    original = (data.run / "candidate.json").read_bytes()
    updated = copy.deepcopy(data.records.get(data.run.name)["record"])
    updated.update(schema_version="0.2.0", rule_version="0.6")
    client = ReplayClient([audit(revision=True), ExtractionRecord.model_validate(updated), audit()])
    result = reaudit(data.settings, data.records, data.run.name, "Review synthetic quantitative completeness", client_factory=client.factory)
    assert result["revision"]["accepted"]
    record = data.records.get(result["revision"]["record_id"])
    assert record["record"]["rule_version"] == "0.6" and record["record"]["schema_version"] == "0.2.0"
    assert (data.run / "candidate.json").read_bytes() == original
    run = data.settings.data_root / "runs" / result["run_id"]
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    assert plan["scientific_plan"]["schema_version"] == "0.2.0"
    assert (run / "response_schema.json").is_file()
    assert result["result"]["quantitative_coverage_summary"]["has_quantitative_inventory"] is False
    assert result["result"]["status"] == "candidate_audited_with_limitations"


def test_question_tool_can_read_quantitative_inventory_and_extensions():
    assert RecordQuery(section="quantitative_coverage").section == "quantitative_coverage"
    assert RecordQuery(section="extensions").section == "extensions"
