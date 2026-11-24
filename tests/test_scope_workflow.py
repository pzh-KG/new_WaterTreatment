"""Offline scope integration: immutable plans, bounded supplementation and admission.

All sources and model replies are synthetic; passing tests proves the protocol,
not accuracy of scientific reading or a measured production latency.
"""
from pathlib import Path
import json
import shutil

import pytest

from test_workflow import workspace, import_and_prepare
from test_web_model import model_fixture, fixture, ReplayClient, audit
from test_web_records import completed_call, read
from test_research_scope import record as scoped_record
from watertreatment.domain import ExtractionRecord, NamedFact, QuantitativeCoverage
from watertreatment.scope_policy import frozen_scope, scope_plan_issues
from watertreatment.storage import digest_json, hash_file, write_new_json
from watertreatment.web_model import reaudit
from watertreatment.workflow import schema_version_for_rules, validate_plan_metadata


def install_prompts(project, version="0.7"):
    repository = Path(__file__).resolve().parents[1]
    prompts = project / "prompts"
    prompts.mkdir(exist_ok=True, parents=True)
    for path in (repository / "prompts").glob(f"*.v{version}.txt"):
        shutil.copyfile(path, prompts / path.name)


def policy_plan(candidate, policy):
    return {"paper_id": candidate.paper.id, "cohort_role": "unassigned", "rule_version": "0.7",
            "schema_version": "0.3.0", "extraction_policy": policy}


def error_codes(candidate, policy):
    return {issue.code for issue in scope_plan_issues(candidate, policy_plan(candidate, policy))}


def register_new_scope_case(data, *, version="0.7"):
    """Create a new temporary fixture run instead of rewriting a prior result."""
    install_prompts(data.settings.project_root, version)
    install_prompts(data.settings.project_root, "0.7")
    data.settings.rules_version = "0.7"
    run = data.store.new_run("extraction", {"paper_id": data.record.paper.id, "rule_version": version})
    shutil.copyfile(data.run / "sources.json", run / "sources.json")
    value = data.record.model_copy(deep=True)
    value.rule_version = version
    value.schema_version = schema_version_for_rules(version)
    value.quantitative_coverage = [QuantitativeCoverage(id="rate-course", source_id="main", block_id="paragraph",
        label="Synthetic evaporation course", data_kind="time_series", state="partial", observation_ids=["rate"],
        reason="Only the synthetic endpoint is recorded.", limitations=["Synthetic author sampling count is unknown."])]
    value.observations[0].series_id = "rate-course"
    value.observations[0].coordinates = [NamedFact(name="elapsed_time",
        fact={"value": 60, "status": "reported", "evidence_ids": ["source-value"]},
        unit={"value": "min", "status": "reported", "evidence_ids": ["source-value"]})]
    if version == "0.7":
        value.extraction_scope = "research_core"
        for observation in value.observations:
            observation.analysis_role = "key_result"
    source_path = Path(read(run / "sources.json")["documents"][0]["source"]["path"])
    plan = {"run_id": run.name, "paper_id": value.paper.id, "cohort_role": "unassigned", "rule_version": version,
            "schema_version": value.schema_version,
            "input_paths": [{"path": str(path), "sha256": hash_file(path)} for path in [run / "sources.json", source_path]]}
    if version == "0.7":
        plan["extraction_policy"] = frozen_scope()
    write_new_json(run / "plan.json", plan)
    data.store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    completed_call(run, value)
    data.records.refresh()
    detail = data.records.get(run.name)
    assert detail["summary"]["current"] and detail["summary"]["admissible"], detail["summary"]
    return run, value


def test_scope_workflow_fresh_plan_freezes_default_and_shared_policy(workspace):
    install_prompts(workspace.project)
    workspace.settings.rules_version = "0.7"
    workspace.settings.reading.enabled = True
    workspace.settings.codex.model = "gpt-6.2-sol"
    workspace.settings.codex.max_calls_per_run = 7
    _, run = import_and_prepare(workspace)
    plan = read(run / "plan.json")
    assert plan["rule_version"] == "0.7" and plan["schema_version"] == "0.3.0"
    assert plan["extraction_policy"] == frozen_scope()
    assert plan["extraction_policy"]["allow_new_dense"] is False
    assert plan["extraction_policy"]["preserved_observation_ids"] == []
    extract = (run / "extract_prompt.txt").read_text(encoding="utf-8")
    policy = (workspace.project / "prompts" / "quantitative-policy.v0.7.txt").read_text(encoding="utf-8")
    assert extract.count(policy) == 1
    delivered = json.loads(extract.split("INPUT MANIFEST (metadata, not scientific evidence):\n", 1)[1])
    assert delivered["extraction_policy"]["mode"] == "research_core"
    assert delivered["extraction_policy"]["allow_new_dense"] is False
    assert (run / "understand_prompt.txt").read_text(encoding="utf-8").count(policy) == 1
    assert all(hash_file(Path(item["path"])) == item["sha256"] for item in plan["reading_artifacts"])
    assert hash_file(run / "extract_prompt.txt") == plan["prompt_sha256"]


def test_scope_workflow_installed_schema_does_not_reinterpret_old_rule():
    assert schema_version_for_rules("0.5") == "0.1.0"
    assert schema_version_for_rules("0.6") == "0.2.0"
    assert schema_version_for_rules("0.7") == "0.3.0"


def test_scope_workflow_metadata_validation_requires_frozen_policy():
    candidate = scoped_record()
    plan = policy_plan(candidate, frozen_scope())
    del plan["extraction_policy"]
    issues = validate_plan_metadata(candidate, plan, [])
    assert "SCOPE_PLAN_REQUIRED" in {issue.code for issue in issues}


def test_scope_workflow_core_blocks_new_dense_but_retains_existing_archive():
    candidate = scoped_record()
    before = candidate.model_dump_json()
    assert "UNREQUESTED_DENSE_POINT" in error_codes(candidate, frozen_scope())
    inherited = frozen_scope(baseline=candidate)
    assert not error_codes(candidate, inherited)
    assert inherited["baseline_record_fingerprint"] == digest_json(candidate.model_dump(mode="json"))
    assert candidate.model_dump_json() == before


def test_scope_workflow_only_requested_series_can_receive_new_dense_points():
    baseline = scoped_record()
    baseline.observations = [baseline.observations[0]]
    baseline.quantitative_coverage[0].observation_ids = ["end"]
    other = baseline.quantitative_coverage[0].model_copy(update={"id": "other", "observation_ids": []})
    baseline.quantitative_coverage.append(other)
    policy = frozen_scope("series_digitization", ["curve"], baseline)
    changed = scoped_record("series_digitization")
    changed.quantitative_coverage.append(other)
    assert not error_codes(changed, policy)
    changed.observations[1].series_id = "other"
    assert "UNREQUESTED_DENSE_POINT" in error_codes(changed, policy)


def test_scope_workflow_existing_observations_cannot_be_deleted_to_clear_errors():
    candidate = scoped_record()
    policy = frozen_scope(baseline=candidate)
    candidate.observations = candidate.observations[:1]
    assert "SCOPE_ARCHIVE_LOSS" in error_codes(candidate, policy)


def test_scope_workflow_scope_cannot_be_changed_by_model():
    candidate = scoped_record("series_digitization")
    assert "SCOPE_PLAN_MISMATCH" in error_codes(candidate, frozen_scope())


def test_scope_workflow_digitization_reuses_record_then_audits_and_registers(model_fixture, monkeypatch):
    data = model_fixture
    old_original = (data.run / "candidate.json").read_bytes()
    run, baseline = register_new_scope_case(data)
    original = (run / "candidate.json").read_bytes()
    supplemented = baseline.model_copy(deep=True)
    supplemented.extraction_scope = "series_digitization"
    supplemented.digitization_targets = ["rate-course"]
    point = supplemented.observations[0].model_copy(deep=True)
    point.id = "rate-interior"
    point.analysis_role = "dense_series_point"
    point.role_reason = "Optional synthetic interior marker requested for this series only."
    point.value.status = "estimated"
    point.origin = "plot_estimate"
    point.coordinates[0].fact.value = 20
    supplemented.observations.append(point)
    supplemented.quantitative_coverage[0].observation_ids.append(point.id)
    client = ReplayClient([supplemented, audit()])
    monkeypatch.setattr("watertreatment.graph_store.load_graph", lambda *a, **k: pytest.fail("Neo4j publication is not part of extraction"))
    result = reaudit(data.settings, data.records, run.name, "Digitize only the explicitly selected synthetic curve.",
        digitization_targets=["rate-course"], client_factory=client.factory)
    assert result["revision"]["accepted"] and result["result"]["graph_published"] is False
    assert client.calls_started == 2 and client.closed
    assert [call["stage"] for call in client.calls] == ["revision_digitization_0", "audit_web_0"]
    assert not any(call["stage"] in {"comprehension", "extraction"} for call in client.calls)
    detail = data.records.get(result["revision"]["record_id"])
    assert detail["summary"]["current"] and detail["summary"]["admissible"]
    assert detail["summary"]["publish_status"] == "not_published"
    assert detail["record"]["extraction_scope"] == "series_digitization"
    assert {o["id"] for o in detail["record"]["observations"]} == {o.id for o in supplemented.observations}
    assert (run / "candidate.json").read_bytes() == original
    assert (data.run / "candidate.json").read_bytes() == old_original
    audit_run = data.settings.data_root / "runs" / result["run_id"]
    assert read(audit_run / "plan.json")["extraction_policy"] == frozen_scope("series_digitization", ["rate-course"], baseline)
    assert read(audit_run / "input_candidate.json") == baseline.model_dump(mode="json")


def test_scope_workflow_new_default_does_not_migrate_legacy_record(model_fixture):
    data = model_fixture
    run, baseline = register_new_scope_case(data, version="0.6")
    original = (run / "candidate.json").read_bytes()
    client = ReplayClient([audit()])
    result = reaudit(data.settings, data.records, run.name, "Review this existing version without migration.", client_factory=client.factory)
    detail = data.records.get(result["revision"]["record_id"])
    assert result["revision"]["accepted"]
    assert detail["record"]["rule_version"] == "0.6" and detail["record"]["schema_version"] == "0.2.0"
    assert "extraction_scope" not in detail["record"] and "digitization_targets" not in detail["record"]
    assert (run / "candidate.json").read_bytes() == original
    assert client.calls_started == 1
    audit_run = data.settings.data_root / "runs" / result["run_id"]
    assert "extraction_policy" not in read(audit_run / "plan.json")


def test_scope_workflow_legacy_digitization_is_rejected_before_model(model_fixture):
    data = model_fixture
    data.settings.rules_version = "0.7"
    with pytest.raises(ValueError, match="旧版本"):
        reaudit(data.settings, data.records, data.run.name, "Attempt an implicit migration.", digitization_targets=["curve"],
                client_factory=lambda *a, **k: pytest.fail("No provider or replay should be called"))


@pytest.mark.parametrize("violation", ["new_dense_core", "outside_target", "archive_loss"])
def test_scope_workflow_positive_model_audit_cannot_override_scope_errors(model_fixture, violation):
    data = model_fixture
    run, baseline = register_new_scope_case(data)
    original = (run / "candidate.json").read_bytes()
    invalid = baseline.model_copy(deep=True)
    targets = None if violation == "new_dense_core" else ["rate-course"]
    if targets is not None:
        invalid.extraction_scope, invalid.digitization_targets = "series_digitization", targets
    if violation == "archive_loss":
        invalid.observations = invalid.observations[:1]
        expected = "SCOPE_ARCHIVE_LOSS"
    else:
        point = invalid.observations[0].model_copy(deep=True)
        point.id, point.analysis_role = "new-interior", "dense_series_point"
        point.role_reason = "Optional synthetic interior marker, not a critical event."
        point.coordinates[0].fact.value = 30
        point.value.status, point.origin = "estimated", "plot_estimate"
        invalid.observations.append(point)
        if violation == "outside_target":
            point.series_id = "unrequested-course"
            invalid.quantitative_coverage.append(invalid.quantitative_coverage[0].model_copy(
                update={"id": "unrequested-course", "observation_ids": [point.id]}))
        else:
            invalid.quantitative_coverage[0].observation_ids.append(point.id)
        expected = "UNREQUESTED_DENSE_POINT"
    responses = [invalid, audit(), invalid, audit()]
    if targets is None:
        responses.insert(0, audit(revision=True))
    client = ReplayClient(responses)
    result = reaudit(data.settings, data.records, run.name, "Synthetic positive reviews must not waive the frozen scope.",
        digitization_targets=targets, client_factory=client.factory)
    assert result["incomplete"] and result["result"]["status"] == "needs_revision"
    assert result["result"]["validation_error_count"] > 0
    assert "revision" not in result and not result["result"]["graph_published"]
    assert data.records.get(run.name)["summary"]["current"]
    assert (run / "candidate.json").read_bytes() == original
    audit_run = data.settings.data_root / "runs" / result["run_id"]
    last_validation = sorted(audit_run.glob("validation.v*.json"))[-1]
    assert expected in {item["code"] for item in read(last_validation)["issues"]}
