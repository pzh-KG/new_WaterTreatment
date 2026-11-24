"""Authenticate a failed CLI revision checkpoint without promoting it.

Only public protocol envelopes are inspected in event logs.  The event log is
never an input to a later model.  A staged record remains fallible and requires
a new source audit; this module supplies provenance, not scientific approval.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from .model_policy import receipt_matches_plan
from .domain import ExtractionRecord
from .reading import AUDIT_DIMENSIONS, ReadingAudit
from .storage import digest_json, hash_file
from .validation import validate_record


POLICY = "staged-cli-source-resume-v1"
_STAGE = re.compile(r"tool-(\d{4,})")


@dataclass(frozen=True)
class AuthenticatedStagedCandidate:
    record: ExtractionRecord
    bundle: dict
    documents: list
    parent_run: Path
    baseline_record: ExtractionRecord
    baseline_audit: ReadingAudit
    baseline_candidate_path: Path
    baseline_audit_path: Path
    receipt: dict
    input_paths: list[Path]


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _inside(records, run, path):
    resolved = records._safe_data_path(path)
    if not resolved.is_relative_to(run):
        raise ValueError("staged resume artifact escaped its parent run")
    return resolved


def _identity(record):
    return (record.paper.id, record.paper.dataset_role, record.paper.source_ids,
            record.paper.role_history, record.rule_version, record.schema_version,
            record.extraction_scope, record.digitization_targets)


def _call(records, run, path, plan, *, stage, status):
    path = _inside(records, run, path)
    result_path = _inside(records, run, path.parent / "result.json")
    envelope, completed = _read(path), _read(result_path)
    request = envelope.get("request", {})
    bound = {str(Path(i["path"]).resolve()): i["sha256"] for i in request.get("sources", [])}
    if (envelope.get("fingerprint") != digest_json(request)
            or request.get("stage") != stage or completed.get("status") != status
            or request.get("code_identity") != plan.get("code_identity")
            or request.get("rule_version") != plan.get("rule_version")
            or not receipt_matches_plan(plan, request, completed)
            or not isinstance(completed.get("thread_id"), str) or not completed["thread_id"]
            or not isinstance(completed.get("turn_id"), str) or not completed["turn_id"]
            or not plan.get("input_paths")
            or any(bound.get(str(Path(i["path"]).resolve())) != i["sha256"] for i in plan["input_paths"])):
        raise ValueError("staged resume requires an intact source-bound Sol MAX call")
    return request, completed, bound, result_path


def _candidate_binding(records, run, request, sources, record):
    binding = request.get("candidate_binding")
    if (not isinstance(binding, dict) or set(binding) != {"path", "sha256", "content_hash", "scope"}
            or any(not isinstance(x, str) or not x.strip() for x in binding.values())):
        raise ValueError("failed revision has no exact controller-bound candidate")
    snapshot = _inside(records, run, binding["path"])
    if (snapshot.parent != run / "revision_bases"
            or snapshot.name != binding["scope"] + ".json"
            or hash_file(snapshot) != binding["sha256"]
            or sources.get(str(snapshot)) != binding["sha256"]
            or binding["content_hash"] != digest_json(record.model_dump(mode="json"))
            or "CANDIDATE_SNAPSHOT " + binding["content_hash"] not in request.get("prompt", "")
            or ExtractionRecord.model_validate(_read(snapshot)) != record):
        raise ValueError("failed revision base differs from the nearest audited candidate")
    return snapshot


def _stage(records, run, path):
    path = _inside(records, run, path)
    request_path = _inside(records, run, path.parent / "request.json")
    result_path = _inside(records, run, path.parent / "result.json")
    request, result = _read(request_path), _read(result_path)
    data = result.get("data", {})
    record = ExtractionRecord.model_validate(_read(path))
    if (request.get("tool") not in {"wt_patch_record", "wt_submit_record"}
            or result.get("success") is not True or data.get("schema_valid") is not True
            or data.get("published") is not False
            or data.get("canonical_record_sha256") != hash_file(path)
            or data.get("base_hash") != digest_json(record.model_dump(mode="json"))
            or data.get("submission_id") != path.parent.name + "-" + hash_file(path)[:16]):
        raise ValueError("latest staged candidate lacks a matching successful local receipt")
    return {"path": path, "request_path": request_path, "result_path": result_path,
            "request": request, "data": data, "record": record}


def _public_protocol(events_path, audit_call, failed_call):
    """Read just public turn/tool envelopes; never expose other event contents."""
    before = hash_file(events_path)
    audit_key = audit_call["thread_id"], audit_call["turn_id"]
    failed_key = failed_call["thread_id"], failed_call["turn_id"]
    starts, ends, pending, successes, seen_requests = {}, {}, {}, [], set()
    with events_path.open(encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # Only an unfinished last line may be a crash artifact.  Never
                # silently ignore malformed interior provenance.
                if not line.endswith("\n") and not stream.readline():
                    break
                raise ValueError("staged protocol has malformed interior provenance")
            message = event.get("message", {})
            params = message.get("params", {})
            method = message.get("method")
            if event.get("direction") == "receive" and method in {"turn/started", "turn/completed"}:
                turn = params.get("turn", {})
                key = params.get("threadId"), turn.get("id")
                if key in {audit_key, failed_key}:
                    if method == "turn/started":
                        if key in starts:
                            raise ValueError("ambiguous duplicated turn-start provenance")
                        starts[key] = index
                    else:
                        if key in ends:
                            raise ValueError("ambiguous duplicated turn-completion provenance")
                        ends[key] = index, turn.get("status")
            if (event.get("direction") == "receive" and method == "item/tool/call"
                    and (params.get("threadId"), params.get("turnId")) == failed_key
                    and params.get("namespace") in (None, "")
                    and params.get("tool") in {"wt_patch_record", "wt_submit_record"}):
                key = message.get("id")
                if (not isinstance(key, (int, str)) or isinstance(key, bool)
                        or key in seen_requests):
                    raise ValueError("ambiguous repeated staged tool request ID")
                seen_requests.add(key)
                pending[key] = (index, params["tool"], params.get("arguments"))
            elif event.get("direction") == "send" and message.get("id") in pending:
                request_index, tool, arguments = pending.pop(message["id"])
                response = message.get("result", {})
                if response.get("success") is not True:
                    continue
                payloads = []
                for item in response.get("contentItems", []):
                    if item.get("type") == "inputText":
                        try:
                            payload = json.loads(item.get("text", ""))
                        except json.JSONDecodeError:
                            continue
                        if isinstance(payload, dict) and "submission_id" in payload:
                            payloads.append(payload)
                if not payloads:
                    from .arm_transport import arm_checkpoint_response
                    if arm_checkpoint_response(tool, arguments, response, events_path.parent.parent / "reading_tools"):
                        continue
                if len(payloads) != 1:
                    raise ValueError("successful staging response lacks one unambiguous receipt")
                successes.append({"request_index": request_index, "response_index": index,
                                  "tool": tool, "arguments": arguments, "data": payloads[0]})
    if hash_file(events_path) != before:
        raise ValueError("staged protocol changed during verification")
    ended = ends.get(audit_key)
    if (audit_key not in starts or failed_key not in starts or ended is None
            or ended[1] != "completed" or not starts[audit_key] < ended[0] < starts[failed_key]
            or (failed_key in ends and ends[failed_key][1] == "completed") or not successes
            or any(item["request_index"] <= starts[failed_key]
                   or item["response_index"] <= item["request_index"]
                   or (failed_key in ends and item["response_index"] >= ends[failed_key][0])
                   for item in successes)):
        raise ValueError("failed revision does not follow the completed full audit in protocol order")
    return successes, before


def _replay(original, request):
    # The same literal-only transport code is used by live staging. It has no
    # provider/filesystem side effects and does not infer scientific values.
    from .literal_record_transport import apply_record_edits, expand_record_wire
    arguments = request["arguments"]
    if "repair_draft" in arguments:
        raise ValueError("rejected initial submissions require their exact earlier raw tool request and receipt")
    if request["tool"] == "wt_patch_record":
        if arguments.get("base_hash") != digest_json(original.model_dump(mode="json")):
            raise ValueError("staged patch chain does not bind its preceding exact candidate")
        changed, _ = apply_record_edits(original.model_dump(mode="json"), arguments["edits"])
    else:
        changed, _ = expand_record_wire(arguments["record"], arguments.get("shared_values", {}), arguments.get("field_layouts", {}))
    return ExtractionRecord.model_validate(changed)


def _replay_stage(original, item, expected_paper_id):
    """Replay literal science and an optional exact initial controller ID binding.

    A revision already owns a source-bound identity and cannot receive a new
    binding. Initial projections require the retained raw input and receipt;
    no other field may change, even if a model supplied a misleading DOI.
    """
    arguments = item["request"]["arguments"]
    if "repair_draft" in arguments:
        from .rejected_submission import DraftRepair, repair_input
        if original is not None or item["request"]["tool"] != "wt_submit_record" or set(arguments) != {"repair_draft"}:
            raise ValueError("a rejected initial draft cannot replace an existing candidate")
        raw, proof = repair_input(item["path"].parent, DraftRepair.model_validate(arguments["repair_draft"]))
        transport = item["data"].get("transport", {})
        if any(transport.get(k) != v for k, v in proof.items()):
            raise ValueError("rejected draft correction disagrees with the recorded transport")
        current = ExtractionRecord.model_validate(raw)
    elif arguments.get("arm_part_ids"):
        from .arm_transport import replay_arm_assembly
        if original is not None or item["request"]["tool"] != "wt_submit_record":
            raise ValueError("Arm assembly cannot replace a revision base")
        if item["data"].get("transport", {}).get("arm_part_ids") != arguments["arm_part_ids"]:
            raise ValueError("Assembly receipt disagrees with its frozen arm references")
        current = ExtractionRecord.model_validate(replay_arm_assembly(item["path"].parent, arguments))
    else:
        current = _replay(original, item["request"])
    stream_binding = item["data"].get("transport", {}).get("stream_identity_binding")
    if stream_binding:
        from .stream_identity import bind_stream_identifiers
        receipt = item["path"].parent / "stream_identity_binding.json"
        if original is not None or item["request"]["tool"] != "wt_submit_record":
            raise ValueError("Revision cannot silently rename stream identities")
        proof = bind_stream_identifiers(current)
        if (not proof or proof != _read(receipt) or stream_binding != {"policy": proof["policy"], "sha256": hash_file(receipt),
                "references": proof["references"]}):
            raise ValueError("Stream references do not reproduce their original explicit configuration binding")
    binding = item["data"].get("transport", {}).get("system_identity_binding")
    if binding:
        raw_path = item["path"].parent / "unbound_submitted_record.json"
        projected = current.model_copy(deep=True)
        projected.paper.id = expected_paper_id
        expected = {"policy": "controller-paper-id-binding-v1", "path": "paper.id",
            "before": current.paper.id, "after": projected.paper.id,
            "raw_record_path": str(raw_path), "raw_record_sha256": hash_file(raw_path),
            "corrected_content_hash": digest_json(projected.model_dump(mode="json")),
            "scientific_fields_changed": False}
        if (original is not None or item["request"]["tool"] != "wt_submit_record"
                or binding != expected or ExtractionRecord.model_validate(_read(raw_path)) != current):
            raise ValueError("controller identity binding changed non-system fields or an existing revision identity")
        current = projected
    return current


def authenticate_staged_candidate(settings, stage_path: Path) -> AuthenticatedStagedCandidate:
    """Authenticate the latest checkpoint of a failed audited CLI revision.

    No new run, record version, graph write, or model call is made.  The caller
    must separately freeze a new continuation scope and require a new audit.
    In particular, this function never creates an invented ``candidate.vNN``.
    """
    from .web_records import WebRecords
    from .web_model import _reading_checks
    from .workflow import validate_plan_metadata

    records = WebRecords(settings)
    path = records._safe_data_path(stage_path)
    match = _STAGE.fullmatch(path.parent.name)
    run = path.parent.parent.parent
    if (path.name != "submitted_record.json" or match is None
            or path.parent.parent != run / "reading_tools"
            or run.parent != (settings.data_root / "runs").resolve()):
        raise ValueError("staged resume requires a registered run's exact staging path")
    registration = next((entry for entry in records.store.runs()
                         if entry["run_id"] == run.name and entry["kind"] == "extraction"), None)
    if registration is None:
        raise ValueError("staged resume requires a registered CLI extraction run")
    plan, source_path, bundle, documents = records._source(run.name)
    terminal_path = _inside(records, run, run / "result.json")
    failure_path = _inside(records, run, run / "failure.json")
    terminal, failure = _read(terminal_path), _read(failure_path)
    # Production (unassigned) papers share the same provenance contract as
    # development papers. Never relabel them to enter recovery. Heldout work
    # remains separate so a repaired case cannot silently become validation.
    if (plan.get("cohort_role") not in {"development", "unassigned"} or plan.get("rule_version") not in {"0.7", "0.8", "0.9"}
            or settings.rules_version != plan["rule_version"]
            or terminal.get("status") != "candidate_incomplete_failure"
            or terminal.get("human_validated") is not False or terminal.get("graph_published") is not False
            or terminal.get("dataset_role") != plan["cohort_role"] or not failure.get("error")):
        raise ValueError("only unpublished failed non-heldout revisions with unchanged cohort can supply this checkpoint")
    stages = sorted((run / "reading_tools").glob("*/submitted_record.json"),
                    key=lambda p: int(_STAGE.fullmatch(p.parent.name)[1]) if _STAGE.fullmatch(p.parent.name) else -1)
    if not stages or _inside(records, run, stages[-1]) != path:
        raise ValueError("older staged revisions cannot bypass a later staged artifact")
    selected = _stage(records, run, path)  # Corrupt newest files fail; never fall back.
    candidate = selected["record"]
    baseline_name = terminal.get("last_candidate_version", "")
    if not re.fullmatch(r"candidate\.v[0-9]{2}\.json", baseline_name):
        raise ValueError("failed revision must identify its nearest completed candidate")
    index = int(baseline_name[11:13])
    baseline_path = _inside(records, run, run / baseline_name)
    audit_path = _inside(records, run, run / f"audit.v{index:02d}.json")
    baseline = ExtractionRecord.model_validate(_read(baseline_path))
    audit = ReadingAudit.model_validate(_read(audit_path))
    formal_path = _inside(records, run, run / "candidate.json")
    if (hash_file(formal_path) != hash_file(baseline_path)
            or _identity(candidate) != _identity(baseline)
            or audit.review_scope != "full" or audit.review_scope_sha256 is not None
            or set(audit.checked_dimensions) != set(AUDIT_DIMENSIONS)
            or len(audit.checked_dimensions) != len(AUDIT_DIMENSIONS)
            or not (audit.potential_omissions or any(f.needs_revision or f.verdict == "unsupported" for f in audit.findings))
            or not records._completed_audit_provenance(run, baseline, audit, allow_citation=False)):
        raise ValueError("checkpoint lacks its exact latest completed but unresolved full audit")
    newer = [p for p in run.glob("candidate.v[0-9][0-9].json") if int(p.stem[-2:]) > index]
    newer += [p for p in run.glob("audit.v[0-9][0-9].json") if int(p.stem[-2:]) > index]
    if newer:
        raise ValueError("a newer candidate or audit must not be bypassed by staged recovery")
    for record in (baseline, candidate):
        issues = validate_record(record, source_documents=documents)
        issues += validate_plan_metadata(record, plan, documents, store=records.store)
        if any(issue.severity == "error" for issue in issues):
            raise ValueError("staged recovery requires structurally valid source-bound records")
    if _reading_checks(baseline, audit, documents):
        raise ValueError("baseline has unresolved reading consistency defects")
    audit_matches, failed_matches = [], []
    for request_path in sorted((run / "calls").glob("*/request.json")):
        stage = _read(request_path).get("request", {}).get("stage")
        numbered = re.fullmatch(r"(audit|revision)_([0-9]+)", stage or "")
        if numbered and int(numbered[2]) > index + (numbered[1] == "revision"):
            raise ValueError("a later audit or revision call cannot be bypassed by staged recovery")
        if stage == f"audit_{index}":
            details = _call(records, run, request_path, plan, stage=stage, status="schema_valid_candidate")
            answer_path = _inside(records, run, request_path.parent / "answer.json")
            if ReadingAudit.model_validate(_read(answer_path)) != audit:
                raise ValueError("nearest audit answer differs from its saved report")
            if not records._queried_candidate_snapshot(run, baseline, details[0], details[2]):
                raise ValueError("nearest audit did not access its bound candidate snapshot")
            audit_matches.append((request_path, details, answer_path))
        elif stage == f"revision_{index + 1}":
            failed_matches.append((request_path, _call(records, run, request_path, plan, stage=stage, status="failed")))
    if len(audit_matches) != 1 or len(failed_matches) != 1:
        raise ValueError("resume requires one exact full audit and its immediately following failed revision")
    audit_request_path, audit_details, answer_path = audit_matches[0]
    failed_request_path, failed_details = failed_matches[0]
    snapshot = _candidate_binding(records, run, failed_details[0], failed_details[2], baseline)
    audit_snapshot = _inside(records, run, audit_details[0]["candidate_binding"]["path"])
    audit_queries = []
    for request_path in (run / "reading_tools").glob("*/request.json"):
        result_path = request_path.parent / "result.json"
        if result_path.is_file():
            request, completed = _read(request_path), _read(result_path)
            data = completed.get("data", {})
            if (request.get("tool") == "wt_query_candidate" and completed.get("success") is True
                    and isinstance(data, dict) and data.get("candidate_binding") == audit_details[0]["candidate_binding"]
                    and data.get("mode") in {"section", "path"}
                    and type(data.get("returned_count")) is int and data["returned_count"] > 0):
                audit_queries.extend((_inside(records, run, request_path), _inside(records, run, result_path)))
    events_path = _inside(records, run, run / "app_server/events.jsonl")
    public_steps, events_hash = _public_protocol(events_path, audit_details[1], failed_details[1])
    chain, current = [], baseline
    for event in public_steps:
        submission = event["data"].get("submission_id", "")
        folder_name = submission.rsplit("-", 1)[0]
        if _STAGE.fullmatch(folder_name) is None:
            raise ValueError("protocol submission has no exact local staging identity")
        item = _stage(records, run, run / "reading_tools" / folder_name / "submitted_record.json")
        if (item["request"] != {"tool": event["tool"], "arguments": event["arguments"]}
                or item["data"] != event["data"]):
            raise ValueError("staged tool files differ from their source-bound protocol exchange")
        replayed = _replay(current, item["request"])
        if _identity(replayed) != _identity(baseline) or replayed != item["record"]:
            raise ValueError("staged changes do not reproduce the retained complete record")
        current = replayed
        chain.append(item)
    if chain[-1]["path"] != path or current != candidate:
        raise ValueError("selected stage is not the latest successful revision-turn checkpoint")
    inputs = list(dict.fromkeys([run / "plan.json", source_path, baseline_path, audit_path, formal_path,
        terminal_path, failure_path, audit_request_path, audit_details[3], answer_path,
        failed_request_path, failed_details[3], snapshot, audit_snapshot, *audit_queries,
        *[Path(i["path"]).resolve() for i in plan["input_paths"]],
        *[p for item in chain for p in (item["path"], item["request_path"], item["result_path"])]]))
    receipt = {"policy": POLICY, "parent_run": run.name, "parent_plan_sha256": hash_file(run / "plan.json"),
        "parent_sources_sha256": hash_file(source_path), "parent_code_identity": plan.get("code_identity"),
        "baseline_candidate_path": str(baseline_path), "baseline_candidate_sha256": hash_file(baseline_path),
        "baseline_audit_path": str(audit_path), "baseline_audit_sha256": hash_file(audit_path),
        "stage_path": str(path), "stage_sha256": hash_file(path),
        "stage_content_hash": digest_json(candidate.model_dump(mode="json")),
        "submission_id": selected["data"]["submission_id"], "failed_stage": failed_details[0]["stage"],
        "failed_call_path": str(failed_request_path.parent), "events_sha256": events_hash,
        "model": failed_details[1]["server_selected_model"], "effort": failed_details[1]["server_selected_effort"],
        "turn_status": "failed_with_verified_staged_candidate", "scientific_validation": "not_established",
        "requires_new_source_audit": True, "artifact_hashes": [{"path": str(p), "sha256": hash_file(p)} for p in inputs],
        "staged_chain": [{"submission_id": item["data"]["submission_id"], "path": str(item["path"]),
                           "sha256": hash_file(item["path"])} for item in chain]}
    if hash_file(events_path) != events_hash:
        raise ValueError("parent protocol changed before staged authentication completed")
    return AuthenticatedStagedCandidate(candidate, bundle, documents, run, baseline, audit,
        baseline_path, audit_path, receipt, inputs)
