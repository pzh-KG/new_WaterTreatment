"""Full comprehension -> extraction -> fresh audit -> bounded evidence-based repair."""
from __future__ import annotations

import json
from pathlib import Path

from .domain import ExtractionRecord
from .reading import AUDIT_DIMENSIONS, ReadingAudit, ReadingDossier
from .reading_tools import ReadingTools
from .review_packet import review_packet
from .storage import digest_json, hash_file, write_new_json
from .validation import validate_record
from .quantitative import quantitative_summary
from .audit_followups import quantitative_followup_issues
from .audit_execution import prepare_citation_review, citation_review_prompt, citation_review_issues, citation_paths


def reading_checks(dossier, candidate, audit, documents):
    """Check traceability and explicit coverage, without asserting semantic truth."""
    known = {d.source.source_id: {b.block_id for b in d.blocks} for d in documents}
    issues = []
    if dossier.paper_id != candidate.paper.id:
        issues.append("Reading dossier paper identity differs from candidate.")
    for label, entries in (("dossier", dossier.coverage), ("candidate", candidate.coverage)):
        seen = {key: set() for key in known}
        for entry in entries:
            if entry.source_id not in known or not set(entry.block_ids).issubset(known.get(entry.source_id, set())):
                issues.append(f"{label} coverage references unknown source/blocks.")
            # One malformed identifier must not erase the valid dispositions in
            # the same entry. Unknown identifiers still block authentication.
            if entry.source_id in known:
                seen[entry.source_id].update(set(entry.block_ids) & known[entry.source_id])
        for source_id, blocks in known.items():
            missing = blocks - seen[source_id]
            if missing:
                issues.append(f"{label} lacks explicit coverage disposition for {len(missing)} blocks in {source_id}: " + ", ".join(sorted(missing)))
    scope_outline = getattr(dossier, "scope_outline", [])
    anchors = [a for entry in [*dossier.material_map, *dossier.experiment_map, *scope_outline] for a in entry.evidence]
    if candidate.rule_version in {"0.8", "0.9"} and not scope_outline:
        issues.append("dossier lacks the explicit three-layer research-scope outline required by the design scope.")
    if audit:
        anchors += [a for finding in audit.findings for a in finding.evidence]
        missing = set(AUDIT_DIMENSIONS) - set(audit.checked_dimensions)
        if missing:
            issues.append("Audit dimensions missing: " + ", ".join(sorted(missing)))
    for anchor in anchors:
        if anchor.block_id not in known.get(anchor.source_id, set()):
            issues.append(f"Unknown reading/audit anchor: {anchor.source_id}/{anchor.block_id}")
    issues.extend(quantitative_followup_issues(candidate, audit, documents))
    return issues


def execute_reader(settings, plan, run, inputs, documents, store, client):
    from .workflow import validate_plan_metadata
    from .reading_budget import validate_reading_tool_budget
    tool_budget = None
    if "reading_tool_budget" in plan:
        seed = ExtractionRecord.model_validate_json((run / "reused_candidate.json").read_bytes()) if plan.get("reused_candidate") else None
        tool_budget = validate_reading_tool_budget(plan["reading_tool_budget"], seed)
        if (tool_budget["source_limit"] != plan["reading_settings"]["max_tool_calls"]
                or tool_budget["max_model_turns"] != plan["max_calls"]):
            raise ValueError("reading tool budget differs from the frozen workflow limits")
    bundle = json.loads((run / "sources.json").read_text(encoding="utf-8"))
    tools = ReadingTools(bundle, run,
                         max_calls=plan["reading_settings"]["max_tool_calls"], expected_inputs=plan["input_paths"],
                         candidate_query_limit=tool_budget["candidate_query_limit"] if tool_budget else None,
                         submission_reserve=plan.get("revision_submission_reserve",0) if tool_budget else 0)
    client.bind_tools(tools)
    images = [Path(p) for p in plan["image_paths"]]
    identity = {"code_identity": plan["code_identity"], "rule_version": plan["rule_version"], "runtime": plan["runtime"]}
    context = (run / "reading_context.txt").read_text(encoding="utf-8")
    rules = (run / "extract_prompt.txt").read_text(encoding="utf-8").split("\n\nDELIVERY:", 1)[0]
    interrupted = {}
    if plan.get("initial_recovery"):
        interrupted = json.loads((run/"initial_recovery.json").read_bytes()).get("public_progress", {})
    prior_views = interrupted.get("source_views", [])[:12]
    first_audit = None
    if plan.get("interrupted_first_audit"):
        from .interrupted_reading import verify_first_audit_progress
        first_audit = verify_first_audit_progress(settings,run,plan)

    if plan.get("measurement_navigation"):
        identity["measurement_navigation"] = plan["measurement_navigation"]

    def complete(model, prompt, stage, *, key="reader", attach=False, citation=None, shared_reader=False):
        store.event(run.name, "reading_stage_started", {"stage": stage, "model_calls_started": client.calls_started})
        attachments = images if attach else []
        if stage == "comprehension" and attach:
            attachments = [*attachments,*(Path(v["image_path"]) for v in prior_views)]
        elif stage == "audit_0" and first_audit:
            attachments = [Path(v["image_path"]) for v in first_audit["source_views"][:12]]
        output = client.complete(model, prompt=prompt, input_paths=inputs + citation_paths(citation), workdir=run,
            audit_root=run / "calls", identity={**identity, "stage": stage,
                **({"same_reader_source_context": True} if shared_reader else {}),
                **({"interrupted_first_audit":plan["interrupted_first_audit"]} if stage=="audit_0" and first_audit else {}),
                **({"citation_review": citation[0]} if citation else {})},
            image_paths=attachments, conversation_key=key)
        store.event(run.name, "reading_stage_completed", {"stage": stage, "model_calls_started": client.calls_started})
        return output

    from .measurement_review import POLICY as ENDPOINT_POLICY, READING_GUIDE, EXTRACTION_GUIDE, audit_navigation_text, measurement_navigation
    focused = plan.get("measurement_navigation") == ENDPOINT_POLICY
    reused = bool(plan.get("reused_dossier"))
    if reused:
        dossier = ReadingDossier.model_validate_json((run / "reading_dossier.json").read_bytes())
        store.event(run.name, "reading_dossier_reused", plan["reused_dossier"])
    else:
        from .interrupted_reading import progress_prompt
        navigation = progress_prompt(interrupted, crops=prior_views) if interrupted else ""
        dossier = complete(ReadingDossier, (run / "understand_prompt.txt").read_text(encoding="utf-8") + (READING_GUIDE if focused else "") + context + navigation,
                           "comprehension", attach=True)
        from .identity_resume import bind_dossier_identity
        dossier = bind_dossier_identity(dossier, plan["paper_id"], run)
        write_new_json(run / "reading_dossier.json", dossier.model_dump(mode="json"))
    from .coverage_correction import apply_structural_correction
    dossier, structural_proof = apply_structural_correction(run, dossier, documents)
    from .coverage_transport import dossier_for_prompt
    dossier_context = dossier_for_prompt(dossier, bundle)
    resumed = bool(plan.get("reused_candidate"))
    if resumed:
        candidate = ExtractionRecord.model_validate_json((run / "reused_candidate.json").read_bytes())
        store.event(run.name, "reading_candidate_resumed", plan["reused_candidate"])
        if plan.get("audit_guidance"):
            from .audit_guidance_resume import verify_guidance
            verify_guidance(settings, run, plan)
    else:
        # Fresh extraction continues the SAME reader conversation. Repeat the authenticated
        # dossier if it came from another turn lineage or its recorded contents were corrected.
        context_check = getattr(client, "has_reader_context", None)
        same_thread = focused and not reused and callable(context_check) and context_check("reader")
        shared_dossier = same_thread and not structural_proof and not (run / "dossier_identity_binding.json").exists()
        send_sources = reused or (focused and not same_thread)
        handoff = (("Use your immediately preceding reading dossier and all original sources already delivered. "
                    "Reuse adequate views in this conversation. Reopen the exact panel or method wherever its "
                    "ownership, quality or interpretation remains uncertain.\n")
                   if shared_dossier else dossier_context)
        guidance = (EXTRACTION_GUIDE if focused else "")
        candidate = complete(ExtractionRecord, rules + guidance +
            "\nThe reading dossier is fallible. Resolve consequential uncertainties against original sources. "
            "Produce the complete ExtractionRecord with literal block IDs.\n" + handoff +
            (context if send_sources else ""), "extraction", attach=send_sources, shared_reader=same_thread)

    # Explicitly opted-in continuation repairs precede the first new audit. The
    # immutable staged seed is retained, and the full original baseline (not this
    # repair output) remains the authority for inherited coverage.
    if plan.get("continuation_repairs"):
        from .continuation_repair import repair_contract, source_navigation
        if not resumed or not plan.get("resumed_full_audit"):
            raise ValueError("targeted continuation requires a source-authenticated candidate and full baseline")
        contract = (run / "continuation_repair_prompt.txt").read_text(encoding="utf-8")
        if contract != repair_contract(plan["continuation_repairs"]):
            raise ValueError("continuation repair contract differs from the frozen supported targets")
        initial = candidate
        tools.bind_candidate(initial, run / "reused_candidate.json")
        tools.prepare_revision(initial)
        initial_issues = validate_record(initial, source_documents=documents)
        candidate = complete(ExtractionRecord, contract + "\nSCIENTIFIC RULES:\n" + rules +
            source_navigation(bundle) +
            "\nINHERITED DOSSIER (fallible, not new source evidence):\n" + dossier_context +
            "\nMACHINE CHECKS (warnings are questions, not automatic source conclusions):\n" +
            json.dumps(tools.validation_feedback(initial_issues, candidate=initial), ensure_ascii=False),
            "continuation_repair_0", key="continuation-repair-0")
        write_new_json(run / "continuation_repair.json", {
            "targets": plan["continuation_repairs"],
            "input_path": str(run / "reused_candidate.json"), "input_sha256": hash_file(run / "reused_candidate.json"),
            "input_content_hash": digest_json(initial.model_dump(mode="json")),
            "output_content_hash": digest_json(candidate.model_dump(mode="json")),
            "status": "model_repaired_requires_source_audit", "human_validated": False,
            "graph_published": False})
    audit = None
    revision = 0
    history = []
    stop_reason = None
    protocol_retries = 0
    while True:
        version_path = run / f"candidate.v{revision:02d}.json"
        write_new_json(version_path, candidate.model_dump(mode="json"))
        issues = validate_record(candidate, source_documents=documents)
        issues.extend(validate_plan_metadata(candidate, plan, documents, store=store))
        serialized = [issue.model_dump(mode="json") for issue in issues]
        write_new_json(run / f"validation.v{revision:02d}.json", {"issues": serialized, "semantic_accuracy": "not_established"})
        feedback = tools.validation_feedback(issues, candidate=candidate) if candidate.rule_version in {"0.7", "0.8", "0.9"} else serialized
        if client.calls_started >= plan["max_calls"]:
            stop_reason = "turn_budget_before_audit"
            audit = None
            break
        reuse_repair_audit = bool(revision == 0 and resumed and plan["reused_candidate"].get("repair_audit"))
        scope_diagnostics = []
        citation = (prepare_citation_review(run, revision, candidate, history, plan=plan,
                        diagnostics=scope_diagnostics, settings=settings)
                    if not reuse_repair_audit and not (revision == 0 and plan.get("resumed_full_audit")) else None)
        if revision == 0 and plan.get("resumed_full_audit"):
            from .audit_resume import prepare_resumed_review
            citation = prepare_resumed_review(settings, plan, run, candidate)
            reuse_repair_audit = False
        if revision > 0 and citation is None and candidate.rule_version in {"0.7", "0.8", "0.9"}:
            from .audit_execution import ScientificReviewScopeUnavailable
            diagnostic = {"version": revision, "candidate_sha256": hash_file(version_path),
                "status": "recoverable_scope_block", "automatic_full_audit": False,
                "diagnostics": scope_diagnostics or [{"code": "local_scope_unavailable",
                    "message": "The completed repair has no authenticated bounded audit scope.",
                    "path": None, "recoverable": True}]}
            write_new_json(run / f"scope_rejection.v{revision:02d}.json", diagnostic)
            raise ScientificReviewScopeUnavailable(diagnostic["diagnostics"])
        if revision > 0 and citation is None and candidate.rule_version not in {"0.7", "0.8", "0.9"}:
            store.event(run.name, "legacy_full_audit_policy", {"rule_version": candidate.rule_version,
                "reason": "Retained historical reading contract; bounded scientific review is supported from rule 0.7."})
        if (revision > 0 and (plan.get("resumed_full_audit") or {}).get("policy") == "bounded-scientific-revision-v1"
                and citation is None):
            raise ValueError("Continuation repairs no longer have a safe cumulative local review scope; retained candidate requires explicit review planning, not an automatic whole-paper restart")
        if not reuse_repair_audit or plan.get("audit_guidance"):
            tools.bind_candidate(candidate, version_path)
        audit_context = context
        pending_first = revision == 0 and first_audit and not citation
        if pending_first:
            from .interrupted_reading import progress_prompt
            audit_context = (progress_prompt(first_audit,crops=first_audit["source_views"][:12]) +
                "\nFinish the first source-audit report on this unchanged candidate. Previous candidate lookups below "
                "are navigation, not approved verdicts. Check the unresolved dimensions, use stored source views and "
                "look up only the outstanding original evidence. Preserve actual conflicts and missing information. "
                "Report anything insufficiently supported. Do not start another paper-wide reading pass.\n"
                + json.dumps([q["arguments"] for q in first_audit["candidate_queries"][:100]],
                             ensure_ascii=False,separators=(",",":")))
        if citation and citation[1].get("policy") in {"bounded-revision-audit-v1", "bounded-scientific-revision-v1"}:
            from .audit_context import bounded_source_context
            audit_context = bounded_source_context(run, citation[1])
        measurement_context = ""
        if focused and not citation and not pending_first and dossier.printed_endpoints:
            navigation_report = measurement_navigation(dossier, candidate, documents)
            write_new_json(run / f"printed_endpoints.v{revision:02d}.json", navigation_report)
            from .source_delivery import MAX_PROMPT_BYTES
            planned = plan["prompt_budget_preflight"]["stages"]["full_audit"]["planned_utf8_bytes"]
            measurement_context = audit_navigation_text(navigation_report, min(24000, max(0, MAX_PROMPT_BYTES - planned)))
        audit_request = ((citation_review_prompt(citation, candidate) if citation else
            (run / "audit_reading_prompt.txt").read_text(encoding="utf-8")) +
            "\nSCIENTIFIC RULES:\n" + rules + audit_context + measurement_context +
            ("" if citation or pending_first else "\nCANDIDATE (fallible; omitted fields retain schema defaults, not source conclusions):\n" + candidate.model_dump_json(exclude_defaults=True)) +
            "\nMACHINE CHECKS (not scientific validation):\n" + json.dumps(feedback, ensure_ascii=False))
        audit = ReadingAudit.model_validate_json((run / "reused_repair_audit.json").read_bytes()) if reuse_repair_audit else complete(
            ReadingAudit, audit_request, f"audit_{revision}", key=f"audit-{revision}", attach=not bool(citation), citation=citation)
        # Authenticated inherited delta reports remain guidance, not a new audit
        # of this run. Their original scope was verified with their own calls.
        protocol_checks = ([] if reuse_repair_audit and plan.get("audit_guidance")
                           else citation_review_issues(citation, audit))
        if citation and protocol_checks and protocol_retries < 1 and client.calls_started < plan["max_calls"]:
            # A missing disposition is a report defect, not a reason to rewrite
            # the candidate. Preserve the incomplete report and allow one fresh,
            # source-proven correction within the original call/time budget.
            write_new_json(run / f"audit_protocol_incomplete.v{revision:02d}.json", {
                "audit": audit.model_dump(mode="json"), "issues": protocol_checks,
                "candidate_sha256": hash_file(version_path), "accepted": False})
            protocol_retries += 1
            audit = complete(ReadingAudit, audit_request +
                "\nAUDIT REPORT COMPLETION REQUIRED: The prior report below did not satisfy the frozen review contract. "
                "The candidate has NOT changed. Correct only the report after checking its missing dispositions "
                "against the exact candidate and original sources. Preserve every scientific concern, inherited "
                "limitation and conflict. Do not turn a source problem into supported merely to clear these checks. "
                "This is a new source-bound audit call and requires the source tools again.\nINCOMPLETE REPORT:\n" +
                audit.model_dump_json() + "\nREPORT CHECKS:\n" + json.dumps(protocol_checks, ensure_ascii=False),
                f"audit_{revision}", key=f"audit-{revision}-protocol-{protocol_retries}", citation=citation)
            protocol_checks = citation_review_issues(citation, audit)
        write_new_json(run / f"audit.v{revision:02d}.json", audit.model_dump(mode="json"))
        checks = reading_checks(dossier, candidate, audit, documents)
        checks.extend(protocol_checks)
        # Dossier gaps remain visible, but only candidate/audit defects can be fixed
        # by revising a record. Do not spend every revision chasing a frozen note.
        actionable_checks = [i for i in checks if not i.startswith("dossier ")]
        must_revise = (any(i.severity == "error" for i in issues) or actionable_checks or
            audit.potential_omissions or any(f.needs_revision or f.verdict == "unsupported" for f in audit.findings))
        history.append({"version": revision, "candidate_sha256": hash_file(version_path),
                        "audit_origin": "reused_repair_guidance" if reuse_repair_audit else "scientific_delta_model_audit" if citation and audit.review_scope == "scientific_delta" else "citation_delta_model_audit" if citation else "fresh_model_audit",
                        "validation_errors": sum(i.severity == "error" for i in issues),
                        "needs_revision": bool(must_revise), "reading_check_issues": checks})
        if revision == 0 and citation and plan.get("resumed_full_audit"):
            history[-1]["inherited_full_audit"] = plan["resumed_full_audit"]
        if not must_revise:
            stop_reason = "coverage_correction_required" if any(i.startswith("dossier ") for i in checks) else "audit_completed"
            break
        if citation and protocol_checks:
            stop_reason = "audit_report_incomplete"
            break
        if revision >= plan["reading_settings"]["max_revisions"]:
            stop_reason = "revision_budget_exhausted"
            break
        if plan["max_calls"] - client.calls_started < 2:
            stop_reason = "turn_budget_exhausted"
            break
        revision += 1
        tools.prepare_revision(candidate)
        from .continuation_repair import source_navigation
        candidate = complete(ExtractionRecord, (run / "revise_reading_prompt.txt").read_text(encoding="utf-8") +
            "\nSCIENTIFIC RULES:\n" + rules +
            source_navigation(bundle, audit) +
            "\nINHERITED DOSSIER (fallible, not new source evidence):\n" + dossier_context +
            "\nThis is an isolated repair turn. Read the exact candidate fields and relevant original sources using wt_ tools; "
            "preserve all unaffected content and inherited limitations. Patch only concrete defects. "
            "Do not re-read unrelated pages or reproduce the complete record merely to change local fields.\n" +
            "\nCANDIDATE (omitted fields retain schema defaults):\n" + candidate.model_dump_json(exclude_defaults=True) + "\nMACHINE CHECKS:\n" + json.dumps(feedback, ensure_ascii=False) +
            "\nREADING CHECKS:\n" + json.dumps(checks, ensure_ascii=False) + "\nMODEL AUDIT:\n" + audit.model_dump_json(), f"revision_{revision}", key=f"revision-{revision}")
    checks = reading_checks(dossier, candidate, audit, documents)
    if audit and not (reuse_repair_audit and plan.get("audit_guidance")):
        checks.extend(citation_review_issues(citation, audit))
    quantitative = quantitative_summary(candidate)
    unresolved = bool(checks or (plan["rule_version"] != "0.5" and quantitative["limitations"]) or (audit and (audit.quantitative_followups or audit.coverage_limitations or any(f.verdict in {"unresolved", "not_checked"} for f in audit.findings))))
    status = ("candidate_incomplete_reading_coverage" if stop_reason == "coverage_correction_required" else
        "candidate_audited_with_limitations" if stop_reason == "audit_completed" and unresolved else
        "candidate_model_audited" if stop_reason == "audit_completed" else "candidate_incomplete_budget")
    result = {"status": status, "stop_reason": stop_reason, "model_calls": client.calls_started,
              "reading_tool_calls": tools.calls_started, "revisions": revision,
              "audit_report_retries": protocol_retries,
              "validation_error_count": sum(i.severity == "error" for i in issues),
              "reading_check_issues": checks, "history": history, "human_validated": False,
              "scientific_accuracy": "not_established", "graph_published": False, "dataset_role": plan["cohort_role"],
              "quantitative_coverage_summary": quantitative}
    if structural_proof:
        result["structural_coverage_correction"] = structural_proof
    if stop_reason == "coverage_correction_required":
        from .coverage_correction import build_coverage_correction_scope
        scope = build_coverage_correction_scope(dossier, documents)
        result["scientific_audit_state"] = "completed_with_inherited_limitations"
        result["reading_coverage_state"] = "source_disposition_correction_required"
        result["coverage_correction_required"] = {"remaining_blocks": len(scope["required_dispositions"]),
            "source_counts": {d.source.source_id: sum(r["source_id"] == d.source.source_id
                for r in scope["required_dispositions"]) for d in documents},
            "recovery": "source-bound coverage correction from this candidate; do not re-extract"}
    if resumed:
        result["resumed_from"] = plan["reused_candidate"]
    if plan.get("audit_guidance"):
        result["audit_guidance"] = plan["audit_guidance"]
        result["review_mode"] = "inherited_full_and_incremental_findings_then_repair_and_bounded_source_audit"
    if plan.get("resumed_full_audit"):
        proof = plan["resumed_full_audit"]
        result["resume_initial_baseline"] = proof
        result["review_mode"] = ("inherited_full_audit_plus_source_recheck_of_bounded_changes"
            if citation and citation[0]["baseline_candidate_path"] == proof["frozen_candidate_path"]
            else "new_full_audit_plus_bounded_local_recheck" if audit and audit.review_scope in {"citation_delta", "scientific_delta"}
            else "full_audit_after_resumed_attempt")
    if plan.get("extraction_policy"):
        result["extraction_policy"] = plan["extraction_policy"]
        result["reviewed_scope"] = "core_results; dense interior curve points are on-demand and retained series may remain partial"
    write_new_json(run / "candidate.json", candidate.model_dump(mode="json"))
    write_new_json(run / "validation.json", {"issues": serialized, "semantic_accuracy": "not_established"})
    if audit:
        write_new_json(run / "model_review.json", audit.model_dump(mode="json"))
    write_new_json(run / "result.json", result)
    review_packet(run, candidate, audit, result)
    store.event(run.name, "candidate_completed", {"model_calls": client.calls_started, "status": status})
    return run
