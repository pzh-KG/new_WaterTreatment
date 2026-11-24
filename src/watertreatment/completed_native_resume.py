"""Authenticate a completed native repair without trusting its failed audit.

Only public delivery envelopes and local tool receipts are replayed.  This is
provenance for a new source review, never a scientific approval or a rerun.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from .completed_continuation_resume import _completed_protocol
from .domain import ExtractionRecord
from .reading import ReadingAudit
from .staged_source_resume import (AuthenticatedStagedCandidate, _call,
    _candidate_binding, _identity, _inside, _read, _replay, _stage)
from .storage import digest_json, hash_file
from .validation import validate_record

POLICY = "completed-native-revision-source-resume-v1"


def _final_submission(events_path, completed):
    answers = []
    with events_path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # A later crashed review may leave an unfinished final line.
                # Interior corruption must never be hidden as a crash artifact.
                if not line.endswith("\n") and not stream.readline():
                    break
                raise ValueError("native revision has malformed public provenance") from None
            message = event.get("message", {})
            params = message.get("params", {})
            if (event.get("direction") == "receive" and message.get("method") == "item/completed"
                    and (params.get("threadId"), params.get("turnId")) ==
                        (completed["thread_id"], completed["turn_id"])
                    and params.get("item", {}).get("type") == "agentMessage"):
                answers.append(params["item"])
    finals = [a for a in answers if a.get("phase") == "final_answer"] or answers[-1:]
    if not finals:
        raise ValueError("completed revision has no public final submission")
    return json.loads(finals[-1]["text"])


def authenticate_completed_revision(settings, candidate_path: Path, *, scoped_baseline=False) -> AuthenticatedStagedCandidate:
    from .web_records import WebRecords
    from .web_model import _reading_checks
    from .workflow import validate_plan_metadata

    records = WebRecords(settings)
    path = records._safe_data_path(candidate_path)
    run = path.parent
    if (not re.fullmatch(r"candidate\.v[0-9]{2}\.json", path.name)
            or run.parent != (settings.data_root / "runs").resolve()):
        raise ValueError("native resume needs a versioned candidate in the configured run store")
    version = int(path.stem[-2:])
    if version < 1 or not any(r["run_id"] == run.name and r["kind"] == "extraction" for r in records.store.runs()):
        raise ValueError("native resume needs a registered completed repair")
    if ((run / f"audit.v{version:02d}.json").exists()
            or any(int(p.stem[-2:]) > version for p in run.glob("candidate.v[0-9][0-9].json"))):
        raise ValueError("native resume cannot bypass a current audit or later candidate")
    # A completed answer is authoritative even if the controller crashed before
    # writing audit.vNN.json. Do not bypass potentially new scientific findings.
    for call_path in (run / "calls").glob("*/request.json"):
        call_path = _inside(records, run, call_path)
        envelope = _read(call_path)
        stage = envelope.get("request", {}).get("stage", "")
        match = re.fullmatch(r"audit_(\d+)", stage)
        outcome_path = call_path.parent / "result.json"
        if match and int(match[1]) >= version and outcome_path.exists():
            outcome = _read(_inside(records, run, outcome_path))
            if outcome.get("status") == "schema_valid_candidate":
                raise ValueError("native resume cannot bypass a completed current or later audit call")
    plan, source_path, bundle, documents = records._source(run.name)
    terminal = _read(_inside(records, run, run / "result.json"))
    if (plan.get("cohort_role") not in {"development", "unassigned"}
            or settings.rules_version != plan["rule_version"]
            or terminal.get("status") != "candidate_incomplete_failure"
            or terminal.get("last_candidate_version") != path.name
            or terminal.get("human_validated") is not False
            or terminal.get("graph_published") is not False):
        raise ValueError("native resume requires an unpublished stopped non-heldout repair")
    baseline_path = _inside(records, run, run / f"candidate.v{version-1:02d}.json")
    audit_path = _inside(records, run, run / f"audit.v{version-1:02d}.json")
    baseline = ExtractionRecord.model_validate(_read(baseline_path))
    audit = ReadingAudit.model_validate(_read(audit_path))
    candidate = ExtractionRecord.model_validate(_read(path))
    checks = _reading_checks(baseline, audit, documents)
    repaired_followups = (checks and all(x.startswith("QUANTITATIVE_FOLLOWUP_UNSCOPED:") for x in checks)
                          and not _reading_checks(candidate, audit, documents))
    repaired_checks = scoped_baseline and checks and not _reading_checks(candidate, audit, documents)
    delta_receipt = None
    if scoped_baseline and audit.review_scope == "scientific_delta":
        from .retained_delta_lineage import confirm_delta_lineage
        delta_receipt = confirm_delta_lineage(records, run, plan, version - 1, baseline, audit)
    full_baseline = (audit.review_scope == "full" and audit.review_scope_sha256 is None
                     and records._completed_audit_provenance(run, baseline, audit, allow_citation=False))
    if (_identity(candidate) != _identity(baseline) or not (full_baseline or delta_receipt)
            or (checks and not (repaired_followups or repaired_checks))):
        raise ValueError("native repair lacks an authenticated immediate full-audit baseline")
    repaired_errors = []
    for record in (baseline, candidate):
        issues = [*validate_record(record, source_documents=documents),
                  *validate_plan_metadata(record, plan, documents, store=records.store)]
        errors = [i for i in issues if i.severity == "error"]
        if (errors and scoped_baseline and record is baseline and all(i.code in
                {"DESIGN_MISSING_REFERENCE","DESIGN_USE_ASSESSMENT_REQUIRED"} for i in errors)):
            repaired_errors = sorted((i.model_dump(mode="json") for i in errors),key=digest_json)
        elif errors:
            raise ValueError("native repair has source, identity or machine validation errors")
    matching = [p for p in (run / "calls").glob("*/request.json")
                if _read(p).get("request", {}).get("stage") == f"revision_{version}"]
    if len(matching) != 1:
        raise ValueError("native repair requires exactly one completed delivery")
    request_path = matching[0]
    request, completed, bound, result_path = _call(records, run, request_path, plan,
        stage=f"revision_{version}", status="schema_valid_candidate")
    for item, checksum in bound.items():
        if hash_file(records._safe_data_path(item)) != checksum:
            raise ValueError("native repair input changed")
    base_path = _candidate_binding(records, run, request, bound, baseline)
    record_path = _inside(records, run, request_path.parent / "record.json")
    answer_path = _inside(records, run, request_path.parent / "answer.json")
    answer = _read(answer_path)
    if (set(answer) != {"submission_id"} or answer["submission_id"] != completed.get("submission_id")
            or hash_file(record_path) != hash_file(path)
            or (run / "candidate.json").exists() and hash_file(_inside(records, run, run / "candidate.json")) != hash_file(path)):
        raise ValueError("native repair does not match its delivered canonical candidate")
    events = _inside(records, run, run / "app_server/events.jsonl")
    public, event_hash = _completed_protocol(events, completed)
    if _final_submission(events, completed) != answer:
        raise ValueError("native repair final public answer differs from saved delivery")
    current, chain = baseline, []
    for step in public:
        identifier = step["data"].get("submission_id", "").rsplit("-", 1)[0]
        if re.fullmatch(r"tool-\d{4,}", identifier) is None:
            raise ValueError("native repair mutation lacks a local checkpoint")
        item = _stage(records, run, run / "reading_tools" / identifier / "submitted_record.json")
        if item["request"] != {"tool": step["tool"], "arguments": step["arguments"]} or item["data"] != step["data"]:
            raise ValueError("native repair tool receipts differ from public delivery")
        current = _replay(current, item["request"])
        if current != item["record"] or _identity(current) != _identity(baseline):
            raise ValueError("native repair replay changed source or paper identity")
        chain.append(item)
    if current != candidate or chain[-1]["data"]["submission_id"] != answer["submission_id"]:
        raise ValueError("native repair did not deliver its final mutation")
    paths = list(dict.fromkeys([run / "plan.json", source_path, path, baseline_path, audit_path,
        run / "result.json", request_path, result_path, record_path, answer_path, base_path,
        *[Path(x) for x in bound],
        *[p for item in chain for p in (item["path"], item["request_path"], item["result_path"])]]))
    receipt = {"policy": POLICY, "parent_run": run.name,
        "parent_plan_sha256": hash_file(run / "plan.json"), "parent_sources_sha256": hash_file(source_path),
        "candidate_path": str(path), "candidate_sha256": hash_file(path),
        "candidate_content_hash": digest_json(candidate.model_dump(mode="json")),
        "baseline_candidate_sha256": hash_file(baseline_path), "baseline_audit_sha256": hash_file(audit_path),
        "completed_call_path": str(request_path.parent), "events_sha256": event_hash,
        "submission_id": answer["submission_id"], "requires_new_source_audit": True,
        "scientific_validation": "not_established",
        "artifact_hashes": [{"path": str(p), "sha256": hash_file(p)} for p in paths]}
    if delta_receipt:
        receipt["verified_scientific_deltas"] = delta_receipt
    if repaired_errors or (repaired_checks and not repaired_followups):
        receipt["repaired_baseline_checks"] = {"reading":checks,"validation":repaired_errors}
    if hash_file(events) != event_hash:
        raise ValueError("native repair public provenance changed during authentication")
    return AuthenticatedStagedCandidate(candidate, bundle, documents, run, baseline, audit,
        baseline_path, audit_path, receipt, paths)
