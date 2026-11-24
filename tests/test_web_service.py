"""Offline orchestration tests with real import/plan creation and fake providers."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_web_library import MAIN, SI, pdf, docx
from test_web_records import completed_call, fake_loader
from watertreatment import app_server, workflow
from watertreatment.domain import ExtractionRecord
from watertreatment.settings import CodexSettings, ReadingSettings, Settings
from watertreatment.storage import hash_file, write_new_json
from watertreatment.web_service import Workbench


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "code"
    (project / "prompts").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "src").mkdir()
    (project / "src" / "synthetic.py").write_text("# Synthetic stable project identity\n", encoding="utf-8")
    for name in ("extract", "verify", "understand", "audit-reading", "revise-reading"):
        shutil.copyfile(root / "prompts" / (name + ".v0.5.txt"), project / "prompts" / (name + ".v0.5.txt"))
    shutil.copyfile(root / "config" / "taxonomy.v0.5.json", project / "config" / "taxonomy.v0.5.json")
    source = tmp_path / "papers"
    source.mkdir()
    settings = Settings(project_root=project, data_root=tmp_path / "data", source_roots=[source],
                        codex=CodexSettings(model="gpt-6.2-sol", reasoning_effort="max", max_calls_per_run=7),
                        reading=ReadingSettings(enabled=True))
    service = Workbench(settings)
    clients = []

    class FakeClient:
        def __init__(self, model_settings, *, live, total_timeout_seconds):
            assert live and model_settings.model == "gpt-6.2-sol" and model_settings.reasoning_effort == "max"
            self.closed = False
            clients.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(app_server, "AppServerClient", FakeClient)
    service.jobs.handler = service.handle_job
    yield SimpleNamespace(service=service, settings=settings, source=source, clients=clients)
    service.close()


def add_paper(workbench, number=1, *, duplicate=False):
    folder = workbench.source / f"{number}_synthetic_article"
    folder.mkdir()
    main = pdf(folder / "main.pdf", [MAIN + f"\nSynthetic result rate 2.4 for study {number}."])
    docx(folder / "support.docx", SI)
    if duplicate:
        (folder / "duplicate.pdf").write_bytes(main.read_bytes())
    paper = workbench.service.library.scan([str(folder)])[0]
    assert paper["attachment_status"] == "ready"
    return paper


def candidate_from_plan(path):
    plan = read(path)
    bundle = read(path.parent / "sources.json")
    documents = bundle["documents"]
    main = next(d for d in documents if d["source"]["role"] == "main_article")
    block = next(b for b in main["blocks"] if "rate 2.4" in b["text"])
    return ExtractionRecord.model_validate({
        "paper": {"id": plan["paper_id"], "source_ids": [d["source"]["source_id"] for d in documents],
                  "title": {"value": "Synthetic orchestration fixture", "status": "reported"},
                  "dataset_role": {"development": "development", "heldout": "validation_reserved", "unassigned": "unspecified"}[plan["cohort_role"]]},
        "materials": [{"id": "final", "role": "primary_final"}],
        "experiments": [{"id": "coupled", "material_ids": ["final"], "purpose": "coupled"}],
        "observations": [{"id": "rate", "experiment_id": "coupled", "material_id": "final",
                          "metric_kind": "evaporation_rate", "value": {"value": 2.4, "status": "reported", "evidence_ids": ["rate-evidence"]},
                          "unit": {"status": "not_reported"}}],
        "evidence": [{"id": "rate-evidence", "source_id": main["source"]["source_id"], "block_id": block["block_id"], "quote": "rate 2.4"}],
        "coverage": [{"source_id": d["source"]["source_id"], "block_ids": [b["block_id"] for b in d["blocks"]],
                      "state": "read_structured", "text_read": True} for d in documents],
    })


def install_fake_execution(monkeypatch, *, unsupported=False, after=None):
    captured = []

    def execute(settings, plan_path, *, client, verify):
        # Crucially, prepare() is NOT mocked: it returns a run directory, and
        # orchestration must pass its existing plan.json to execution.
        assert plan_path.name == "plan.json" and plan_path.is_file()
        assert verify is True
        captured.append(plan_path)
        completed_call(plan_path.parent, candidate_from_plan(plan_path), unsupported=unsupported)
        if after:
            after(plan_path)
        return plan_path.parent

    monkeypatch.setattr(workflow, "execute_plan", execute)
    return captured


def extract(workbench, paper, **changes):
    args = {"paper_id": paper["id"], "cohort_role": "unassigned", "auto_publish": False, **changes}
    return workbench.service.handle_job({"kind": "extract", "payload": args}, lambda *args, **kwargs: None)


def test_real_import_prepare_and_candidate_registration_preserve_sources(workbench, monkeypatch):
    paper = add_paper(workbench, duplicate=True)
    hashes = {p: hash_file(p) for p in Path(paper["folder"]).iterdir()}
    plans = install_fake_execution(monkeypatch)
    output = extract(workbench, paper)
    assert len(plans) == 1
    plan = read(plans[0])
    assert plan["workflow"] == "active_reading"
    assert plan["model_settings"]["model"] == "gpt-6.2-sol"
    assert plan["model_settings"]["reasoning_effort"] == "max"
    bundle = read(plans[0].parent / "sources.json")
    assert len(bundle["documents"]) == 2
    assert {d["source"]["role"] for d in bundle["documents"]} == {"main_article", "supporting_information"}
    assert not output["incomplete"]
    assert output["record_id"] == plans[0].parent.name
    assert workbench.service.records.get(output["record_id"])["summary"]["admissible"]
    assert all(client.closed for client in workbench.clients)
    assert all(hash_file(path) == before for path, before in hashes.items())
    assert workbench.service.jobs.list() == []  # Explicit manual-publish policy.


def budget_policy():
    return {"initial_seconds": 900, "initial_calls": 5, "addition_seconds": 300,
            "addition_calls": 2, "max_auto_additions": 1,
            "max_total_seconds": 1200, "max_total_calls": 7}


def test_batch_freezes_one_policy_and_repeated_click_does_not_duplicate_jobs(workbench):
    first, second = add_paper(workbench, 21), add_paper(workbench, 22)
    policy = budget_policy()
    ids = workbench.service.queue_extractions([first["id"], second["id"]], budget_policy=policy)
    assert workbench.service.queue_extractions([first["id"], second["id"]], budget_policy=policy) == ids
    jobs = [workbench.service.jobs.get(identifier) for identifier in ids]
    assert jobs[0]["payload"]["batch_id"] == jobs[1]["payload"]["batch_id"]
    assert all(j["payload"]["budget_policy"] == policy for j in jobs)
    policy["max_total_calls"] = 9
    assert workbench.service.jobs.get(ids[0])["payload"]["budget_policy"]["max_total_calls"] == 7
    with pytest.raises(ValueError, match="冻结额度"):
        workbench.service.queue_extractions([first["id"], second["id"]], budget_policy=policy)
    assert len(workbench.service.jobs.list()) == 2


def test_batch_validates_every_paper_and_active_recheck_before_any_new_job(workbench, monkeypatch):
    first, second = add_paper(workbench, 31), add_paper(workbench, 32)
    job_id = workbench.service.jobs.enqueue("recheck", {"record_id": "synthetic-current-record"})
    monkeypatch.setattr(workbench.service.records, "get", lambda _: {"summary": {"paper_id": second["id"]}})
    with pytest.raises(ValueError, match="已有排队"):
        workbench.service.queue_extractions([first["id"], second["id"]], budget_policy=budget_policy())
    assert [j["id"] for j in workbench.service.jobs.list()] == [job_id]


def test_batch_settings_apply_only_to_new_plan_and_preserve_global_settings(workbench, monkeypatch):
    paper = add_paper(workbench, 41)
    before = workbench.settings.model_dump(mode="json")
    captured = install_fake_execution(monkeypatch)
    identifier = workbench.service.queue_extractions([paper["id"]], budget_policy=budget_policy())[0]
    assert workbench.service.jobs.run_one()
    assert workbench.service.jobs.get(identifier)["status"] == "completed"
    plan = read(captured[0])
    assert plan["max_calls"] == 5 and plan["reading_settings"]["total_timeout_seconds"] == 900
    frozen = read(Path(plan["budget_policy"]["path"]))
    assert frozen["policy"] == budget_policy()
    assert frozen["job_id"] == identifier
    assert frozen["batch_id"] == workbench.service.jobs.get(identifier)["payload"]["batch_id"]
    assert plan["model_settings"]["model"] == "gpt-6.2-sol"
    assert plan["model_settings"]["reasoning_effort"] == "max"
    assert workbench.settings.model_dump(mode="json") == before


def test_budget_policy_invalid_batch_creates_no_jobs_or_model_clients(workbench):
    paper = add_paper(workbench, 51)
    policy = {**budget_policy(), "max_total_seconds": 300}
    with pytest.raises(ValueError, match="累计上限"):
        workbench.service.queue_extractions([paper["id"]], budget_policy=policy)
    assert workbench.service.jobs.list() == [] and workbench.clients == []


def test_service_terminal_callback_runs_only_after_durable_failure(workbench, monkeypatch):
    paper = add_paper(workbench, 61)
    observed = []
    monkeypatch.setattr(workbench.service.recovery, "automatic_budget_extension", lambda identifier:
        observed.append(workbench.service.jobs.get(identifier)) or {"action": "blocked", "reason": "source usage needs review"},
        raising=False)
    identifier = workbench.service.queue_extractions([paper["id"]], budget_policy=budget_policy())[0]
    workbench.service.jobs.handler = lambda *_: (_ for _ in ()).throw(TimeoutError("bounded extraction failed"))
    assert workbench.service.jobs.run_one()
    assert observed[0]["status"] == "failed"
    job = workbench.service.jobs.get(identifier)
    assert job["error"] == "TimeoutError: bounded extraction failed"
    assert job["payload"]["auto_budget_decision"]["reason"] == "source usage needs review"


@pytest.mark.parametrize("expired", [False, True])
def test_web_extraction_passes_only_current_attachment_limits(workbench, monkeypatch, expired):
    from test_web_attachment_reviews import review_for
    paper = add_paper(workbench)
    manifest = review_for(workbench.service.library, paper,
        limitations=["not_located_in_current_local_files: supplementary figure image"])
    reviewed = workbench.service.library.apply_attachment_review(paper["id"], manifest)
    if expired:
        pdf(Path(paper["folder"]) / "main.pdf", [MAIN + "\nNew version rate 2.4."])
        reviewed = workbench.service.library.scan([paper["folder"]])[0]
        assert reviewed["attachment_review_details"]["state"] == "expired"
        assert reviewed["attachment_status"] == "ready"
    plans = install_fake_execution(monkeypatch)
    extract(workbench, reviewed)
    bundle = read(plans[0].parent / "sources.json")
    for document in bundle["documents"]:
        context = document["source"]["metadata"].get("acquisition_context")
        if expired:
            assert context is None
        else:
            assert context["human_validated"] is False
            assert context["scientific_validated"] is False
            assert context["limitations"] == ["not_located_in_current_local_files: supplementary figure image"]
            assert context["review_pointer"] == reviewed["attachment_review"]
    assert workbench.service.jobs.list() == []


def test_development_exposure_is_retained_in_queue_and_real_plan(workbench, monkeypatch):
    paper = add_paper(workbench)
    workbench.service.store.register_cohort(paper["id"], [f["sha256"] for f in paper["files"]], "development")
    # The source cache is intentionally still unassigned; prepare must consult
    # durable cohort history, not trust a stale UI label.
    assert workbench.service.library.get(paper["id"])["cohort_role"] == "unassigned"
    plans = install_fake_execution(monkeypatch)
    output = extract(workbench, paper)
    assert read(plans[0])["cohort_role"] == "development"
    assert workbench.service.records.get(output["record_id"])["record"]["paper"]["dataset_role"] == "development"
    workbench.service.library.scan([paper["folder"]])
    job = workbench.service.queue_extractions([paper["id"]], cohort_role="heldout")[0]
    assert workbench.service.jobs.get(job)["payload"]["cohort_role"] == "development"


@pytest.mark.parametrize("unsupported", [False, True])
def test_auto_publish_requires_admission_and_only_enqueues_separate_write(workbench, monkeypatch, unsupported):
    paper = add_paper(workbench)
    install_fake_execution(monkeypatch, unsupported=unsupported)
    monkeypatch.setattr(workbench.service.records, "publish", lambda *a, **k: pytest.fail("extraction must not synchronously write Neo4j"))
    output = extract(workbench, paper, auto_publish=True)
    jobs = workbench.service.jobs.list()
    assert output["incomplete"] is unsupported
    assert bool(jobs) is (not unsupported)
    if not unsupported:
        assert len(jobs) == 1 and jobs[0]["kind"] == "publish"
        assert jobs[0]["payload"] == {"record_id": output["record_id"]}
        assert output["publish_job_id"] == jobs[0]["id"]
    else:
        assert "publish_job_id" not in output


@pytest.mark.parametrize("unsupported", [False, True])
def test_only_a_current_admissible_replacement_can_clear_source_change(workbench, monkeypatch, unsupported):
    paper = add_paper(workbench)
    workbench.service.records.mark_source_stale(paper["id"], "Sources were replaced before this run")
    install_fake_execution(monkeypatch, unsupported=unsupported)
    output = extract(workbench, paper, auto_publish=True)
    summary = workbench.service.records.get(output["record_id"])["summary"]
    assert bool(summary["source_change"]) is unsupported
    assert summary["admissible"] is (not unsupported)
    assert bool(workbench.service.jobs.list()) is (not unsupported)


def test_each_failed_paper_keeps_its_run_and_does_not_prevent_next_paper(workbench, monkeypatch):
    first = add_paper(workbench, 1)
    second = add_paper(workbench, 2)

    def execute(settings, plan_path, *, client, verify):
        if read(plan_path)["paper_id"] == first["id"]:
            write_new_json(plan_path.parent / "failure.json", {"error": "synthetic provider failure"})
            raise RuntimeError("synthetic provider failure")
        completed_call(plan_path.parent, candidate_from_plan(plan_path))
        return plan_path.parent

    monkeypatch.setattr(workflow, "execute_plan", execute)
    identifiers = workbench.service.queue_extractions([first["id"], second["id"]])
    assert workbench.service.jobs.run_one()
    assert workbench.service.jobs.run_one()
    failed, completed = [workbench.service.jobs.get(i) for i in identifiers]
    assert failed["status"] == "failed" and "synthetic provider failure" in failed["error"]
    assert completed["status"] == "completed"
    run = workbench.settings.data_root / "runs" / failed["payload"]["run_id"]
    assert (run / "plan.json").is_file() and read(run / "failure.json")["error"] == "synthetic provider failure"
    assert all(client.closed for client in workbench.clients)


def test_incomplete_without_candidate_retains_original_result_status(workbench, monkeypatch):
    paper = add_paper(workbench)

    def incomplete(settings, plan_path, *, client, verify):
        write_new_json(plan_path.parent / "result.json", {"status": "candidate_incomplete_budget", "stop_reason": "budget_exhausted"})
        return plan_path.parent

    monkeypatch.setattr(workflow, "execute_plan", incomplete)
    output = extract(workbench, paper, auto_publish=True)
    assert output["incomplete"]
    assert output["status"] == "candidate_incomplete_budget"
    assert output.get("record_id") is None
    assert workbench.service.jobs.list() == []
    assert all(client.closed for client in workbench.clients)


def test_original_replaced_during_extraction_blocks_new_candidate_publication(workbench, monkeypatch):
    paper = add_paper(workbench)

    def replace_original(plan_path):
        pdf(Path(paper["folder"]) / "main.pdf", [MAIN + "\nA replaced source after the snapshot was read."])

    plans = install_fake_execution(monkeypatch, after=replace_original)
    with pytest.raises(ValueError):
        extract(workbench, paper, auto_publish=True)
    summary = workbench.service.records.get(plans[0].parent.name)["summary"]
    assert summary["source_change"]
    assert not summary["admissible"]
    assert workbench.service.jobs.list() == []


def test_batch_scan_keeps_success_and_each_error_and_marks_changed_source(workbench, monkeypatch):
    first = add_paper(workbench, 1)
    second = add_paper(workbench, 2)
    pdf(Path(first["folder"]) / "main.pdf", [MAIN + "\nChanged source content for the first study."])
    real_scan = workbench.service.library.scan

    def scan(folders=None):
        if folders == [second["folder"]]:
            raise ValueError("synthetic attachment failure")
        # Reproduce a partial batch mutation if orchestration invokes the old
        # all-at-once approach: the first inventory changes before the failure.
        if folders == [first["folder"], second["folder"]]:
            real_scan([first["folder"]])
            raise ValueError("synthetic attachment failure")
        return real_scan(folders)

    monkeypatch.setattr(workbench.service.library, "scan", scan)
    output = workbench.service.handle_job({"kind": "scan", "payload": {"folders": [first["folder"], second["folder"]]}}, lambda *a, **k: None)
    assert output["incomplete"]
    assert output["papers"] == 1
    assert len(output["errors"]) == 1
    assert output["errors"][0]["folder"] == second["folder"]
    assert "synthetic attachment failure" in output["errors"][0]["error"]
    with workbench.service.records._db() as db:
        assert db.execute("SELECT reason FROM source_changes WHERE paper_id=?", (first["id"],)).fetchone()


def test_current_version_replaces_old_question_scope_and_queued_publication(workbench, monkeypatch):
    from watertreatment import graph_store

    paper = add_paper(workbench)
    install_fake_execution(monkeypatch)
    first = extract(workbench, paper)
    queued = workbench.service.jobs.enqueue("publish", {"record_id": first["record_id"]})
    second = extract(workbench, paper)
    assert second["record_id"] != first["record_id"]
    with pytest.raises(ValueError, match="当前版本"):
        workbench.service.question_scope("selected", [first["record_id"]])
    details, graph = workbench.service.question_scope("selected", [second["record_id"], second["record_id"]])
    assert graph is None and len(details) == 1 and details[0]["id"] == second["record_id"]
    monkeypatch.setattr(graph_store, "load_graph", lambda *a, **k: pytest.fail("old queued publication must be blocked before DB access"))
    assert workbench.service.jobs.run_one()
    assert workbench.service.jobs.get(queued)["status"] == "failed"
    assert workbench.service.records.publication_scope() == []


def test_publish_job_is_explicit_and_failure_retains_previous_active_snapshot(workbench, monkeypatch):
    from watertreatment import graph_store

    paper = add_paper(workbench)
    install_fake_execution(monkeypatch)
    first = extract(workbench, paper)
    monkeypatch.setattr(graph_store, "load_graph", fake_loader)
    result = workbench.service.handle_job({"kind": "publish", "payload": {"record_id": first["record_id"]}}, lambda *a, **k: None)
    assert result["persistent_graph_write"]
    active = workbench.service.records.publication_scope()
    assert active[0]["record_id"] == first["record_id"]
    second = extract(workbench, paper)

    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic database unavailable")

    monkeypatch.setattr(graph_store, "load_graph", unavailable)
    with pytest.raises(RuntimeError, match="synthetic database unavailable"):
        workbench.service.handle_job({"kind": "publish", "payload": {"record_id": second["record_id"]}}, lambda *a, **k: None)
    after = workbench.service.records.publication_scope()
    assert after[0]["record_id"] == first["record_id"] and after[0]["stale"]
    assert workbench.service.records.get(second["record_id"])["summary"]["publish_status"] == "failed"
    monkeypatch.setattr(workbench.service, "_graph_context", lambda *a: pytest.fail("stale publication must not enter ordinary graph questions"))
    with pytest.raises(ValueError, match="待更新"):
        workbench.service.question_scope("published", [])
