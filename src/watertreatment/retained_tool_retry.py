"""Authenticate an interrupted, source-only review without adopting an unfinished verdict."""
from dataclasses import replace
from pathlib import Path

from .reading_budget import MAX_SOURCE_CALLS
from .staged_source_resume import _call
from .storage import digest_json, hash_file

def source_tool_limit(packet):
    # A source may require separate text and visual inspection. The time and
    # model-call caps are separately enforced for the entire paper.
    return min(MAX_SOURCE_CALLS, max(30, 2 * len(packet.get("sources", [])) + len(packet.get("findings", [])) + 4))

TOOL_EXHAUSTION = "ToolBudgetExceeded: reading-tool total attempt budget exhausted"
TURN_DEADLINE = "TimeoutError: Codex reading turn exceeded its time budget"


def is_source_interruption(run):
    from .retained_review_parent import read
    fail = Path(run) / "failure.json"
    try:
        error = read(fail).get("error", "") if fail.is_file() else ""
        return isinstance(error, str) and (error.startswith(TOOL_EXHAUSTION) or error == TURN_DEADLINE)
    except (OSError, ValueError):
        return False

def _confirm_interrupt(events_file, outcome, limit=None):
    from json import loads
    attempts = 0
    interrupted = False
    with events_file.open(encoding="utf-8") as stream:
        for line in stream:
            event = loads(line)
            message = event.get("message", {})
            params = message.get("params") or {}
            if (params.get("turnId"), params.get("threadId")) != (outcome["turn_id"], outcome["thread_id"]):
                continue
            method = message.get("method")
            if method == "item/tool/call" and event.get("direction") == "receive":
                attempts += 1
            elif method == "turn/interrupt" and event.get("direction") == "send":
                interrupted = True
            elif method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage" and item.get("phase") != "commentary" and item.get("text"):
                    raise ValueError("A delivered source-review answer requires reconciliation before another turn")
    if not interrupted or (limit is not None and attempts <= limit):
        raise ValueError("Missing provider-side source-tool exhaustion or interruption")
    return attempts


def failed_parent(settings, run, records, ancestor, plan, packet):
    from .retained_review import prompt, _schema, _confirm_delivered_inputs
    from .retained_review_parent import read
    from .codex import strict_schema

    paths = list(run.glob("calls/*/request.json"))
    if (len(paths) != 1 or (run / "result.json").exists() or list(run.glob("calls/*/answer.json"))
            or not is_source_interruption(run) or read(run / "failure.json").get("model_calls") != 1
            or read(run / "execution_started.json").get("plan_sha256") != hash_file(run / "plan.json")):
        raise ValueError("No single interrupted source-only turn to resume")
    path = paths[0]
    request, outcome, sources, _ = _call(records, run, path, plan, stage="retained_issue_review", status="failed")
    if (outcome.get("error") != read(run / "failure.json")["error"]
            or request.get("schema") != strict_schema(_schema(plan.get("packet_version", 1)).model_json_schema())
            or not request["prompt"].startswith(prompt(packet)) or request["images"] != plan["image_paths"]
            or any(hash_file(records._safe_data_path(k)) != v for k,v in sources.items())):
        raise ValueError("Interrupted review differs from its registered source or candidate")
    limit = plan.get("source_tool_limit", 30)
    if request.get("tool_budget_at_start", {}).get("limits", {}).get("total_attempts") != limit:
        raise ValueError("Interrupted tool allowance differs from the frozen review")
    calls = sorted((run / "reading_tools").glob("tool-*/request.json"))
    timed_out = outcome["error"] == TURN_DEADLINE
    events_file = records._safe_data_path(run / "app_server/events.jsonl")
    attempts = _confirm_interrupt(events_file, outcome, None if timed_out else limit)
    if timed_out:
        seconds = plan["model_settings"]["timeout_seconds"]
        if (outcome["elapsed_seconds"] < seconds or seconds != plan["reading_settings"]["total_timeout_seconds"]
                or not len(calls) <= attempts <= len(calls) + 1 or len(calls) > limit):
            raise ValueError("The stopped turn does not match the registered deadline and source calls")
    elif len(calls) != limit:
        raise ValueError("The recorded source attempts do not exhaust the registered allowance")
    _confirm_delivered_inputs(run, request, outcome)
    receipt = {"review_origin": ancestor.receipt.get("interrupted_source_review", {}).get("review_origin", ancestor.run.name),
               "failed_run": run.name, "packet_version": plan.get("packet_version", 1),
               "packet_hash": packet["packet_hash"], "proposal_hash": hash_file(run / "proposal.json"),
               "failed_call_hash": hash_file(path.with_name("result.json")),
               "events_hash": hash_file(events_file), "spent_source_attempts": len(calls),
               "spent_seconds": outcome["elapsed_seconds"]}
    if timed_out:
        receipt.update(interruption="turn_deadline", registered_timeout_seconds=seconds)
    return replace(ancestor, run=run, receipt={**ancestor.receipt, "interrupted_source_review": receipt})

def checked_proposal(parent, proposal):
    from .retained_review_parent import read
    previous = read(parent.run / "proposal.json")
    if proposal is not None and proposal != previous:
        if proposal.get("continues_failed_candidate_sha256") != digest_json(previous["candidate"]):
            raise ValueError("Additional source corrections must bind the interrupted proposal")
        from .native_repair_review import _merge_reviewed_fields
        merged = _merge_reviewed_fields(parent.candidate.model_dump(mode="json"), previous["candidate"], proposal["candidate"])
        if merged != proposal["candidate"] or not {tuple(b) for b in previous["source_blocks"]}.issubset(
                {tuple(b) for b in proposal["source_blocks"]}):
            raise ValueError("The continued proposal discarded an interrupted edit or required source")
    return previous if proposal is None else proposal
