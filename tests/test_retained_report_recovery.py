"""Offline regressions for partial ledger reuse and explicit, resumable audit judgments."""
import json
from types import SimpleNamespace

import pytest

from watertreatment.coverage_correction import CoverageCorrectionReport, CoverageDisposition
from watertreatment.coverage_resume import ResumedCoverageReport, concern_issues, eligible_entries
from watertreatment.partial_report_transport import (
    CompactReport, DraftFindings, ReportFinding, draft_batch, expand_report, public_drafts,
)
from watertreatment.reading import SourceAnchor
from watertreatment.storage import digest_json
from test_web_records import fixture


def report_case():
    return {"source_anchors": [], "inherited_source_anchors": [
        {"source_id": "s", "block_id": "a", "evidence_id": "e1"},
        {"source_id": "s", "block_id": "b", "evidence_id": "e2"}],
        "targets": [{"required_evidence_ids": ["e1", "e2"]}, {"required_evidence_ids": ["e2"]}],
        "required_query_paths": ["observations[0]", "observations[1]"],
        **{key: digest_json(key) for key in ("scope_sha256", "parent_audit_hash", "candidate_hash")}}


def finding(**changes):
    return ReportFinding.model_validate({"record_path": "first comparison", "record_paths": ["observations[0]"],
        "severity": "major", "verdict": "supported", "needs_revision": False,
        "explanation": "The two source blocks explicitly identify the same comparison.", **changes})


def wire(scope, **changes):
    return CompactReport(**{key: scope[key] for key in ("scope_sha256", "parent_audit_hash", "candidate_hash")},
        **changes).model_dump(mode="json")


def test_literal_groups_require_model_selection_and_preserve_exact_sources():
    scope = report_case()
    parsed = expand_report(scope, wire(scope, findings=[finding(cite_groups=[1])]), {})
    assert [x.block_id for x in parsed.findings[0].evidence] == ["b"]
    with pytest.raises(ValueError, match="explicit"):
        expand_report(scope, wire(scope, findings=[finding()]), {})
    with pytest.raises(ValueError, match="unknown citation"):
        expand_report(scope, wire(scope, findings=[finding(cite_groups=[-1])]), {})


def test_public_drafts_are_turn_bound_and_unresolved_judgments_cannot_disappear(tmp_path):
    scope = report_case()
    args = DraftFindings(findings=[finding(cite_blocks=[0], verdict="unresolved", needs_revision=True)])
    rows = draft_batch(scope, args)
    call = tmp_path / "calls" / "one"
    result = {"source_review_call": call.name, "draft_ids": list(rows)}
    sent = {"direction": "send", "message": {"id": 1, "result": {"success": True, "contentItems": [
        {"type": "inputText", "text": json.dumps(result)}]}}}
    received = {"direction": "receive", "message": {"method": "item/tool/call", "id": 1, "params": {
        "threadId": "t", "turnId": "u", "tool": "wt_save_review_findings", "arguments": args.model_dump(mode="json")}}}
    events = tmp_path / "app_server" / "events.jsonl"
    events.parent.mkdir()
    events.write_text("\n".join(map(json.dumps, (received, sent))), encoding="utf-8")
    assert public_drafts(tmp_path, call, scope, {"thread_id": "t", "turn_id": "u"}) == rows
    assert public_drafts(tmp_path, call, scope, {"thread_id": "t", "turn_id": "other"}) == {}
    with pytest.raises(ValueError, match="omits"):
        expand_report(scope, wire(scope), rows)
    confirmed = expand_report(scope, wire(scope, confirmed_drafts=list(rows)), rows)
    assert confirmed.findings[0].needs_revision
    with pytest.raises(ValueError, match="changed"):
        expand_report(scope, wire(scope, confirmed_drafts=list(rows)), {next(iter(rows)): finding().model_dump(mode="json")})


def test_inherit_only_delivered_declarations_and_keep_questioned_blocks_open():
    pairs = [{"source_id": "s", "block_id": b} for b in ("a", "b", "c")]
    docs = [SimpleNamespace(source=SimpleNamespace(source_id="s"), blocks=[
        SimpleNamespace(block_id=b, text="Original claim " + b, kind="paragraph", metadata={}) for b in "abc"])]
    scope = {"scope_sha256": "test", "source_anchors": pairs, "required_dispositions": pairs}
    entries = [CoverageDisposition(source_id="s", block_ids=[b], state="read_unquantified", text_read=True,
        visual_checked=False, category="substantive", note="Retained reading of this source.",
        evidence=[SourceAnchor(source_id="s", block_id=b, quote="Original claim " + b)]) for b in "abc"]
    report = CoverageCorrectionReport(scope_sha256="test", dispositions=entries)
    progress = {"delivered": pairs[:2], "full_views": [], "missing_blocks": pairs[2:]}
    concerns = [{"source_id": "s", "block_ids": ["b"], "explanation": "Independent source concern."}]
    assert eligible_entries(scope, report, progress, docs, True, concerns) == entries[:1]
    assert eligible_entries(scope, report, progress, docs, False, concerns) == []
    parent = SimpleNamespace(receipt={"coverage_resume": {"scientific_concerns": concerns}}, documents=docs, scope=scope)
    def reviewed(rows):
        return ResumedCoverageReport(scope_sha256="test", dispositions=entries, concern_reviews=rows)
    assert concern_issues(parent, reviewed([]))
    row = {"concern_index": 0, "decision": "supported_as_recorded", "explanation": "The candidate agrees with the source.",
           "evidence": entries[0].evidence}
    assert concern_issues(parent, reviewed([row]))
    row["evidence"] = entries[1].evidence
    assert concern_issues(parent, reviewed([row])) == []
    row["decision"] = "requires_scientific_revision"
    assert concern_issues(parent, reviewed([row]))


def test_native_report_can_checkpoint_then_confirm_its_exact_public_findings(fixture, tmp_path):
    from test_app_server import ReplayTransport
    from watertreatment.app_server import AppServerClient
    from watertreatment.audit_addendum_workflow import _completed_call_protocol
    from watertreatment.partial_audit_completion import PartialAuditAddendum, POLICY
    from watertreatment.reading_tools import ReadingTools
    from watertreatment.storage import hash_file, write_new_json

    work = tmp_path / "synthetic-report"
    work.mkdir()
    record = fixture.record
    bundle = json.loads((fixture.run / "sources.json").read_bytes())
    write_new_json(work / "sources.json", bundle)
    write_new_json(work / "candidate.v00.json", record.model_dump(mode="json"))
    source = bundle["documents"][0]
    scope = report_case()
    scope.update(policy=POLICY, candidate_hash=digest_json(record.model_dump(mode="json")),
        required_query_paths=["observations[0]"], targets=[{"required_evidence_ids": ["e1"]}],
        inherited_source_anchors=[{"evidence_id": "e1", "source_id": source["source"]["source_id"],
                                  "block_id": source["blocks"][0]["block_id"]}])
    scope.pop("scope_sha256")
    scope["scope_sha256"] = digest_json(scope)
    scope_file = work / "scope.json"
    write_new_json(scope_file, scope)
    staged = DraftFindings(findings=[finding(cite_groups=[0])])

    class ReportReplay(ReplayTransport):
        def send(self, message):
            thread = {"threadId": "thread-test", "turnId": "turn-synthetic"}
            def tool(number, name, args):
                self.messages.append({"id": number, "method": "item/tool/call", "params": {
                    **thread, "tool": name, "arguments": args}})
            if message.get("method") == "turn/start":
                turn = {"id": thread["turnId"]}
                self.messages.append({"id": message["id"], "result": {"turn": turn}})
                self.messages.append({"method": "turn/started", "params": {"threadId": thread["threadId"], "turn": turn}})
                tool(801, "wt_query_candidate", {"paths": [["observations", 0]]})
            elif message.get("id") == 801:
                assert message["result"]["success"]
                tool(802, "wt_save_review_findings", staged.model_dump(mode="json"))
            elif message.get("id") == 802:
                assert message["result"]["success"]
                ids = json.loads(message["result"]["contentItems"][0]["text"])["draft_ids"]
                self.messages.append({"method": "item/completed", "params": {**thread, "item": {
                    "type": "agentMessage", "phase": "final_answer", "text": json.dumps(wire(scope, confirmed_drafts=ids))}}})
                self.messages.append({"method": "turn/completed", "params": {"threadId": thread["threadId"],
                    "turn": {"id": thread["turnId"], "status": "completed"}}})
            else:
                super().send(message)

    tools = ReadingTools(bundle, work, max_calls=5, expected_inputs=[])
    tools.bind_candidate(record, work / "candidate.v00.json")
    config = fixture.settings.codex.model_copy(update={"model": "gpt-6.2-sol", "reasoning_effort": "max"})
    client = AppServerClient(config, live=True, transport_factory=lambda *args: ReportReplay(), config_factory=lambda: {})
    client.bind_tools(tools)
    try:
        answer = client.complete(PartialAuditAddendum, prompt="Synthetic report-protocol replay.",
            input_paths=[scope_file, work / "sources.json", work / "candidate.v00.json"], workdir=work,
            audit_root=work / "calls", identity={"stage": "partial_audit_completion_0", "partial_report_version": 3,
                "citation_review": {"scope_path": str(scope_file), "scope_file_sha256": hash_file(scope_file)}})
    finally:
        client.close()
    call = next((work / "calls").iterdir())
    output = json.loads((call / "result.json").read_bytes())
    raw = json.loads((call / "answer.json").read_bytes())
    assert not raw["findings"] and len(raw["confirmed_drafts"]) == 1
    assert _completed_call_protocol(work, output, raw)
    assert expand_report(scope, raw, public_drafts(work, call, scope, output)) == answer
