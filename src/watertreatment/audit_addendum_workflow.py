"""Authenticated, one-call source addenda; never a new extraction or full audit."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from .model_policy import plan_model, require_active_model
from .audit_addendum import (AuditAddendum, POLICY, audit_addendum_issues,
    audit_addendum_prompt, build_audit_addendum_scope, merge_audit_addendum)
from .domain import ExtractionRecord
from .reading import ReadingAudit, ReadingDossier
from .storage import DataStore, digest_json, hash_file, write_new_json


@dataclass(frozen=True)
class AddendumParent:
    run: Path
    plan: dict
    bundle: dict
    documents: list
    record: ExtractionRecord
    dossier: ReadingDossier
    baseline_record: ExtractionRecord
    baseline_audit: ReadingAudit
    audit: ReadingAudit
    additional_audits: list
    full_scope: dict
    scope: dict
    receipt: dict
    remaining_seconds: float


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _completed_call_protocol(run, completed, answer):
    """Inspect public completion/tool envelopes only, never model reasoning."""
    path = Path(run) / "app_server" / "events.jsonl"
    before = hash_file(path)
    key = completed["thread_id"], completed["turn_id"]
    starts, ends, answers, mutations = [], [], [], []
    with path.open(encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            event = json.loads(line)
            if event.get("direction") != "receive":
                continue
            message = event.get("message", {})
            params = message.get("params", {})
            method = message.get("method")
            if method in {"turn/started", "turn/completed"}:
                turn = params.get("turn", {})
                if (params.get("threadId"), turn.get("id")) == key:
                    if method == "turn/started":
                        starts.append(index)
                    else:
                        ends.append((index, turn.get("status")))
            elif (params.get("threadId"), params.get("turnId")) == key:
                if method == "item/tool/call" and params.get("tool") in {"wt_patch_record", "wt_submit_record"}:
                    mutations.append(index)
                elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                    item = params["item"]
                    answers.append((index, item.get("phase"), item.get("text")))
    finals = [x for x in answers if x[1] == "final_answer"] or answers[-1:]
    if (hash_file(path) != before or len(starts) != 1 or len(ends) != 1
            or ends[0][1] != "completed" or not starts[0] < ends[0][0]
            or mutations or not finals or not starts[0] < finals[-1][0] < ends[0][0]
            or json.loads(finals[-1][2]) != answer):
        raise ValueError("source audit is not an unchanged, read-only, completed public model delivery")
    return before


def _call_auth(records, run, plan, call_path, record, model, expected, scope, stage):
    from .source_review import verify_source_review
    from .staged_source_resume import _call
    request, completed, sources, result_path = _call(records, run, call_path / "request.json", plan,
        stage=stage, status="schema_valid_candidate")
    for raw_path, checksum in sources.items():
        if hash_file(records._safe_data_path(raw_path)) != checksum:
            raise ValueError("completed audit input changed")
    current_query = False
    for query_path in (run / "reading_tools").glob("*/request.json"):
        query = _json(query_path)
        if query.get("tool") != "wt_query_candidate" or query.get("source_review_call") != call_path.name:
            continue
        response = _json(query_path.parent / "result.json")
        data = response.get("data", {})
        if (response.get("success") is True and data.get("source_review_call") == call_path.name
                and data.get("candidate_binding") == request.get("candidate_binding")
                and type(data.get("returned_count")) is int and data["returned_count"] > 0):
            if data.get("mode") in {"path", "section"}:
                current_query = True
            elif data.get("mode") == "paths":
                from .reading_tools import candidate_path_batch_matches
                current_query |= candidate_path_batch_matches(record, request["candidate_binding"], query["arguments"], data)
    if (model.model_validate(_json(call_path / "answer.json")) != expected
            or not current_query
            or not records._queried_candidate_snapshot(run, record, request, sources)
            or request.get("source_review_call") != call_path.name
            or request.get("source_review_scope_sha256") != scope["scope_sha256"]
            or not verify_source_review(run, scope, completed.get("source_review_proof"), call_path.name)):
        raise ValueError("audit output, candidate access or original-source delivery is not authenticated")
    event_hash = _completed_call_protocol(run, completed, _json(call_path / "answer.json"))
    return request, completed, sources, event_hash


def authenticate_addendum_parent(settings, parent_run: Path, *, allow_exhausted=False) -> AddendumParent:
    """Authenticate a stopped, completed scientific audit with only local source-disposition gaps."""
    from .audit_execution import _verified_scientific_inputs
    from .audit_resume import verify_resumed_review
    from .reader_workflow import reading_checks
    from .validation import validate_record
    from .web_records import WebRecords
    from .workflow import validate_plan_metadata

    records = WebRecords(settings)
    run = records._safe_data_path(parent_run)
    if run.parent != (settings.data_root / "runs").resolve():
        raise ValueError("addendum parent must be a run in the configured store")
    if not any(r["run_id"] == run.name and r["kind"] == "extraction" for r in records.store.runs()):
        raise ValueError("addendum parent is not a registered extraction run")
    plan, source_path, bundle, documents = records._source(run.name)
    terminal = _json(run / "result.json")
    history = terminal.get("history", [])
    if (terminal.get("status") != "candidate_incomplete_budget"
            or terminal.get("stop_reason") != "audit_report_incomplete"
            or terminal.get("human_validated") is not False or terminal.get("graph_published") is not False
            or not history or history[-1].get("audit_origin") != "scientific_delta_model_audit"
            or plan.get("cohort_role") != "development" or settings.rules_version != plan.get("rule_version")):
        raise ValueError("addendum requires an unpublished incomplete scientific audit, not a new scientific repair")
    version = history[-1].get("version")
    if type(version) is not int or not 0 <= version <= 3 or len(history) != version + 1:
        raise ValueError("parent has an ambiguous audit history")
    candidate_path, audit_path = run / f"candidate.v{version:02d}.json", run / f"audit.v{version:02d}.json"
    record = ExtractionRecord.model_validate(_json(candidate_path))
    audit = ReadingAudit.model_validate(_json(audit_path))
    dossier = ReadingDossier.model_validate(_json(run / "reading_dossier.json"))
    if (hash_file(candidate_path) != history[-1].get("candidate_sha256")
            or hash_file(run / "candidate.json") != hash_file(candidate_path)
            or ReadingAudit.model_validate(_json(run / "model_review.json")) != audit
            or any(int(p.stem[-2:]) > version for p in run.glob("candidate.v[0-9][0-9].json"))
            or reading_checks(dossier, record, audit, documents)
            or any(i.severity == "error" for i in [*validate_record(record, source_documents=documents),
                *validate_plan_metadata(record, plan, documents, store=records.store)])):
        raise ValueError("parent candidate, audit, reading coverage or machine identity is inconsistent")
    calls = sorted((run / "calls").glob("*/request.json"))
    if not calls or len(calls) != terminal.get("model_calls"):
        raise ValueError("parent has an incomplete model-call ledger")
    call = calls[-1].parent
    envelope = _json(calls[-1])
    raw_receipt = envelope.get("request", {}).get("citation_review", {})
    scope_path = records._safe_data_path(raw_receipt["scope_path"])
    if scope_path != run / f"citation_scope.v{version:02d}.json":
        raise ValueError("parent full scientific scope is not the current version")
    full_scope = _json(scope_path)
    baseline = ReadingAudit.model_validate(_json(raw_receipt["baseline_audit_path"]))
    old, current, additional = _verified_scientific_inputs((raw_receipt, full_scope, baseline))
    if current != record or not verify_resumed_review(settings, run, record, old, baseline, raw_receipt):
        raise ValueError("parent scientific audit lost its authenticated full-audit ancestry")
    for entry, previous in zip(raw_receipt["additional_audits"], additional):
        previous_record = ExtractionRecord.model_validate(_json(entry["candidate_path"]))
        if not records._completed_audit_provenance(run, previous_record, previous):
            raise ValueError("parent scientific audit bypasses an earlier delta's source obligations")
    request, completed, sources, event_hash = _call_auth(records, run, plan, call, record,
        ReadingAudit, audit, full_scope, f"audit_{version}")
    scope = build_audit_addendum_scope(full_scope, baseline, audit, old_record=old,
        new_record=record, additional_audits=additional)
    if (scope is None or terminal.get("reading_check_issues") != scope["parent_protocol_issues"]
            or history[-1].get("reading_check_issues") != scope["parent_protocol_issues"]):
        raise ValueError("parent has scientific blockers or issues outside a bounded addendum")
    elapsed = []
    for path in calls:
        seconds = _json(path.parent / "result.json").get("elapsed_seconds")
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 <= seconds < 100000:
            raise ValueError("parent elapsed model budget is missing")
        elapsed.append(seconds)
    remaining = plan["reading_settings"]["total_timeout_seconds"] - sum(elapsed)
    if not allow_exhausted and (remaining <= 0 or len(calls) >= plan["max_calls"]):
        raise ValueError("parent has no remaining model time or call budget")
    paths = {run / name for name in ("plan.json", "sources.json", "result.json", "candidate.json",
        "model_review.json", "reading_dossier.json")}
    paths.update((candidate_path, audit_path, scope_path, call / "request.json", call / "result.json", call / "answer.json"))
    paths.update(Path(p) for p in sources)
    for item in completed["source_review_proof"]["tool_receipts"]:
        paths.update((Path(item["request_path"]), Path(item["result_path"])))
    receipt = {"policy": POLICY, "parent_run": run.name, "parent_version": version,
        "candidate_path": str(candidate_path), "candidate_sha256": hash_file(candidate_path),
        "candidate_hash": digest_json(record.model_dump(mode="json")),
        "audit_path": str(audit_path), "audit_sha256": hash_file(audit_path),
        "audit_hash": digest_json(audit.model_dump(mode="json")),
        "scope_path": str(scope_path), "scope_sha256": full_scope["scope_sha256"],
        "completed_call": str(call), "events_sha256": event_hash,
        "remaining_model_seconds": remaining, "remaining_model_calls": plan["max_calls"] - len(calls),
        "artifact_hashes": [{"path": str(p), "sha256": hash_file(p)} for p in sorted(paths)],
        "scientific_validation": "not_established"}
    return AddendumParent(run, plan, bundle, documents, record, dossier, old, baseline, audit,
        additional, full_scope, scope, receipt, remaining)


def _targets_prompt(parent):
    from .citation_audit import _parts
    data = parent.record.model_dump(mode="json")
    targets = []
    for item in parent.scope["targets"]:
        value = data
        for part in _parts(item["record_path"]):
            value = value[part]
        targets.append({"record_path": item["record_path"], "value": value})
    return audit_addendum_prompt(parent.scope, parent.audit) + "\nEXACT TARGET VALUES:\n" + json.dumps(targets, ensure_ascii=False)


def _single_child(store, parent_id, *, current_run=None):
    # A failed or merely prepared addendum is retained, not silently granted a
    # fresh copy of the parent's remaining time. Recovery needs a new explicit
    # budget ledger, not another invocation of this one-call protocol.
    for row in store.runs():
        manifest = _json(Path(row["manifest_path"]))
        if (manifest.get("operation") == POLICY and manifest.get("parent_run") == parent_id
                and row["run_id"] != current_run):
            raise ValueError("this parent already has an addendum attempt; its remaining budget cannot be reset")


def prepare_audit_addendum(settings, parent_run: Path, *, timeout_seconds=1200, budget_grant=None) -> Path:
    """Freeze a distinct one-call plan without changing the parent or its candidate."""
    from .codex import code_identity
    from .workflow import runtime_versions
    from .budget_grants import preparation_grant
    addition = preparation_grant(settings, parent_run, budget_grant)
    parent = (authenticate_addendum_parent(settings, parent_run, allow_exhausted=True) if addition
              else authenticate_addendum_parent(settings, parent_run))
    if (type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 1200
            or timeout_seconds > (addition["allocated_seconds"] if addition else parent.remaining_seconds)):
        raise ValueError("addendum must fit the original remaining budget and the 1200-second cap")
    require_active_model(settings.codex)
    store = DataStore(settings.data_root)
    _single_child(store, parent.run.name)
    run = store.new_run("extraction", {"paper_id": parent.record.paper.id,
        "cohort_role": parent.plan["cohort_role"], "rule_version": parent.plan["rule_version"],
        "schema_version": parent.record.schema_version, "workflow": "source_audit_addendum",
        "operation": POLICY, "parent_run": parent.run.name})
    copies = {"sources.json": parent.bundle, "candidate.v00.json": parent.record.model_dump(mode="json"),
        "reading_dossier.json": parent.dossier.model_dump(mode="json"),
        "addendum_parent_receipt.json": parent.receipt, "addendum_parent_audit.json": parent.audit.model_dump(mode="json"),
        "addendum_full_scope.json": parent.full_scope, "audit_addendum_scope.json": parent.scope,
        "addendum_baseline_candidate.json": parent.baseline_record.model_dump(mode="json"),
        "addendum_baseline_audit.json": parent.baseline_audit.model_dump(mode="json"),
        "response_schema.json": AuditAddendum.model_json_schema()}
    for name, data in copies.items():
        write_new_json(run / name, data)
    (run / "audit_addendum_prompt.txt").write_text(_targets_prompt(parent), encoding="utf-8")
    paths = [run / name for name in copies] + [run / "audit_addendum_prompt.txt"]
    paths += [Path(d.source.path) for d in parent.documents]
    model_settings = {**settings.codex.model_dump(mode="json"), "max_calls_per_run": 1,
                      "timeout_seconds": timeout_seconds}
    plan = {"version": 1, "run_id": run.name, "paper_id": parent.record.paper.id,
        "cohort_role": parent.plan["cohort_role"], "rule_version": parent.plan["rule_version"],
        "schema_version": parent.record.schema_version, "workflow": "source_audit_addendum",
        "workdir": str(run), "input_paths": [{"path": str(p), "sha256": hash_file(p)} for p in paths],
        "reading_artifacts": [], "image_paths": [], "code_identity": code_identity(settings.project_root),
        "runtime": runtime_versions(), "model_settings": model_settings, "max_calls": 1,
        "reading_settings": {"enabled": True, "max_revisions": 0, "max_tool_calls": 48,
            "total_timeout_seconds": timeout_seconds},
        "audit_addendum_parent": parent.receipt, "status": "prepared_no_model_calls",
        "extraction_policy": parent.plan.get("extraction_policy")}
    if addition:
        plan["budget_grant_preparation"] = budget_grant
    write_new_json(run / "plan.json", plan)
    store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    return run / "plan.json"


def _authenticate_plan(settings, plan_path):
    from .web_records import WebRecords
    records = WebRecords(settings)
    path = records._safe_data_path(plan_path)
    run = path.parent
    if path.name != "plan.json" or run.parent != (settings.data_root / "runs").resolve():
        raise ValueError("invalid addendum run")
    plan, _, bundle, _ = records._source(run.name)
    parent_receipt = plan["audit_addendum_parent"]
    from .budget_grants import prepared_grant
    parent_path = settings.data_root / "runs" / parent_receipt["parent_run"]
    addition = prepared_grant(settings, run, plan, parent_path)
    parent = (authenticate_addendum_parent(settings, parent_path, allow_exhausted=True) if addition
              else authenticate_addendum_parent(settings, parent_path))
    if (parent.receipt != parent_receipt or _json(run / "addendum_parent_receipt.json") != parent.receipt
            or _json(run / "audit_addendum_scope.json") != parent.scope
            or _json(run / "addendum_full_scope.json") != parent.full_scope
            or ExtractionRecord.model_validate(_json(run / "candidate.v00.json")) != parent.record
            or ReadingAudit.model_validate(_json(run / "addendum_parent_audit.json")) != parent.audit
            or ReadingAudit.model_validate(_json(run / "addendum_baseline_audit.json")) != parent.baseline_audit
            or ExtractionRecord.model_validate(_json(run / "addendum_baseline_candidate.json")) != parent.baseline_record
            or ReadingDossier.model_validate(_json(run / "reading_dossier.json")) != parent.dossier
            or bundle != parent.bundle or (run / "audit_addendum_prompt.txt").read_text(encoding="utf-8") != _targets_prompt(parent)
            or plan["max_calls"] != 1 or plan["model_settings"]["max_calls_per_run"] != 1
            or plan_model(plan) is None
            or not 0 < plan["reading_settings"]["total_timeout_seconds"] <= min(1200, addition["allocated_seconds"] if addition else parent.remaining_seconds)
            or plan["model_settings"]["timeout_seconds"] != plan["reading_settings"]["total_timeout_seconds"]):
        raise ValueError("frozen addendum plan, parent, sources or budgets changed")
    return records, run, plan, parent


def execute_audit_addendum(settings, plan_path: Path, *, client) -> Path:
    """Execute exactly one read-only source check; the caller explicitly enables the client."""
    from .codex import code_identity
    from .reader_workflow import reading_checks
    from .reading_tools import ReadingTools
    from .validation import validate_record
    from .quantitative import quantitative_summary
    from .workflow import runtime_versions
    records, run, plan, parent = _authenticate_plan(settings, plan_path)
    if plan["max_calls"]:
        require_active_model(settings.codex)
        if plan_model(plan) != settings.codex.model:
            raise ValueError("当前模型与冻结计划不一致，请为该检查点创建续处理计划")
    _single_child(records.store, parent.run.name, current_run=run.name)
    from .budget_grants import verify_lineage_plan
    verify_lineage_plan(settings, run, plan)
    if plan.get("budget_continuation"):
        from .budget_extension_workflow import verify_extension_plan
        verify_extension_plan(settings, run, plan)
    if (plan["code_identity"] != code_identity(settings.project_root) or plan["runtime"] != runtime_versions()
            or client.calls_started != 0 or client.settings.model_dump(mode="json") != plan["model_settings"]
            or client.total_timeout_seconds != plan["reading_settings"]["total_timeout_seconds"]
            or (run / "result.json").exists() or (run / "calls").exists()):
        raise ValueError("addendum execution requires its frozen code and a fresh one-call bounded client")
    tools = ReadingTools(parent.bundle, run, max_calls=plan["reading_settings"]["max_tool_calls"],
        expected_inputs=plan["input_paths"])
    tools.bind_candidate(parent.record, run / "candidate.v00.json")
    client.bind_tools(tools)
    scope_receipt = {"scope_path": str(run / "audit_addendum_scope.json"),
                     "scope_file_sha256": hash_file(run / "audit_addendum_scope.json")}
    try:
        records.store.event(run.name, "execution_started", {"operation": POLICY, "max_calls": 1})
        supplement = client.complete(AuditAddendum,
            prompt=(run / "audit_addendum_prompt.txt").read_text(encoding="utf-8"),
            input_paths=[Path(x["path"]) for x in plan["input_paths"]], workdir=run,
            audit_root=run / "calls", identity={"stage": "audit_addendum_0", "rule_version": plan["rule_version"],
                "code_identity": plan["code_identity"], "runtime": plan["runtime"], "citation_review": scope_receipt},
            image_paths=[], conversation_key="audit-addendum-0")
        if client.calls_started != 1:
            raise ValueError("addendum exceeded its one-call contract")
        write_new_json(run / "audit_addendum.json", supplement.model_dump(mode="json"))
        # Flush the public completion event before independent provenance replay.
        if getattr(client, "events", None):
            client.events.flush()
        calls = list((run / "calls").glob("*/request.json"))
        if len(calls) != 1:
            raise ValueError("addendum has an ambiguous raw call ledger")
        request, completed, _, events_hash = _call_auth(records, run, plan, calls[0].parent,
            parent.record, AuditAddendum, supplement, parent.scope, "audit_addendum_0")
        if request.get("citation_review") != scope_receipt:
            raise ValueError("addendum raw call differs from its frozen subset scope")
        merged = merge_audit_addendum(parent.scope, parent.full_scope, parent.baseline_audit, parent.audit,
            supplement, old_record=parent.baseline_record, new_record=parent.record, additional_audits=parent.additional_audits)
        checks = audit_addendum_issues(parent.scope, parent.full_scope, parent.baseline_audit, parent.audit,
            supplement, old_record=parent.baseline_record, new_record=parent.record, additional_audits=parent.additional_audits)
        checks += reading_checks(parent.dossier, parent.record, merged, parent.documents)
        issues = validate_record(parent.record, source_documents=parent.documents)
        errors = sum(i.severity == "error" for i in issues)
        proof = {"policy": POLICY, "parent_receipt_sha256": hash_file(run / "addendum_parent_receipt.json"),
            "addendum_sha256": hash_file(run / "audit_addendum.json"), "source_review_proof_sha256": completed["source_review_proof"]["proof_sha256"],
            "completed_call": str(calls[0].parent), "events_sha256": events_hash,
            "merged_audit_hash": digest_json(merged.model_dump(mode="json")),
            "interpretation": "Append-only source addendum to the retained original audit; no new full audit or scientific validation."}
        write_new_json(run / "audit.v00.json", merged.model_dump(mode="json"))
        write_new_json(run / "model_review.json", merged.model_dump(mode="json"))
        write_new_json(run / "candidate.json", parent.record.model_dump(mode="json"))
        write_new_json(run / "validation.json", {"issues": [x.model_dump(mode="json") for x in issues], "semantic_accuracy": "not_established"})
        result = {"status": "candidate_audited_with_limitations" if not checks and not errors else "candidate_incomplete_budget",
            "stop_reason": "audit_completed" if not checks and not errors else "addendum_unresolved",
            "model_calls": client.calls_started, "reading_tool_calls": tools.calls_started, "revisions": 0,
            "validation_error_count": errors, "reading_check_issues": checks, "human_validated": False,
            "graph_published": False, "dataset_role": plan["cohort_role"], "scientific_accuracy": "not_established",
            "audit_addendum_proof": proof, "quantitative_coverage_summary": quantitative_summary(parent.record),
            "history": [{"version": 0, "candidate_sha256": hash_file(run / "candidate.json"),
                "audit_origin": "audit_addendum_composite", "validation_errors": errors,
                "needs_revision": bool(checks or errors), "reading_check_issues": checks}]}
        if plan.get("budget_continuation"):
            from .budget_grants import addition_usage
            result["budget_addition"] = addition_usage(settings, run, plan, completed["elapsed_seconds"], 1)
        write_new_json(run / "result.json", result)
        records.store.event(run.name, "candidate_completed", {"status": result["status"], "model_calls": client.calls_started})
    except BaseException as error:
        if not (run / "failure.json").exists():
            write_new_json(run / "failure.json", {"error": f"{type(error).__name__}: {error}", "model_calls": client.calls_started})
        records.store.event(run.name, "failed", {"error": str(error), "model_calls": client.calls_started})
        raise
    finally:
        client.close()
    return run


def verify_audit_addendum_run(settings, run, record, parsed) -> bool:
    """Reauthenticate the composite, its raw supplement and the original full baseline."""
    try:
        records, run, plan, parent = _authenticate_plan(settings, Path(run) / "plan.json")
        supplement = AuditAddendum.model_validate(_json(run / "audit_addendum.json"))
        result = _json(run / "result.json")
        proof = result["audit_addendum_proof"]
        calls = list((run / "calls").glob("*/request.json"))
        if len(calls) != 1 or result.get("model_calls") != 1 or record != parent.record:
            return False
        request, completed, _, event_hash = _call_auth(records, run, plan, calls[0].parent, record,
            AuditAddendum, supplement, parent.scope, "audit_addendum_0")
        if (request.get("citation_review") != {"scope_path": str(run / "audit_addendum_scope.json"),
                "scope_file_sha256": hash_file(run / "audit_addendum_scope.json")}
                or proof.get("policy") != POLICY or proof.get("completed_call") != str(calls[0].parent)
                or proof.get("parent_receipt_sha256") != hash_file(run / "addendum_parent_receipt.json")
                or proof.get("addendum_sha256") != hash_file(run / "audit_addendum.json")
                or proof.get("source_review_proof_sha256") != completed["source_review_proof"]["proof_sha256"]
                or proof.get("events_sha256") != event_hash):
            return False
        merged = merge_audit_addendum(parent.scope, parent.full_scope, parent.baseline_audit, parent.audit,
            supplement, old_record=parent.baseline_record, new_record=record, additional_audits=parent.additional_audits)
        issues = audit_addendum_issues(parent.scope, parent.full_scope, parent.baseline_audit, parent.audit,
            supplement, old_record=parent.baseline_record, new_record=record, additional_audits=parent.additional_audits)
        from .budget_grants import addition_usage
        return (not issues and parsed == merged and ReadingAudit.model_validate(_json(run / "audit.v00.json")) == merged
            and result.get("budget_addition") == addition_usage(settings, run, plan, completed.get("elapsed_seconds"), 1)
            and ReadingAudit.model_validate(_json(run / "model_review.json")) == merged
            and proof.get("merged_audit_hash") == digest_json(merged.model_dump(mode="json"))
            and hash_file(run / "candidate.json") == hash_file(run / "candidate.v00.json")
            and result.get("status") == "candidate_audited_with_limitations"
            and result.get("stop_reason") == "audit_completed" and result.get("reading_check_issues") == []
            and result.get("validation_error_count") == 0 and result.get("human_validated") is False
            and result.get("graph_published") is False and len(result.get("history", [])) == 1
            and result["history"][0].get("audit_origin") == "audit_addendum_composite"
            and result["history"][0].get("candidate_sha256") == hash_file(run / "candidate.json"))
    except (ValueError, OSError, KeyError, TypeError, IndexError, AttributeError):
        return False
