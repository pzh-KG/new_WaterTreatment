"""Recover a staged model draft from a failed turn, never treat it as approval."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .model_policy import manifest_matches_plan, receipt_matches_plan
from .domain import ExtractionRecord
from .storage import digest_json, hash_file, safe_name


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _inside_run(records, prior, path):
    try:
        resolved = records._safe_data_path(path)
    except (OSError, RuntimeError):
        raise ValueError("checkpoint artifact is missing or inaccessible") from None
    if not resolved.is_relative_to(prior.resolve(strict=True)):
        raise ValueError("checkpoint artifact belongs to another run")
    return resolved


def _completed_delivery(records, prior, request_path, result, candidate, data):
    """A final receipt must select exactly the latest successfully staged record."""
    answer_path = _inside_run(records, prior, request_path.parent / "answer.json")
    record_path = _inside_run(records, prior, request_path.parent / "record.json")
    answer = _read(answer_path)
    if (not isinstance(answer, dict) or set(answer) != {"submission_id"}
            or not isinstance(data.get("submission_id"), str) or not data["submission_id"]
            or answer["submission_id"] != data["submission_id"]
            or result.get("submission_id") != data["submission_id"]
            or ExtractionRecord.model_validate_json(record_path.read_bytes()) != candidate):
        raise ValueError("completed revision does not deliver the latest staged candidate")
    return [answer_path, record_path]


def _following_audit_stage(stage):
    if not isinstance(stage, str):
        return None
    if stage == "revision_digitization_0":
        return "audit_web_0"
    match = re.fullmatch(r"revision_web_(\d+)", stage)
    return f"audit_web_{int(match[1]) + 1}" if match else None


def _audit_after_revision(records, prior, plan, candidate, revision_request, *, expected_status="failed"):
    """Bind the next audit to its delivered candidate, never imply approval."""
    expected_stage = _following_audit_stage(revision_request.get("stage", ""))
    if expected_stage is None:
        raise ValueError("completed revision has no explicit revision round")
    matches = []
    for request_path in (prior / "calls").glob("*/request.json"):
        envelope = _read(_inside_run(records, prior, request_path))
        request = envelope.get("request", {})
        if request.get("stage") != expected_stage:
            continue
        result_path = _inside_run(records, prior, request_path.parent / "result.json")
        result = _read(result_path)
        bound = {str(Path(i["path"]).resolve()): i["sha256"] for i in request.get("sources", [])}
        binding = request.get("candidate_binding")
        if (envelope.get("fingerprint") != digest_json(request) or result.get("status") != expected_status
                or not receipt_matches_plan(plan, request, result, legacy_sparse=True)
                or not result.get("thread_id") or not result.get("turn_id")
                or any(bound.get(str(Path(i["path"]).resolve())) != i["sha256"] for i in plan["input_paths"])
                or not isinstance(binding, dict) or set(binding) != {"path", "sha256", "content_hash", "scope"}
                or any(not isinstance(value, str) or not value.strip() for value in binding.values())):
            raise ValueError("following failed audit is not source-bound Sol MAX for this candidate")
        safe_name(binding["scope"])
        snapshot = _inside_run(records, prior, binding["path"])
        expected_path = prior / "candidate_snapshots" / f"{binding['scope']}.json"
        if (snapshot != expected_path or hash_file(snapshot) != binding["sha256"]
                or bound.get(str(snapshot)) != binding["sha256"]
                or binding["content_hash"] != digest_json(candidate.model_dump(mode="json"))
                or "CANDIDATE_SNAPSHOT " + binding["content_hash"] not in request.get("prompt", "")
                or ExtractionRecord.model_validate_json(snapshot.read_bytes()) != candidate):
            raise ValueError("following failed audit snapshot differs from the delivered candidate")
        matches.append((request_path, result_path, snapshot, request, result, binding))
    if len(matches) != 1:
        raise ValueError("completed revision requires one following failed audit of the same candidate")
    return matches[0]


def _preflight_after_revision(records, prior, candidate, stage, completion_index, started_turns):
    """A native Word preflight can stop before a model request or turn exists."""
    from .source_review import review_requirements

    matches = []
    for path in prior.glob("source_review_preflight-*.json"):
        path = _inside_run(records, prior, path)
        event = _read(path)
        if event.get("stage") == stage and "unresolved" in event:
            matches.append((path, event))
    if not matches:
        # A cleanly delivered repair may have no bounded scientific scope. Recover its
        # bytes for an explicitly requested review without transferring any approval.
        index = int(stage.removeprefix("audit_web_"))
        rejection = prior / f"scope_rejection.v{index:02d}.json"
        if not rejection.is_file():
            return None
        path = _inside_run(records, prior, rejection)
        stopped = _read(path)
        version = _inside_run(records, prior, prior / f"candidate.v{index:02d}.json")
        if (stopped.get("version") != index or stopped.get("new_turn_started") is not False
                or stopped.get("automatic_full_audit") is not False or not stopped.get("diagnostics")
                or stopped.get("candidate_sha256") != hash_file(version)
                or ExtractionRecord.model_validate_json(version.read_bytes()) != candidate
                or any(i > completion_index for i in started_turns.values())
                or any(_read(_inside_run(records, prior, p)).get("request", {}).get("stage") == stage
                       for p in (prior / "calls").glob("*/request.json"))):
            raise ValueError("bounded scope rejection differs from the completed revision or later protocol")
        return [path, version], {"following_audit_stage": stage, "following_audit_status": "scope_rejected_no_model_turn",
                                 "preflight_artifacts": [{"path": str(p.relative_to(prior)), "sha256": hash_file(p)}
                                                         for p in (path, version)]}
    if (len(matches) != 1 or any(index > completion_index for index in started_turns.values())
            or any(_read(_inside_run(records, prior, path)).get("request", {}).get("stage") == stage
                   for path in (prior / "calls").glob("*/request.json"))):
        raise ValueError("source preflight conflicts with a later audit request or model turn")
    path, event = matches[0]
    index = int(stage.removeprefix("audit_web_"))
    version_path = _inside_run(records, prior, prior / f"candidate.v{index:02d}.json")
    scope_path = _inside_run(records, prior, prior / f"citation_scope.v{index:02d}.json")
    scope = _read(scope_path)
    if (event.get("new_turn_started") is not False or event.get("scope_sha256") != scope.get("scope_sha256")
            or scope.get("new_record_hash") != digest_json(candidate.model_dump(mode="json"))
            or ExtractionRecord.model_validate_json(version_path.read_bytes()) != candidate):
        raise ValueError("source preflight does not bind the completed revision and its scope")
    unresolved = [r for r in review_requirements(prior, scope, "preflight") if "unresolved_word_visual" in r]
    if not unresolved or unresolved != event["unresolved"]:
        raise ValueError("source preflight cannot be reproduced against the frozen sources")
    paths = [path, scope_path, version_path]
    return paths, {"following_audit_stage": stage, "following_audit_status": "source_preflight_no_model_turn",
                   "preflight_artifacts": [{"path": str(p.relative_to(prior)), "sha256": hash_file(p)} for p in paths]}


def authenticate_preflight_baseline(settings, prior_run_id):
    """Authenticate both the delivered revision and the actual immediately preceding full audit."""
    from .reading import ReadingAudit
    from .staged_source_resume import AuthenticatedStagedCandidate
    from .web_model import _reading_checks
    from .web_records import WebRecords

    records = WebRecords(settings)
    safe_name(prior_run_id)
    prior = settings.data_root / "runs" / prior_run_id
    plan, sources_path, bundle, docs = records._source(prior_run_id)
    detail = records.get(plan["record_id"])
    record, paths, checkpoint = staged_revision(settings, records, detail, prior_run_id)
    if checkpoint.get("following_audit_status") != "source_preflight_no_model_turn":
        raise ValueError("Only a verified stopped-before-turn revision can inherit this baseline")
    stage = checkpoint["following_audit_stage"]
    index = int(stage.removeprefix("audit_web_"))
    if index < 1:
        return None
    version = _inside_run(records, prior, prior / f"candidate.v{index-1:02d}.json")
    review = _inside_run(records, prior, prior / f"audit.v{index-1:02d}.json")
    old = ExtractionRecord.model_validate_json(version.read_bytes())
    audit = ReadingAudit.model_validate_json(review.read_bytes())
    if audit.review_scope != "full":
        return None
    scope_path = _inside_run(records, prior, prior / f"citation_scope.v{index:02d}.json")
    scope = _read(scope_path)
    if (scope.get("baseline_audit_hash") != digest_json(audit.model_dump(mode="json"))
            or scope.get("old_record_hash") != digest_json(old.model_dump(mode="json"))
            or _reading_checks(old, audit, docs)
            or not records._completed_audit_provenance(prior, old, audit, allow_citation=False)):
        raise ValueError("The preceding full review does not authenticate this exact web repair")
    paths = list(dict.fromkeys([*paths, version, review, sources_path]))
    receipt = {"web_run": prior_run_id, "web_checkpoint": checkpoint,
               "candidate_content_hash": digest_json(record.model_dump(mode="json")),
               "parent_plan_sha256": hash_file(prior / "plan.json"), "parent_sources_sha256": hash_file(sources_path),
               "baseline_candidate_sha256": hash_file(version), "baseline_audit_sha256": hash_file(review),
               "events_sha256": checkpoint["events_sha256"],
               "artifact_hashes": [{"path": str(path), "sha256": hash_file(path)} for path in paths]}
    return AuthenticatedStagedCandidate(record, bundle, docs, prior, old, audit, version, review, receipt, paths)


def _terminal_audit_proof(records, prior, candidate, terminal, audit_request_path, audit_request,
                          *, initial_digitization=False):
    """A completed but unresolved final audit permits fresh review, not admission."""
    from .reading import ReadingAudit
    revision = terminal.get("revisions")
    history = terminal.get("history")
    if (type(revision) is not int or revision < (0 if initial_digitization else 1) or not isinstance(history, list)
            or len(history) != revision + 1 or not all(isinstance(item, dict) for item in history)
            or audit_request.get("stage") != f"audit_web_{revision}"
            or history[-1].get("needs_revision") is not True
            or history[-1].get("audit_origin") not in {"fresh_model_audit", "citation_delta_model_audit", "scientific_delta_model_audit"}):
        raise ValueError("terminal recovery lacks a final unresolved fresh audit round")
    result_path = _inside_run(records, prior, prior / "result.json")
    candidate_path = _inside_run(records, prior, prior / "candidate.json")
    version_path = _inside_run(records, prior, prior / f"candidate.v{revision:02d}.json")
    review_path = _inside_run(records, prior, prior / "model_review.json")
    audit_version_path = _inside_run(records, prior, prior / f"audit.v{revision:02d}.json")
    validation_path = _inside_run(records, prior, prior / f"validation.v{revision:02d}.json")
    answer_path = _inside_run(records, prior, audit_request_path.parent / "answer.json")
    if any(ExtractionRecord.model_validate_json(path.read_bytes()) != candidate
           or hash_file(path) != history[-1].get("candidate_sha256") for path in (candidate_path, version_path)):
        raise ValueError("terminal final candidate differs from the latest delivered stage/history")
    review = _read(review_path)
    audit = ReadingAudit.model_validate(review)
    if (ReadingAudit.model_validate(_read(audit_version_path)) != audit
            or ReadingAudit.model_validate(_read(answer_path)) != audit
            or not records._audit_provenance(prior, candidate, terminal, review)):
        raise ValueError("terminal final audit lacks matching completed source-bound provenance")
    validation = _read(validation_path)
    issues = validation.get("issues")
    if not isinstance(issues, list) or not all(isinstance(issue, dict) for issue in issues):
        raise ValueError("terminal validation evidence is malformed")
    errors = sum(issue.get("severity") == "error" for issue in issues)
    checks = terminal.get("reading_check_issues")
    if (type(terminal.get("validation_error_count")) is not int
            or terminal["validation_error_count"] != errors
            or history[-1].get("validation_error_count") != errors
            or not isinstance(checks, list) or history[-1].get("reading_check_issues") != checks
            or not (errors or checks or audit.potential_omissions
                    or any(f.needs_revision or f.verdict == "unsupported" for f in audit.findings))):
        raise ValueError("terminal needs_revision is not supported by its retained final checks")
    # Freeze one successful full-field query proof, not private model events.
    query_paths = None
    binding = audit_request["candidate_binding"]
    for request_path in sorted((prior / "reading_tools").glob("*/request.json")):
        result = request_path.parent / "result.json"
        if not result.is_file():
            continue
        request_path, result = _inside_run(records, prior, request_path), _inside_run(records, prior, result)
        request, completed = _read(request_path), _read(result)
        data = completed.get("data", {})
        if (request.get("tool") == "wt_query_candidate" and completed.get("success") is True
                and isinstance(data, dict) and data.get("candidate_binding") == binding
                and data.get("mode") in {"section", "path"}
                and type(data.get("returned_count")) is int and data["returned_count"] > 0):
            query_paths = [request_path, result]
            break
    if query_paths is None:
        raise ValueError("terminal final audit lacks a successful full-field candidate query")
    paths = [result_path, candidate_path, version_path, review_path, audit_version_path,
             validation_path, answer_path, *query_paths]
    proof = {"terminal_run_status": terminal["status"], "terminal_stop_reason": terminal["stop_reason"],
             "terminal_artifacts": [{"path": str(path.relative_to(prior)), "sha256": hash_file(path)} for path in paths]}
    return paths, proof


def staged_revision(settings, records, detail, prior_run_id):
    safe_name(prior_run_id)
    prior = settings.data_root / "runs" / prior_run_id
    registered = next((r for r in records.store.runs()
                       if r["run_id"] == prior_run_id and r["kind"] == "web_reaudit"), None)
    if registered is None:
        raise ValueError("checkpoint recovery requires a registered failed re-audit")
    terminal = None
    if (prior / "result.json").is_file():
        terminal = _read(_inside_run(records, prior, prior / "result.json"))
        if (not isinstance(terminal, dict) or terminal.get("status") != "needs_revision"
                or terminal.get("stop_reason") != "revision_budget_exhausted"
                or terminal.get("human_validated") is not False or terminal.get("graph_published") is not False
                or (prior / "failure.json").exists()):
            raise ValueError("terminal recovery requires an unpublished unresolved revision-budget result")
    if terminal is None and (not (prior / "failure.json").is_file() or (prior / "result.json").exists()):
        raise ValueError("checkpoint recovery requires a registered failed re-audit")
    plan, sources, _, _ = records._source(prior_run_id)
    if (plan.get("record_id") != detail["id"] or plan.get("record_hash") != detail["summary"]["record_hash"]
            or hash_file(sources) != hash_file(Path(detail["sources_bundle"]))):
        raise ValueError("checkpoint belongs to another current record/source version")
    base = ExtractionRecord.model_validate(detail["record"])
    if ExtractionRecord.model_validate_json((prior / "input_candidate.json").read_bytes()) != base:
        raise ValueError("checkpoint original candidate differs from the current record")
    from .workflow import schema_version_for_rules
    scientific = plan.get("scientific_plan", {})
    if (scientific.get("rule_version") != settings.rules_version
            or scientific.get("schema_version") != schema_version_for_rules(settings.rules_version)):
        raise ValueError("checkpoint target rule/schema differs from the current target")
    manifest = _read(records._safe_data_path(registered["manifest_path"]))
    if (manifest.get("record_id") != detail["id"]
            or manifest.get("record_hash") != detail["summary"]["record_hash"]
            or manifest.get("parent_run") != detail["summary"]["run_id"]
            or manifest.get("source_bundle_sha256") != hash_file(sources)
            or not manifest_matches_plan(plan, manifest)):
        raise ValueError("checkpoint manifest does not bind the current parent, source and Sol MAX")
    original_plan, _, _, _ = records._source(detail["summary"]["run_id"])
    records._scientific_plan(original_plan, plan, manifest)
    policy = None
    if scientific["rule_version"] in {"0.7", "0.8", "0.9"}:
        from .scope_policy import validate_scope_baseline
        policy = plan.get("extraction_policy")
        validate_scope_baseline(policy, base)
    candidates = sorted((prior / "reading_tools").glob("*/submitted_record.json"), reverse=True)
    if not candidates:
        raise ValueError("failed run has no staged scientific candidate")
    stage = candidates[0]
    _inside_run(records, prior, stage)
    stage_request_path, stage_result_path = stage.parent / "request.json", stage.parent / "result.json"
    stage_request, stage_result = (_read(_inside_run(records, prior, stage_request_path)),
                                  _read(_inside_run(records, prior, stage_result_path)))
    data = stage_result.get("data", {})
    if (stage_request.get("tool") not in {"wt_patch_record", "wt_submit_record"}
            or stage_result.get("success") is not True or data.get("schema_valid") is not True
            or data.get("published") is not False or data.get("canonical_record_sha256") != hash_file(stage)):
        raise ValueError("staged candidate does not match its successful local tool result")
    candidate = ExtractionRecord.model_validate_json(stage.read_bytes())
    if data.get("base_hash") != digest_json(candidate.model_dump(mode="json")):
        raise ValueError("staged candidate content hash differs from the tool receipt")
    if any(getattr(candidate.paper, key) != getattr(base.paper, key)
           for key in ("id", "dataset_role", "source_ids", "role_history")):
        raise ValueError("checkpoint changed paper/cohort/source identity")
    if candidate.rule_version != scientific["rule_version"] or candidate.schema_version != scientific["schema_version"]:
        raise ValueError("staged candidate did not reach the frozen target schema/rule")
    if policy and (candidate.extraction_scope != policy["mode"]
                   or candidate.digitization_targets != policy["digitization_targets"]):
        raise ValueError("staged candidate differs from the frozen extraction scope")
    calls = []
    for request_path in (prior / "calls").glob("*/request.json"):
        call_result = request_path.parent / "result.json"
        if not call_result.is_file():
            continue
        envelope, result = (_read(_inside_run(records, prior, request_path)),
                            _read(_inside_run(records, prior, call_result)))
        request = envelope.get("request", {})
        bound = {str(Path(i["path"]).resolve()): i["sha256"] for i in request.get("sources", [])}
        revision_stage = request.get("stage", "")
        if (envelope.get("fingerprint") != digest_json(request)
                or _following_audit_stage(revision_stage) is None
                or (revision_stage == "revision_digitization_0" and (not policy
                    or policy["mode"] != "series_digitization" or not policy["allow_new_dense"]))
                or result.get("status") not in {"failed", "schema_valid_candidate"}
                or not receipt_matches_plan(plan, request, result, legacy_sparse=True)
                or not result.get("thread_id") or not result.get("turn_id")
                or any(bound.get(str(Path(i["path"]).resolve())) != i["sha256"] for i in plan["input_paths"])):
            continue
        calls.append((request_path, call_result, result))
    if not calls:
        raise ValueError("checkpoint lacks a source-bound Sol MAX revision call")

    # Inspect only protocol tool envelopes. Never return reasoning text or feed
    # the event log into a later model prompt. Keep its hash for provenance.
    events_path = records._safe_data_path(prior / "app_server" / "events.jsonl")
    events_hash = hash_file(events_path)
    pending = {}
    verified = None
    verified_index = None
    started_turns, ended_turns = {}, {}
    with events_path.open(encoding="utf-8") as stream:
        for event_index, line in enumerate(stream):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # A crashed process may leave a partial last event.
            message = event.get("message", {})
            params = message.get("params", {})
            if event.get("direction") == "receive" and message.get("method") in {"turn/started", "turn/completed"}:
                turn = params.get("turn", {})
                key = (params.get("threadId"), turn.get("id"))
                if message["method"] == "turn/started":
                    started_turns[key] = event_index
                else:
                    ended_turns[key] = (event_index, turn.get("status"))
            if event.get("direction") == "receive" and message.get("method") == "item/tool/call":
                for request_path, result_path, result in calls:
                    if (params.get("threadId") == result["thread_id"] and params.get("turnId") == result["turn_id"]
                            and params.get("tool") == stage_request["tool"]
                            and params.get("namespace") in (None, "")
                            and params.get("arguments") == stage_request.get("arguments")):
                        pending[message.get("id")] = (request_path, result_path, result)
            elif event.get("direction") == "send" and message.get("id") in pending:
                response = message.get("result", {})
                if response.get("success") is not True:
                    continue
                for content in response.get("contentItems", []):
                    if content.get("type") != "inputText":
                        continue
                    try:
                        payload = json.loads(content.get("text", ""))
                    except json.JSONDecodeError:
                        continue
                    if payload == data:
                        verified = pending[message["id"]]
                        verified_index = event_index
    if hash_file(events_path) != events_hash or verified is None:
        raise ValueError("checkpoint tool receipt is not verified against the bound model protocol")
    request_path, call_result_path, call_result = verified
    extra_paths, extra_receipt = [], {}
    turn_status = "failed_with_verified_staged_candidate"
    if terminal is not None and call_result["status"] != "schema_valid_candidate":
        raise ValueError("terminal recovery requires a completed delivery, not an unfinished stage")
    if call_result["status"] == "schema_valid_candidate":
        # Merely changing a failed status cannot authorize recovery: the final
        # receipt must agree with the latest stage bound by protocol above.
        extra_paths = _completed_delivery(records, prior, request_path, call_result, candidate, data)
        request = _read(request_path)["request"]
        completion = ended_turns.get((call_result["thread_id"], call_result["turn_id"]))
        if completion is None or completion[1] != "completed" or verified_index >= completion[0]:
            raise ValueError("completed revision and subsequent failed audit lack ordered protocol events")
        preflight = None if terminal is not None else _preflight_after_revision(
            records, prior, candidate, _following_audit_stage(request["stage"]), completion[0], started_turns)
        if preflight:
            proof_paths, extra_receipt = preflight
            extra_receipt.update(revision_result_status=call_result["status"],
                                 completed_answer_sha256=hash_file(extra_paths[0]),
                                 completed_record_sha256=hash_file(extra_paths[1]))
            extra_paths += proof_paths
            turn_status = "completed_revision_awaiting_fresh_audit"
        else:
            audit_request_path, audit_result_path, snapshot, audit_request, audit_result, binding = (
                _audit_after_revision(records, prior, plan, candidate, request,
                                      expected_status="schema_valid_candidate" if terminal is not None else "failed"))
            audit_start = started_turns.get((audit_result["thread_id"], audit_result["turn_id"]))
            if audit_start is None or audit_start <= completion[0]:
                raise ValueError("completed revision and subsequent failed audit lack ordered protocol events")
            extra_paths += [audit_request_path, audit_result_path, snapshot]
            extra_receipt = {"revision_result_status": call_result["status"],
                             "following_audit_stage": audit_request["stage"], "following_audit_status": audit_result["status"],
                             "following_audit_request_sha256": hash_file(audit_request_path),
                             "following_audit_result_sha256": hash_file(audit_result_path),
                             "following_audit_candidate_binding": binding,
                             "completed_answer_sha256": hash_file(extra_paths[0]),
                             "completed_record_sha256": hash_file(extra_paths[1])}
            turn_status = "completed_revision_awaiting_fresh_audit"
            if terminal is not None:
                audit_completion = ended_turns.get((audit_result["thread_id"], audit_result["turn_id"]))
                if audit_completion is None or audit_completion[1] != "completed" or audit_completion[0] <= audit_start:
                    raise ValueError("terminal final audit lacks its completed protocol event")
                terminal_paths, terminal_receipt = _terminal_audit_proof(
                    records, prior, candidate, terminal, audit_request_path, audit_request,
                    initial_digitization=request.get("stage") == "revision_digitization_0")
                extra_paths += terminal_paths
                extra_receipt.update(terminal_receipt)
                turn_status = "completed_revision_with_unresolved_final_audit"
    if hash_file(events_path) != events_hash:
        raise ValueError("checkpoint protocol changed during recovery verification")
    receipt = {"run_id": prior_run_id, "stage": stage.parent.name,
               "candidate_sha256": hash_file(stage), "candidate_base_hash": data["base_hash"],
               "submission_id": data["submission_id"], "events_sha256": events_hash,
               "call_request_sha256": hash_file(request_path), "call_result_sha256": hash_file(call_result_path),
               "server_selected_model": call_result["server_selected_model"],
               "server_selected_effort": call_result["server_selected_effort"],
               "turn_status": turn_status,
               "scientific_validation": "not_established", "requires_fresh_audit": True, **extra_receipt}
    if policy:
        receipt["extraction_policy"] = policy
    return candidate, [prior / "plan.json", prior / "input_candidate.json", stage,
                       stage_request_path, stage_result_path, request_path, call_result_path,
                       *([] if terminal is not None else [prior / "failure.json"]),
                       *extra_paths], receipt
