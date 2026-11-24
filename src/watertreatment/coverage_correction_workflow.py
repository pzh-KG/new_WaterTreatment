"""Source-bound coverage-ledger continuation of an unchanged audited candidate.

Original runs are never edited. Scientific audit inheritance is authenticated
independently of the new disposition report; neither establishes scientific
validation. This module never invokes Neo4j or silently enables a provider.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .model_policy import plan_model, require_active_model
from .coverage_correction import (POLICY, EQUATION_CONTEXT_POLICY, VISUAL_CONTEXT_POLICY, CoverageCorrectionReport,
    build_coverage_correction_scope, corrected_dossier, coverage_correction_issues,
    coverage_correction_prompt)
from .domain import ExtractionRecord
from .reading import ReadingAudit, ReadingDossier
from .storage import DataStore, digest_json, hash_file, write_new_json
from .coverage_visual_inputs import POLICY as VISUAL_INPUTS, coverage_tool_limit, coverage_visual_bundle


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _copy_new(source, target):
    """Preserve the original JSON bytes, not only its parsed values."""
    with Path(target).open("xb") as stream:
        stream.write(Path(source).read_bytes())


@dataclass(frozen=True)
class CoverageParent:
    run: Path
    plan: dict
    bundle: dict
    documents: list
    record: ExtractionRecord
    dossier: ReadingDossier
    audit: ReadingAudit
    scope: dict
    receipt: dict


def authenticate_coverage_parent(settings, parent_run):
    from .audit_addendum_workflow import _completed_call_protocol
    from .reader_workflow import reading_checks
    from .staged_source_resume import _call
    from .validation import validate_record
    from .web_records import WebRecords
    from .workflow import validate_plan_metadata

    records = WebRecords(settings)
    run = records._safe_data_path(parent_run)
    if run.parent != (settings.data_root / "runs").resolve():
        raise ValueError("coverage parent must be an original run in the configured store")
    if not any(r["run_id"] == run.name and r["kind"] == "extraction" for r in records.store.runs()):
        raise ValueError("coverage parent is not a registered extraction run")
    plan, _, bundle, documents = records._source(run.name)
    if plan.get("workflow") == "reading_coverage_correction" and (run / "failure.json").is_file():
        from .coverage_resume import authenticate_failed_coverage
        return authenticate_failed_coverage(settings, run)
    if plan.get("workflow") == "partial-audit-remaining-resume-v1":
        # A completed, authenticated composite has its own full ancestry proof.
        # It must not be represented as a fictitious new single full-audit call.
        from .partial_audit_remaining_resume import authenticate_composite_coverage_parent
        return authenticate_composite_coverage_parent(settings, run)
    if plan.get("workflow") == "partial_scientific_audit_completion":
        from .coverage_audit_parent import authenticate_partial_coverage_parent
        return authenticate_partial_coverage_parent(settings, run)
    terminal = _json(run / "result.json")
    history = terminal.get("history", [])
    if (terminal.get("status") not in {"candidate_audited_with_limitations", "candidate_model_audited", "candidate_incomplete_reading_coverage"}
            or terminal.get("stop_reason") not in {"audit_completed", "coverage_correction_required"} or not history
            or terminal.get("human_validated") is not False or terminal.get("graph_published") is not False
            or plan.get("rule_version") != settings.rules_version
            or plan.get("coverage_correction_parent")):
        raise ValueError("coverage continuation requires an unpublished completed scientific audit")
    version = history[-1].get("version")
    if type(version) is not int or not 0 <= version <= 5:
        raise ValueError("coverage parent has an ambiguous candidate version")
    candidate_path = run / f"candidate.v{version:02d}.json"
    audit_path = run / f"audit.v{version:02d}.json"
    record = ExtractionRecord.model_validate(_json(candidate_path))
    audit = ReadingAudit.model_validate(_json(audit_path))
    if audit.review_scope in {"scientific_delta", "citation_delta"}:
        from .coverage_audit_parent import authenticate_incremental_coverage_parent
        return authenticate_incremental_coverage_parent(settings, run)
    dossier = ReadingDossier.model_validate(_json(run / "reading_dossier.json"))
    checks = reading_checks(dossier, record, audit, documents)
    gap = lambda issue: issue.startswith(("dossier lacks explicit coverage disposition for ",
                                          "dossier coverage references unknown source/blocks."))
    if (hash_file(candidate_path) != history[-1].get("candidate_sha256")
            or hash_file(run / "candidate.json") != hash_file(candidate_path)
            or hash_file(run / "model_review.json") != hash_file(audit_path)
            or audit.review_scope != "full" or audit.review_scope_sha256 is not None
            or audit.potential_omissions or any(f.needs_revision or f.verdict == "unsupported" for f in audit.findings)
            or not checks or any(not gap(issue) for issue in checks)
            or not terminal.get("reading_check_issues")
            or any(not gap(issue) for issue in terminal["reading_check_issues"])
            or terminal.get("validation_error_count") != 0
            or any(i.severity == "error" for i in [*validate_record(record, source_documents=documents),
                *validate_plan_metadata(record, plan, documents, store=records.store)])
            or not records._completed_audit_provenance(run, record, audit, allow_citation=False)):
        raise ValueError("parent has scientific/schema/identity/provenance defects beyond dossier coverage")
    matches = []
    for request_path in (run / "calls").glob("*/request.json"):
        call = request_path.parent
        if not (call / "answer.json").exists() or not (call / "result.json").exists():
            continue
        request = _json(request_path).get("request", {})
        if request.get("stage") != f"audit_{version}":
            continue
        try:
            if ReadingAudit.model_validate(_json(call / "answer.json")) != audit:
                continue
            _, completed, sources, _ = _call(records, run, request_path, plan,
                stage=f"audit_{version}", status="schema_valid_candidate")
            for raw_path, checksum in sources.items():
                if hash_file(records._safe_data_path(raw_path)) != checksum:
                    raise ValueError("parent audit input changed")
            events = _completed_call_protocol(run, completed, _json(call / "answer.json"))
            matches.append((call, events))
        except (ValueError, KeyError, OSError):
            continue
    if len(matches) != 1:
        raise ValueError("coverage parent needs one exact completed source-bound full-audit call")
    scope = build_coverage_correction_scope(dossier, documents)
    paths = [run / name for name in ("plan.json", "sources.json", "result.json", "candidate.json",
        "model_review.json", "reading_dossier.json")]
    paths += [candidate_path, audit_path] + [matches[0][0] / n for n in ("request.json", "answer.json", "result.json")]
    receipt = {"policy": POLICY, "parent_run": run.name, "parent_version": version,
        "candidate_path": str(candidate_path), "candidate_sha256": hash_file(candidate_path),
        "audit_path": str(audit_path), "audit_sha256": hash_file(audit_path),
        "dossier_path": str(run / "reading_dossier.json"), "dossier_sha256": hash_file(run / "reading_dossier.json"),
        "source_bundle_sha256": hash_file(run / "sources.json"),
        "completed_full_audit_call": str(matches[0][0]), "events_sha256": matches[0][1],
        "artifact_hashes": [{"path": str(p), "sha256": hash_file(p)} for p in paths],
        "original_reading_check_issues": terminal["reading_check_issues"],
        "replayed_reading_check_issues": checks, "scientific_validation": "not_established"}
    return CoverageParent(run, plan, bundle, documents, record, dossier, audit, scope, receipt)


def _prompt(parent):
    overview = {"article_question_and_design": parent.dossier.article_question_and_design,
        "cross_source_conflicts": parent.dossier.cross_source_conflicts,
        "unresolved_questions": parent.dossier.unresolved_questions,
        "inherited_audit_limitations": parent.audit.coverage_limitations}
    prompt = coverage_correction_prompt(parent.scope) + "\nINHERITED FALLIBLE CONTEXT (not source evidence):\n" + json.dumps(overview, ensure_ascii=False)
    if prior := parent.receipt.get("coverage_resume"):
        prompt += ("\nResume the remaining source dispositions only. Revisit every prior concern against the unchanged "
            "candidate and original blocks. Return one indexed concern_reviews entry per concern with exact quotes "
            "or actual visual locators. If a correction or further evidence is needed, mark it accordingly. "
            "Previous limitations remain in force. Prior concerns and limitations:\n"
            + json.dumps([prior["scientific_concerns"], prior["source_limitations"]], ensure_ascii=False))
    return prompt


def _single_attempt(store, parent_run, current=None):
    for row in store.runs():
        manifest = _json(row["manifest_path"])
        if (manifest.get("operation") == POLICY and manifest.get("parent_run") == parent_run
                and row["run_id"] != current):
            raise ValueError("parent already has a retained coverage attempt; do not silently reset its bounded budget")


def prepare(settings, parent_run, *, timeout_seconds=900, validation_policy=VISUAL_CONTEXT_POLICY, budget_grant=None):
    """Return a new immutable plan path; does not enable or call a provider."""
    from .codex import code_identity
    from .workflow import runtime_versions

    parent = authenticate_coverage_parent(settings, parent_run)
    from .coverage_resume import report_type
    if validation_policy not in {POLICY, EQUATION_CONTEXT_POLICY, VISUAL_CONTEXT_POLICY}:
        raise ValueError("unknown coverage-disposition validation contract")
    if type(timeout_seconds) is not int or not 60 <= timeout_seconds <= 1200:
        raise ValueError("coverage correction requires an explicit 60–1200 second one-call budget")
    require_active_model(settings.codex)
    store = DataStore(settings.data_root)
    _single_attempt(store, parent.run.name)
    model_required = bool(parent.scope["required_dispositions"])
    if parent.receipt.get("coverage_resume") and model_required:
        from .budget_grants import preparation_grant, retained_stage_budget
        grant = preparation_grant(settings, parent_run, budget_grant)
        authorized = ({"remaining_calls": grant["allocated_calls"], "remaining_seconds": grant["allocated_seconds"]}
                      if grant else retained_stage_budget(settings, parent_run))
        if authorized["remaining_calls"] < 1 or timeout_seconds > min(900, authorized["remaining_seconds"]):
            raise ValueError("本次阅读补正超过单篇剩余额度，请先检查追加额度")
    run = store.new_run("extraction", {"paper_id": parent.record.paper.id,
        "cohort_role": parent.plan["cohort_role"], "rule_version": parent.plan["rule_version"],
        "schema_version": parent.record.schema_version, "workflow": "reading_coverage_correction",
        "operation": POLICY, "parent_run": parent.run.name})
    effective_bundle = coverage_visual_bundle(parent, run, create=True)
    if effective_bundle == _json(parent.run / "sources.json"):
        _copy_new(parent.run / "sources.json", run / "sources.json")
    else:
        write_new_json(run / "sources.json", effective_bundle)
    for name, source in {"candidate.v00.json": Path(parent.receipt["candidate_path"]),
            "reading_dossier.json": Path(parent.receipt.get("dossier_path", parent.run / "reading_dossier.json")),
            "coverage_parent_audit.json": Path(parent.receipt["audit_path"])}.items():
        _copy_new(source, run / name)
    for name, value in {"coverage_parent_receipt.json": parent.receipt,
            "coverage_correction_scope.json": parent.scope,
            "response_schema.json": report_type(parent).model_json_schema()}.items():
        write_new_json(run / name, value)
    (run / "coverage_correction_prompt.txt").write_text(_prompt(parent), encoding="utf-8")
    paths = [p for p in run.iterdir() if p.is_file() and p.name != "manifest.json"]
    paths += [Path(d.source.path) for d in parent.documents]
    from .workflow import _image_manifest
    paths += [Path(i["path"]) for i in _image_manifest(effective_bundle)]
    paths = list(dict.fromkeys(paths))
    model_settings = {**settings.codex.model_dump(mode="json"), "max_calls_per_run": 1,
                      "timeout_seconds": timeout_seconds}
    # Source rereads are bounded by document block ranges, with modest context/
    # image allowance. There is no candidate-query or rewrite loop.
    source_limit = coverage_tool_limit(parent.scope)
    plan = {"version": 1, "run_id": run.name, "paper_id": parent.record.paper.id,
        "cohort_role": parent.plan["cohort_role"], "rule_version": parent.plan["rule_version"],
        "schema_version": parent.record.schema_version, "workflow": "reading_coverage_correction",
        "workdir": str(run), "input_paths": [{"path": str(p), "sha256": hash_file(p)} for p in paths],
        "reading_artifacts": [], "image_paths": [], "code_identity": code_identity(settings.project_root),
        "runtime": runtime_versions(), "model_settings": model_settings, "max_calls": int(model_required),
        "reading_settings": {"enabled": True, "max_revisions": 0, "max_tool_calls": source_limit,
            "total_timeout_seconds": timeout_seconds}, "coverage_correction_parent": parent.receipt,
        "status": "prepared_no_model_calls", "extraction_policy": parent.plan.get("extraction_policy"),
        "coverage_validation_policy": validation_policy, "coverage_visual_inputs": VISUAL_INPUTS}
    if budget_grant:
        plan["budget_grant_preparation"] = budget_grant
    write_new_json(run / "plan.json", plan)
    store.event(run.name, "prepared", {"plan_sha256": hash_file(run / "plan.json")})
    store.record_development_use(run.name, parent.record.paper.id,
        [d.source.sha256 for d in parent.documents],
        "Engineering coverage-ledger continuation; not independent validation. Original candidate/cohort preserved; parent=" + parent.run.name)
    return run / "plan.json"


def _authenticate_plan(settings, plan_path):
    from .web_records import WebRecords
    records = WebRecords(settings)
    path = records._safe_data_path(plan_path)
    run = path.parent
    if path.name != "plan.json" or run.parent != (settings.data_root / "runs").resolve():
        raise ValueError("coverage correction plan is outside the configured run store")
    plan, _, bundle, _ = records._source(run.name)
    receipt = plan["coverage_correction_parent"]
    rows = {r["run_id"]: r["created_at"] for r in records.store.runs()}
    if receipt["parent_run"] not in rows or rows[receipt["parent_run"]] >= rows[run.name]:
        raise ValueError("coverage continuation must reference an earlier registered run")
    parent = authenticate_coverage_parent(settings, settings.data_root / "runs" / receipt["parent_run"])
    if "coverage_visual_inputs" in plan:
        from .workflow import _image_manifest
        expected_bundle = coverage_visual_bundle(parent, run, create=False)
        inputs = {item["path"]: item["sha256"] for item in plan["input_paths"]}
        if (plan["coverage_visual_inputs"] != VISUAL_INPUTS or expected_bundle != bundle
                or plan["reading_settings"]["max_tool_calls"] != coverage_tool_limit(parent.scope)
                or any(inputs.get(i["path"]) != hash_file(records._safe_data_path(Path(i["path"])))
                       for i in _image_manifest(bundle))):
            raise ValueError("coverage visual input or scoped tool limit changed")
    elif hash_file(run / "sources.json") != receipt["source_bundle_sha256"] or bundle != parent.bundle:
        raise ValueError("original coverage source bundle changed")
    if (plan.get("coverage_validation_policy", POLICY) not in {POLICY, EQUATION_CONTEXT_POLICY, VISUAL_CONTEXT_POLICY}
            or receipt != parent.receipt or _json(run / "coverage_parent_receipt.json") != receipt
            or _json(run / "coverage_correction_scope.json") != parent.scope
            or hash_file(run / "candidate.v00.json") != receipt["candidate_sha256"]
            or hash_file(run / "coverage_parent_audit.json") != receipt["audit_sha256"]
            or hash_file(run / "reading_dossier.json") != receipt["dossier_sha256"]
            or (run / "coverage_correction_prompt.txt").read_text(encoding="utf-8") != _prompt(parent)
            or plan["max_calls"] != int(bool(parent.scope["required_dispositions"]))
            or plan["model_settings"]["max_calls_per_run"] != 1
            or plan_model(plan) is None
            or not 60 <= plan["reading_settings"]["total_timeout_seconds"] <= 1200
            or plan["model_settings"]["timeout_seconds"] != plan["reading_settings"]["total_timeout_seconds"]):
        raise ValueError("coverage correction parent, frozen inputs or limits changed")
    return records, run, plan, parent


def _substantive_candidate_read(records, run, parent, call, request, sources):
    for path in (run / "reading_tools").glob("*/request.json"):
        query = _json(path)
        if query.get("tool") != "wt_query_candidate" or query.get("source_review_call") != call.name:
            continue
        response = _json(path.with_name("result.json"))
        data = response.get("data", {})
        if (response.get("success") is True and data.get("source_review_call") == call.name
                and data.get("candidate_binding") == request.get("candidate_binding")
                and type(data.get("returned_count")) is int and data["returned_count"] > 0
                and data.get("mode") in {"path", "paths", "section"}
                and records._queried_candidate_snapshot(run, parent.record, request, sources)):
            return True
    return False


def _call_auth(records, run, plan, parent, report):
    from .coverage_resume import report_type
    from .audit_addendum_workflow import _completed_call_protocol
    from .source_review import verify_source_review
    from .staged_source_resume import _call
    calls = list((run / "calls").glob("*/request.json"))
    if len(calls) != 1:
        raise ValueError("coverage correction requires exactly one completed bounded call")
    call = calls[0].parent
    request, completed, sources, _ = _call(records, run, calls[0], plan,
        stage="coverage_correction_0", status="schema_valid_candidate")
    receipt = {"scope_path": str(run / "coverage_correction_scope.json"),
               "scope_file_sha256": hash_file(run / "coverage_correction_scope.json")}
    if (report_type(parent).model_validate(_json(call / "answer.json")) != report
            or request.get("citation_review") != receipt or request.get("source_review_call") != call.name
            or request.get("source_review_scope_sha256") != parent.scope["scope_sha256"]
            or not verify_source_review(run, parent.scope, completed.get("source_review_proof"), call.name)):
        raise ValueError("coverage model declaration lacks authenticated current source delivery")
    if any(d.category == "substantive" for d in report.dispositions) or getattr(report, "concern_reviews", []):
        if not _substantive_candidate_read(records, run, parent, call, request, sources):
            raise ValueError("substantive source correction requires current access to the exact immutable candidate")
    for raw_path, checksum in sources.items():
        if hash_file(records._safe_data_path(raw_path)) != checksum:
            raise ValueError("coverage source input changed after the model call")
    event_hash = _completed_call_protocol(run, completed, _json(call / "answer.json"))
    return {"completed_call": str(call), "events_sha256": event_hash,
        "source_review_proof_sha256": completed["source_review_proof"]["proof_sha256"]}


def delivered_context(run, scope, policy):
    if policy != VISUAL_CONTEXT_POLICY:
        return set()
    from .partial_audit_completion_workflow import _source_receipts
    from .source_review import partial_source_review_progress
    calls = list((run / "calls").glob("*/result.json"))
    if not calls:
        return set()
    if len(calls) != 1:
        raise ValueError("coverage context requires one source-bound reading turn")
    key = calls[0].parent.name
    progress = partial_source_review_progress(run, scope, _source_receipts(run, key), key)
    return {(x["source_id"], x["block_id"]) for x in progress["delivered"]}


def execute(settings, plan_path, *, client=None):
    """Exactly one explicit provider call, or zero for purely structural gaps."""
    from .codex import code_identity
    from .coverage_resume import report_type, concern_issues
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
    model_required = bool(plan["max_calls"])
    from .budget_grants import verify_lineage_plan
    verify_lineage_plan(settings, run, plan)
    if plan.get("budget_continuation"):
        from .budget_extension_workflow import verify_extension_plan
        verify_extension_plan(settings, run, plan)
    if parent.receipt.get("coverage_resume") and model_required:
        from .budget_grants import prepared_grant, retained_stage_budget
        grant = prepared_grant(settings, run, plan, parent.run)
        allowed = retained_stage_budget(settings, parent.run) if not grant else {
            "remaining_calls": grant["allocated_calls"], "remaining_seconds": grant["allocated_seconds"]}
        if allowed["remaining_calls"] < 1 or plan["reading_settings"]["total_timeout_seconds"] > min(900, allowed["remaining_seconds"]):
            raise ValueError("阅读补正的单篇累计额度发生变化")
    if (plan["code_identity"] != code_identity(settings.project_root) or plan["runtime"] != runtime_versions()
            or (run / "result.json").exists() or (run / "calls").exists()
            or (model_required and (client is None or client.calls_started != 0
                or client.settings.model_dump(mode="json") != plan["model_settings"]
                or client.total_timeout_seconds != plan["reading_settings"]["total_timeout_seconds"]))):
        raise ValueError("coverage execution needs the frozen code and an explicitly enabled fresh bounded client")
    tools = None
    try:
        records.store.event(run.name, "execution_started", {"operation": POLICY, "max_calls": plan["max_calls"]})
        if model_required:
            tools = ReadingTools(_json(run / "sources.json"), run, max_calls=plan["reading_settings"]["max_tool_calls"],
                expected_inputs=plan["input_paths"])
            tools.bind_candidate(parent.record, run / "candidate.v00.json")
            client.bind_tools(tools)
            report = client.complete(report_type(parent),
                prompt=(run / "coverage_correction_prompt.txt").read_text(encoding="utf-8"),
                input_paths=[Path(x["path"]) for x in plan["input_paths"]], workdir=run,
                audit_root=run / "calls", identity={"stage": "coverage_correction_0", "rule_version": plan["rule_version"],
                    "code_identity": plan["code_identity"], "runtime": plan["runtime"],
                    "citation_review": {"scope_path": str(run / "coverage_correction_scope.json"),
                        "scope_file_sha256": hash_file(run / "coverage_correction_scope.json")}},
                image_paths=[], conversation_key="coverage-correction-0")
            if client.calls_started != 1:
                raise ValueError("coverage correction exceeded one model call")
            if getattr(client, "events", None):
                client.events.flush()
            call_proof = _call_auth(records, run, plan, parent, report)
        else:
            report = report_type(parent)(scope_sha256=parent.scope["scope_sha256"], dispositions=[],
                **({"concern_reviews": []} if parent.receipt.get("coverage_resume") else {}))
            call_proof = {"completed_call": None, "reason": "Only empty structural blocks/invalid identifiers; no reading or model call asserted."}
        write_new_json(run / "coverage_correction.v01.json", report.model_dump(mode="json"))
        effective = corrected_dossier(parent.scope, report, parent.dossier, parent.documents)
        write_new_json(run / "reading_dossier.corrected.v01.json", effective.model_dump(mode="json"))
        checks = coverage_correction_issues(parent.scope, report, parent.dossier, parent.documents,
            validation_policy=plan.get("coverage_validation_policy", POLICY),
            delivered=delivered_context(run, parent.scope, plan.get("coverage_validation_policy")))
        checks += reading_checks(effective, parent.record, parent.audit, parent.documents)
        checks += concern_issues(parent, report)
        issues = validate_record(parent.record, source_documents=parent.documents)
        errors = sum(x.severity == "error" for x in issues)
        for name, source in {"candidate.json": run / "candidate.v00.json",
                "audit.v00.json": run / "coverage_parent_audit.json",
                "model_review.json": run / "coverage_parent_audit.json"}.items():
            _copy_new(source, run / name)
        proof = {"policy": POLICY, "parent_receipt_sha256": hash_file(run / "coverage_parent_receipt.json"),
            "original_dossier_sha256": parent.receipt["dossier_sha256"],
            "correction_sha256": hash_file(run / "coverage_correction.v01.json"),
            "corrected_dossier_sha256": hash_file(run / "reading_dossier.corrected.v01.json"),
            "scope_sha256": parent.scope["scope_sha256"], **call_proof,
            **({"validation_policy": plan["coverage_validation_policy"]} if "coverage_validation_policy" in plan else {}),
            "interpretation": "Inherited completed scientific source audit plus a separate source-bound coverage-ledger correction; not a new scientific audit or scientific validation."}
        write_new_json(run / "coverage_correction_proof.json", proof)
        write_new_json(run / "validation.json", {"issues": [x.model_dump(mode="json") for x in issues],
            "semantic_accuracy": "not_established"})
        result = {"status": "candidate_audited_with_limitations" if not checks and not errors else "candidate_incomplete_budget",
            "stop_reason": "audit_completed" if not checks and not errors else "coverage_correction_unresolved",
            "model_calls": int(model_required), "reading_tool_calls": tools.calls_started if tools else 0,
            "revisions": 0, "validation_error_count": errors, "reading_check_issues": checks,
            "human_validated": False, "graph_published": False, "scientific_accuracy": "not_established",
            "dataset_role": plan["cohort_role"], "coverage_correction_proof": proof,
            "coverage_source_limitations": list(dict.fromkeys(parent.receipt.get("coverage_resume", {}).get("source_limitations", []) + report.source_limitations)),
            "quantitative_coverage_summary": quantitative_summary(parent.record),
            "review_mode": "inherited_scientific_audit_plus_reading_disposition_correction",
            "history": [{"version": 0, "candidate_sha256": hash_file(run / "candidate.json"),
                "audit_origin": "inherited_scientific_audit_with_coverage_correction",
                "validation_errors": errors, "needs_revision": bool(checks or errors), "reading_check_issues": checks}]}
        write_new_json(run / "result.json", result)
        records.store.event(run.name, "candidate_completed", {"status": result["status"], "model_calls": int(model_required)})
    except BaseException as error:
        if not (run / "failure.json").exists():
            write_new_json(run / "failure.json", {"error": f"{type(error).__name__}: {error}",
                "model_calls": client.calls_started if client else 0})
        records.store.event(run.name, "failed", {"error": str(error)})
        raise
    finally:
        if client is not None:
            client.close()
    return run


def verify_coverage_correction_run(settings, run, record, parsed):
    """Replay the parent audit, immutable byte copies and current reading proof."""
    from .reader_workflow import reading_checks
    from .coverage_resume import report_type, concern_issues
    try:
        records, run, plan, parent = _authenticate_plan(settings, Path(run) / "plan.json")
        report = report_type(parent).model_validate(_json(run / "coverage_correction.v01.json"))
        if concern_issues(parent, report):
            return False
        result = _json(run / "result.json")
        if parent.receipt.get("coverage_resume") and result.get("coverage_source_limitations") != list(dict.fromkeys(
                parent.receipt["coverage_resume"]["source_limitations"] + report.source_limitations)):
            return False
        proof = result["coverage_correction_proof"]
        effective = corrected_dossier(parent.scope, report, parent.dossier, parent.documents)
        if coverage_correction_issues(parent.scope, report, parent.dossier, parent.documents,
            validation_policy=plan.get("coverage_validation_policy", POLICY),
            delivered=delivered_context(run, parent.scope, plan.get("coverage_validation_policy"))):
            return False
        if reading_checks(effective, record, parsed, parent.documents):
            return False
        call_proof = (_call_auth(records, run, plan, parent, report) if plan["max_calls"] else
            {"completed_call": None, "reason": "Only empty structural blocks/invalid identifiers; no reading or model call asserted."})
        expected = {"policy": POLICY, "parent_receipt_sha256": hash_file(run / "coverage_parent_receipt.json"),
            "original_dossier_sha256": parent.receipt["dossier_sha256"],
            "correction_sha256": hash_file(run / "coverage_correction.v01.json"),
            "corrected_dossier_sha256": hash_file(run / "reading_dossier.corrected.v01.json"),
            "scope_sha256": parent.scope["scope_sha256"], **call_proof,
            **({"validation_policy": plan["coverage_validation_policy"]} if "coverage_validation_policy" in plan else {}),
            "interpretation": "Inherited completed scientific source audit plus a separate source-bound coverage-ledger correction; not a new scientific audit or scientific validation."}
        return (record == parent.record and parsed == parent.audit and proof == expected
            and _json(run / "coverage_correction_proof.json") == expected
            and ReadingDossier.model_validate(_json(run / "reading_dossier.corrected.v01.json")) == effective
            and hash_file(run / "candidate.json") == parent.receipt["candidate_sha256"]
            and hash_file(run / "audit.v00.json") == parent.receipt["audit_sha256"]
            and hash_file(run / "model_review.json") == parent.receipt["audit_sha256"]
            and result.get("status") == "candidate_audited_with_limitations"
            and result.get("stop_reason") == "audit_completed" and result.get("reading_check_issues") == []
            and result.get("model_calls") == plan["max_calls"] and result.get("validation_error_count") == 0
            and result.get("human_validated") is False and result.get("graph_published") is False
            and len(result.get("history", [])) == 1
            and result["history"][0].get("audit_origin") == "inherited_scientific_audit_with_coverage_correction"
            and result["history"][0].get("candidate_sha256") == parent.receipt["candidate_sha256"])
    except (ValueError, OSError, KeyError, TypeError, IndexError, AttributeError):
        return False
