"""Independently adjudicate a proposed bounded repair to a delivered, unfinished report."""
from datetime import datetime
import json
import hashlib
import sqlite3
from pathlib import Path
from shutil import copyfile

from .budget_grants import retained_stage_budget
from .codex import code_identity, source_snapshot, strict_schema
from .domain import ExtractionRecord
from .model_policy import require_active_model
from .quantitative import quantitative_summary
from .reader_workflow import reading_checks
from .retained_review_packet import FOCUSED_VERSIONS, LocalReview, build_packet, finish, prompt
from .retained_review_parent import authenticate, read
from .storage import DataStore, digest_json, hash_file, write_new_json
from .validation import validate_record
from .workflow import runtime_versions, validate_plan_metadata


POLICY = "retained-issue-source-review-v1"
ORIGIN = "authenticated_retained_issue_review"


def _packet_version(parent):
    if "interrupted_source_review" in parent.receipt:
        return parent.receipt["interrupted_source_review"]["packet_version"]
    if parent.plan.get("workflow") == POLICY:
        from .focused_review_packet import named_residual
        if named_residual(parent):
            return 7
    partial = parent.plan.get("workflow") in {"partial_scientific_audit_completion","partial-audit-remaining-resume-v1"}
    if partial:
        return 5
    if parent.receipt.get("mode") == "native-repair-targets-v1" and parent.candidate.rule_version == "0.9":
        from .ownership_audit import literal_quote_extension
        previous = {e.id: e for e in parent.before.evidence}
        if any(e.id in previous and previous[e.id] != e and
               literal_quote_extension(previous[e.id].model_dump(mode="json"), e.model_dump(mode="json"))
               for e in parent.candidate.evidence):
            return 9
    if parent.receipt.get("baseline",{}).get("repaired_baseline_checks"):
        return 6
    return 4 if parent.receipt.get("mode") == "native-repair-targets-v1" else 2


def _packet(parent, candidate, source_blocks, version):
    retry = parent.receipt.get("interrupted_source_review")
    if retry:
        from dataclasses import replace
        parent = replace(parent, run=parent.run.parent / retry["review_origin"])
    if version in FOCUSED_VERSIONS:
        from .focused_review_packet import build
        return build(parent,candidate,source_blocks,version=version)
    packet,images=build_packet(parent,candidate,source_blocks,version=version)
    return packet,images,{}


def _schema(version):
    if version in FOCUSED_VERSIONS:
        from .focused_review_packet import FocusedReview
        return FocusedReview
    return LocalReview


def guided_proposal(settings, run):
    """Use a provider-submitted patch already on disk and its own cited originals."""
    from .ownership_audit import _changes
    from .scientific_revision_audit import _change_evidence_ids
    parent = authenticate(settings, run)
    old, current = parent.before.model_dump(mode="json"), parent.candidate.model_dump(mode="json")
    version = _packet_version(parent)
    if "interrupted_source_review" in parent.receipt:
        from .retained_tool_retry import checked_proposal
        preserved = checked_proposal(parent, None)
        proposed = ExtractionRecord.model_validate(preserved["candidate"])
        packet, _, _ = _packet(parent, proposed, preserved["source_blocks"], version)
        return preserved, packet["packet_hash"]
    if version in FOCUSED_VERSIONS:
        proposal,version=_default_focused_proposal(parent,version)
        proposed=ExtractionRecord.model_validate(proposal["candidate"])
        if old==proposed.model_dump(mode="json"):
            raise ValueError("此断点仍需要对已定位的科学疑点做定向修订")
        packet,_,_ = _packet(parent,proposed,proposal["source_blocks"],version)
        return proposal,packet["packet_hash"]
    if old == current:
        raise ValueError("此断点仍需要对已定位的科学疑点做定向修订")
    changes = _changes(old, current)
    references = {eid for change in changes for side in ("old_value", "new_value")
                  for eid in _change_evidence_ids(change, side)}
    anchors = {(e.source_id, e.block_id) for e in parent.candidate.evidence if e.id in references}
    anchors.update((a["source_id"], a["block_id"]) for f in parent.findings for a in f.get("evidence", []))
    proposal = {"base_hash": digest_json(current), "candidate": current, "source_blocks": sorted(map(list, anchors))}
    packet, _ = build_packet(parent, parent.candidate, proposal["source_blocks"], version=2)
    return proposal, packet["packet_hash"]


def _native_proposal(parent, version):
    if version == 6 or (version == 9 and parent.receipt.get("baseline", {}).get("repaired_baseline_checks")):
        from .repaired_baseline import canonical_proposal
        return canonical_proposal(parent)
    return parent.candidate


def _default_focused_proposal(parent, version):
    if "interrupted_source_review" in parent.receipt:
        from .retained_tool_retry import checked_proposal
        return checked_proposal(parent, None), version
    from .native_repair_review import retained_tail_proposal, _merge_reviewed_fields
    current=parent.candidate.model_dump(mode="json")
    proposal=retained_tail_proposal(parent)
    if proposal:
        if version in (6, 9):
            proposal["candidate"]=_merge_reviewed_fields(current,_native_proposal(parent,version).model_dump(mode="json"),proposal["candidate"])
            return proposal, version
        return proposal,8
    if parent.plan.get("workflow")==POLICY:
        result=read(parent.run/"result.json")
        pending=result.get("deferred_source_review_corrections",[])
        if pending:
            return _deferred_proposal(parent,current,pending),4
    return {"base_hash":digest_json(current),"candidate":_native_proposal(parent,version).model_dump(mode="json"),"source_blocks":[]},version


def _deferred_proposal(parent,current,pending):
    from .citation_audit import _parts, _resolve
    from .literal_record_transport import apply_record_edits
    packet=read(parent.run/"local_review_packet.json")
    cards={c["id"]:(c["source"],c["block"]) for c in packet["sources"]}
    edits,anchors=[],set()
    for item in pending:
        path=list(_parts(item["path"]))
        _resolve(current,path)
        # Treat the supplied JSON as a literal replacement. Any project schema
        # defaults become visible in the next before/after review packet.
        edits.append({"operation":"set","paths":[path],"value":json.loads(item["replacement_json"])})
        anchors.update(cards[key] for key in item["source_ids"])
    raw,_=apply_record_edits(current,edits)
    candidate=ExtractionRecord.model_validate(raw)
    return {"base_hash":digest_json(current),"candidate":candidate.model_dump(mode="json"),
            "source_blocks":sorted(map(list,anchors)),"proposal_origin":{"policy":"deferred-review-fields-v1",
            "parent_run":parent.run.name,"result_sha256":hash_file(parent.run/"result.json"),
            "pending_fields_sha256":digest_json(pending)}}


def _proposal_version(parent, proposal):
    expected,version=_default_focused_proposal(parent,_packet_version(parent))
    if proposal==expected:
        return version
    # An explicit supplement is reviewed against the actual retained record.
    # Its independent provenance binds the precise starting proposal, never a
    # claim that the earlier native command contained the supplemental fields.
    if (expected.get("proposal_origin") and proposal.get("proposal_origin")==expected["proposal_origin"]
            and proposal.get("base_hash")==expected["base_hash"]
            and proposal.get("supplements_proposal_sha256")==digest_json(expected["candidate"])):
        return version
    raise ValueError("Retained proposal differs from its rejected public command")


def _on_demand_word_previews(settings, parent, selected):
    """Freeze registered Word figure pixels for follow-up navigation without attaching them all."""
    root=settings.data_root.resolve()
    chosen={Path(p).resolve() for p in selected}
    occurrences={(d.source.source_id,b.block_id) for d in parent.documents if d.source.format=="docx"
                 for b in d.blocks if b.kind=="image"}
    verified={}
    for rendering in parent.bundle.get("renderings",[]):
        for item in rendering.get("images",[]):
            if not item.get("path"):
                continue
            path=Path(item["path"]).resolve()
            occurrence=(rendering["source_id"],item["block_id"])
            if (occurrence not in occurrences or not path.is_relative_to(root) or not path.is_file()
                    or hash_file(path)!=item.get("sha256")):
                if path in chosen:
                    raise ValueError("Selected Word image does not match its registered original rendering")
                continue
            verified[path]=item["sha256"]
    return verified


def _canonical_parent_receipt(receipt):
    # Validator findings may be discovered through a set. Order is immaterial;
    # every complete finding, duplicate and all other proof bytes remain bound.
    baseline=receipt.get("baseline",{})
    checks=baseline.get("repaired_baseline_checks")
    if not checks:
        return receipt
    ordered={**checks,"validation":sorted(checks["validation"],key=digest_json)}
    return {**receipt,"baseline":{**baseline,"repaired_baseline_checks":ordered}}


def _machine(settings, parent, proposed):
    issues = validate_record(proposed, source_documents=parent.documents)
    issues += validate_plan_metadata(proposed, parent.plan, parent.documents, store=DataStore(settings.data_root))
    errors = [x for x in issues if x.severity == "error"]
    if errors:
        raise ValueError("Candidate failed machine checks: " + str([(x.code, x.path, x.message) for x in errors[:12]]))
    return issues


def continue_native(settings, original, *, progress=None, seconds=900):
    """Use the same bounded recovery through web jobs and offline-launched live jobs."""
    balance=retained_stage_budget(settings,original)
    duration=min(seconds,balance["remaining_seconds"])
    if balance["remaining_calls"]<1 or duration<60:
        raise ValueError("当前单篇剩余额度不足以完成局部原文核验")
    child=prepare(settings,original,None,seconds=duration)
    if progress:
        progress("正在用原文局部核验已保留的修订",run_id=child.name)
    execute(settings,child,live=True)
    return child


def complete_failed_revision(settings, run, failure, *, progress):
    """Resume only an authenticated, already-submitted revision, within the paper's allowance."""
    from .native_repair_review import available
    if not available(run):
        raise failure
    progress("保留已提交的修订与原文审查，仅续核受影响的字段、对照及比较", run_id=run.name)
    return continue_native(settings,run,progress=progress)


def _latest(settings, parent, exclude=None, rows=None):
    if rows is None:
        rows = DataStore(settings.data_root).runs()
    since = next(r["created_at"] for r in rows if r["run_id"] == parent.run.name)
    for r in rows:
        if r["kind"] != "extraction" or r["created_at"] <= since or r["run_id"] == exclude:
            continue
        path = Path(r["manifest_path"]).parent
        if read(path / "manifest.json").get("paper_id") == parent.plan["paper_id"] and (
                (path / "calls").exists() or (path / "result.json").exists() or (path / "execution_started.json").exists()):
            raise ValueError("A newer execution already exists for this paper")


def prepare(settings, parent_run, proposal, *, seconds=900, extra_seconds=0, extra_calls=0):
    require_active_model(settings.codex)
    from .retained_amendment import parent_for_proposal
    proposal = read(proposal) if isinstance(proposal, (str, Path)) else proposal
    parent = parent_for_proposal(authenticate, settings, parent_run, proposal)
    _latest(settings, parent)
    version=_packet_version(parent)
    if "interrupted_source_review" in parent.receipt:
        from .retained_tool_retry import checked_proposal
        proposal = checked_proposal(parent, proposal)
    elif proposal is None and version in FOCUSED_VERSIONS:
        proposal,version=_default_focused_proposal(parent,version)
    elif proposal and "proposal_origin" in proposal:
        version=_proposal_version(parent,proposal)
    if proposal["base_hash"] != digest_json(parent.candidate.model_dump(mode="json")):
        raise ValueError("Proposed repair is not bound to the latest retained candidate")
    proposed = ExtractionRecord.model_validate(proposal["candidate"])
    _machine(settings, parent, proposed)
    packet, images, available_images = _packet(parent,proposed,proposal["source_blocks"],version)
    word_previews=_on_demand_word_previews(settings,parent,[*images,*available_images.values()])
    balance = retained_stage_budget(settings, parent.run)
    if type(extra_calls) is not int or extra_calls not in (0, 1):
        raise ValueError("One local stage can authorize at most one additional call")
    granted_calls = max(extra_calls, int(extra_seconds > 0))
    if (type(seconds) is not int or not 60 <= seconds <= 1200 or type(extra_seconds) is not int
            or not 0 <= extra_seconds <= 900 or balance["remaining_calls"] + granted_calls < 1
            or seconds > balance["remaining_seconds"] + extra_seconds):
        raise ValueError("Scoped model turn exceeds the explicit remaining authority")
    store = DataStore(settings.data_root)
    run = store.new_run("extraction", {"paper_id": parent.plan["paper_id"], "operation": POLICY, "workflow": POLICY,
        "parent_run": parent.run.name, "cohort_role": parent.plan["cohort_role"]})
    copyfile(parent.run / "sources.json", run / "sources.json")
    for name, value in {"candidate.v00.json": parent.candidate.model_dump(mode="json"),
            "candidate.v01.json": proposed.model_dump(mode="json"), "reading_dossier.json": parent.dossier.model_dump(mode="json"),
            "proposal.json": proposal, "parent_receipt.json": parent.receipt, "local_review_packet.json": packet,
            "response_schema.json": _schema(version).model_json_schema()}.items():
        write_new_json(run / name, value)
    (run / "local_review_prompt.txt").write_text(prompt(packet), encoding="utf-8")
    immutable = [p for p in run.iterdir() if p.is_file() and p.name != "manifest.json"]
    immutable.extend(Path(d.source.path) for d in parent.documents)
    immutable.extend(map(Path, dict.fromkeys([*images,*available_images.values()])))
    immutable=list(dict.fromkeys([*immutable,*word_previews]))
    inputs=source_snapshot(immutable)
    if any(i["sha256"]!=word_previews[Path(i["path"])] for i in inputs if Path(i["path"]) in word_previews):
        raise ValueError("Word source preview changed while freezing the local review")
    model = settings.codex.model_copy(update={"max_calls_per_run": 1, "timeout_seconds": seconds})
    plan = {k: parent.plan.get(k) for k in ("cohort_role","paper_id","rule_version","schema_version","extraction_policy")}
    from .retained_tool_retry import source_tool_limit
    plan.update(version=1, packet_version=version, source_tool_limit=source_tool_limit(packet), run_id=run.name, workflow=POLICY, parent_run=parent.run.name, workdir=str(run),
        input_paths=inputs, reading_artifacts=[], image_paths=images, model_settings=model.model_dump(mode="json"),
        reading_settings={"enabled":True, "total_timeout_seconds":seconds, "max_tool_calls":source_tool_limit(packet)}, max_calls=1,
        runtime=runtime_versions(), code_identity=code_identity(settings.project_root), budget_before=balance,
        explicit_extra_seconds=extra_seconds, explicit_extra_calls=granted_calls, status="prepared_no_model_calls")
    write_new_json(run / "plan.json", plan)
    store.event(run.name, "prepared", {"plan_sha256":hash_file(run / "plan.json")})
    if granted_calls:
        store.event(run.name, "explicit_local_recovery_authority", {"parent_run":parent.run.name,
            "additional_model_calls":1, "additional_seconds":extra_seconds, "plan_sha256":hash_file(run / "plan.json")})
    store.record_development_use(run.name, plan["paper_id"], [d.source.sha256 for d in parent.documents],
        "Targeted investigation and repair from preserved model findings, not independent validation")
    return run


def _bound(settings, run):
    from .web_records import WebRecords
    records = WebRecords(settings)
    run = records._safe_data_path(run)
    plan, _, bundle, _ = records._source(run.name)
    if run.parent != (settings.data_root/"runs").resolve() or plan.get("workflow") != POLICY:
        raise ValueError("Invalid retained-issue run")
    original = settings.data_root / "runs" / plan["parent_run"]
    if read(original/"manifest.json")["created_at"] >= read(run/"manifest.json")["created_at"]:
        raise ValueError("Nonchronological review lineage")
    from .retained_amendment import parent_for_proposal
    proposal = read(run / "proposal.json")
    parent = parent_for_proposal(authenticate, settings, original, proposal)
    proposed = ExtractionRecord.model_validate(read(run / "candidate.v01.json"))
    version = plan.get("packet_version",1)
    if "interrupted_source_review" in parent.receipt:
        from .retained_tool_retry import checked_proposal
        checked_proposal(parent, proposal)
        if version != _packet_version(parent):
            raise ValueError("Interrupted source review format changed")
    elif "proposal_origin" in proposal or version==8:
        if version!=_proposal_version(parent,proposal):
            raise ValueError("Retained proposal no longer matches its source-bound revision")
    packet, images, available_images = _packet(parent,proposed,proposal["source_blocks"],version)
    from .retained_tool_retry import source_tool_limit
    if "source_tool_limit" in plan and plan["source_tool_limit"] != source_tool_limit(packet):
        raise ValueError("Source-tool allowance differs from the scope of the frozen review")
    if (bundle != parent.bundle or proposal["candidate"] != proposed.model_dump(mode="json")
            or proposal["base_hash"] != digest_json(parent.candidate.model_dump(mode="json"))
            or ExtractionRecord.model_validate(read(run/"candidate.v00.json")) != parent.candidate
            or packet != read(run/"local_review_packet.json")
            or _canonical_parent_receipt(read(run/"parent_receipt.json")) != _canonical_parent_receipt(parent.receipt)
            or (run/"local_review_prompt.txt").read_text(encoding="utf-8") != prompt(packet)
            or images != plan["image_paths"] or read(run/"response_schema.json") != _schema(version).model_json_schema()
            or not set(available_images.values()) <= {p["path"] for p in plan["input_paths"]}):
        raise ValueError("Review inputs differ from the authenticated candidate, original sources or issue list")
    _machine(settings, parent, proposed)
    return records, parent, proposed, plan, packet


def _confirm_delivered_inputs(run, request, outcome):
    # Match what the adapter actually sent to the same provider thread/turn,
    # including every numbered full image; a local prompt file alone is insufficient.
    sent = {}
    matched = []
    with (run/"app_server/events.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            msg = row.get("message", {})
            params = msg.get("params", {})
            if row["direction"] == "send" and msg.get("method") == "turn/start" and params.get("threadId") == outcome["thread_id"]:
                sent[msg["id"]] = params
            if row["direction"] == "receive" and msg.get("id") in sent and msg.get("result",{}).get("turn",{}).get("id") == outcome["turn_id"]:
                matched.append(sent[msg["id"]])
    expected_input = [{"type":"text","text":request["prompt"]}] + [{"type":"localImage","path":p,"detail":"original"} for p in request["images"]]
    if len(matched) != 1 or matched[0].get("input") != expected_input or matched[0].get("outputSchema") != request["schema"]:
        raise ValueError("Native delivered text/images/schema do not match the local packet")


def _public(records, run, plan, packet):
    from .audit_addendum_workflow import _completed_call_protocol
    from .staged_source_resume import _call
    calls = list((run/"calls").glob("*/request.json"))
    if len(calls) != 1:
        raise ValueError("Expected one completed local review")
    path = calls[0]
    request, outcome, inputs, _ = _call(records, run, path, plan, stage="retained_issue_review", status="schema_valid_candidate")
    raw = read(path.with_name("answer.json"))
    if "source_tool_limit" in plan and request.get("tool_budget_at_start", {}).get("limits", {}).get("total_attempts") != plan["source_tool_limit"]:
        raise ValueError("Delivered source-review allowance differs from the frozen plan")
    events = _completed_call_protocol(run, outcome, raw)
    schema=_schema(plan.get("packet_version",1))
    if (request["schema"] != strict_schema(schema.model_json_schema())
            or not request["prompt"].startswith(prompt(packet)) or request["images"] != plan["image_paths"]
            or any(hash_file(records._safe_data_path(f)) != sha for f,sha in inputs.items())):
        raise ValueError("Source packet, attached originals or effective review changed")
    _confirm_delivered_inputs(run, request, outcome)
    return schema.model_validate(raw), {"call":str(path.parent), "events_sha256":events,
        "elapsed_seconds":outcome["elapsed_seconds"], "server_selected_model":outcome["server_selected_model"],
        "server_selected_effort":outcome["server_selected_effort"], "packet_hash":packet["packet_hash"]}


def _outcome(run, parent, proposed, packet, report, proof):
    dossier, audit, issues = finish(parent, proposed, packet, report)
    pending=[]
    if packet.get("version") in FOCUSED_VERSIONS:
        proposed,pending=_reviewed_candidate(parent,proposed,packet,report)
        for correction in report.final_corrections:
            from .reading import AuditFinding, SourceAnchor
            refs=[SourceAnchor(source_id=c["source"],block_id=c["block"]) for c in packet["sources"] if c["id"] in correction.source_ids]
            needs_review = correction in pending
            if needs_review:
                issues.append("Pending source-review proposal: "+correction.path)
            explanation=(correction.explanation+" Proposed replacement, still requiring independent source adjudication: "+
                         correction.replacement_json if needs_review else correction.explanation)
            audit.findings.append(AuditFinding(record_path=correction.path,record_paths=[correction.path],severity="major" if needs_review else "minor",
                verdict="not_checked" if needs_review else "supported",needs_revision=needs_review,explanation=explanation,evidence=refs))
    issues += reading_checks(dossier, proposed, audit, parent.documents)
    status = "candidate_incomplete_budget" if issues else "candidate_audited_with_limitations"
    result = {"status":status, "stop_reason":"local_review_unresolved" if issues else "audit_completed",
        "model_calls":1, "human_validated":False, "graph_published":False, "scientific_accuracy":"not_established",
        "reading_check_issues":issues, "validation_error_count":0, "dataset_role":parent.plan["cohort_role"],
        "review_mode":POLICY, "retained_review_proof":proof, "quantitative_coverage_summary":quantitative_summary(proposed),
        "history":[{"version":1,"candidate_sha256":hash_file(run/"candidate.v01.json"), "audit_origin":ORIGIN,
            "validation_errors":0,"needs_revision":bool(issues),"reading_check_issues":issues}]}
    if packet.get("version") in FOCUSED_VERSIONS:
        result["history"][0]["candidate_sha256"] = hashlib.sha256(json.dumps(proposed.model_dump(mode="json"),
            ensure_ascii=False,indent=2,allow_nan=False).encode("utf-8")).hexdigest()
        result["source_review_final_corrections"]=[c.model_dump(mode="json") for c in report.final_corrections if c not in pending]
        if pending:
            result["deferred_source_review_corrections"]=[c.model_dump(mode="json") for c in pending]
    return result, dossier, audit


def _adjudicated(parent,proposed,packet,report):
    return _reviewed_candidate(parent,proposed,packet,report)[0]


def _reviewed_candidate(parent,proposed,packet,report):
    if packet.get("version") not in FOCUSED_VERSIONS:
        return proposed,[]
    from .focused_review_packet import correction_result, pending_structured_corrections
    try:
        result=correction_result(proposed,packet,report)
    except ValueError as error:
        if str(error) == "A dependency correction may clarify only explicitly reviewed relation text":
            from .focused_review_packet import pending_reference_wording
            separated = pending_reference_wording(proposed, packet, report)
            if separated is None:
                raise
            corrected, pending = separated
            if any(e.severity == "error" for e in validate_record(corrected, source_documents=parent.documents)):
                raise ValueError("Other source corrections require further machine or source review") from error
            return corrected, pending
        if str(error)!="Terminal corrections cannot silently change scientific identity or replace a whole scientific object":
            raise
        pending=pending_structured_corrections(proposed,packet,report)
        if not pending:
            raise
        return proposed,pending
    if any(e.severity=="error" for e in validate_record(result,source_documents=parent.documents)):
        raise ValueError("A final field correction requires further machine or source review")
    return result,[]


def execute(settings, run, *, live=False):
    from .app_server import AppServerClient
    from .reading_tools import ReadingTools
    records, parent, proposed, plan, packet = _bound(settings, run)
    run = Path(run).resolve()
    if not live or plan["code_identity"] != code_identity(settings.project_root) or plan["runtime"] != runtime_versions():
        raise ValueError("Execution needs an explicit live, frozen source-review plan")
    _latest(settings, parent, exclude=run.name)
    if plan["explicit_extra_calls"] and not records.store.last_event(run.name, "explicit_local_recovery_authority"):
        raise ValueError("Missing explicit authority for this paper")
    if retained_stage_budget(settings,parent.run) != plan["budget_before"]:
        raise ValueError("Original cumulative budget changed")
    model = settings.codex.model_copy(update={"timeout_seconds":plan["model_settings"]["timeout_seconds"],"max_calls_per_run":1})
    if model.model_dump(mode="json") != plan["model_settings"]:
        raise ValueError("Configured model differs from frozen live plan")
    # Serialize just the check and claim. The registered marker also survives
    # an interrupted worker, and a database lock is not held during a model turn.
    with records.store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.row_factory = sqlite3.Row
        _latest(settings, parent, exclude=run.name, rows=[dict(row) for row in db.execute("SELECT * FROM runs")])
        write_new_json(run/"execution_started.json", {"started_at":datetime.now().astimezone().isoformat(),"plan_sha256":hash_file(run/"plan.json")})
    client = AppServerClient(model, live=True, total_timeout_seconds=model.timeout_seconds)
    try:
        class SourceTools(ReadingTools):
            @staticmethod
            def specifications():
                read_only = {"wt_inventory", "wt_read_blocks", "wt_search", "wt_view", "wt_page_text"}
                return [spec for spec in ReadingTools.specifications() if spec["name"] in read_only]

        tools = SourceTools(parent.bundle, run, max_calls=plan.get("source_tool_limit", 30), expected_inputs=plan["input_paths"])
        client.bind_tools(tools)
        client.complete(_schema(plan.get("packet_version",1)), prompt=prompt(packet), input_paths=[Path(x["path"]) for x in plan["input_paths"]],
            workdir=run, audit_root=run/"calls", identity={"stage":"retained_issue_review","runtime":plan["runtime"],
                "rule_version":plan["rule_version"],"code_identity":plan["code_identity"]},
            image_paths=[Path(p) for p in plan["image_paths"]], conversation_key="independent-local-source-review")
        if client.events:
            client.events.flush()
        return finalize(settings, run, bound=(records,parent,proposed,plan,packet))
    except BaseException as exc:
        if not (run/"failure.json").exists():
            write_new_json(run/"failure.json", {"error":f"{type(exc).__name__}: {exc}", "model_calls":client.calls_started})
        records.store.event(run.name, "failed", {"error":str(exc)})
        raise
    finally:
        client.close()


def finalize(settings, run, *, bound=None):
    run = Path(run).resolve()
    records,parent,proposed,plan,packet = bound or _bound(settings, run)
    report, proof = _public(records, run, plan, packet)
    result, dossier, audit = _outcome(run,parent,proposed,packet,report,proof)
    proposed = _adjudicated(parent,proposed,packet,report)
    for name, value in {"reading_dossier.corrected.v01.json":dossier.model_dump(mode="json"),
            "model_review.json":audit.model_dump(mode="json"), "candidate.json":proposed.model_dump(mode="json"),
            "result.json":result}.items():
        target = run/name
        if target.exists():
            if read(target) != value:
                raise ValueError("An existing local result differs")
        else:
            write_new_json(target,value)
    records.store.event(run.name,"candidate_completed",{"status":result["status"],"result_sha256":hash_file(run/"result.json")})
    return result


def verify(settings, run, record, audit):
    try:
        records,parent,proposed,plan,packet = _bound(settings, run)
        report, proof = _public(records,run,plan,packet)
        result,dossier,parsed = _outcome(run,parent,proposed,packet,report,proof)
        return (not result["reading_check_issues"] and result == read(run/"result.json") and _adjudicated(parent,proposed,packet,report) == record and parsed == audit
                and dossier.model_dump(mode="json") == read(run/"reading_dossier.corrected.v01.json")
                and records.store.last_event(run.name,"candidate_completed")["result_sha256"] == hash_file(run/"result.json"))
    except (ValueError, KeyError, TypeError, IndexError, OSError, AttributeError):
        return False
