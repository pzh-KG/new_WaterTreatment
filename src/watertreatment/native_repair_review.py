"""Recover a submitted local revision, or a reproducible proposal denied before staging."""
import json
from pathlib import Path

from .audit_addendum_workflow import _completed_call_protocol
from .completed_native_resume import _final_submission, authenticate_completed_revision
from .domain import ExtractionRecord
from .reading import ReadingAudit, ReadingDossier
from .retained_review_parent import RetainedParent, read
from .scientific_revision_audit import validate_scientific_revision_audit
from .source_review import verify_source_review
from .staged_source_resume import _call, _candidate_binding, _replay
from .storage import digest_json, hash_file
from .web_records import WebRecords


MODE = "native-repair-targets-v1"


def _one_call(records, run, plan, stage, status):
    paths = [p for p in run.glob("calls/*/request.json") if read(p)["request"].get("stage") == stage]
    if len(paths) != 1:
        raise ValueError("Expected one native call at the retained step")
    request, completed, bound, _ = _call(records, run, paths[0], plan, stage=stage, status=status)
    if any(hash_file(records._safe_data_path(p)) != sha for p, sha in bound.items()):
        raise ValueError("An immutable native input changed")
    return paths[0].parent, request, completed, bound


def _denied_request(run, completed, *, submitted=None):
    """A denied command is a proposal only. Never issue a successful submission receipt for it."""
    events = run/"app_server/events.jsonl"
    original_hash = hash_file(events)
    key = completed["thread_id"], completed["turn_id"]
    starts, ends, pending, exchanges, seen = [], [], {}, [], set()
    with events.open(encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            row = json.loads(line)
            m = row.get("message", {})
            p = m.get("params", {})
            method = m.get("method")
            if row["direction"] == "receive":
                if (p.get("threadId"), p.get("turn", {}).get("id")) == key:
                    if method == "turn/started":
                        starts.append(index)
                    elif method == "turn/completed":
                        ends.append((index,p["turn"]["status"]))
                if (method == "item/tool/call" and (p.get("threadId"),p.get("turnId")) == key
                        and p.get("tool") in {"wt_patch_record","wt_submit_record"}):
                    if m["id"] in seen or p.get("namespace") not in (None, ""):
                        raise ValueError("Ambiguous native revision command")
                    seen.add(m["id"])
                    pending[m["id"]] = (index, {"tool":p["tool"], "arguments":p["arguments"]})
            elif row["direction"] == "send" and m.get("id") in pending and "result" in m:
                start, command = pending.pop(m["id"])
                exchanges.append((start,index,command,m["result"]))
    if (len(starts) != 1 or len(ends) != 1 or ends[0][1] != "completed" or pending or not exchanges
            or any(not starts[0] < first < last < ends[0][0] for first,last,_,_ in exchanges)
            or any(a[1] >= b[0] for a,b in zip(exchanges,exchanges[1:])) or hash_file(events) != original_hash):
        raise ValueError("Denied proposals require an intact ordered completed turn")
    _, _, command, denial = exchanges[-1]
    if submitted:
        # All successful mutations have already been authenticated by the native
        # delivery verifier. Require exactly one later, publicly rejected attempt.
        if (len(exchanges) < 2 or not _returned_submission(exchanges[-2][3], submitted)
                or _final_submission(events, completed) != {"submission_id":submitted}):
            raise ValueError("Rejected tail is not immediately after the final delivered submission")
    elif any(reply.get("success") is not False for _,_,_,reply in exchanges):
        raise ValueError("The allowance-denied turn contains a successful submission")
    messages=[c.get("text","") for c in denial.get("contentItems",[]) if c.get("type")=="inputText"]
    overlap = "grouped edits cannot overlap; submit sequential batches explicitly"
    if (command["tool"] != "wt_patch_record" or denial.get("success") is not False or not messages
            or not ("pool exhausted" in messages[0] or submitted and messages[0] == overlap)):
        raise ValueError("The final proposed patch was not denied by a tool allowance")
    stored_denial = dict(denial)
    items=denial.get("contentItems",[])
    if items and items[-1].get("type")=="inputText":
        try:
            clock=json.loads(items[-1]["text"])
        except ValueError:
            clock=None
        if (isinstance(clock,dict) and set(clock)=={"budget_source","remaining_turn_seconds"}
                and clock["budget_source"]=="controller_monotonic_deadline" and type(clock["remaining_turn_seconds"]) is int):
            stored_denial["contentItems"]=items[:-1]
    matches = [p for p in (run/"reading_tools").glob("*/request.json") if read(p) == command
               and p.with_name("result.json").is_file() and read(p.with_name("result.json")) == stored_denial]
    if len(matches) != 1:
        raise ValueError("The rejected exchange does not exactly match its local tool record")
    return command, {"native_events_sha256":original_hash, "denied_request":str(matches[0]),
                     "denied_request_sha256":hash_file(matches[0]), "denial_sha256":hash_file(matches[0].with_name("result.json"))}


def _returned_submission(reply, expected):
    if reply.get("success") is not True:
        return False
    ids=[]
    for item in reply.get("contentItems",[]):
        if item.get("type") != "inputText":
            continue
        try:
            payload=json.loads(item["text"])
        except (ValueError, KeyError):
            continue
        if isinstance(payload,dict) and "submission_id" in payload:
            ids.append(payload["submission_id"])
    return ids == [expected]


def _merge_reviewed_fields(base, reviewed, proposal):
    """Keep both independent edits, and reject a conflict with a newer source judgment."""
    if reviewed == base or reviewed == proposal:
        return proposal
    if proposal == base:
        return reviewed
    if all(isinstance(v,dict) for v in (base,reviewed,proposal)) and base.keys()==reviewed.keys()==proposal.keys():
        if "id" not in base or base["id"]==reviewed["id"]==proposal["id"]:
            return {k:_merge_reviewed_fields(base[k],reviewed[k],proposal[k]) for k in base}
    if (all(isinstance(v,list) for v in (base,reviewed,proposal))
            and len(reviewed)==len(base) and len(proposal)>=len(base)):
        result = [_merge_reviewed_fields(a,b,c) for a,b,c in zip(base,reviewed,proposal)]
        return result + proposal[len(base):]
    raise ValueError("A rejected proposal overlaps a subsequent independent source correction")


def retained_tail_proposal(parent):
    """Recover the last rejected command as a new proposal on an authenticated lineage."""
    origin, receipt = parent.run, parent.receipt
    plan = parent.plan
    while plan.get("workflow")=="retained-issue-source-review-v1":
        if read(origin/"proposal.json").get("proposal_origin",{}).get("policy")=="public-denied-tail-proposal-v1":
            return None  # This exact tail already entered an independently reviewed candidate.
        receipt=read(origin/"parent_receipt.json")
        earlier=(origin.parent/plan["parent_run"]).resolve()
        if earlier.parent!=origin.parent or earlier.name>=origin.name:
            raise ValueError("Unexpected parent of a retained revision proposal")
        origin,plan=earlier,read(earlier/"plan.json")
    proof=receipt.get("baseline",{})
    if proof.get("policy")!="completed-native-revision-source-resume-v1" or proof.get("parent_run")!=origin.name:
        return None
    submitted=proof["submission_id"]
    final_step=submitted.rsplit("-",1)[0]
    # Older, successfully retried denials have no outstanding proposal.
    tools_dir=origin/"reading_tools"
    later=sorted((p for p in tools_dir.glob("tool-*/request.json")
                  if int(p.parent.name.split("-")[-1])>int(final_step.split("-")[-1])
                  and read(p).get("tool") in {"wt_patch_record","wt_submit_record"}),
                 key=lambda p:int(p.parent.name.split("-")[-1]))
    if not later:
        return None
    terminal=read(later[-1].with_name("result.json"))
    items=terminal.get("contentItems",[])
    reason=items[0].get("text","") if items else ""
    if (terminal.get("success") is not False or (reason!="grouped edits cannot overlap; submit sequential batches explicitly"
            and "pool exhausted" not in reason)):
        return None
    completed=read(Path(proof["completed_call_path"])/"result.json")
    command, denial=_denied_request(origin,completed,submitted=submitted)
    if Path(denial["denied_request"])!=later[-1] or denial["native_events_sha256"]!=proof["events_sha256"]:
        raise ValueError("Rejected command differs from the authenticated final revision turn")
    original=ExtractionRecord.model_validate(read(Path(proof["candidate_path"])))
    amended=_replay(original,command)
    before,reviewed,proposed=(r.model_dump(mode="json") for r in (original,parent.candidate,amended))
    merged=_merge_reviewed_fields(before,reviewed,proposed)
    if merged==reviewed:
        return None
    return {"base_hash":digest_json(reviewed),"candidate":merged,"source_blocks":[],
            "proposal_origin":{"policy":"public-denied-tail-proposal-v1","submission_succeeded":False,
                               "prior_submission_id":submitted,"origin_run":origin.name,**denial}}


def _denied(records, run, plan, version, baseline):
    audit = ReadingAudit.model_validate(read(run/f"audit.v{version:02d}.json"))
    call, request, outcome, inputs = _one_call(records, run, plan, f"audit_{version}", "schema_valid_candidate")
    if not records._queried_candidate_snapshot(run, baseline, request, inputs) or ReadingAudit.model_validate(read(call/"answer.json")) != audit:
        raise ValueError("Nearest source audit does not match its original candidate and answer")
    audit_events = _completed_call_protocol(run,outcome,read(call/"answer.json"))
    if audit.review_scope == "scientific_delta" and version == 1:
        desc = request["citation_review"]
        pairs = (("scope",run/"citation_scope.v01.json"), ("current_candidate",run/"candidate.v01.json"),
                 ("baseline_candidate",run/"candidate.v00.json"), ("baseline_audit",run/"audit.v00.json"))
        for kind, path in pairs:
            digest_key = "scope_file_sha256" if kind == "scope" else kind+"_sha256"
            if Path(desc[kind+"_path"]).resolve() != path or hash_file(path) != desc[digest_key] or inputs.get(str(path)) != desc[digest_key]:
                raise ValueError("Previous local review inputs differ from authenticated sources")
        scope = read(run/"citation_scope.v01.json")
        before = ExtractionRecord.model_validate(read(run/"candidate.v00.json"))
        old_audit = ReadingAudit.model_validate(read(run/"audit.v00.json"))
        if (desc.get("additional_audits") != [] or not records._completed_audit_provenance(run,before,old_audit,allow_citation=False)
                or request.get("source_review_call") != call.name or request.get("source_review_scope_sha256") != scope["scope_sha256"]
                or not verify_source_review(run,scope,outcome.get("source_review_proof"),call.name)
                or validate_scientific_revision_audit(scope,old_audit,audit,old_record=before,new_record=baseline)):
            raise ValueError("The previous independent local source review still has unauthenticated obligations")
    elif not records._completed_audit_provenance(run,baseline,audit):
        raise ValueError("Missing independent baseline audit for the proposed patch")
    call, request, outcome, inputs = _one_call(records,run,plan,f"revision_{version+1}","failed")
    _candidate_binding(records,run,request,inputs,baseline)
    final = _final_submission(run/"app_server/events.jsonl",outcome)
    if final != {"submission_id":""} or read(call/"answer.json") != final:
        raise ValueError("The failed turn requires separate submission reconciliation")
    command, receipt = _denied_request(run,outcome)
    proposed = _replay(baseline,command)
    return proposed,audit,{"policy":"native-denied-patch-proposal-v1","submission_succeeded":False, **receipt,
                          "candidate_content_hash":digest_json(proposed.model_dump(mode="json")), "audit_events_sha256":audit_events}


def available(run):
    if not run or not (run/"result.json").is_file():
        return False
    result = read(run/"result.json")
    return (result.get("status") == "candidate_incomplete_failure" and
        (any(run.glob("scope_rejection.v*.json")) or any(
         (p.with_name("answer.json").is_file() and read(p.with_name("answer.json")) == {"submission_id":""})
         for p in run.glob("calls/*/request.json") if read(p)["request"].get("stage","").startswith("revision_"))))


def authenticate_native(settings, run):
    records = WebRecords(settings)
    plan, _, bundle, docs = records._source(run.name)
    final = read(run/"result.json")
    if not available(run) or final["human_validated"] is not False or final["graph_published"] is not False:
        raise ValueError("Expected an unpublished failed localized revision")
    versions = sorted(run.glob("candidate.v[0-9][0-9].json"))
    path = versions[-1]
    if final.get("last_candidate_version") != path.name or hash_file(path) != hash_file(run/"candidate.json"):
        raise ValueError("The terminal result no longer identifies its latest retained candidate")
    version = int(path.stem[-2:])
    audit_file = run/f"audit.v{version:02d}.json"
    if audit_file.exists():
        before = ExtractionRecord.model_validate(read(path))
        current,audit,receipt = _denied(records,run,plan,version,before)
    else:
        rejected = read(run/f"scope_rejection.v{version:02d}.json")
        if rejected.get("candidate_sha256") != hash_file(path) or rejected.get("automatic_full_audit") is not False:
            raise ValueError("Local rejection differs from its immutable candidate")
        delivered = authenticate_completed_revision(settings,path,scoped_baseline=True)
        before,current,audit,receipt = delivered.baseline_record,delivered.record,delivered.baseline_audit,delivered.receipt
    dossier = ReadingDossier.model_validate(read(run/"reading_dossier.json"))
    if plan.get("identity_correction"):
        from .identity_resume import verify_identity_resume
        verify_identity_resume(settings,run,plan)
    elif plan.get("reused_dossier"):
        from .workflow import _reusable_dossier
        if plan.get("budget_continuation"):
            from .budget_extension_workflow import verify_extension_plan
            verify_extension_plan(settings,run,plan)
        origin = records._safe_data_path(plan["reused_dossier"]["path"])
        inherited, proof = _reusable_dossier(settings,origin,bundle,plan["cohort_role"],historical_review=True)
        if (inherited != dossier or origin.parent.name != plan["reused_dossier"].get("parent_run")
                or hash_file(origin) != plan["reused_dossier"].get("sha256")
                or proof["parent_code_identity"] != plan["reused_dossier"].get("parent_code_identity")):
            raise ValueError("Inherited reading dossier no longer matches its original completed comprehension")
    else:
        call, request, completed, _ = _one_call(records,run,plan,"comprehension","schema_valid_candidate")
        answer=read(call/"answer.json")
        inherited=ReadingDossier.model_validate(answer)
        if request.get("dossier_coverage_transport"):
            from .coverage_transport import POLICY, expand_dossier
            inherited, expansion=expand_dossier(answer,bundle)
            if request["dossier_coverage_transport"]!=POLICY or expansion!=completed.get("dossier_coverage_expansion"):
                raise ValueError("The original range expansion has changed")
        if (run/"dossier_identity_binding.json").is_file():
            from .identity_resume import verify_dossier_binding
            verify_dossier_binding(run,plan)
            if ReadingDossier.model_validate(read(run/"reading_dossier.model.json"))!=inherited:
                raise ValueError("Source identity binding has an unrelated model dossier")
            inherited=inherited.model_copy(update={"paper_id":plan["paper_id"]})
        if inherited != dossier:
            raise ValueError("Reading dossier differs from the delivered comprehension")
        _completed_call_protocol(run,completed,answer)
    findings = [{"text":f.explanation,"paths":f.record_paths or [f.record_path],
                 "evidence":[a.model_dump(mode="json") for a in f.evidence]}
                for f in audit.findings if f.needs_revision or f.verdict in {"unsupported","not_checked"}]
    findings.extend({"text":omission,"evidence":[]} for omission in audit.potential_omissions)
    return RetainedParent(run,plan,bundle,docs,before,current,dossier,audit,findings,[],
                          {"mode":MODE,"baseline":receipt,"dossier_sha256":hash_file(run/"reading_dossier.json")})
