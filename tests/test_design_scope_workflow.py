"""Offline contract wiring; synthetic answers do not establish model accuracy."""
import json
from pathlib import Path

import pytest

from test_active_reading import FakeReader
from test_scope_workflow import install_prompts
from test_scope_workflow import register_new_scope_case
from test_web_model import model_fixture, fixture, ReplayClient, audit
from test_workflow import workspace, import_and_prepare, read_json
from watertreatment.design_scope import CORE_AREAS, RESEARCH_USES
from watertreatment.domain import (
    CoreResearchCheck, ExtractionRecord, ResearchDesignScope,
    ResearchEligibility, ResearchSourceIndex,
)
from watertreatment.reading import ReadingDossier, ReadingScopeEntry, SourceAnchor
from watertreatment.reading_tools import ReadingTools
from watertreatment.settings import ReadingSettings
from watertreatment.scope_policy import frozen_scope
from watertreatment.storage import hash_file
from watertreatment.workflow import execute_plan, schema_version_for_rules


@pytest.fixture
def planned08(workspace):
    install_prompts(workspace.project, "0.8")
    settings = workspace.settings
    settings.rules_version = "0.8"
    settings.codex.model = "gpt-6.2-sol"
    settings.codex.reasoning_effort = "max"
    settings.codex.max_calls_per_run = 7
    settings.reading = ReadingSettings(enabled=True)
    _, run = import_and_prepare(workspace)
    return workspace, run, read_json(run / "sources.json"), read_json(run / "plan.json")


class DesignReader(FakeReader):
    def __init__(self, settings, bundle, *, missing_contract=False):
        super().__init__(settings, bundle)
        self.missing_contract = missing_contract
        self.candidate.rule_version = "0.8"
        self.candidate.schema_version = "0.4.0"
        self.candidate.extraction_scope = "research_core"
        self.candidate.design_scope = ResearchDesignScope(
            core_checks=[CoreResearchCheck(area=area, state="checked",
                reason="Synthetic source contains no measured research result.") for area in sorted(CORE_AREAS)],
            source_index=[ResearchSourceIndex(id=f"idx-{i}", source_id=doc["source"]["source_id"],
                block_id=doc["blocks"][0]["block_id"], content_types=["synthetic_text"], reading_state="read",
                summary="Synthetic fixture; no numeric scientific results.",
                material_relation="No measured final-material experiment in this synthetic fixture.",
                reason="Indexed without fabricating performance observations.")
                for i, doc in enumerate(bundle["documents"])],
            eligibility=[ResearchEligibility(use=use, target_kind="paper", target_id="paper-test",
                status="not_assessed", reason="Synthetic fixture has no scientific applicability.")
                for use in sorted(RESEARCH_USES)])

    def complete(self, model, **kwargs):
        result = super().complete(model, **kwargs)
        if model is ReadingDossier:
            entry = self.candidate.design_scope.source_index[0]
            result.scope_outline = [ReadingScopeEntry(layer="core", subject="Synthetic source scope",
                purpose="Establish applicability before extraction", planned_action="Record source limits",
                evidence=[SourceAnchor(source_id=entry.source_id, block_id=entry.block_id)])]
        if model is ExtractionRecord:
            result.paper.id = "paper-test"
            if self.missing_contract and kwargs["identity"]["stage"] == "extraction":
                result.design_scope = None
        return result


def test_new_plan_freezes_actual_scope_contract_and_selected_model(planned08):
    workspace, run, _, plan = planned08
    assert (plan["rule_version"], plan["schema_version"]) == ("0.8", "0.4.0")
    assert plan["model_settings"]["model"] == "gpt-6.2-sol"
    assert plan["model_settings"]["reasoning_effort"] == "max"
    assert plan["extraction_policy"] == frozen_scope()
    policy = (workspace.project / "prompts" / "quantitative-policy.v0.8.txt").read_text(encoding="utf-8")
    for name in ("extract_prompt.txt", "understand_prompt.txt", "verify_prompt.txt"):
        assert (run / name).read_text(encoding="utf-8").count(policy) == 1
    schema = read_json(run / "response_schema.json")
    assert "design_scope" in schema["properties"]
    assert all(hash_file(Path(item["path"])) == item["sha256"] for item in plan["reading_artifacts"])
    assert schema_version_for_rules("0.7") == "0.3.0"


@pytest.mark.parametrize("missing_contract", [False, True])
def test_real_workflow_requires_contract_before_finishing(planned08, missing_contract):
    workspace, run, bundle, _ = planned08
    client = DesignReader(workspace.settings, bundle, missing_contract=missing_contract)
    if missing_contract:
        from watertreatment.audit_execution import ScientificReviewScopeUnavailable
        # Reconstructing a missing whole design contract cannot inherit a clean
        # scientific baseline. Retain the repaired candidate for explicit review
        # planning instead of silently spending a second whole-paper audit.
        with pytest.raises(ScientificReviewScopeUnavailable):
            execute_plan(workspace.settings, run / "plan.json", client=client)
        result = read_json(run / "result.json")
        assert result["status"] == "candidate_incomplete_failure"
        assert result["last_candidate_version"] == "candidate.v01.json"
        rejection = read_json(run / "scope_rejection.v01.json")
        assert rejection["automatic_full_audit"] is False
        assert any(x["code"] == "unclean_prior_history" for x in rejection["diagnostics"])
        assert not (run / "audit.v01.json").exists()
        assert not [x for x in read_json(run / "validation.v01.json")["issues"] if x["severity"] == "error"]
    else:
        execute_plan(workspace.settings, run / "plan.json", client=client)
    result = read_json(run / "result.json")
    if not missing_contract:
        assert result["validation_error_count"] == 0
        assert result["revisions"] == 0
    assert result["human_validated"] is False and result["graph_published"] is False
    candidate = read_json(run / "candidate.json")
    assert candidate["observations"] == []  # No synthetic numeric placeholders for indexed sources.
    assert candidate["design_scope"]["version"] == "design-scope-v1"
    if missing_contract:
        old = read_json(run / "candidate.v00.json")
        assert "design_scope" not in old
        assert any(issue["code"] == "DESIGN_SCOPE_REQUIRED" for issue in read_json(run / "validation.v00.json")["issues"])


def test_reading_tools_can_inspect_new_scope_fields_without_whole_record(planned08):
    _, run, bundle, plan = planned08
    candidate = DesignReader(planned08[0].settings, bundle).candidate
    snapshot = run / "query-fixture.json"
    snapshot.write_text(candidate.model_dump_json(), encoding="utf-8")
    tools = ReadingTools(bundle, run, max_calls=12, expected_inputs=plan["input_paths"])
    tools.bind_candidate(candidate, snapshot)
    reply = tools.handle("wt_query_candidate", {"section": "design_scope", "fields": ["core_checks", "source_index"]})
    assert reply["success"]
    payload = json.loads(reply["contentItems"][0]["text"])
    assert len(payload["items"][0]["core_checks"]) == 8
    assert "observations" not in payload["items"][0]
    by_path = tools.handle("wt_query_candidate", {"path": ["design_scope", "source_index", 0]})
    assert by_path["success"]


def test_legacy_dossier_serialization_unchanged():
    data = dict(paper_id="old", article_question_and_design="source notes", material_map=[],
        experiment_map=[], coverage=[], cross_source_conflicts=[], unresolved_questions=[], schema_gaps=[])
    assert ReadingDossier.model_validate(data).model_dump(mode="json") == data


def test_default08_recheck_keeps_existing07_scope_and_archive(model_fixture):
    from watertreatment.web_model import reaudit
    data = model_fixture
    run, baseline = register_new_scope_case(data)
    install_prompts(data.settings.project_root, "0.8")
    data.settings.rules_version = "0.8"
    before = (run / "candidate.json").read_bytes()
    client = ReplayClient([audit()])
    result = reaudit(data.settings, data.records, run.name, "Synthetic existing scope check.",
                    client_factory=client.factory)
    saved = read_json(data.settings.data_root / "runs" / result["run_id"] / "plan.json")
    assert saved["scientific_plan"]["rule_version"] == "0.7"
    assert saved["scientific_plan"]["schema_version"] == "0.3.0"
    assert saved["extraction_policy"]["mode"] == baseline.extraction_scope
    assert (run / "candidate.json").read_bytes() == before
    assert not result["result"]["graph_published"]
