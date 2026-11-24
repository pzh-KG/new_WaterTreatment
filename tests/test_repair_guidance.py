"""Offline recovery protocol: old source audits guide repair, never approve it."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_web_model import ReplayClient, audit, model_fixture
from test_web_records import fixture, read
from watertreatment.domain import ExtractionRecord
from watertreatment.reading import ReadingAudit
from watertreatment.storage import digest_json, hash_file, write_new_json
from watertreatment.web_model import _repair_guidance, reaudit


def rewrite(path, value):
    """Mutate synthetic test artifacts only to exercise integrity failures."""
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def timeout(model, prompt):
    raise TimeoutError("Synthetic revision timeout; no model provider was called")


@pytest.fixture
def failed_repair(model_fixture):
    original_hash = hash_file(model_fixture.run / "candidate.json")
    original_records = len(model_fixture.records.list_records())
    replay = ReplayClient([audit(revision=True), timeout])
    with pytest.raises(TimeoutError, match="Synthetic revision timeout"):
        reaudit(model_fixture.settings, model_fixture.records, model_fixture.run.name,
                "Check the synthetic source value and repair its binding", client_factory=replay.factory)
    assert replay.closed and replay.calls_started == 2
    assert replay.calls[0]["stage"] == "audit_web_0"
    row = next(row for row in model_fixture.store.runs() if row["kind"] == "web_reaudit")
    prior = Path(row["manifest_path"]).parent
    assert read(prior / "audit.v00.json")["findings"][0]["needs_revision"]
    assert (prior / "failure.json").is_file()
    assert not (prior / "result.json").exists()
    assert hash_file(model_fixture.run / "candidate.json") == original_hash
    assert len(model_fixture.records.list_records()) == original_records
    return SimpleNamespace(f=model_fixture, prior=prior, call=prior / "calls" / "call-01",
        detail=model_fixture.records.get(model_fixture.run.name), original_hash=original_hash,
        original_records=original_records)


def changed_candidate(case):
    candidate = case.f.record.model_copy(deep=True)
    candidate.observations[0].value.value = 2.5
    return candidate


def test_failed_revision_retains_a_reusable_completed_source_audit(failed_repair):
    case = failed_repair
    guidance, paths = _repair_guidance(case.f.settings, case.f.records, case.detail, case.prior.name)
    assert guidance == audit(revision=True)
    assert len(paths) == 6 and all(path.is_file() for path in paths)
    assert case.prior / "audit.v00.json" in paths
    assert case.call / "request.json" in paths
    assert case.call / "result.json" in paths
    assert case.call / "answer.json" in paths
    assert case.f.records.get(case.f.run.name)["summary"]["current"]


def test_reused_audit_only_guides_revision_then_fresh_audit_registers_new_version(failed_repair):
    case = failed_repair
    previous_audit_hash = hash_file(case.prior / "audit.v00.json")
    replay = ReplayClient([changed_candidate(case), audit()])
    result = reaudit(case.f.settings, case.f.records, case.f.run.name, "Repair using the preserved source audit",
        client_factory=replay.factory, repair_audit_run_id=case.prior.name)
    assert replay.closed and replay.calls_started == 2
    assert [item["model"] for item in replay.calls] == [ExtractionRecord, ReadingAudit]
    assert [item["stage"] for item in replay.calls] == ["revision_web_0", "audit_web_1"]
    assert replay.calls[0]["conversation_key"] != replay.calls[1]["conversation_key"]
    assert "Synthetic recheck requested" in replay.calls[0]["prompt"]
    assert result["revision"]["accepted"] and result["revision"]["changed"]
    assert result["result"]["model_calls"] == 2 and result["result"]["revisions"] == 1
    history = result["result"]["history"]
    assert history[0]["audit_origin"] == "reused_repair_guidance" and history[0]["needs_revision"]
    assert history[1]["audit_origin"] == "fresh_model_audit" and not history[1]["needs_revision"]
    resumed = case.f.settings.data_root / "runs" / result["run_id"]
    plan = read(resumed / "plan.json")
    assert plan["repair_audit_run_id"] == case.prior.name
    copies = list((resumed / "repair_guidance").iterdir())
    assert len(copies) == 6
    frozen = {Path(item["path"]): item["sha256"] for item in plan["input_paths"]}
    assert all(frozen.get(path) == hash_file(path) for path in copies)
    assert hash_file(case.prior / "audit.v00.json") == previous_audit_hash
    assert hash_file(case.f.run / "candidate.json") == case.original_hash
    assert result["revision"]["record_id"] != case.f.run.name
    assert case.f.records.get(result["revision"]["record_id"])["summary"]["current"]


def test_repair_without_completed_fresh_audit_cannot_register_a_candidate(failed_repair):
    case = failed_repair
    replay = ReplayClient([changed_candidate(case), timeout])
    with pytest.raises(TimeoutError):
        reaudit(case.f.settings, case.f.records, case.f.run.name, "Repair then independently audit",
            client_factory=replay.factory, repair_audit_run_id=case.prior.name)
    assert replay.closed and replay.calls_started == 2
    assert replay.calls[0]["stage"] == "revision_web_0"
    assert len(case.f.records.list_records()) == case.original_records
    assert case.f.records.get(case.f.run.name)["summary"]["current"]
    resumed_row = next(row for row in case.f.store.runs()
                       if row["kind"] == "web_reaudit" and row["run_id"] != case.prior.name)
    resumed = Path(resumed_row["manifest_path"]).parent
    assert (resumed / "failure.json").is_file() and (resumed / "candidate.v01.json").is_file()
    assert not (resumed / "model_review.json").exists()


@pytest.mark.parametrize("mismatch", ["record_id", "record_hash", "sources_bundle", "candidate"])
def test_guidance_is_bound_to_the_current_record_and_source_version(failed_repair, mismatch):
    case = failed_repair
    detail = copy.deepcopy(case.detail)
    if mismatch == "record_id":
        detail["id"] = "other-record"
    elif mismatch == "record_hash":
        detail["summary"]["record_hash"] = "0" * 64
    elif mismatch == "sources_bundle":
        other = case.f.settings.data_root / "different-source-bundle.json"
        write_new_json(other, {**read(Path(detail["sources_bundle"])), "different_version": True})
        detail["sources_bundle"] = str(other)
    else:
        detail["record"]["observations"][0]["value"]["value"] = 99
    with pytest.raises(ValueError, match="another record/source version|differs from the current record"):
        _repair_guidance(case.f.settings, case.f.records, detail, case.prior.name)


@pytest.mark.parametrize("mismatch", ["fingerprint", "uncompleted", "model", "effort", "answer",
    "dimensions", "stage", "source_binding", "missing_result"])
def test_repair_guidance_rejects_unverified_or_unmatched_audit_protocol(failed_repair, mismatch):
    case = failed_repair
    if mismatch in {"fingerprint", "stage", "source_binding"}:
        envelope = read(case.call / "request.json")
        if mismatch == "fingerprint":
            envelope["fingerprint"] = "0" * 64
        elif mismatch == "stage":
            envelope["request"]["stage"] = "revision_web_0"
            envelope["fingerprint"] = digest_json(envelope["request"])
        else:
            envelope["request"]["sources"] = []
            envelope["fingerprint"] = digest_json(envelope["request"])
        rewrite(case.call / "request.json", envelope)
    elif mismatch in {"uncompleted", "model", "effort"}:
        completed = read(case.call / "result.json")
        key, value = {"uncompleted": ("status", "incomplete"), "model": ("server_selected_model", "other-model"),
                      "effort": ("server_selected_effort", "low")}[mismatch]
        completed[key] = value
        rewrite(case.call / "result.json", completed)
    elif mismatch == "answer":
        rewrite(case.call / "answer.json", audit().model_dump(mode="json"))
    elif mismatch == "dimensions":
        incomplete = read(case.prior / "audit.v00.json")
        incomplete["checked_dimensions"] = incomplete["checked_dimensions"][:1]
        rewrite(case.prior / "audit.v00.json", incomplete)
    else:
        (case.call / "result.json").rename(case.call / "retained-incomplete-result.json")
    with pytest.raises(ValueError, match="matching completed Sol MAX audit|required audit dimensions"):
        _repair_guidance(case.f.settings, case.f.records, case.detail, case.prior.name)


@pytest.mark.parametrize("changed", ["plan.json", "input_candidate.json", "sources.json"])
def test_changed_frozen_plan_or_input_is_rejected_before_provider_factory(failed_repair, changed):
    case = failed_repair
    target = case.prior / changed
    # Even semantically equivalent bytes must not silently replace frozen input.
    target.write_bytes(target.read_bytes() + b"\n ")
    runs_before = len(case.f.store.runs())
    with pytest.raises(ValueError, match="changed|frozen plan"):
        reaudit(case.f.settings, case.f.records, case.f.run.name, "This invalid guidance must stop before model use",
            client_factory=lambda *a, **k: pytest.fail("provider factory was reached"),
            repair_audit_run_id=case.prior.name)
    assert len(case.f.store.runs()) == runs_before


def test_guidance_must_be_a_registered_reaudit_not_an_extraction_or_unregistered_folder(failed_repair):
    case = failed_repair
    for identifier in (case.f.run.name, "unregistered-repair"):
        with pytest.raises(ValueError, match="registered re-audit"):
            _repair_guidance(case.f.settings, case.f.records, case.detail, identifier)


def test_frozen_guidance_copy_cannot_change_during_repair_and_then_be_registered(failed_repair):
    case = failed_repair
    def change_copy(run):
        target = next((run / "repair_guidance").glob("*-audit.v00.json"))
        target.write_bytes(target.read_bytes() + b"\n ")
    replay = ReplayClient([changed_candidate(case), audit()], change_after=change_copy)
    with pytest.raises(ValueError, match="冻结输入发生变化"):
        reaudit(case.f.settings, case.f.records, case.f.run.name, "Check frozen guidance integrity",
            client_factory=replay.factory, repair_audit_run_id=case.prior.name)
    assert replay.closed and len(case.f.records.list_records()) == case.original_records
    assert case.f.records.get(case.f.run.name)["summary"]["current"]
