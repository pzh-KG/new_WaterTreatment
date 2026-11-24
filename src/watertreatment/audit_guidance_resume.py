"""Resume source-authenticated audit findings without a new full-paper audit.

The old reports remain full/citation/scientific reports with their original
provenance. A new run first repairs their outstanding findings, then performs a
bounded source review against the retained full baseline and every later audit.
"""
from __future__ import annotations

import json
from pathlib import Path

from .domain import ExtractionRecord
from .reading import ReadingAudit, ReadingDossier
from .reading_coverage_state import only_deferred_coverage_issues
from .storage import DataStore, digest_json, hash_file, write_new_json

POLICY = "authenticated-audit-guidance-v1"


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _file(records, path):
    supplied = Path(path)
    actual = records._safe_data_path(supplied)
    if supplied != actual or not actual.is_file():
        raise ValueError("audit guidance artifact moved or changed into a link")
    return actual


def _node(records, settings, candidate_path, audit_path):
    """Authenticate the actual candidate/report/call, including public completion."""
    from .audit_addendum_workflow import _completed_call_protocol
    from .source_review import verify_source_review
    from .staged_source_resume import _call

    candidate_path, audit_path = _file(records, candidate_path), _file(records, audit_path)
    run = candidate_path.parent
    if run.parent != (settings.data_root / "runs").resolve() or audit_path.parent != run:
        raise ValueError("guidance node is not within a registered research run")
    plan, _, bundle, documents = records._source(run.name)
    record, audit = ExtractionRecord.model_validate(_json(candidate_path)), ReadingAudit.model_validate(_json(audit_path))
    matches = []
    for request_path in (run / "calls").glob("*/request.json"):
        call = request_path.parent
        if not (call / "answer.json").is_file() or not (call / "result.json").is_file():
            continue
        try:
            if ReadingAudit.model_validate(_json(call / "answer.json")) != audit:
                continue
            stage = _json(request_path).get("request", {}).get("stage", "")
            if not stage.startswith("audit_"):
                continue
            request, completed, sources, _ = _call(records, run, request_path, plan,
                stage=stage, status="schema_valid_candidate")
            if any(hash_file(_file(records, path)) != sha for path, sha in sources.items()):
                continue
            matched = (records._queried_candidate_snapshot(run, record, request, sources)
                if "candidate_binding" in request else record.model_dump_json(exclude_defaults=True) in request.get("prompt", ""))
            if not matched:
                continue
            if request.get("citation_review"):
                if not records._citation_provenance(run, record, audit, request, sources):
                    continue
                scope = _json(_file(records, request["citation_review"]["scope_path"]))
                if audit.review_scope_sha256 != scope.get("scope_sha256"):
                    continue
                if scope.get("policy") in {"bounded-revision-audit-v1", "bounded-scientific-revision-v1"} and (
                        request.get("source_review_call") != call.name
                        or request.get("source_review_scope_sha256") != scope["scope_sha256"]
                        or not verify_source_review(run, scope, completed.get("source_review_proof"), call.name)):
                    continue
            elif audit.review_scope != "full" or audit.review_scope_sha256 is not None:
                continue
            events_hash = _completed_call_protocol(run, completed, _json(call / "answer.json"))
            matches.append((call, request, events_hash))
        except (ValueError, KeyError, TypeError, OSError):
            continue
    if len(matches) != 1:
        raise ValueError("guidance requires one exact completed source-bound Sol MAX call for every inherited report")
    call, request, events_hash = matches[0]
    paths = [candidate_path, audit_path, run / "plan.json", run / "sources.json",
             call / "request.json", call / "result.json", call / "answer.json", run / "app_server/events.jsonl"]
    return {"run_id": run.name, "candidate_path": str(candidate_path), "audit_path": str(audit_path),
        "candidate_sha256": hash_file(candidate_path), "audit_sha256": hash_file(audit_path),
        "completed_call": str(call), "events_sha256": events_hash,
        "artifact_hashes": [{"path": str(p), "sha256": hash_file(p)} for p in paths]}, record, audit, request, plan, bundle, documents


def _original_reference(records, run, candidate_path, audit_path):
    """Resolve explicit frozen copies back to the original authenticated files."""
    candidate_path, audit_path = _file(records, candidate_path), _file(records, audit_path)
    plan = _json(run / "plan.json")
    proof = plan.get("resumed_full_audit") or {}
    if (str(candidate_path) == proof.get("frozen_candidate_path")
            and str(audit_path) == proof.get("frozen_audit_path")):
        return Path(proof["original_candidate_path"]), Path(proof["original_audit_path"])
    descriptor = plan.get("audit_guidance")
    if descriptor:
        path = _file(records, descriptor["path"])
        if hash_file(path) != descriptor["sha256"]:
            raise ValueError("guidance navigation proof changed")
        data = _json(path)
        for item in data["frozen_nodes"]:
            if item["candidate_path"] == str(candidate_path) and item["audit_path"] == str(audit_path):
                return Path(item["original_candidate_path"]), Path(item["original_audit_path"])
    return candidate_path, audit_path


COMPOSITE_WORKFLOWS = {"partial_scientific_audit_completion", "partial-audit-remaining-resume-v1"}


def select_composite_repair(settings, run, budget):
    from .budget_grants import lineage_snapshot
    from .budget_extension_workflow import _import_bundle
    from .workflow import _reusable_dossier
    parent = authenticate_guidance_parent(settings, run, run / "candidate.v00.json")
    origin = settings.data_root / "runs" / lineage_snapshot(settings, run)["root_run"]
    source = _import_bundle(settings, origin)
    dossier, _ = _reusable_dossier(settings, origin / "reading_dossier.json", parent["bundle"],
        parent["plan"]["cohort_role"], historical_review=True)
    if _json(source) != parent["bundle"] or dossier.model_dump(mode="json") != _json(run / "reading_dossier.json"):
        raise ValueError("局部报告的来源或阅读档案与原始理解不一致")
    return {"route": "composite_repair", "label": "修订已有候选并局部复审", "minimum_calls": 2,
        "guidance": True, "bundle_path": str(source), "dossier_path": str(origin / "reading_dossier.json"),
        "source_proof": parent["receipt"], "cohort_role": parent["plan"]["cohort_role"], "budget": budget,
        "description": "沿用候选及原审查发现，按原文修订有疑点的字段，再局部复审。"}


def _composite_node(records, settings, path, audit_path, plan, record, audit):
    from .citation_audit import _parts
    from .partial_audit_completion_workflow import verify_partial_audit_completion_run
    from .partial_audit_remaining_resume import verify_remaining_completion_run
    from .scientific_revision_audit import finding_paths

    run = path.parent
    result = _json(run / "result.json")
    if plan["workflow"] == "partial_scientific_audit_completion":
        verify = verify_partial_audit_completion_run
        proof_name = "partial_audit_completion_proof.json"
        scientific_run = plan["partial_audit_parent"]["parent_run"]
    else:
        verify = verify_remaining_completion_run
        proof_name = "partial_remaining_proof.json"
        scientific_run = plan["partial_remaining_parent"]["scientific_parent_run"]
    if (path != run / "candidate.v00.json" or audit_path != run / "audit.v00.json"
            or not verify(settings, run, record, audit, for_repair=True)):
        raise ValueError("局部审查的候选、原文交付或合并报告与原调用不符")
    blocking = [f for f in audit.findings if f.needs_revision or f.verdict in {"unsupported", "not_checked"}]
    data = record.model_dump(mode="json")
    paths = {p for f in blocking for p in finding_paths(f, data)}
    issues = [s for s in result["reading_check_issues"] if not only_deferred_coverage_issues([s])]
    missing = "Completion addendum lacks an exact source disposition: "
    if (not paths or not issues or audit.potential_omissions
            or any(not (s.startswith(missing) and _parts(s[len(missing):]) in paths
                or s in {"Completion addendum reports a new scientific concern: " + f.record_path for f in blocking})
                for s in issues)):
        raise ValueError("局部报告还有无法归属到具体候选字段的缺项，需保留待核查")
    proof = _json(run / proof_name)
    prior, _, _, _ = records._source(scientific_run)
    base = prior["resumed_full_audit"]
    node = {"run_id": run.name, "candidate_path": str(path), "audit_path": str(audit_path),
        "candidate_sha256": hash_file(path), "audit_sha256": hash_file(audit_path),
        "completed_call": proof["completed_call"], "composite_proof_sha256": hash_file(run / proof_name)}
    return node, (Path(base["original_candidate_path"]), Path(base["original_audit_path"]))


def _chain(records, settings, candidate_path, audit_path, *, seen=()):
    key = (str(candidate_path), str(audit_path))
    if key in seen or len(seen) > 12:
        raise ValueError("guidance ancestry is cyclic or exceeds its bounded history")
    run = Path(candidate_path).parent
    plan = _json(_file(records, run / "plan.json"))
    if plan.get("workflow") in COMPOSITE_WORKFLOWS:
        record = ExtractionRecord.model_validate(_json(_file(records, candidate_path)))
        audit = ReadingAudit.model_validate(_json(_file(records, audit_path)))
        node, base = _composite_node(records, settings, Path(candidate_path), Path(audit_path), plan, record, audit)
        ancestors = _chain(records, settings, *base, seen=(*seen, key))
        if len(ancestors) != 1 or ancestors[0][2].review_scope != "full":
            raise ValueError("局部审查缺少原始完整审查基线")
        return [*ancestors, (node, record, audit)]
    node, record, audit, request, plan, bundle, docs = _node(records, settings, candidate_path, audit_path)
    if audit.review_scope == "full":
        return [(node, record, audit)]
    receipt = request.get("citation_review") or {}
    base = _original_reference(records, Path(candidate_path).parent,
        receipt["baseline_candidate_path"], receipt["baseline_audit_path"])
    result = _chain(records, settings, *base, seen=(*seen, key))
    if len(result) != 1 or result[0][2].review_scope != "full":
        raise ValueError("an incremental audit cannot become a full guidance baseline")
    for item in receipt.get("additional_audits", []):
        original = _original_reference(records, Path(candidate_path).parent, item["candidate_path"], item["audit_path"])
        additions = _chain(records, settings, *original, seen=(*seen, key))
        if additions[0][0] != result[0][0]:
            raise ValueError("guidance incremental histories do not share one full baseline")
        for entry in additions[1:]:
            if entry[0] not in [old[0] for old in result]:
                result.append(entry)
    result.append((node, record, audit))
    if len(result) > 4:
        raise ValueError("inherited audit guidance exceeds the bounded cumulative audit chain")
    return result


def authenticate_guidance_parent(settings, parent_run, candidate_path):
    from .web_records import WebRecords
    from .reader_workflow import reading_checks
    from .validation import validate_record
    from .workflow import validate_plan_metadata

    records = WebRecords(settings)
    run = Path(parent_run).resolve(strict=True)
    path = _file(records, candidate_path)
    if path.parent != run or path.name not in {p.name for p in run.glob("candidate.v[0-9][0-9].json")}:
        raise ValueError("audit guidance must start from a retained versioned candidate")
    if path != sorted(run.glob("candidate.v[0-9][0-9].json"))[-1]:
        raise ValueError("audit guidance cannot bypass a newer candidate")
    plan, _, bundle, docs = records._source(run.name)
    if (plan.get("cohort_role") not in {"development", "unassigned"}
            or plan.get("rule_version") not in {"0.7", "0.8", "0.9"}
            or plan["rule_version"] != settings.rules_version):
        raise ValueError("guidance requires unchanged research rules and non-heldout source identity")
    record = ExtractionRecord.model_validate(_json(path))
    audit_path = path.with_name(path.name.replace("candidate.", "audit."))
    audit = ReadingAudit.model_validate(_json(audit_path))
    terminal = _json(run / "result.json")
    dossier = ReadingDossier.model_validate(_json(run / "reading_dossier.json"))
    checks = reading_checks(dossier, record, audit, docs)
    if (hash_file(run / "candidate.json") != hash_file(path)
            or hash_file(run / "model_review.json") != hash_file(audit_path)
            or (plan.get("workflow") not in COMPOSITE_WORKFLOWS
                and not records._audit_provenance(run, record, terminal, audit.model_dump(mode="json")))
            or not only_deferred_coverage_issues(checks)
            or any(i.severity == "error" for i in [*validate_record(record, source_documents=docs),
                *validate_plan_metadata(record, plan, docs, store=records.store)])
            or not (audit.potential_omissions or any(f.needs_revision or f.verdict == "unsupported" for f in audit.findings))):
        raise ValueError("guidance requires a source-authenticated outstanding scientific review, without other identity/schema defects")
    staged = sorted((run / "reading_tools").glob("*/submitted_record.json"))
    if staged and ExtractionRecord.model_validate(_json(staged[-1])) != record:
        raise ValueError("guidance cannot discard a newer staged scientific revision")
    chain = _chain(records, settings, path, audit_path)
    for node, item, report in chain:
        if (item.paper.id != record.paper.id or item.paper.dataset_role != record.paper.dataset_role
                or item.paper.source_ids != record.paper.source_ids or item.paper.role_history != record.paper.role_history):
            raise ValueError("guidance ancestry changes scientific identity")
    receipt = {"policy": POLICY, "parent_run": run.name, "parent_plan_sha256": hash_file(run / "plan.json"),
        "parent_result_sha256": hash_file(run / "result.json"), "parent_sources_sha256": hash_file(run / "sources.json"),
        "candidate_path": str(path), "candidate_sha256": hash_file(path),
        "audit_path": str(audit_path), "audit_sha256": hash_file(audit_path),
        "nodes": [item[0] for item in chain]}
    return {"run": run, "plan": plan, "record": record, "audit": audit, "bundle": bundle,
            "documents": docs, "chain": chain, "receipt": receipt}


def freeze_guidance(settings, new_run, parent_run, candidate_path):
    """Attach source-audit ancestry to an unexecuted new continuation plan."""
    from .audit_execution import select_scientific_scope_limits

    parent = authenticate_guidance_parent(settings, parent_run, candidate_path)
    run = Path(new_run).resolve(strict=True)
    path = run / "plan.json"
    plan = _json(path)
    if (run.parent != (settings.data_root / "runs").resolve() or plan.get("run_id") != run.name
            or plan.get("audit_guidance") or plan.get("resumed_full_audit")
            or (run / "execution_started.json").exists() or list((run / "calls").glob("*/request.json"))
            or not plan.get("reused_candidate") or not plan.get("reused_dossier")
            or ExtractionRecord.model_validate(_json(run / "reused_candidate.json")) != parent["record"]
            or hash_file(run / "reading_dossier.json") != hash_file(parent["run"] / "reading_dossier.json")
            or digest_json(_json(run / "sources.json")["documents"]) != digest_json(parent["bundle"]["documents"])
            or plan.get("max_calls", 0) < 2 or plan["reading_settings"].get("max_revisions", 0) < 1):
        raise ValueError("audit guidance requires a new source-identical candidate continuation with repair and audit budget")
    artifacts, frozen = [], []
    for index, (node, record, audit) in enumerate(parent["chain"]):
        item = {}
        for kind, value in (("candidate", record), ("audit", audit)):
            target = run / f"guidance_{kind}.{index:02d}.json"
            write_new_json(target, value.model_dump(mode="json"))
            item[kind + "_path"], item[kind + "_sha256"] = str(target), hash_file(target)
            item["original_" + kind + "_path"] = node[kind + "_path"]
            artifacts.append({"path": str(target), "sha256": hash_file(target)})
        frozen.append(item)
    guide_path = run / "reused_repair_audit.json"
    if guide_path.exists():
        if ReadingAudit.model_validate(_json(guide_path)) != parent["audit"]:
            raise ValueError("existing repair guidance differs from the latest source-authenticated report")
    else:
        write_new_json(guide_path, parent["audit"].model_dump(mode="json"))
        artifacts.append({"path": str(guide_path), "sha256": hash_file(guide_path)})
    proof = {"policy": POLICY, "parent": parent["receipt"], "frozen_nodes": frozen,
        "parent_scope_limits": select_scientific_scope_limits(parent["run"], parent["plan"]),
        "scope_limits": select_scientific_scope_limits(run, plan),
        "interpretation": "Retained full and incremental scientific reports guide a new repair; they are not a new completed audit."}
    proof_path = run / "audit_guidance.json"
    write_new_json(proof_path, proof)
    descriptor = {"path": str(proof_path), "sha256": hash_file(proof_path), "parent_run": parent["run"].name}
    artifacts.append({"path": str(proof_path), "sha256": hash_file(proof_path)})
    write_new_json(run / "plan.before-guidance.json", plan)
    plan["audit_guidance"] = descriptor
    plan["reused_candidate"]["repair_audit"] = {"path": parent["receipt"]["audit_path"],
        "sha256": parent["receipt"]["audit_sha256"], "completed_call": parent["chain"][-1][0]["completed_call"]}
    plan["scientific_scope_limits"] = proof["scope_limits"]
    plan["reading_artifacts"].extend(artifacts)
    plan["input_paths"].extend(a for a in artifacts if a not in plan["input_paths"])
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    DataStore(settings.data_root).event(run.name, "prepared", {"plan_sha256": hash_file(path)})
    return descriptor


def verify_guidance(settings, run, plan=None):
    run = Path(run).resolve(strict=True)
    plan = plan or _json(run / "plan.json")
    descriptor = plan.get("audit_guidance")
    if not descriptor:
        return None
    path = Path(descriptor["path"])
    if path != run / "audit_guidance.json" or path.resolve(strict=True) != path or hash_file(path) != descriptor["sha256"]:
        raise ValueError("audit guidance proof changed")
    proof = _json(path)
    receipt = proof["parent"]
    parent_run = settings.data_root / "runs" / receipt["parent_run"]
    rows = {r["run_id"]: r for r in DataStore(settings.data_root).runs()}
    if (descriptor.get("parent_run") != receipt["parent_run"] or run.name not in rows
            or rows[parent_run.name]["created_at"] >= rows[run.name]["created_at"]):
        raise ValueError("guidance must inherit a strictly earlier registered run")
    parent = authenticate_guidance_parent(settings, parent_run, Path(receipt["candidate_path"]))
    if plan.get("reused_candidate", {}).get("composite_repair"):
        from .workflow import _reusable_dossier
        dossier, lineage = _reusable_dossier(settings, Path(plan["reused_dossier"]["path"]), parent["bundle"],
            plan["cohort_role"], historical_review=True)
        if lineage != plan["reused_dossier"] or dossier.model_dump(mode="json") != _json(run / "reading_dossier.json"):
            raise ValueError("原阅读档案的提示或来源与保留的模型交付不同")
    if (proof.get("policy") != POLICY or receipt != parent["receipt"]
            or len(proof["frozen_nodes"]) != len(parent["chain"])):
        raise ValueError("original guidance audit, source or completion protocol changed")
    if (ExtractionRecord.model_validate(_json(run / "reused_candidate.json")) != parent["record"]
            or ReadingAudit.model_validate(_json(run / "reused_repair_audit.json")) != parent["audit"]
            or hash_file(run / "reading_dossier.json") != hash_file(parent["run"] / "reading_dossier.json")
            or digest_json(_json(run / "sources.json")["documents"]) != digest_json(parent["bundle"]["documents"])
            or plan.get("resumed_full_audit")
            or plan.get("reused_candidate", {}).get("repair_audit") != {
                "path": receipt["audit_path"], "sha256": receipt["audit_sha256"],
                "completed_call": parent["chain"][-1][0]["completed_call"]}):
        raise ValueError("guidance frozen candidate/source interpretation changed")
    for item, (node, record, audit) in zip(proof["frozen_nodes"], parent["chain"]):
        for kind, value in (("candidate", record), ("audit", audit)):
            target = Path(item[kind + "_path"])
            if (target.parent != run or target.resolve(strict=True) != target or hash_file(target) != item[kind + "_sha256"]
                    or item["original_" + kind + "_path"] != node[kind + "_path"]
                    or _json(target) != value.model_dump(mode="json")):
                raise ValueError("guidance frozen navigation changed")
    from .audit_execution import select_scientific_scope_limits
    inherited_limits = select_scientific_scope_limits(parent["run"], parent["plan"])
    # Historical guidance has a single inherited policy. New guidance freezes
    # both the old policy and the explicitly selected new-run policy; ancestral
    # calls are still authenticated against their own unchanged scope files.
    if (proof.get("parent_scope_limits", proof["scope_limits"]) != inherited_limits
            or plan.get("scientific_scope_limits") != proof["scope_limits"]
            or select_scientific_scope_limits(run, plan) != proof["scope_limits"]):
        raise ValueError("guidance scientific scope limits changed")
    return parent, proof


def _review_inputs(settings, run, plan, revision, candidate, history):
    from .scientific_revision_audit import build_scientific_revision_scope

    parent, proof = verify_guidance(settings, run, plan)
    if type(revision) is not int or not 1 <= revision <= 5 or len(history) != revision:
        raise ValueError("guidance audit must follow its retained repair step and contiguous local history")
    original = history[0]
    if (original.get("audit_origin") != "reused_repair_guidance" or original.get("version") != 0
            or original.get("candidate_sha256") != hash_file(run / "candidate.v00.json")
            or ExtractionRecord.model_validate(_json(run / "candidate.v00.json")) != parent["record"]
            or ReadingAudit.model_validate(_json(run / "audit.v00.json")) != parent["audit"]):
        raise ValueError("the initial guidance was altered or relabelled as a new scientific review")
    base = proof["frozen_nodes"][0]
    old, baseline = parent["chain"][0][1:]
    entries = [{k: v for k, v in item.items() if not k.startswith("original_")} for item in proof["frozen_nodes"][1:]]
    additional = [item[2] for item in parent["chain"][1:]]
    for version in range(1, revision):
        item = history[version]
        record_path, audit_path = run / f"candidate.v{version:02d}.json", run / f"audit.v{version:02d}.json"
        if (item.get("version") != version or item.get("audit_origin") != "scientific_delta_model_audit"
                or item.get("candidate_sha256") != hash_file(record_path)
                or item.get("validation_error_count", item.get("validation_errors")) != 0
                or not only_deferred_coverage_issues(item.get("reading_check_issues"))):
            raise ValueError("new guidance history contains a gap or unresolved structural/reading checks")
        audit = ReadingAudit.model_validate(_json(audit_path))
        if audit.review_scope != "scientific_delta":
            raise ValueError("a new guidance review is not a bounded scientific delta")
        entries.append({"candidate_path": str(record_path), "candidate_sha256": hash_file(record_path),
                        "audit_path": str(audit_path), "audit_sha256": hash_file(audit_path)})
        additional.append(audit)
    current_path = run / f"candidate.v{revision:02d}.json"
    if ExtractionRecord.model_validate(_json(current_path)) != candidate:
        raise ValueError("guidance review candidate differs from its retained version")
    diagnostics = []
    scope = build_scientific_revision_scope(old, candidate, baseline,
        baseline_origin="fresh_model_audit", additional_audits=additional,
        diagnostics=diagnostics, **proof["scope_limits"])
    if scope is None:
        from .audit_execution import ScientificReviewScopeUnavailable
        raise ScientificReviewScopeUnavailable(diagnostics)
    return base, old, baseline, additional, entries, current_path, scope


def prepare_guidance_review(settings, run, plan, revision, candidate, history):
    base, old, baseline, additional, entries, current, scope = _review_inputs(
        settings, Path(run), plan, revision, candidate, history)
    path = Path(run) / f"citation_scope.v{revision:02d}.json"
    write_new_json(path, scope)
    receipt = {"scope_path": str(path), "scope_file_sha256": hash_file(path),
        "baseline_candidate_path": base["candidate_path"], "baseline_candidate_sha256": base["candidate_sha256"],
        "baseline_audit_path": base["audit_path"], "baseline_audit_sha256": base["audit_sha256"],
        "current_candidate_path": str(current), "current_candidate_sha256": hash_file(current),
        "additional_audits": entries}
    return receipt, scope, baseline


def verify_guidance_review(settings, run, record, audit, request, sources):
    """Authenticate inheritance and every intervening new source-bound audit."""
    import re
    from .scientific_revision_audit import validate_scientific_revision_audit
    from .web_records import WebRecords

    try:
        run = Path(run).resolve(strict=True)
        receipt = request["citation_review"]
        path = Path(receipt["current_candidate_path"])
        match = re.fullmatch(r"candidate\.v([0-9]{2})\.json", path.name)
        if not match or path.parent != run:
            return False
        version = int(match[1])
        plan, terminal = _json(run / "plan.json"), _json(run / "result.json")
        history = terminal.get("history", [])
        if (not isinstance(history, list) or len(history) <= version
                or history[version].get("version") != version
                or history[version].get("audit_origin") != "scientific_delta_model_audit"
                or history[version].get("candidate_sha256") != hash_file(path)
                or request.get("stage") != f"audit_{version}"):
            return False
        base, old, baseline, additional, entries, current, expected = _review_inputs(
            settings, run, plan, version, record, history[:version])
        scope_path = run / f"citation_scope.v{version:02d}.json"
        expected_receipt = {"scope_path": str(scope_path), "scope_file_sha256": hash_file(scope_path),
            "baseline_candidate_path": base["candidate_path"], "baseline_candidate_sha256": base["candidate_sha256"],
            "baseline_audit_path": base["audit_path"], "baseline_audit_sha256": base["audit_sha256"],
            "current_candidate_path": str(current), "current_candidate_sha256": hash_file(current),
            "additional_audits": entries}
        if (receipt != expected_receipt or _json(scope_path) != expected
                or audit.review_scope != "scientific_delta" or audit.review_scope_sha256 != expected["scope_sha256"]):
            return False
        pairs = [(scope_path, hash_file(scope_path)), (current, hash_file(current)),
                 (Path(base["candidate_path"]), base["candidate_sha256"]), (Path(base["audit_path"]), base["audit_sha256"])]
        pairs.extend((Path(item[k + "_path"]), item[k + "_sha256"]) for item in entries for k in ("candidate", "audit"))
        if any(sources.get(str(path)) != sha for path, sha in pairs):
            return False
        records = WebRecords(settings)
        for index in range(1, version):
            earlier = ExtractionRecord.model_validate(_json(run / f"candidate.v{index:02d}.json"))
            report = ReadingAudit.model_validate(_json(run / f"audit.v{index:02d}.json"))
            if not records._completed_audit_provenance(run, earlier, report):
                return False
        return not validate_scientific_revision_audit(expected, baseline, audit,
            old_record=old, new_record=record, additional_audits=additional)
    except (ValueError, OSError, KeyError, TypeError, IndexError):
        return False
