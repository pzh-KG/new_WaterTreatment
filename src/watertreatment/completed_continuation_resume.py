"""Authenticate a completed targeted repair awaiting its first source audit.

The completed model delivery is distinct from scientific approval.  Its original
full-audit baseline and failed-turn seed remain separately authenticated.  Only
public protocol envelopes are inspected; event text is never a model input.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from .domain import ExtractionRecord
from .staged_source_resume import (AuthenticatedStagedCandidate, _call, _candidate_binding,
    _identity, _inside, _read, _replay, _stage)
from .storage import digest_json, hash_file
from .validation import validate_record

POLICY = "completed-continuation-source-resume-v1"


def _completed_protocol(path, call):
    """Require a complete turn and every successful mutation in exact order."""
    before = hash_file(path)
    key = call["thread_id"], call["turn_id"]
    start, end, pending, seen, steps = None, None, {}, set(), []
    with path.open(encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                if not line.endswith("\n") and not stream.readline():
                    break
                raise ValueError("completed continuation has malformed public provenance")
            message = event.get("message", {})
            params = message.get("params", {})
            method = message.get("method")
            if event.get("direction") == "receive" and method in {"turn/started", "turn/completed"}:
                turn = params.get("turn", {})
                if (params.get("threadId"), turn.get("id")) == key:
                    if method == "turn/started":
                        if start is not None:
                            raise ValueError("duplicated continuation turn start")
                        start = index
                    else:
                        if end is not None or turn.get("status") != "completed":
                            raise ValueError("continuation turn is not uniquely completed")
                        end = index
            if (event.get("direction") == "receive" and method == "item/tool/call"
                    and (params.get("threadId"), params.get("turnId")) == key
                    and params.get("namespace") in (None, "")
                    and params.get("tool") in {"wt_patch_record", "wt_submit_record"}):
                identifier = message.get("id")
                if (not isinstance(identifier, (str, int)) or isinstance(identifier, bool) or identifier in seen):
                    raise ValueError("ambiguous completed continuation mutation request")
                seen.add(identifier)
                pending[identifier] = (index, params["tool"], params.get("arguments"))
            elif event.get("direction") == "send" and message.get("id") in pending:
                first, tool, arguments = pending.pop(message["id"])
                result = message.get("result", {})
                if result.get("success") is not True:
                    continue
                receipts = []
                for content in result.get("contentItems", []):
                    if content.get("type") == "inputText":
                        try:
                            data = json.loads(content.get("text", ""))
                        except json.JSONDecodeError:
                            continue
                        if isinstance(data, dict) and "submission_id" in data:
                            receipts.append(data)
                if not receipts:
                    from .arm_transport import arm_checkpoint_response
                    if arm_checkpoint_response(tool, arguments, result, path.parent.parent / "reading_tools"):
                        continue
                if len(receipts) != 1:
                    raise ValueError("completed continuation lacks one exact mutation receipt")
                steps.append({"request_index": first, "response_index": index,
                    "tool": tool, "arguments": arguments, "data": receipts[0]})
    if (hash_file(path) != before or start is None or end is None or start >= end or not steps
            or pending or any(not start < step["request_index"] < step["response_index"] < end for step in steps)):
        raise ValueError("continuation mutations are not enclosed in a stable completed turn")
    return steps, before


def authenticate_completed_continuation(settings, candidate_path: Path, *, _seen=()) -> AuthenticatedStagedCandidate:
    """Read-only authentication; never create versions, run repairs or publish."""
    from .audit_resume import authenticate_baseline, _scientific_frozen_inputs
    from .continuation_repair import repair_contract
    from .web_records import WebRecords
    from .workflow import validate_plan_metadata

    records = WebRecords(settings)
    path = records._safe_data_path(candidate_path)
    run = path.parent
    if (path.name != "candidate.v00.json" or run.parent != (settings.data_root / "runs").resolve()
            or str(run) in _seen or len(_seen) >= 4):
        raise ValueError("completed continuation requires an acyclic first candidate in this run store")
    seen = (*_seen, str(run))
    if not any(x["run_id"] == run.name and x["kind"] == "extraction" for x in records.store.runs()):
        raise ValueError("completed continuation is not a registered extraction run")
    plan, source_path, bundle, documents = records._source(run.name)
    proof = plan.get("resumed_full_audit", {})
    terminal_path, failure_path = _inside(records, run, run / "result.json"), _inside(records, run, run / "failure.json")
    terminal, failure = _read(terminal_path), _read(failure_path)
    if (plan.get("cohort_role") not in {"development", "unassigned"} or plan.get("rule_version") not in {"0.7", "0.8", "0.9"}
            or settings.rules_version != plan["rule_version"]
            or proof.get("policy") != "bounded-scientific-revision-v1"
            or terminal.get("status") != "candidate_incomplete_failure"
            or terminal.get("last_candidate_version") != path.name
            or terminal.get("human_validated") is not False or terminal.get("graph_published") is not False
            or terminal.get("dataset_role") != plan["cohort_role"] or not failure.get("error")):
        raise ValueError("only an unpublished completed repair awaiting source review is resumable here")
    if (list(run.glob("audit.v*.json")) or (run / "model_review.json").exists()
            or any(p.name != path.name for p in run.glob("candidate.v*.json"))):
        raise ValueError("a later candidate or any completed audit must not be bypassed")
    candidate = ExtractionRecord.model_validate(_read(path))
    baseline, audit, original_bundle = authenticate_baseline(settings, proof, candidate, _seen=seen)
    seed = _scientific_frozen_inputs(proof, run, baseline, audit)
    seed_path = _inside(records, run, run / "reused_candidate.json")
    if (ExtractionRecord.model_validate(_read(seed_path)) != seed
            or _identity(seed) != _identity(candidate)
            or digest_json(original_bundle["documents"]) != digest_json(bundle["documents"])):
        raise ValueError("completed continuation differs from its frozen source-bound seed")
    contract_path = _inside(records, run, run / "continuation_repair_prompt.txt")
    contract = repair_contract(plan.get("continuation_repairs"))
    if contract_path.read_text(encoding="utf-8") != contract:
        raise ValueError("completed continuation repair instructions changed")
    calls = list((run / "calls").glob("*/request.json"))
    if len(calls) != 1:
        raise ValueError("completed initial continuation cannot bypass later model calls")
    request_path = _inside(records, run, calls[0])
    request, completed, sources, result_path = _call(records, run, request_path, plan,
        stage="continuation_repair_0", status="schema_valid_candidate")
    if contract not in request.get("prompt", "") or sources.get(str(contract_path)) != hash_file(contract_path):
        raise ValueError("completed repair call did not bind its frozen targeted contract")
    base_path = _candidate_binding(records, run, request, sources, seed)
    record_path = _inside(records, run, request_path.parent / "record.json")
    answer_path = _inside(records, run, request_path.parent / "answer.json")
    answer = _read(answer_path)
    if (set(answer) != {"submission_id"} or answer["submission_id"] != completed.get("submission_id")
            or hash_file(path) != hash_file(record_path)
            or ExtractionRecord.model_validate(_read(record_path)) != candidate):
        raise ValueError("completed continuation final delivery differs from its saved candidate")
    progress_path = _inside(records, run, run / "continuation_repair.json")
    progress = _read(progress_path)
    if (progress.get("targets") != plan["continuation_repairs"]
            or Path(progress.get("input_path", "")).resolve() != seed_path
            or progress.get("input_sha256") != hash_file(seed_path)
            or progress.get("input_content_hash") != digest_json(seed.model_dump(mode="json"))
            or progress.get("output_content_hash") != digest_json(candidate.model_dump(mode="json"))
            or progress.get("status") != "model_repaired_requires_source_audit"
            or progress.get("human_validated") is not False or progress.get("graph_published") is not False):
        raise ValueError("completed repair summary is not bound to its exact input and output")
    issues = validate_record(candidate, source_documents=documents)
    issues += validate_plan_metadata(candidate, plan, documents, store=records.store)
    if any(issue.severity == "error" for issue in issues):
        raise ValueError("completed continuation has structural source/identity errors")
    event_path = _inside(records, run, run / "app_server/events.jsonl")
    public, event_hash = _completed_protocol(event_path, completed)
    current, chain = seed, []
    for step in public:
        identifier = step["data"].get("submission_id", "").rsplit("-", 1)[0]
        if re.fullmatch(r"tool-\d{4,}", identifier) is None:
            raise ValueError("completed continuation mutation has no local stage")
        item = _stage(records, run, run / "reading_tools" / identifier / "submitted_record.json")
        if (item["request"] != {"tool": step["tool"], "arguments": step["arguments"]}
                or item["data"] != step["data"]):
            raise ValueError("completed continuation stage differs from public tool delivery")
        current = _replay(current, item["request"])
        if current != item["record"] or _identity(current) != _identity(seed):
            raise ValueError("completed continuation patch chain does not reproduce its retained records")
        chain.append(item)
    stages = sorted((run / "reading_tools").glob("tool-*/submitted_record.json"),
                    key=lambda p: int(p.parent.name.removeprefix("tool-")))
    if (current != candidate or chain[-1]["data"]["submission_id"] != answer["submission_id"]
            or not stages or stages[-1].resolve() != chain[-1]["path"]):
        raise ValueError("completed continuation did not deliver its last complete checkpoint")
    formal = run / "candidate.json"
    if formal.exists() and hash_file(formal) != hash_file(path):
        raise ValueError("completed continuation formal candidate differs")
    inputs = list(dict.fromkeys([run / "plan.json", source_path, path, terminal_path, failure_path,
        seed_path, contract_path, base_path, progress_path, request_path, result_path, record_path, answer_path,
        *[Path(x["path"]).resolve() for x in plan["input_paths"]],
        *[Path(x["path"]).resolve() for x in plan["reading_artifacts"]],
        *[p for item in chain for p in (item["path"], item["request_path"], item["result_path"])]]))
    receipt = {"policy": POLICY, "parent_run": run.name, "parent_plan_sha256": hash_file(run / "plan.json"),
        "parent_sources_sha256": hash_file(source_path), "parent_code_identity": plan["code_identity"],
        "candidate_path": str(path), "candidate_sha256": hash_file(path),
        "candidate_content_hash": digest_json(candidate.model_dump(mode="json")),
        "baseline_candidate_path": proof["original_candidate_path"],
        "baseline_candidate_sha256": proof["original_candidate_sha256"],
        "baseline_audit_path": proof["original_audit_path"], "baseline_audit_sha256": proof["original_audit_sha256"],
        "inherited_full_audit": proof, "completed_call_path": str(request_path.parent),
        "submission_id": answer["submission_id"], "events_sha256": event_hash,
        "model": completed["server_selected_model"], "effort": completed["server_selected_effort"],
        "turn_status": "completed_continuation_awaiting_source_audit", "requires_new_source_audit": True,
        "scientific_validation": "not_established",
        "artifact_hashes": [{"path": str(p), "sha256": hash_file(p)} for p in inputs],
        "staged_chain": [{"path": str(x["path"]), "sha256": hash_file(x["path"]),
                          "submission_id": x["data"]["submission_id"]} for x in chain]}
    if hash_file(event_path) != event_hash:
        raise ValueError("completed continuation protocol changed while authenticating")
    return AuthenticatedStagedCandidate(candidate, bundle, documents, run, baseline, audit,
        Path(proof["original_candidate_path"]), Path(proof["original_audit_path"]), receipt, inputs)
