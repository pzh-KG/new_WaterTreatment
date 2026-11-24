"""Source-bound repair of controller identities; never repair scientific facts."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
from types import SimpleNamespace

from .model_policy import plan_model, receipt_matches_plan
from .domain import ExtractionRecord
from .reading import ReadingDossier
from .storage import DataStore, digest_json, hash_file, write_new_json, utc_now


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _artifact(path):
    return {"path": str(path), "sha256": hash_file(path)}


def _completed_extraction_chain(settings, parent, completed, candidate):
    from .completed_continuation_resume import _completed_protocol
    from .completed_native_resume import _final_submission
    from .staged_source_resume import _stage, _replay_stage
    def safe_path(path):
        path = Path(path).resolve(strict=True)
        if not path.is_relative_to(settings.data_root):
            raise ValueError("identity artifact escaped the configured data root")
        return path
    records = SimpleNamespace(_safe_data_path=safe_path)
    events = parent / "app_server" / "events.jsonl"
    steps, event_hash = _completed_protocol(events, completed)
    answer = _final_submission(events, completed)
    if set(answer) != {"submission_id"} or answer["submission_id"] != completed.get("submission_id"):
        raise ValueError("identity extraction lacks its completed public submission")
    current, paths = None, [events]
    for step in steps:
        identifier = step["data"].get("submission_id", "").rsplit("-", 1)[0]
        if re.fullmatch(r"tool-\d{4,}", identifier) is None:
            raise ValueError("identity extraction has an invalid submission locator")
        item = _stage(records, parent, parent / "reading_tools" / identifier / "submitted_record.json")
        if (item["request"] != {"tool": step["tool"], "arguments": step["arguments"]}
                or item["data"] != step["data"] or current is None and step["tool"] != "wt_submit_record"):
            raise ValueError("identity extraction local submission differs from public delivery")
        current = _replay_stage(current, item, _json(parent / "plan.json")["paper_id"])
        if "repair_draft" in step["arguments"]:
            draft_folder = step["arguments"]["repair_draft"]["draft_id"].rsplit("-", 1)[0]
            rejected = item["path"].parent.parent / draft_folder
            paths.extend([rejected / "request.json", rejected / "result.json"])
        binding = item["data"].get("transport", {}).get("system_identity_binding")
        if binding:
            raw_path = item["path"].parent / "unbound_submitted_record.json"
            paths.append(raw_path)
        if current != item["record"]:
            raise ValueError("identity extraction submission chain changed scientific fields")
        paths.extend([item["path"], item["request_path"], item["result_path"]])
    if (current != candidate or steps[-1]["data"]["submission_id"] != answer["submission_id"]
            or hash_file(events) != event_hash):
        raise ValueError("identity extraction did not deliver this exact candidate")
    return paths


def check_identity_reservation(store, parent_run, current_run=None, *, reserve=False):
    """An immutable sibling run must not reset the same parent's remaining budget."""
    with store.connection() as db:
        if reserve:
            db.execute("BEGIN IMMEDIATE")
        for row in db.execute("SELECT run_id,payload FROM run_events WHERE event='identity_recovery_started'"):
            if json.loads(row[1]).get("parent_run") == parent_run and row[0] != current_run:
                raise ValueError("identity parent's remaining budget already belongs to another executed continuation")
        if reserve:
            db.execute("INSERT INTO run_events(run_id,event,payload,created_at) VALUES (?,?,?,?)",
                (current_run, "identity_recovery_started", json.dumps({"parent_run": parent_run}), utc_now()))


def authenticate_identity_parent(settings, parent, bundle, cohort_role, *, allow_exhausted=False):
    """Authenticate two completed raw outputs before their first scientific audit.

    Only the system-owned paper identifiers change. Calls, original outputs,
    scientific values, coverage, sources and cohort identity remain untouched.
    """
    from .workflow import _read_bundle
    from .scope_policy import first_audit_prompt_transition

    parent = Path(parent).resolve(strict=True)
    if parent.parent != settings.data_root / "runs" or cohort_role not in {"development", "unassigned"}:
        raise ValueError("identity recovery requires a registered non-heldout parent run")
    store = DataStore(settings.data_root)
    plan_path = parent / "plan.json"
    plan = _json(plan_path)
    registration = store.last_event(parent.name, "prepared")
    if not registration or registration.get("plan_sha256") != hash_file(plan_path):
        raise ValueError("identity parent plan is unregistered or changed")
    if (plan.get("workflow") != "active_reading" or plan.get("cohort_role") != cohort_role
            or plan.get("rule_version") != settings.rules_version or plan.get("reused_candidate")
            or plan.get("reused_dossier")):
        raise ValueError("identity recovery requires an original matching reading plan")
    frozen = {str(Path(x["path"]).resolve()): x["sha256"] for x in plan["input_paths"]}
    source_path = parent / "sources.json"
    if frozen.get(str(source_path)) != hash_file(source_path):
        raise ValueError("identity parent source bundle is not frozen")
    for path, sha in frozen.items():
        if hash_file(Path(path)) != sha:
            raise ValueError("identity parent source input changed")
    prior, _ = _read_bundle(source_path)
    if (prior["paper_id"] != plan["paper_id"] or prior["paper_id"] != bundle["paper_id"]
            or digest_json(prior["documents"]) != digest_json(bundle["documents"])):
        raise ValueError("identity recovery cannot change source ownership or representations")
    if hash_file(parent / "taxonomy.json") != hash_file(settings.taxonomy_path):
        raise ValueError("identity recovery taxonomy changed")
    transitions = []
    for stem, filename, field in (("understand", "understand_prompt.txt", None),
                                  ("extract", "extract_prompt.txt", "prompt_sha256")):
        path = parent / filename
        expected = plan.get(field) if field else next((x["sha256"] for x in plan["reading_artifacts"]
            if Path(x["path"]).resolve() == path), None)
        if expected != hash_file(path):
            raise ValueError("identity recovery scientific instructions changed")
        original = path.read_text(encoding="utf-8").split("\n\nDELIVERY:", 1)[0]
        change = first_audit_prompt_transition(settings, stem, original, plan_model(plan))
        if change:
            transitions.append(change)
    if (list(parent.glob("audit.v*.json")) or (parent / "model_review.json").exists()
            or any(p.name != "candidate.v00.json" for p in parent.glob("candidate.v*.json"))):
        raise ValueError("identity recovery cannot replace an existing scientific audit")
    dossier_path, candidate_path = parent / "reading_dossier.json", parent / "candidate.v00.json"
    dossier = ReadingDossier.model_validate(_json(dossier_path))
    candidate = ExtractionRecord.model_validate(_json(candidate_path))
    expected_role = {"development": "development", "unassigned": "unspecified"}[cohort_role]
    if (candidate.paper.dataset_role.value != expected_role or candidate.rule_version != settings.rules_version
            or set(candidate.paper.source_ids) != {d["source"]["source_id"] for d in bundle["documents"]}):
        raise ValueError("identity recovery cannot change cohort, rules or source membership")
    if dossier.paper_id == bundle["paper_id"] and candidate.paper.id == bundle["paper_id"]:
        raise ValueError("no system identity correction is needed")
    calls, elapsed = {}, 0.0
    artifacts = [plan_path, source_path, dossier_path, candidate_path,
                 parent / "understand_prompt.txt", parent / "extract_prompt.txt", parent / "taxonomy.json"]
    for request_path in sorted((parent / "calls").glob("*/request.json")):
        envelope = _json(request_path)
        request = envelope.get("request", {})
        stage = request.get("stage")
        result_path = request_path.parent / "result.json"
        result = _json(result_path)
        if stage not in {"comprehension", "extraction"} or stage in calls:
            raise ValueError("identity recovery requires exactly one comprehension and extraction call")
        stem = "understand" if stage == "comprehension" else "extract"
        if any(change["stage"] == stem for change in transitions):
            original = (parent / (stem + "_prompt.txt")).read_text(encoding="utf-8").split("\n\nDELIVERY:", 1)[0]
            if original not in request.get("prompt", ""):
                raise ValueError("Historical scientific instructions were not delivered to the completed stage")
        observed = {str(Path(x["path"]).resolve()): x["sha256"] for x in request.get("sources", [])}
        if (envelope.get("fingerprint") != digest_json(request)
                or any(observed.get(path) != sha for path, sha in frozen.items())
                    or request.get("code_identity") != plan["code_identity"] or request.get("rule_version") != plan["rule_version"]
                or result.get("status") != "schema_valid_candidate"
                or not receipt_matches_plan(plan, request, result)):
            raise ValueError("identity recovery lacks completed source-bound Sol MAX calls")
        if any(hash_file(Path(path)) != sha for path, sha in observed.items()):
            raise ValueError("identity completed call input changed")
        seconds = result.get("elapsed_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("identity recovery lacks authenticated elapsed time")
        answer_path = request_path.parent / ("answer.json" if stage == "comprehension" else "record.json")
        from .coverage_transport import verified_dossier
        delivered_record = (verified_dossier(_json(answer_path), bundle, request, result)
                            if stage == "comprehension" else ExtractionRecord.model_validate(_json(answer_path)))
        if delivered_record != (dossier if stage == "comprehension" else candidate):
            raise ValueError("identity recovery output differs from its completed model call")
        if stage == "comprehension":
            from .audit_addendum_workflow import _completed_call_protocol
            _completed_call_protocol(parent, result, _json(answer_path))
            artifacts.append(parent / "app_server" / "events.jsonl")
        else:
            delivered = _json(request_path.parent / "answer.json")
            if delivered != {"submission_id": result.get("submission_id")}:
                raise ValueError("identity extraction final answer differs from completion receipt")
            artifacts.append(request_path.parent / "answer.json")
            artifacts.extend(_completed_extraction_chain(settings, parent, result, candidate))
        elapsed += seconds
        calls[stage] = str(request_path.parent)
        artifacts.extend([request_path, result_path, answer_path])
    if set(calls) != {"comprehension", "extraction"}:
        raise ValueError("identity recovery needs both completed model outputs")
    remaining = math.floor(plan["reading_settings"]["total_timeout_seconds"] - elapsed)
    remaining_calls = plan["max_calls"] - len(calls)
    if not allow_exhausted and (remaining < 60 or remaining_calls < 1):
        raise ValueError("identity recovery has no remaining parent budget")
    corrected_dossier = dossier.model_copy(deep=True)
    corrected_candidate = candidate.model_copy(deep=True)
    corrected_dossier.paper_id = bundle["paper_id"]
    corrected_candidate.paper.id = bundle["paper_id"]
    proof = {"policy": "registered-source-paper-identity-v1", "parent_run": parent.name,
        **({"first_audit_prompt_transitions": transitions} if transitions else {}),
        "paper_id": bundle["paper_id"], "parent_artifacts": [_artifact(p) for p in dict.fromkeys(artifacts)],
        "completed_calls": calls,
        "changes": [{"artifact": "reading_dossier.json", "path": "paper_id", "before": dossier.paper_id, "after": bundle["paper_id"]},
                    {"artifact": "reused_candidate.json", "path": "paper.id", "before": candidate.paper.id, "after": bundle["paper_id"]}],
        "corrected_dossier_content_hash": digest_json(corrected_dossier.model_dump(mode="json")),
        "corrected_candidate_content_hash": digest_json(corrected_candidate.model_dump(mode="json")),
        "scientific_fields_changed": False, "scientific_audit_inherited": False,
        "budget": {"original_total_seconds": plan["reading_settings"]["total_timeout_seconds"],
                   "used_model_seconds": elapsed, "remaining_seconds": remaining,
                   "original_max_calls": plan["max_calls"], "used_calls": len(calls), "remaining_calls": remaining_calls}}
    return corrected_dossier, corrected_candidate, proof


def verify_identity_resume(settings, run, plan=None):
    """Rebuild the exact correction from archived authorities, including at admission."""
    plan = plan or _json(run / "plan.json")
    descriptor = plan.get("identity_correction")
    if not descriptor:
        return None
    path = run / "identity_correction.json"
    if descriptor != _artifact(path):
        raise ValueError("frozen identity correction changed")
    proof = _json(path)
    from .budget_grants import prepared_grant
    addition = prepared_grant(settings, run, plan, settings.data_root / "runs" / proof["parent_run"])
    dossier, candidate, expected = authenticate_identity_parent(settings,
        settings.data_root / "runs" / proof["parent_run"], _json(run / "sources.json"), plan["cohort_role"], allow_exhausted=bool(addition))
    check_identity_reservation(DataStore(settings.data_root), proof["parent_run"], run.name)
    if (proof != expected or _json(run / "reading_dossier.json") != dossier.model_dump(mode="json")
            or _json(run / "reused_candidate.json") != candidate.model_dump(mode="json")
            or type(plan["max_calls"]) is not int or plan["max_calls"] < 1
            or plan["max_calls"] > (addition["allocated_calls"] if addition else proof["budget"]["remaining_calls"])
            or type(plan.get("continuation_timeout_seconds")) is not int
            or not 60 <= plan["continuation_timeout_seconds"] <= (addition["allocated_seconds"] if addition else proof["budget"]["remaining_seconds"])
            or plan.get("resumed_full_audit")):
        raise ValueError("identity correction, preserved science or continuation budget differs")
    return proof


def bind_dossier_identity(dossier, paper_id, run):
    """Retain the model answer and explicitly project only the controller-owned ID."""
    if dossier.paper_id == paper_id:
        return dossier
    raw_path = run / "reading_dossier.model.json"
    write_new_json(raw_path, dossier.model_dump(mode="json"))
    corrected = dossier.model_copy(deep=True)
    corrected.paper_id = paper_id
    write_new_json(run / "dossier_identity_binding.json", {
        "policy": "controller-paper-id-binding-v1", "raw": _artifact(raw_path),
        "path": "paper_id", "before": dossier.paper_id, "after": paper_id,
        "corrected_content_hash": digest_json(corrected.model_dump(mode="json")),
        "scientific_fields_changed": False})
    return corrected


def verify_dossier_binding(run, plan):
    """Return the unmodified model notes after replaying an explicit ID binding."""
    current = ReadingDossier.model_validate(_json(run / "reading_dossier.json"))
    path = run / "dossier_identity_binding.json"
    if not path.exists():
        return current
    proof = _json(path)
    raw_path = run / "reading_dossier.model.json"
    raw = ReadingDossier.model_validate(_json(raw_path))
    corrected = raw.model_copy(deep=True)
    corrected.paper_id = plan["paper_id"]
    expected = {"policy": "controller-paper-id-binding-v1", "raw": _artifact(raw_path),
        "path": "paper_id", "before": raw.paper_id, "after": plan["paper_id"],
        "corrected_content_hash": digest_json(corrected.model_dump(mode="json")),
        "scientific_fields_changed": False}
    if proof != expected or current != corrected:
        raise ValueError("controller dossier identity binding or scientific notes changed")
    return raw
