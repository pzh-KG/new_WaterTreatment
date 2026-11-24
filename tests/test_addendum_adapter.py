"""Offline adapter replay for a read-only, exact-target source addendum."""
import json
from pathlib import Path

import pytest

from test_workflow import workspace
from test_active_reading import active, toolset
from test_app_server import ReplayTransport
from test_source_review import source_case, scope_for, begin, read_blocks, view
from watertreatment.app_server import AppServerClient
from watertreatment.audit_addendum import AuditAddendum, POLICY
from watertreatment.partial_audit_completion import PartialAuditAddendum, POLICY as PARTIAL_POLICY
from watertreatment.codex import ModelCallError
from watertreatment.domain import ExtractionRecord
from watertreatment.source_review import verify_source_review
from watertreatment.storage import digest_json, hash_file, write_new_json


class AddendumReplay(ReplayTransport):
    def __init__(self, calls, answer):
        super().__init__()
        self.calls, self.answer = calls, answer

    def enqueue_call(self, index):
        name, args = self.calls[index]
        self.messages.append({"id": 900 + index, "method": "item/tool/call", "params": {
            "threadId": "thread-test", "turnId": "turn-1", "callId": f"tool-{index}",
            "tool": name, "arguments": args}})

    def send(self, message):
        identifier = message.get("id", 0)
        if "result" in message and isinstance(identifier, int) and identifier >= 900:
            self.sent.append(message)
            if identifier - 899 < len(self.calls):
                self.enqueue_call(identifier - 899)
            else:
                self.messages.extend([
                    {"method": "item/completed", "params": {"threadId": "thread-test", "turnId": "turn-1",
                        "item": {"type": "agentMessage", "phase": "final_answer", "text": json.dumps(self.answer)}}},
                    {"method": "turn/completed", "params": {"threadId": "thread-test", "turn": {"id": "turn-1", "status": "completed"}}}])
            return
        super().send(message)
        if message.get("method") == "turn/start":
            self.messages.pop()
            self.enqueue_call(0)


@pytest.mark.parametrize("exact_read", [True, False])
@pytest.mark.parametrize("completion", [False, True])
def test_addendum_binds_exact_paths_without_whole_candidate_and_no_mutation(active, exact_read, completion):
    ws, run, bundle, plan = active
    doc = next(d for d in bundle["documents"] if d["source"]["format"] == "pdf")
    sid, bid = doc["source"]["source_id"], doc["blocks"][0]["block_id"]
    candidate = ExtractionRecord.model_validate({"paper": {"id": "synthetic-addendum", "title": {
        "value": "Exact source disposition", "status": "reported"}}, "materials": [{"id": "NEVER_INLINE_UNRELATED_MATERIAL"}]})
    candidate_path = run / "bound-addendum-candidate.json"
    write_new_json(candidate_path, candidate.model_dump(mode="json"))
    tools = toolset(active)
    binding = tools.bind_candidate(candidate, candidate_path)
    # A previous turn's target read must not authorize this addendum.
    assert tools.handle("wt_query_candidate", {"path": ["paper", "title"]})["success"]
    scope = {"policy": POLICY, "targets": [{"record_path": "paper.title", "entity_hash": digest_json(candidate.paper.title.model_dump(mode="json")), "required_evidence_ids": ["e"]}],
        "source_anchors": [{"evidence_id": "e", "source_id": sid, "block_id": bid, "locator": {"physical_page": 1}}]}
    if completion:
        scope["policy"] = PARTIAL_POLICY
        scope["required_query_paths"] = ["paper.title", "paper.title.value"]
    scope["scope_sha256"] = digest_json(scope)
    scope_path = run / "audit-addendum-scope.json"
    write_new_json(scope_path, scope)
    queried_paths = [["paper", "title"]] if exact_read else [["paper", "id"]]
    if completion:
        # Parent-entity retrieval does not meet the extra child-claim obligation.
        queried_paths = [["paper", "title"], ["paper", "title", "value"]] if exact_read else [["paper", "title"]]
    calls = [("wt_submit_record", {"record": candidate.model_dump(mode="json")}),
        ("wt_patch_record", {"base_hash": binding["content_hash"], "edits": [{"operation": "set", "path": ["paper", "title"], "value": {"value": "forged", "status": "reported"}}]}),
        ("wt_query_candidate", {"paths": queried_paths}),
        ("wt_read_blocks", {"source_id": sid, "start_order": 1, "end_order": 1})]
    answer = {"scope_sha256": scope["scope_sha256"], "parent_audit_hash": "b" * 64, "candidate_hash": binding["content_hash"],
        "findings": [{"record_path": "paper.title", "severity": "minor", "verdict": "supported", "needs_revision": False,
            "explanation": "Synthetic target source disposition", "evidence": [{"source_id": sid, "block_id": bid}]}]}
    transport = AddendumReplay(calls, answer)
    client = AppServerClient(ws.settings.codex, live=True, transport_factory=lambda *a: transport, config_factory=lambda: {})
    client.bind_tools(tools)
    kwargs = dict(prompt="Inspect only the exact target.", input_paths=[Path(i["path"]) for i in plan["input_paths"]],
        workdir=run, audit_root=run / "calls", identity={"stage": "audit_addendum", "citation_review": {
            "scope_path": str(scope_path), "scope_file_sha256": hash_file(scope_path)}})
    try:
        response_model = PartialAuditAddendum if completion else AuditAddendum
        if exact_read:
            assert client.complete(response_model, **kwargs).scope_sha256 == scope["scope_sha256"]
        else:
            with pytest.raises(ModelCallError, match="every exact bound target"):
                client.complete(response_model, **kwargs)
    finally:
        client.close()
    sent = next(m for m in transport.sent if m.get("method") == "turn/start")
    prompt = sent["params"]["input"][0]["text"]
    assert "CANDIDATE_SNAPSHOT " + binding["content_hash"] in prompt
    assert "NEVER_INLINE_UNRELATED_MATERIAL" not in prompt
    assert sent["params"]["model"] == "gpt-6.2-sol" and sent["params"]["effort"] == "max"
    for identifier in (900, 901):
        response = next(m for m in transport.sent if m.get("id") == identifier and "result" in m)
        assert not response["result"]["success"]
    assert not tools.submissions and hash_file(candidate_path) == binding["sha256"]
    if exact_read:
        result = json.loads(next((run / "calls").glob("*/result.json")).read_bytes())
        proof = result["source_review_proof"]
        assert verify_source_review(run, scope, proof, proof["call_id"])


def test_addendum_source_policy_retains_full_visual_delivery_requirement(source_case):
    c = source_case
    scope = scope_for(c.docs[:1], visual=True)
    scope["policy"] = POLICY
    scope["scope_sha256"] = digest_json({k: v for k, v in scope.items() if k != "scope_sha256"})
    begin(c, scope=scope)
    read_blocks(c, "main")
    view(c, rectangle=[0, 0, 0.2, 0.2])
    with pytest.raises(ValueError, match="missing full-page"):
        c.tools.source_review_proof()
    view(c)
    assert verify_source_review(c.run, scope, c.tools.source_review_proof(), c.call_id)
