"""Offline workbench tests: scientific versions, scoped review and publication.

Synthetic call artifacts exercise protocol validation, never model accuracy.
"""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from watertreatment.domain import ExtractionRecord
from watertreatment.reading import AUDIT_DIMENSIONS
from watertreatment.settings import Settings
from watertreatment.storage import DataStore, digest_json, hash_file, write_new_json
from watertreatment.web_records import ReviewConflict, WebRecords


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def completed_call(run, record, *, changed=False, unsupported=False):
    audit = {"findings": [], "potential_omissions": [],
             "coverage_limitations": ["Synthetic fixture; scientific accuracy is not established"],
             "checked_dimensions": list(AUDIT_DIMENSIONS), "human_validated": False}
    if unsupported:
        audit["findings"].append({"record_path": "observations[0].value", "severity": "major",
            "verdict": "unsupported", "needs_revision": True, "explanation": "synthetic conflict", "evidence": []})
    write_new_json(run / "candidate.json", record.model_dump(mode="json"))
    write_new_json(run / "model_review.json", audit)
    plan = read(run / "plan.json")
    model = plan.get("model_settings", {}).get("model") or plan.get("model") or "gpt-6.1-sol"
    complete = {"status": "schema_valid_candidate", "server_selected_model": model, "server_selected_effort": "max"}
    request = {"model": model, "reasoning_effort": "max", "stage": "audit_1", "prompt": "Inspect this exact candidate: " + record.model_dump_json(exclude_defaults=True),
               "sources": read(run / "plan.json")["input_paths"]}
    write_new_json(run / "calls" / "audit" / "request.json", {"request": request, "fingerprint": digest_json(request)})
    write_new_json(run / "calls" / "audit" / "result.json", complete)
    write_new_json(run / "calls" / "audit" / "answer.json", audit)
    if changed:
        request = {"model": model, "reasoning_effort": "max", "stage": "revision_1", "prompt": "Synthetic bounded source-based revision"}
        write_new_json(run / "calls" / "revision" / "request.json", {"request": request, "fingerprint": digest_json(request)})
        write_new_json(run / "calls" / "revision" / "result.json", complete)
        write_new_json(run / "calls" / "revision" / "record.json", record.model_dump(mode="json"))
    result = {"status": "candidate_audited_with_limitations", "stop_reason": "audit_completed",
              "validation_error_count": 0, "reading_check_issues": [], "human_validated": False,
              "history": [{"candidate_sha256": hash_file(run / "candidate.json")}]}
    write_new_json(run / "result.json", result)


@pytest.fixture
def fixture(tmp_path):
    settings = Settings(project_root=tmp_path / "code", data_root=tmp_path / "data")
    store = DataStore(settings.data_root)
    store.initialize()
    source = tmp_path / "synthetic-source.txt"
    source.write_text("Synthetic final material: rate 2.4 and conversion 80. Measurement units kg m-2 h-1 and percent.", encoding="utf-8")
    original = store.import_file(source)
    run = store.new_run("extraction", {"paper_id": "synthetic-paper"})
    bundle = {"documents": [{"source": {"source_id": "main", "role": "main_article",
        "path": original["stored_path"], "sha256": original["sha256"], "size_bytes": original["size_bytes"],
        "format": "txt"}, "blocks": [{"block_id": "paragraph", "source_id": "main", "kind": "paragraph",
            "order": 1, "text": source.read_text(encoding="utf-8")}]}]}
    write_new_json(run / "sources.json", bundle)
    plan = {"run_id": run.name, "paper_id": "synthetic-paper", "cohort_role": "unassigned", "rule_version": "0.5",
            "model_settings": {"model": "gpt-6.1-sol", "reasoning_effort": "max"},
            "input_paths": [{"path": str(p), "sha256": hash_file(p)} for p in [run / "sources.json", Path(original["stored_path"])]]}
    write_new_json(run / "plan.json", plan)
    store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    record = ExtractionRecord.model_validate({
        "paper": {"id": "synthetic-paper", "source_ids": ["main"],
                  "title": {"value": "Synthetic dual-function record", "status": "reported"}},
        "materials": [{"id": "final", "role": "primary_final"}],
        "experiments": [{"id": "joint", "material_ids": ["final"], "purpose": "coupled"},
                        {"id": "separate", "material_ids": ["final"], "purpose": "evaporation"}],
        "observations": [{"id": "rate", "experiment_id": "joint", "material_id": "final",
            "metric_kind": "evaporation_rate", "value": {"value": 2.4, "status": "reported", "evidence_ids": ["source-value"]},
            "unit": {"value": "kg m-2 h-1", "status": "reported", "evidence_ids": ["source-value"]}},
            {"id": "conversion", "experiment_id": "joint", "material_id": "final", "metric_kind": "degradation_efficiency",
             "value": {"status": "not_reported"}}],
        "evidence": [{"id": "source-value", "source_id": "main", "block_id": "paragraph", "quote": "rate 2.4"}],
        "coverage": [{"source_id": "main", "block_ids": ["paragraph"], "state": "read_structured", "text_read": True}],
    })
    completed_call(run, record)
    records = WebRecords(settings)
    records.refresh()
    return SimpleNamespace(settings=settings, store=store, run=run, record=record, records=records, source=source)


def entry(*, action="confirm", scope=None, patch=None, note="", observation_id="rate"):
    return {"observation_id": observation_id, "action": action, "scope": scope or ["value"],
            "patch": patch or {}, "note": note, "evidence_ids": ["source-value"]}


def submit(fixture, entries, identifier=None):
    identifier = identifier or fixture.run.name
    return fixture.records.submit_review(identifier, "reviewer", entries,
        fixture.records.get(identifier)["summary"]["record_hash"])


def fake_loader(settings, graph, *, source_record, live):
    assert live and source_record.is_file()
    return {"status": "loaded_as_candidate", "persistent_graph_write": True,
            "snapshot_id": digest_json(graph.model_dump(mode="json"))}


def reaudit(fixture, identifier, *, mutate=None, unsupported=False):
    detail = fixture.records.get(identifier)
    run = fixture.store.new_run("web_reaudit", {"record_id": identifier, "record_hash": detail["summary"]["record_hash"],
        "parent_run": detail["summary"]["run_id"], "source_bundle_sha256": hash_file(Path(detail["sources_bundle"])),
        "model": "gpt-6.1-sol", "effort": "max"})
    shutil.copyfile(detail["sources_bundle"], run / "sources.json")
    write_new_json(run / "input_candidate.json", detail["record"])
    plan = {"run_id": run.name, "record_id": identifier, "record_hash": detail["summary"]["record_hash"],
            "input_paths": [{"path": str(p), "sha256": hash_file(p)} for p in [run / "sources.json", run / "input_candidate.json"]]}
    write_new_json(run / "plan.json", plan)
    fixture.store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    candidate = copy.deepcopy(detail["record"])
    if mutate:
        mutate(candidate)
    completed_call(run, ExtractionRecord.model_validate(candidate), changed=bool(mutate), unsupported=unsupported)
    return run


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("previous_version", [None, 2, 3, 4, 5])
def test_refresh_rechecks_cached_unverified_local_receipt_only_on_identical_files(fixture, monkeypatch, changed, previous_version):
    service, run = fixture.records, fixture.run
    result = read(run / "result.json")
    result["review_mode"] = "retained-issue-source-review-v1"
    if previous_version is not None:
        result["source_review_final_corrections"] = []
    (run / "result.json").write_text(json.dumps(result), encoding="utf8")
    with service._db() as db:
        meta = json.loads(db.execute("SELECT metadata FROM records WHERE id=?", (run.name,)).fetchone()[0])
        old_assessment = {**meta["assessment"], "admissible":False, "reasons":["audit_provenance_unverified"]}
        if previous_version is not None:
            meta["receipt_assessment_version"] = previous_version
        meta.update(assessment=old_assessment, result=result, result_hash=hash_file(run/"result.json"))
        db.execute("UPDATE records SET metadata=? WHERE id=?", (json.dumps(meta), run.name))
    if changed:
        with (run/"candidate.json").open("ab") as stream:
            stream.write(b" ")
    monkeypatch.setattr(service, "_audit_provenance", lambda *args: True)
    service.refresh()
    with service._db() as db:
        revised = json.loads(db.execute("SELECT metadata FROM records WHERE id=?", (run.name,)).fetchone()[0])
    assert revised["assessment"]["admissible"] is (not changed)
    assert revised.get("previous_assessments", []) == ([] if changed else [old_assessment])
    if previous_version is not None and not changed:
        assert revised["receipt_assessment_version"] > previous_version


def test_completed_with_limits_can_publish_without_whole_paper_human_review(fixture):
    service = fixture.records
    detail = service.get(fixture.run.name)
    assert detail["summary"]["admissible"] and detail["summary"]["human_validated"] is False
    assert detail["record"]["observations"][1]["value"]["status"] == "not_reported"
    assert detail["record_path"] == str(fixture.run / "candidate.json")
    with pytest.raises(ValueError, match="explicit live"):
        service.publish(fixture.run.name, loader=fake_loader)
    result = service.publish(fixture.run.name, live=True, loader=fake_loader)
    assert service.publication_scope()[0]["snapshot_id"] == result["snapshot_id"]
    assert service.current_records(published_only=True)[0]["id"] == fixture.run.name
    assert service.publish(fixture.run.name, live=True, loader=lambda *a, **k: pytest.fail("duplicate load"))["status"] == "already_published"


def test_draft_is_not_a_review_and_confirm_is_exact_scope_only(fixture):
    service, identifier = fixture.records, fixture.run.name
    original = hash_file(fixture.run / "candidate.json")
    service.save_draft(identifier, "reviewer", [entry()], original)
    assert service.get_draft(identifier, "reviewer")["base_hash"] == original
    assert service.get(identifier)["reviews"] == []
    outcome = submit(fixture, [entry(scope=["value"])])
    assert outcome["record_id"] == identifier and not outcome["changed"]
    detail = service.get(identifier)
    assert detail["reviews"][0]["payload"]["entries"][0]["scope"] == ["value"]
    assert detail["summary"]["human_validated"] is False and service.get_draft(identifier, "reviewer") is None
    assert hash_file(fixture.run / "candidate.json") == original


def test_correction_is_immutable_and_publish_failure_preserves_previous_pointer(fixture):
    service, identifier = fixture.records, fixture.run.name
    before = hash_file(fixture.run / "candidate.json")
    old = service.publish(identifier, live=True, loader=fake_loader)
    corrected = submit(fixture, [entry(action="correct", patch={"value": {"value": 2.5, "status": "reported"}}, note="Verified source figure")])
    new_id = corrected["record_id"]
    assert new_id != identifier and corrected["summary"]["admissible"]
    assert hash_file(fixture.run / "candidate.json") == before
    assert service.get(new_id)["record"]["observations"][0]["value"]["evidence_ids"] == ["source-value"]
    def failed(*args, **kwargs):
        raise RuntimeError("synthetic unavailable database")
    with pytest.raises(RuntimeError):
        service.publish(new_id, live=True, loader=failed)
    assert service.publication_scope()[0]["snapshot_id"] == old["snapshot_id"]
    assert service.publication_scope()[0]["stale"]
    assert service.get(new_id)["summary"]["publish_status"] == "failed"
    service.publish(new_id, live=True, loader=fake_loader)
    assert len(service.publication_scope()) == 1 and not service.publication_scope()[0]["stale"]
    with pytest.raises(ReviewConflict):
        service.submit_review(identifier, "reviewer", [entry()], before)


@pytest.mark.parametrize("bad", [
    entry(action="correct", scope=["value"], patch={"material_id": "final"}, note="outside scope"),
    entry(action="correct", patch={"value": {"value": 0, "status": "reported"}}, observation_id="conversion", note="missing is not zero"),
    entry(action="correct", patch={"value": {"value": 8, "status": "not_reported"}}, note="invalid state"),
    entry(action="correct", scope=["experiment_id"], patch={"experiment_id": "invented"}, note="invalid binding"),
    entry(action="conflict", patch={"value": {"status": "conflict", "alternatives": []}}, note="must preserve alternatives"),
])
def test_invalid_review_cannot_mutate_registered_facts(fixture, bad):
    original = hash_file(fixture.run / "candidate.json")
    with pytest.raises(ValueError):
        submit(fixture, [bad])
    assert len(fixture.records.list_records()) == 1
    assert hash_file(fixture.run / "candidate.json") == original


def test_not_reported_and_uncertain_remain_distinct(fixture):
    uncertain = submit(fixture, [entry(action="uncertain", note="Figure cannot be read at this scale")])
    assert not uncertain["changed"]
    assert uncertain["needs_reaudit"]
    assert fixture.records.get(uncertain["record_id"])["record"]["observations"][0]["value"]["status"] == "reported"
    removed = submit(fixture, [entry(action="not_reported", note="Full text does not provide a quantified value")], uncertain["record_id"])
    fact = fixture.records.get(removed["record_id"])["record"]["observations"][0]["value"]
    assert fact["value"] is None and fact["status"] == "not_reported"


def test_binding_change_invalidates_same_experiment_reviews_and_requires_reaudit(fixture):
    submit(fixture, [entry(), entry(observation_id="conversion")])
    changed = submit(fixture, [entry(action="correct", scope=["experiment_id"], patch={"experiment_id": "separate"}, note="Correct source experiment")])
    detail = fixture.records.get(changed["record_id"])
    assert changed["needs_reaudit"] and not detail["summary"]["admissible"]
    assert [e["observation_id"] for r in detail["reviews"] for e in r["payload"]["entries"]] == ["rate"]
    with pytest.raises(ValueError, match="completed result"):
        fixture.records.publish(changed["record_id"], live=True, loader=fake_loader)
    fresh = reaudit(fixture, changed["record_id"])
    accepted = fixture.records.register_reaudit(changed["record_id"], fresh.name)
    assert accepted["summary"]["admissible"] and accepted["record_id"] != changed["record_id"]
    assert accepted["summary"]["human_validated"] is False


def test_model_revision_may_not_silently_override_human_field_judgments(fixture):
    submit(fixture, [entry()])
    run = reaudit(fixture, fixture.run.name, mutate=lambda r: r["observations"][0]["value"].update(value=8))
    result = fixture.records.accept_model_revision(fixture.run.name, run.name)
    assert result["status"] == "pending_human_conflict" and not result["accepted"]
    assert result["conflicts"][0]["scope"] == ["value"]
    assert fixture.records.get(fixture.run.name)["summary"]["current"]
    assert len(fixture.records.list_records()) == 1


def test_model_revision_without_conflict_requires_delivered_candidate_and_fresh_audit(fixture):
    run = reaudit(fixture, fixture.run.name, mutate=lambda r: r["observations"][0]["value"].update(value=8))
    with pytest.raises(ValueError, match="audit-only"):
        fixture.records.register_reaudit(fixture.run.name, run.name)
    result = fixture.records.accept_model_revision(fixture.run.name, run.name)
    assert result["accepted"] and result["summary"]["admissible"]
    assert fixture.records.get(result["record_id"])["record"]["observations"][0]["value"]["value"] == 8


def test_unsupported_final_audit_cannot_be_registered(fixture):
    run = reaudit(fixture, fixture.run.name, unsupported=True)
    with pytest.raises(ValueError, match="unresolved revisions"):
        fixture.records.register_reaudit(fixture.run.name, run.name)
    assert len(fixture.records.list_records()) == 1


def test_source_change_is_local_admission_and_query_scope_constraint(fixture):
    fixture.records.publish(fixture.run.name, live=True, loader=fake_loader)
    fixture.records.mark_source_stale("synthetic-paper", "New supplementary information added")
    assert not fixture.records.get(fixture.run.name)["summary"]["admissible"]
    assert fixture.records.publication_scope()[0]["stale"]
    with pytest.raises(ValueError, match="source_changed"):
        fixture.records.publish(fixture.run.name, live=True, loader=fake_loader)


@pytest.mark.parametrize("path", ["candidate.json", "result.json", "model_review.json", "sources.json", "plan.json"])
def test_tampering_after_registration_cannot_be_published(fixture, path):
    target = fixture.run / path
    content = read(target)
    content["tampered"] = True
    target.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ValueError):
        fixture.records.publish(fixture.run.name, live=True, loader=lambda *a, **k: pytest.fail("unsafe graph load"))


def test_unregistered_candidate_is_never_discovered(fixture):
    fake = fixture.settings.data_root / "runs" / "unregistered"
    fake.mkdir()
    shutil.copyfile(fixture.run / "candidate.json", fake / "candidate.json")
    shutil.copyfile(fixture.run / "result.json", fake / "result.json")
    assert len(fixture.records.refresh()) == 1
    with pytest.raises(KeyError):
        fixture.records.get("unregistered")


def upgraded_reaudit(fixture, identifier, *, scientific_updates=None, manifest_updates=None, mutate=None):
    """Synthetic completed upgrade; the source plan and original candidate stay frozen."""
    detail = fixture.records.get(identifier)
    scientific = {"paper_id": "synthetic-paper", "cohort_role": "unassigned",
                  "rule_version": "0.6", "schema_version": "0.2.0"}
    scientific.update(scientific_updates or {})
    manifest = {"record_id": identifier, "record_hash": detail["summary"]["record_hash"],
        "parent_run": detail["summary"]["run_id"], "source_bundle_sha256": hash_file(Path(detail["sources_bundle"])),
        "model": "gpt-6.1-sol", "effort": "max", "target_rule_version": "0.6", "target_schema_version": "0.2.0"}
    manifest.update(manifest_updates or {})
    run = fixture.store.new_run("web_reaudit", manifest)
    shutil.copyfile(detail["sources_bundle"], run / "sources.json")
    write_new_json(run / "input_candidate.json", detail["record"])
    plan = {"run_id": run.name, "record_id": identifier, "record_hash": detail["summary"]["record_hash"],
        "scientific_plan": scientific,
        "input_paths": [{"path": str(p), "sha256": hash_file(p)} for p in [run / "sources.json", run / "input_candidate.json"]]}
    write_new_json(run / "plan.json", plan)
    fixture.store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    candidate = copy.deepcopy(detail["record"])
    candidate.update(rule_version="0.6", schema_version="0.2.0")
    if mutate:
        mutate(candidate)
    completed_call(run, ExtractionRecord.model_validate(candidate), changed=True)
    return run


def test_rule_upgrade_preserves_old_artifacts_and_survives_review_and_publication(fixture):
    old_plan = (fixture.run / "plan.json").read_bytes()
    old_candidate = (fixture.run / "candidate.json").read_bytes()
    run = upgraded_reaudit(fixture, fixture.run.name)
    accepted = fixture.records.accept_model_revision(fixture.run.name, run.name)
    detail = fixture.records.get(accepted["record_id"])
    assert detail["record"]["rule_version"] == "0.6"
    assert detail["record"]["schema_version"] == "0.2.0"
    assert detail["summary"]["run_id"] == fixture.run.name
    assert detail["summary"]["admissible"] and not detail["summary"]["human_validated"]
    assert read(run / "input_candidate.json")["schema_version"] == "0.1.0"
    assert (fixture.run / "plan.json").read_bytes() == old_plan
    assert (fixture.run / "candidate.json").read_bytes() == old_candidate
    assert fixture.records.get(fixture.run.name)["record"]["schema_version"] == "0.1.0"
    fixture.records.publish(accepted["record_id"], live=True, loader=fake_loader)
    revised = submit(fixture, [entry(action="correct", patch={"value": {"value": 2.5, "status": "reported"}},
                                    note="Scoped synthetic correction")], accepted["record_id"])
    human = fixture.records.get(revised["record_id"])
    assert human["record"]["rule_version"] == "0.6" and human["summary"]["admissible"]
    fixture.records.publish(revised["record_id"], live=True, loader=fake_loader)


@pytest.mark.parametrize("scientific_updates,manifest_updates", [
    ({"paper_id": "different-paper"}, {}),
    ({"cohort_role": "heldout"}, {}),
    ({"rule_version": "0.7"}, {}),
    ({"schema_version": "0.3.0"}, {}),
    ({"extra_authority": "invented"}, {}),
    ({}, {"target_rule_version": "0.5"}),
    ({}, {"target_schema_version": None}),
])
def test_upgrade_cannot_change_identity_or_disagree_with_target_manifest(fixture, scientific_updates, manifest_updates):
    run = upgraded_reaudit(fixture, fixture.run.name, scientific_updates=scientific_updates,
                           manifest_updates=manifest_updates)
    with pytest.raises(ValueError, match="scientific_plan"):
        fixture.records.accept_model_revision(fixture.run.name, run.name)
    assert fixture.records.get(fixture.run.name)["summary"]["current"]


@pytest.mark.parametrize("tamper", ["metadata_version", "metadata_removed", "audit_run", "frozen_plan", "manifest"])
def test_registered_upgrade_plan_cannot_be_replaced_in_metadata_or_sources(fixture, tamper):
    run = upgraded_reaudit(fixture, fixture.run.name)
    identifier = fixture.records.accept_model_revision(fixture.run.name, run.name)["record_id"]
    row = fixture.records._row(identifier)
    if tamper == "frozen_plan":
        plan = read(run / "plan.json")
        plan["scientific_plan"]["rule_version"] = "0.7"
        (run / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    elif tamper == "manifest":
        manifest = read(run / "manifest.json")
        manifest["target_rule_version"] = "0.7"
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    else:
        if tamper == "metadata_version":
            row["metadata"]["scientific_plan"]["schema_version"] = "0.1.0"
        elif tamper == "metadata_removed":
            row["metadata"].pop("scientific_plan")
        else:
            row["metadata"]["audit_run_id"] = "unregistered-audit"
        with fixture.records._db() as db:
            db.execute("UPDATE records SET metadata=? WHERE id=?", (json.dumps(row["metadata"]), identifier))
    with pytest.raises(ValueError):
        fixture.records.get(identifier)
    with pytest.raises(ValueError):
        fixture.records.publish(identifier, live=True, loader=lambda *a, **k: pytest.fail("unbound upgrade publication"))


@pytest.mark.parametrize("upgraded", [False, True])
def test_human_plot_value_correction_keeps_estimate_provenance(fixture, upgraded):
    def estimate(record):
        record["observations"][0]["origin"] = "plot_estimate"
        record["observations"][0]["value"]["status"] = "estimated"
    run = (upgraded_reaudit if upgraded else reaudit)(fixture, fixture.run.name, mutate=estimate)
    identifier = fixture.records.accept_model_revision(fixture.run.name, run.name)["record_id"]
    with pytest.raises(ValueError, match="plot estimate must remain estimated"):
        submit(fixture, [entry(action="correct", patch={"value": {"value": 2.5, "status": "reported"}},
                              note="Visual estimate is not an author-printed number")], identifier)
    result = submit(fixture, [entry(action="correct", patch={"value": {"value": 2.5, "status": "estimated"}},
                                  note="Read approximate height of synthetic graph")], identifier)
    observation = fixture.records.get(result["record_id"])["record"]["observations"][0]
    assert observation["origin"] == "plot_estimate"
    assert observation["value"]["status"] == "estimated"
    assert observation["value"]["evidence_ids"] == ["source-value"]


def test_target_versions_without_frozen_scientific_plan_are_rejected(fixture):
    run = reaudit(fixture, fixture.run.name)
    manifest = read(run / "manifest.json")
    manifest.update(target_rule_version="0.6", target_schema_version="0.2.0")
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen scientific_plan"):
        fixture.records.register_reaudit(fixture.run.name, run.name)
