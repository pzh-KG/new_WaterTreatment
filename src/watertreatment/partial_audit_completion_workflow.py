"""Complete one retained partial scientific audit without rerunning its review.

The parent provider result remains failed. Its public completed answer, exact
candidate access and partial source delivery are authenticated separately. Only
an appended report plus replayed source union can satisfy the original scope.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path

from .model_policy import plan_model, require_active_model
from .coverage_correction_workflow import _copy_new
from .domain import ExtractionRecord
from .partial_audit_completion import (POLICY, PartialAuditAddendum,
    build_partial_audit_completion_scope, merge_partial_audit_addendum,
    partial_audit_completion_prompt, validate_partial_audit_addendum)
from .reading import ReadingAudit, ReadingDossier
from .storage import DataStore, digest_json, hash_file, write_new_json


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class PartialAuditParent:
    run: Path
    plan: dict
    bundle: dict
    documents: list
    record: ExtractionRecord
    old_record: ExtractionRecord
    baseline: ReadingAudit
    audit: ReadingAudit
    dossier: ReadingDossier
    full_scope: dict
    progress: dict
    scope: dict
    call: Path
    receipt: dict


def _source_receipts(run, call_id):
    from .source_review import SOURCE_TOOLS
    result = []
    for path in sorted((run / "reading_tools").glob("*/request.json")):
        request = _json(path)
        output = path.with_name("result.json")
        if (request.get("tool") not in SOURCE_TOOLS or request.get("source_review_call") != call_id
                or not output.exists()):
            continue
        response = _json(output)
        if response.get("success") is not True or not response.get("data", {}).get("source_delivery"):
            continue
        result.append({"request_path": str(path), "request_sha256": hash_file(path),
            "result_path": str(output), "result_sha256": hash_file(output)})
    return result


def _scientific_inputs(parent):
    return {"original_scope": parent.full_scope, "old_record": parent.old_record,
        "new_record": parent.record, "baseline_audit": parent.baseline,
        "source_progress": parent.progress, "additional_audits": (),
        "requirement_amendment": parent.scope.get("source_requirement_amendment")}


CAPTION_POLICY = "delivered-native-table-caption-v1"


def delivered_table_captions(parent, supplement, progress):
    """Bind a literal Word caption to its source table or an explicitly cited, viewed drawing."""
    from .source_review import _native_table
    from .word_review_links import linked_structure, figure_caption_for_occurrences
    original = {(a["source_id"], a["block_id"]) for a in parent.full_scope["source_anchors"]}
    received = {(a["source_id"], a["block_id"]) for a in progress["delivered"]}
    viewed = {(a["source_id"], a["block_id"]) for a in progress.get("full_views", [])}
    docs = {d.source.source_id: d for d in parent.documents}
    extra = set()
    for finding in supplement.findings:
        if finding.needs_revision or finding.verdict in {"unsupported", "not_checked"}:
            continue
        for anchor in finding.evidence:
            key = anchor.source_id, anchor.block_id
            if key in original:
                continue
            doc = docs.get(anchor.source_id)
            blocks = {b.block_id: b for b in doc.blocks} if doc else {}
            caption = blocks.get(anchor.block_id)
            if key not in received or not caption or not anchor.quote or anchor.quote not in caption.text:
                raise ValueError("Additional citation lacks an exact delivered caption quote")
            if any((ref.source_id,ref.block_id) in original & viewed and ref.source_id == anchor.source_id
                    and ref.block_id in blocks and figure_caption_for_occurrences(doc,[blocks[ref.block_id]],caption)
                    for ref in finding.evidence):
                extra.add(key)
                continue
            linked = linked_structure(doc, caption, [], set(), lambda bid: _native_table(doc, blocks[bid], set()))
            if not linked or not any((anchor.source_id, bid) in original for bid in linked["block_ids"] if bid != anchor.block_id):
                raise ValueError("Additional caption does not belong to a cited figure or table in the frozen review")
            extra.add(key)
    return extra


def merge_completion(plan, parent, supplement, progress):
    args = _scientific_inputs(parent)
    if plan.get("partial_citation_context_policy") == CAPTION_POLICY:
        args["contextual_citations"] = delivered_table_captions(parent, supplement, progress)
    return (merge_partial_audit_addendum(parent.scope, supplement, parent.audit, **args),
            validate_partial_audit_addendum(parent.scope, supplement, parent.audit, **args))


def report_version(scope):
    return 3 if not scope["source_anchors"] and scope.get("report_query_batches") else 2


def report_schema(version):
    from .partial_report_transport import CompactReport
    return (CompactReport if version == 3 else PartialAuditAddendum).model_json_schema()


def authenticate_partial_audit_parent(settings, parent_run, *, allow_exhausted=False, linked_sources=True):
    from .audit_addendum_workflow import _completed_call_protocol
    from .audit_execution import _verified_scientific_inputs, select_scientific_scope_limits
    from .audit_resume import authenticate_baseline, _scientific_frozen_inputs
    from .coverage_correction import verify_structural_correction
    from .reader_workflow import reading_checks
    from .scientific_revision_audit import build_scientific_revision_scope, POLICY as SCI_POLICY
    from .source_review import partial_source_review_progress, build_native_word_requirement_amendment
    from .staged_source_resume import _call
    from .validation import validate_record
    from .web_records import WebRecords
    from .workflow import validate_plan_metadata

    records = WebRecords(settings)
    run = records._safe_data_path(parent_run)
    if (run.parent != (settings.data_root / "runs").resolve()
            or not any(r["run_id"] == run.name and r["kind"] == "extraction" for r in records.store.runs())):
        raise ValueError("partial audit parent must be a registered extraction run")
    plan, _, bundle, documents = records._source(run.name)
    terminal, failure = _json(run / "result.json"), _json(run / "failure.json")
    ancestry = plan.get("resumed_full_audit", {})
    if (terminal.get("status") != "candidate_incomplete_failure"
            or terminal.get("last_candidate_version") != "candidate.v00.json"
            or terminal.get("human_validated") is not False or terminal.get("graph_published") is not False
            or terminal.get("model_calls") != 1 or failure.get("model_calls") != 1
            or plan.get("cohort_role") not in {"unassigned", "development"}
            or plan.get("rule_version") != settings.rules_version
            or ancestry.get("policy") != SCI_POLICY
            or ancestry.get("seed_provenance", {}).get("kind") != "completed_revision"
            or (run / "audit.v00.json").exists() or (run / "model_review.json").exists()
            or list(run.glob("candidate.v0[1-9].json"))):
        raise ValueError("only a first native-repair delta with a retained failed delivery qualifies")
    candidate_path = run / "candidate.v00.json"
    record = ExtractionRecord.model_validate(_json(candidate_path))
    if hash_file(candidate_path) != hash_file(run / "candidate.json"):
        raise ValueError("partial audit candidate bytes differ from its retained version")
    old, baseline, old_bundle = authenticate_baseline(settings, ancestry, record)
    seed = _scientific_frozen_inputs(ancestry, run, old, baseline)
    if seed != record or digest_json(old_bundle["documents"]) != digest_json(bundle["documents"]):
        raise ValueError("partial audit changed the authenticated native repair/source representations")
    calls = list((run / "calls").glob("*/request.json"))
    if len(calls) != 1:
        raise ValueError("partial audit parent must have exactly one retained model attempt")
    call = calls[0].parent
    request, completed, inputs, _ = _call(records, run, calls[0], plan, stage="audit_0", status="failed")
    if (not str(completed.get("error", "")).startswith("ValueError: bounded source review delivery incomplete;")
            or failure.get("error") != completed["error"]
            or request.get("source_review_call") != call.name):
        raise ValueError("unknown provider/controller failures cannot be relabelled as a partial source audit")
    for path, checksum in inputs.items():
        if hash_file(records._safe_data_path(path)) != checksum:
            raise ValueError("partial model input changed")
    audit = ReadingAudit.model_validate(_json(call / "answer.json"))
    if not records._queried_candidate_snapshot(run, record, request, inputs):
        raise ValueError("partial audit did not query the exact immutable candidate")
    events_hash = _completed_call_protocol(run, completed, _json(call / "answer.json"))
    receipt = request.get("citation_review", {})
    expected_scope_path = run / "citation_scope.v00.json"
    if (receipt.get("scope_path") != str(expected_scope_path)
            or receipt.get("scope_file_sha256") != hash_file(expected_scope_path)
            or receipt.get("additional_audits") != []
            or receipt.get("current_candidate_path") != str(candidate_path)
            or receipt.get("current_candidate_sha256") != hash_file(candidate_path)):
        raise ValueError("partial audit scope receipt differs from the first unchanged candidate")
    full_scope = _json(expected_scope_path)
    bound_old, bound_current, additional = _verified_scientific_inputs((receipt, full_scope, baseline))
    expected = build_scientific_revision_scope(old, record, baseline, baseline_origin="fresh_model_audit",
        **select_scientific_scope_limits(run, plan))
    if (bound_old != old or bound_current != record or additional or expected != full_scope
            or request.get("source_review_scope_sha256") != full_scope["scope_sha256"]):
        raise ValueError("partial scientific scope no longer matches its authenticated full baseline")
    progress = partial_source_review_progress(run, full_scope, _source_receipts(run, call.name), call.name)
    if progress["complete"]:
        raise ValueError("the retained failure is not explained by current source-delivery gaps")
    # Keep the historical visual requirements and failed receipts intact. Native
    # Word table XML is an explicit source-kind amendment, never a performed view.
    amendment = None
    if progress["missing_full_views"]:
        try:
            amendment = build_native_word_requirement_amendment(run, full_scope, progress["missing_full_views"])
        except ValueError:
            if linked_sources:
                # Amend only reproducibly identified Word objects. PDF pages,
                # real image gaps and ambiguous captions retain their own obligations.
                from .source_review import _ReviewSources
                sources = _ReviewSources(run)
                eligible = [r for r in progress["missing_full_views"] if sources.structure(
                    r["source_id"], r["block_id"],
                    [a for a in full_scope["source_anchors"] if a["source_id"] == r["source_id"]],
                    linked=True) is not None]
                if eligible:
                    amendment = build_native_word_requirement_amendment(run, full_scope, eligible, linked=True)
    scope = build_partial_audit_completion_scope(full_scope, audit, old_record=old, new_record=record,
        baseline_audit=baseline, source_progress=progress, requirement_amendment=amendment)
    dossier = ReadingDossier.model_validate(_json(run / "reading_dossier.json"))
    structural = run / "reading_structure_correction.v01.json"
    if structural.exists():
        if not verify_structural_correction(run, _json(structural), documents):
            raise ValueError("parent structural dossier correction changed")
        dossier = ReadingDossier.model_validate(_json(run / "reading_dossier.structural.v01.json"))
    checks = reading_checks(dossier, record, audit, documents)
    if any(not issue.startswith("dossier ") for issue in checks):
        raise ValueError("partial candidate has unrelated reading/audit defects")
    machine = [*validate_record(record, source_documents=documents),
        *validate_plan_metadata(record, plan, documents, store=records.store)]
    if any(i.severity == "error" for i in machine):
        raise ValueError("partial candidate has machine or source identity errors")
    elapsed = completed.get("elapsed_seconds")
    total = plan["reading_settings"]["total_timeout_seconds"]
    if (not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or not math.isfinite(elapsed)
            or elapsed <= 0 or (not allow_exhausted and (elapsed >= total or plan["max_calls"] < 2))):
        raise ValueError("partial parent has no authenticated remaining model budget")
    paths = [run / n for n in ("plan.json", "sources.json", "result.json", "failure.json",
        "candidate.v00.json", "candidate.json", "reading_dossier.json", "citation_scope.v00.json")]
    paths += [call / n for n in ("request.json", "result.json", "answer.json")]
    paths += [run / n for n in ("reading_structure_correction.v01.json", "reading_structure_scope.v01.json",
        "reading_dossier.structural.v01.json") if (run / n).exists()]
    proof = {"policy": POLICY, "parent_run": run.name, "parent_provider_status": "failed",
        "parent_public_turn_status": "completed", "parent_audit_call": str(call), "events_sha256": events_hash,
        "candidate_sha256": hash_file(candidate_path), "parent_answer_sha256": hash_file(call / "answer.json"),
        "progress_sha256": progress["progress_sha256"], "original_scope_sha256": full_scope["scope_sha256"],
        "original_total_model_seconds": total, "original_max_calls": plan["max_calls"],
        "used_model_seconds": elapsed, "used_model_calls": 1, "remaining_model_seconds": total - elapsed,
        "remaining_model_calls": plan["max_calls"] - 1, "retained_dossier_issues": checks,
        "artifact_hashes": [{"path": str(p), "sha256": hash_file(p)} for p in paths],
        "scientific_validation": "not_established"}
    return PartialAuditParent(run, plan, bundle, documents, record, old, baseline, audit, dossier,
        full_scope, progress, scope, call, proof)


def _single_attempt(store, parent_id, current=None):
    for row in store.runs():
        manifest = _json(row["manifest_path"])
        if (manifest.get("operation") == POLICY and manifest.get("parent_run") == parent_id
                and row["run_id"] != current):
            raise ValueError("partial parent already has a retained completion attempt; budget cannot be reset")


def prepare(settings, parent_run, *, timeout_seconds=None, budget_grant=None):
    from .codex import code_identity
    from .workflow import runtime_versions, _image_manifest
    from .budget_grants import preparation_grant
    addition = preparation_grant(settings, parent_run, budget_grant)
    parent = (authenticate_partial_audit_parent(settings, parent_run, allow_exhausted=True) if addition
              else authenticate_partial_audit_parent(settings, parent_run))
    cap = min(900, math.floor(addition["allocated_seconds"] if addition else parent.receipt["remaining_model_seconds"]))
    timeout_seconds = cap if timeout_seconds is None else timeout_seconds
    if type(timeout_seconds) is not int or not 60 <= timeout_seconds <= cap:
        raise ValueError("partial completion must fit the parent's remaining model time and the 900-second cap")
    require_active_model(settings.codex)
    store = DataStore(settings.data_root)
    _single_attempt(store, parent.run.name)
    run = store.new_run("extraction", {"paper_id": parent.record.paper.id,
        "cohort_role": parent.plan["cohort_role"], "rule_version": parent.plan["rule_version"],
        "schema_version": parent.record.schema_version, "workflow": "partial_scientific_audit_completion",
        "operation": POLICY, "parent_run": parent.run.name})
    for name, path in {"sources.json": parent.run / "sources.json", "candidate.v00.json": parent.run / "candidate.v00.json",
            "reading_dossier.json": parent.run / "reading_dossier.json",
            "partial_parent_answer.json": parent.call / "answer.json"}.items():
        _copy_new(path, run / name)
    copies = {"partial_parent_receipt.json": parent.receipt, "partial_parent_source_progress.json": parent.progress,
        "partial_original_scope.json": parent.full_scope, "partial_completion_scope.json": parent.scope,
        "partial_baseline_candidate.json": parent.old_record.model_dump(mode="json"),
        "partial_baseline_audit.json": parent.baseline.model_dump(mode="json"),
        "reading_dossier.effective.json": parent.dossier.model_dump(mode="json"),
        "response_schema.json": report_schema(report_version(parent.scope))}
    for name, value in copies.items():
        write_new_json(run / name, value)
    (run / "partial_completion_prompt.txt").write_text(
        partial_audit_completion_prompt(parent.scope, parent.audit, version=report_version(parent.scope), documents=parent.documents), encoding="utf-8")
    paths = [p for p in run.iterdir() if p.is_file() and p.name != "manifest.json"]
    paths += [Path(d.source.path) for d in parent.documents]
    paths += [Path(item["path"]) for item in _image_manifest(parent.bundle)]
    paths = list(dict.fromkeys(paths))
    model_settings = {**settings.codex.model_dump(mode="json"), "max_calls_per_run": 1, "timeout_seconds": timeout_seconds}
    plan = {"version": 1, "run_id": run.name, "paper_id": parent.record.paper.id,
        "cohort_role": parent.plan["cohort_role"], "rule_version": parent.plan["rule_version"],
        "schema_version": parent.record.schema_version, "workflow": "partial_scientific_audit_completion",
        "workdir": str(run), "input_paths": [{"path": str(p), "sha256": hash_file(p)} for p in paths],
        "reading_artifacts": [], "image_paths": [], "code_identity": code_identity(settings.project_root),
        "runtime": runtime_versions(), "model_settings": model_settings, "max_calls": 1,
        "reading_settings": {"enabled": True, "max_revisions": 0, "max_tool_calls": 40,
            "total_timeout_seconds": timeout_seconds}, "partial_audit_parent": parent.receipt,
        "partial_completion_prompt_version": report_version(parent.scope), "partial_citation_context_policy": CAPTION_POLICY,
        "status": "prepared_no_model_calls", "extraction_policy": parent.plan.get("extraction_policy")}
    if addition:
        plan["budget_grant_preparation"] = budget_grant
    write_new_json(run / "plan.json", plan)
    store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    store.record_development_use(run.name, parent.record.paper.id, [d.source.sha256 for d in parent.documents],
        "Partial scientific audit completion; retained failed parent and unchanged candidate; not independent validation.")
    return run / "plan.json"


def _authenticate_plan(settings, plan_path):
    from .web_records import WebRecords
    records = WebRecords(settings)
    path = records._safe_data_path(plan_path)
    run = path.parent
    if path.name != "plan.json" or run.parent != (settings.data_root / "runs").resolve():
        raise ValueError("invalid partial-completion run")
    plan, _, bundle, _ = records._source(run.name)
    from .budget_grants import prepared_grant
    parent_path = settings.data_root / "runs" / plan["partial_audit_parent"]["parent_run"]
    addition = prepared_grant(settings, run, plan, parent_path)
    from .word_review_links import LINKED_POLICY
    linked = _json(run / "partial_completion_scope.json").get("source_requirement_policy") == LINKED_POLICY
    if linked:
        parent = authenticate_partial_audit_parent(settings, parent_path, allow_exhausted=bool(addition))
    else:
        parent = authenticate_partial_audit_parent(settings, parent_path, allow_exhausted=bool(addition), linked_sources=False)
    expected = {"partial_parent_receipt.json": parent.receipt, "partial_parent_source_progress.json": parent.progress,
        "partial_original_scope.json": parent.full_scope, "partial_completion_scope.json": parent.scope,
        "partial_baseline_candidate.json": parent.old_record.model_dump(mode="json"),
        "partial_baseline_audit.json": parent.baseline.model_dump(mode="json"),
        "reading_dossier.effective.json": parent.dossier.model_dump(mode="json")}
    if (plan["partial_audit_parent"] != parent.receipt or any(_json(run / p) != value for p, value in expected.items())
            or bundle != parent.bundle or hash_file(run / "candidate.v00.json") != parent.receipt["candidate_sha256"]
            or hash_file(run / "partial_parent_answer.json") != parent.receipt["parent_answer_sha256"]
            or hash_file(run / "reading_dossier.json") != hash_file(parent.run / "reading_dossier.json")
            or (run / "partial_completion_prompt.txt").read_text(encoding="utf-8") != partial_audit_completion_prompt(
                parent.scope, parent.audit, version=plan.get("partial_completion_prompt_version", 1), documents=parent.documents)
            or _json(run / "response_schema.json") != report_schema(plan.get("partial_completion_prompt_version", 1))
            or plan["max_calls"] != 1 or plan["model_settings"]["max_calls_per_run"] != 1
            or plan_model(plan) is None
            or not 60 <= plan["reading_settings"]["total_timeout_seconds"] <= min(900, addition["allocated_seconds"] if addition else parent.receipt["remaining_model_seconds"])
            or plan["model_settings"]["timeout_seconds"] != plan["reading_settings"]["total_timeout_seconds"]):
        raise ValueError("partial completion frozen parent, scope, candidate or budget changed")
    return records, run, plan, parent


def _call_auth(records, run, plan, parent, supplement):
    from .audit_addendum_workflow import _completed_call_protocol
    from .citation_audit import _parts
    from .reading_tools import candidate_path_batch_matches, candidate_path_result
    from .source_review import verify_source_review, partial_source_review_progress
    from .staged_source_resume import _call
    calls = list((run / "calls").glob("*/request.json"))
    if len(calls) != 1:
        raise ValueError("partial completion needs exactly one new bounded call")
    call = calls[0].parent
    request, completed, sources, _ = _call(records, run, calls[0], plan,
        stage="partial_audit_completion_0", status="schema_valid_candidate")
    receipt = {"scope_path": str(run / "partial_completion_scope.json"),
        "scope_file_sha256": hash_file(run / "partial_completion_scope.json")}
    raw = _json(call / "answer.json")
    if plan.get("partial_completion_prompt_version") == 3:
        from .partial_report_transport import expand_report, public_drafts
        from .codex import strict_schema
        if request.get("partial_report_version") != 3 or request.get("schema") != strict_schema(report_schema(3)):
            raise ValueError("compact report schema differs from the frozen transport")
        drafts = dict((plan.get("partial_remaining_parent") or {}).get("public_finding_drafts", {}))
        drafts.update(public_drafts(run, call, parent.scope, completed))
        answer = expand_report(parent.scope, raw, drafts)
    else:
        answer = PartialAuditAddendum.model_validate(raw)
    if (answer != supplement
            or request.get("citation_review") != receipt or request.get("source_review_call") != call.name
            or request.get("source_review_scope_sha256") != parent.scope["scope_sha256"]
            or not verify_source_review(run, parent.scope, completed.get("source_review_proof"), call.name)
            or not records._queried_candidate_snapshot(run, parent.record, request, sources)):
        raise ValueError("partial completion lacks its exact candidate or newly required source delivery")
    queried, query_receipts = set(), []
    for p in sorted((run / "reading_tools").glob("*/request.json")):
        q = _json(p)
        if q.get("tool") != "wt_query_candidate" or q.get("source_review_call") != call.name:
            continue
        response = _json(p.with_name("result.json")); data = response.get("data", {})
        if (response.get("success") is not True or data.get("source_review_call") != call.name
                or data.get("candidate_binding") != request.get("candidate_binding")
                or not isinstance(data.get("returned_count"), int) or data["returned_count"] <= 0):
            continue
        args = q["arguments"]
        if data.get("mode") == "path" and isinstance(args.get("path"), list):
            expected_path = candidate_path_result(parent.record, args["path"])
            if any(data.get(k) != v for k, v in expected_path.items()):
                continue
            queried.add(tuple(args["path"]))
        elif data.get("mode") == "paths" and candidate_path_batch_matches(parent.record, request["candidate_binding"], args, data):
            queried.update(tuple(path) for path in args["paths"])
        else:
            continue
        query_receipts.append({"request_path": str(p), "request_sha256": hash_file(p),
            "result_path": str(p.with_name("result.json")), "result_sha256": hash_file(p.with_name("result.json"))})
    if not {_parts(p) for p in parent.scope["required_query_paths"]}.issubset(queried):
        raise ValueError("partial completion did not query every exact required claim/entity path")
    for p, checksum in sources.items():
        if hash_file(records._safe_data_path(p)) != checksum:
            raise ValueError("partial completion model input changed")
    elapsed = completed.get("elapsed_seconds")
    if (not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or not math.isfinite(elapsed)
            or not 0 <= elapsed <= plan["reading_settings"]["total_timeout_seconds"]):
        raise ValueError("partial completion exceeded or lost its remaining-budget record")
    progress = partial_source_review_progress(run, parent.scope,
        completed["source_review_proof"]["tool_receipts"], call.name)
    return {"completed_call": str(call), "events_sha256": _completed_call_protocol(run, completed, _json(call / "answer.json")),
        "elapsed_seconds": elapsed, "source_review_proof_sha256": completed["source_review_proof"]["proof_sha256"],
        "candidate_query_receipts": query_receipts}, progress


def _union(run, parent, call_proof, progress):
    from .source_review import build_source_review_union_proof
    return build_source_review_union_proof(run, parent.full_scope, [
        {"run": str(parent.run), "scope": parent.full_scope, "call_id": parent.call.name, "progress": parent.progress},
        {"run": str(run), "scope": parent.scope, "call_id": Path(call_proof["completed_call"]).name, "progress": progress}],
        requirement_amendment=parent.scope.get("source_requirement_amendment"))


def execute(settings, plan_path, *, client):
    from .codex import code_identity
    from .reader_workflow import reading_checks
    from .reading_tools import ReadingTools
    from .quantitative import quantitative_summary
    from .validation import validate_record
    from .workflow import runtime_versions
    records, run, plan, parent = _authenticate_plan(settings, plan_path)
    if plan["max_calls"]:
        require_active_model(settings.codex)
        if plan_model(plan) != settings.codex.model:
            raise ValueError("当前模型与冻结计划不一致，请为该检查点创建续处理计划")
    _single_attempt(records.store, parent.run.name, run.name)
    from .budget_grants import verify_lineage_plan
    verify_lineage_plan(settings, run, plan)
    if plan.get("budget_continuation"):
        from .budget_extension_workflow import verify_extension_plan
        verify_extension_plan(settings, run, plan)
    if (plan["code_identity"] != code_identity(settings.project_root) or plan["runtime"] != runtime_versions()
            or client.calls_started != 0 or client.settings.model_dump(mode="json") != plan["model_settings"]
            or client.total_timeout_seconds != plan["reading_settings"]["total_timeout_seconds"]
            or (run / "calls").exists() or (run / "result.json").exists()):
        raise ValueError("partial completion requires frozen code and an explicitly enabled one-call client")
    tools = ReadingTools(parent.bundle, run, max_calls=40, expected_inputs=plan["input_paths"])
    tools.bind_candidate(parent.record, run / "candidate.v00.json")
    client.bind_tools(tools)
    try:
        records.store.event(run.name, "execution_started", {"operation": POLICY, "max_calls": 1})
        supplement = client.complete(PartialAuditAddendum,
            prompt=(run / "partial_completion_prompt.txt").read_text(encoding="utf-8"),
            input_paths=[Path(x["path"]) for x in plan["input_paths"]], workdir=run,
            audit_root=run / "calls", identity={"stage": "partial_audit_completion_0", "rule_version": plan["rule_version"],
                **({"partial_report_version": 3} if plan.get("partial_completion_prompt_version") == 3 else {}),
                "code_identity": plan["code_identity"], "runtime": plan["runtime"],
                "citation_review": {"scope_path": str(run / "partial_completion_scope.json"),
                    "scope_file_sha256": hash_file(run / "partial_completion_scope.json")}},
            image_paths=[], conversation_key="partial-audit-completion-0")
        if client.calls_started != 1:
            raise ValueError("partial completion exceeded one model call")
        write_new_json(run / "partial_audit_addendum.json", supplement.model_dump(mode="json"))
        if getattr(client, "events", None):
            client.events.flush()
        call_proof, progress = _call_auth(records, run, plan, parent, supplement)
        union = _union(run, parent, call_proof, progress)
        write_new_json(run / "combined_source_review_proof.json", union)
        merged, science_checks = merge_completion(plan, parent, supplement, progress)
        reading_issues = reading_checks(parent.dossier, parent.record, merged, parent.documents)
        checks = science_checks + reading_issues
        machine = validate_record(parent.record, source_documents=parent.documents)
        errors = sum(i.severity == "error" for i in machine)
        scientific_complete = not science_checks and not errors and all(i.startswith("dossier ") for i in reading_issues)
        stop = ("audit_completed" if not checks and not errors else
            "coverage_correction_required" if scientific_complete else "partial_audit_completion_unresolved")
        status = ("candidate_audited_with_limitations" if stop == "audit_completed" else
            "candidate_incomplete_reading_coverage" if stop == "coverage_correction_required" else "candidate_incomplete_budget")
        _copy_new(run / "candidate.v00.json", run / "candidate.json")
        write_new_json(run / "audit.v00.json", merged.model_dump(mode="json"))
        _copy_new(run / "audit.v00.json", run / "model_review.json")
        write_new_json(run / "validation.json", {"issues": [i.model_dump(mode="json") for i in machine], "semantic_accuracy": "not_established"})
        proof = {"policy": POLICY, "parent_receipt_sha256": hash_file(run / "partial_parent_receipt.json"),
            "addendum_sha256": hash_file(run / "partial_audit_addendum.json"),
            "merged_audit_sha256": hash_file(run / "model_review.json"),
            "combined_source_proof_sha256": hash_file(run / "combined_source_review_proof.json"), **call_proof,
            "interpretation": "Authenticated completed partial parent answer plus new exact addendum and replayed source union. Parent failed status is retained; no whole-paper re-audit or scientific validation is asserted."}
        write_new_json(run / "partial_audit_completion_proof.json", proof)
        used = parent.receipt["used_model_seconds"] + call_proof["elapsed_seconds"]
        result = {"status": status, "stop_reason": stop, "model_calls": 1, "reading_tool_calls": tools.calls_started,
            "revisions": 0, "validation_error_count": errors, "reading_check_issues": checks,
            "human_validated": False, "graph_published": False, "scientific_accuracy": "not_established",
            "dataset_role": plan["cohort_role"], "partial_audit_completion_proof": proof,
            "scientific_audit_state": "completed_with_inherited_limitations" if scientific_complete else "incomplete",
            "reading_coverage_state": "source_disposition_correction_required" if stop == "coverage_correction_required" else "checked_with_limitations",
            "budget_lineage": {"original_total_model_seconds": parent.receipt["original_total_model_seconds"],
                "original_max_calls": parent.receipt["original_max_calls"], "used_model_seconds": used,
                "used_model_calls": 2, "remaining_model_seconds": max(0, parent.receipt["original_total_model_seconds"] - used),
                "remaining_model_calls": max(0, parent.receipt["original_max_calls"] - 2)},
            "quantitative_coverage_summary": quantitative_summary(parent.record),
            "history": [{"version": 0, "candidate_sha256": hash_file(run / "candidate.json"),
                "audit_origin": "partial_scientific_audit_composite", "validation_errors": errors,
                "needs_revision": bool(science_checks or errors), "reading_check_issues": checks}]}
        if addition := plan.get("budget_continuation"):
            from .budget_grants import addition_usage
            result["budget_addition"] = addition_usage(settings, run, plan, call_proof["elapsed_seconds"], 1)
        write_new_json(run / "result.json", result)
        records.store.event(run.name, "candidate_completed", {"status": status, "model_calls": 1})
    except BaseException as error:
        if not (run / "failure.json").exists():
            write_new_json(run / "failure.json", {"error": f"{type(error).__name__}: {error}", "model_calls": client.calls_started})
        records.store.event(run.name, "failed", {"error": str(error)})
        raise
    finally:
        client.close()
    return run


def verify_partial_audit_completion_run(settings, run, record, parsed, *, for_repair=False):
    """Scientific provenance may be valid while independent dossier gaps remain."""
    from .reader_workflow import reading_checks
    try:
        records, run, plan, parent = _authenticate_plan(settings, Path(run) / "plan.json")
        supplement = PartialAuditAddendum.model_validate(_json(run / "partial_audit_addendum.json"))
        result = _json(run / "result.json")
        proof = result["partial_audit_completion_proof"]
        call_proof, progress = _call_auth(records, run, plan, parent, supplement)
        union = _union(run, parent, call_proof, progress)
        merged, science = merge_completion(plan, parent, supplement, progress)
        if bool(science) != for_repair:
            return False
        gaps = reading_checks(parent.dossier, record, parsed, parent.documents)
        if any(not i.startswith("dossier ") for i in gaps):
            return False
        expected = {"policy": POLICY, "parent_receipt_sha256": hash_file(run / "partial_parent_receipt.json"),
            "addendum_sha256": hash_file(run / "partial_audit_addendum.json"),
            "merged_audit_sha256": hash_file(run / "model_review.json"),
            "combined_source_proof_sha256": hash_file(run / "combined_source_review_proof.json"), **call_proof,
            "interpretation": "Authenticated completed partial parent answer plus new exact addendum and replayed source union. Parent failed status is retained; no whole-paper re-audit or scientific validation is asserted."}
        used = parent.receipt["used_model_seconds"] + call_proof["elapsed_seconds"]
        budget = {"original_total_model_seconds": parent.receipt["original_total_model_seconds"],
            "original_max_calls": parent.receipt["original_max_calls"], "used_model_seconds": used,
            "used_model_calls": 2, "remaining_model_seconds": max(0, parent.receipt["original_total_model_seconds"] - used),
            "remaining_model_calls": max(0, parent.receipt["original_max_calls"] - 2)}
        from .budget_grants import addition_usage
        return (record == parent.record and parsed == merged and result.get("budget_lineage") == budget
            and result.get("budget_addition") == addition_usage(settings, run, plan, call_proof["elapsed_seconds"], 1)
            and proof == expected and _json(run / "partial_audit_completion_proof.json") == expected
            and _json(run / "combined_source_review_proof.json") == union
            and hash_file(run / "candidate.json") == parent.receipt["candidate_sha256"]
            and ReadingAudit.model_validate(_json(run / "audit.v00.json")) == merged
            and ReadingAudit.model_validate(_json(run / "model_review.json")) == merged
            and result.get("model_calls") == 1 and result.get("validation_error_count") == 0
            and result.get("human_validated") is False and result.get("graph_published") is False
            and result.get("reading_check_issues") == science + gaps
            and result.get("stop_reason") == ("partial_audit_completion_unresolved" if for_repair else "coverage_correction_required" if gaps else "audit_completed")
            and result.get("status") == ("candidate_incomplete_budget" if for_repair else "candidate_incomplete_reading_coverage" if gaps else "candidate_audited_with_limitations")
            and result.get("scientific_audit_state") == ("incomplete" if for_repair else "completed_with_inherited_limitations")
            and len(result.get("history", [])) == 1
            and result["history"][0].get("audit_origin") == "partial_scientific_audit_composite"
            and result["history"][0].get("needs_revision") is for_repair
            and result["history"][0].get("candidate_sha256") == parent.receipt["candidate_sha256"])
    except (ValueError, OSError, KeyError, TypeError, IndexError, AttributeError):
        return False
