"""Offline protocol replay: no installed server or provider is started."""
import json
import copy
from collections import deque
from pathlib import Path

import pytest
from pydantic import BaseModel

from test_workflow import workspace
from test_active_reading import active, toolset
from watertreatment.app_server import AppServerClient, audit_media
from watertreatment.codex import ModelCallError


class Answer(BaseModel):
    answer: str


class ReplayTransport:
    def __init__(self, *args, mismatch=False, status="completed", unknown_request=False, mutate=None):
        self.messages = deque()
        self.sent = []
        self.closed = False
        self.mismatch = mismatch
        self.status = status
        self.unknown_request = unknown_request
        self.mutate = mutate
        self.turn = 0

    def send(self, message):
        self.sent.append(message)
        method = message.get("method")
        if method == "initialize":
            self.messages.append({"id": message["id"], "result": {"userAgent": "fake"}})
        elif method == "thread/start":
            assert message["params"]["allowProviderModelFallback"] is False
            assert message["params"]["sandbox"] == "read-only"
            assert message["params"]["dynamicTools"]
            self.messages.append({"id": message["id"], "result": {"thread": {"id": "thread-test"},
                "model": "wrong-model" if self.mismatch else "gpt-6.2-sol", "reasoningEffort": "max"}})
        elif method == "turn/start":
            self.turn += 1
            tid = f"turn-{self.turn}"
            # A notification before the response must not be dropped or parsed as final.
            self.messages.append({"method": "item/completed", "params": {"threadId": "thread-test", "turnId": tid,
                "item": {"type": "agentMessage", "phase": "commentary", "text": "Inspecting sources"}}})
            self.messages.append({"id": message["id"], "result": {"turn": {"id": tid}}})
            self.messages.append({"id": 900, "method": "approval/unknown" if self.unknown_request else "item/tool/call",
                "params": {"threadId": "thread-test", "turnId": tid, "callId": "call-1", "tool": "wt_inventory", "arguments": {}}})
        elif message.get("id") == 900 and "result" in message:
            if self.mutate:
                self.mutate()
            tid = f"turn-{self.turn}"
            self.messages.append({"method": "item/completed", "params": {"threadId": "thread-test", "turnId": tid,
                "item": {"type": "agentMessage", "phase": "final_answer", "text": '{"answer":"source checked"}'}}})
            self.messages.append({"method": "thread/tokenUsage/updated", "params": {"threadId": "thread-test", "tokenUsage": {"last": {"inputTokens": 12}}}})
            self.messages.append({"method": "turn/completed", "params": {"threadId": "thread-test", "turn": {"id": tid, "status": self.status}}})

    def receive(self, timeout):
        if not self.messages:
            raise TimeoutError("replayed transport timeout")
        return self.messages.popleft()

    def close(self):
        self.closed = True


def adapter(active, **options):
    workspace, run, _, plan = active
    transport = ReplayTransport(**options)
    client = AppServerClient(workspace.settings.codex, live=True,
        transport_factory=lambda *a: transport, config_factory=lambda: {})
    client.bind_tools(toolset(active))
    kwargs = {"prompt": "Read the source", "input_paths": [Path(p["path"]) for p in plan["input_paths"]],
              "workdir": run, "audit_root": run / "calls", "identity": {"stage": "test"}}
    return client, transport, kwargs


def test_session_retains_context_and_native_tools_are_audited(active):
    client, transport, kwargs = adapter(active)
    try:
        assert client.complete(Answer, **kwargs).answer == "source checked"
        client.complete(Answer, **kwargs)
        assert sum(m.get("method") == "thread/start" for m in transport.sent) == 1
        registered = next(m["params"]["dynamicTools"] for m in transport.sent if m.get("method") == "thread/start")
        assert "wt_review_gaps" not in {spec["name"] for spec in registered}
        turns = [m for m in transport.sent if m.get("method") == "turn/start"]
        assert len(turns) == 2 and all(m["params"]["effort"] == "max" for m in turns)
        assert client.tools.calls_started == 2
    finally:
        client.close()
    assert transport.closed
    results = [json.loads(p.read_text()) for p in (active[1] / "calls").glob("*/result.json")]
    assert all(r["server_selected_model"] == "gpt-6.2-sol" and r["effective_model"] is None for r in results)
    assert all(r["usage"] for r in results)


def test_model_fallback_is_refused_before_any_turn(active):
    client, transport, kwargs = adapter(active, mismatch=True)
    try:
        for _ in range(2):
            with pytest.raises(ModelCallError, match="differs"):
                client.complete(Answer, **kwargs)
        assert client.calls_started == 0
        assert not any(message.get("method") == "turn/start" for message in transport.sent)
    finally:
        client.close()


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_failed_turn_never_accepts_partial_answer(active, status):
    client, _, kwargs = adapter(active, status=status)
    try:
        with pytest.raises(ModelCallError, match="ended"):
            client.complete(Answer, **kwargs)
    finally:
        client.close()


def test_unknown_server_request_is_rejected(active):
    client, transport, kwargs = adapter(active, unknown_request=True)
    try:
        with pytest.raises(ModelCallError, match="unexpected server request"):
            client.complete(Answer, **kwargs)
        assert any(m.get("id") == 900 and "error" in m for m in transport.sent)
    finally:
        client.close()


def test_source_mutation_during_turn_is_not_accepted(active):
    path = active[1] / "sources.json"
    client, _, kwargs = adapter(active, mutate=lambda: path.write_text("{}", encoding="utf-8"))
    try:
        with pytest.raises(ModelCallError, match="inputs changed"):
            client.complete(Answer, **kwargs)
    finally:
        client.close()


def test_live_gate_and_media_audit_do_not_leak_images(active):
    client, transport, kwargs = adapter(active)
    client.live = False
    with pytest.raises(ModelCallError, match="live mode"):
        client.complete(Answer, **kwargs)
    assert not transport.sent
    result = audit_media({"imageUrl": "data:image/png;base64,abc"})
    assert "data_url_sha256" in result["imageUrl"]


def test_tool_gateway_stays_enabled_without_enabling_shell_or_connectors(tmp_path, monkeypatch):
    from watertreatment.app_server import isolated_overrides
    config_dir = tmp_path / "codex-profile"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[mcp_servers.fixture]\nurl="https://invalid.example"\n[mcp_servers.fixture.env]\nTOKEN="synthetic-secret-never-log"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(config_dir))
    result = isolated_overrides()
    assert result["features.code_mode_host"] is True
    assert result["features.shell_tool"] is False
    assert result["features.multi_agent"] is False
    assert result["mcp_servers.fixture.enabled"] is False
    assert "synthetic-secret" not in json.dumps(result)


class CompactReplay(ReplayTransport):
    def send(self, message):
        super().send(message)
        if message.get("method") == "turn/start":
            self.messages[-1]["params"].update(tool="wt_submit_record", arguments={"record": {"paper": {"id": "synthetic-proposal"}}})
        elif message.get("id") == 900 and "result" in message:
            data = json.loads(message["result"]["contentItems"][0]["text"])
            for event in self.messages:
                item = event.get("params", {}).get("item", {})
                if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                    item["text"] = json.dumps({"submission_id": data["submission_id"]})


def test_native_tool_submission_returns_canonical_record_not_receipt(active):
    from watertreatment.domain import ExtractionRecord
    workspace, run, _, plan = active
    transport = CompactReplay()
    client = AppServerClient(workspace.settings.codex, live=True, transport_factory=lambda *a: transport, config_factory=lambda: {})
    client.bind_tools(toolset(active))
    try:
        record = client.complete(ExtractionRecord, prompt="Synthetic transport check", input_paths=[Path(i["path"]) for i in plan["input_paths"]],
            workdir=run, audit_root=run / "calls", identity={"stage": "synthetic"})
    finally:
        client.close()
    # Acquisition owns paper.id; transport returns the bound canonical record,
    # while the model's original proposal remains in the audited tool artifacts.
    assert record.paper.id == plan["paper_id"]
    assert record.paper.title.status.value == "unread"
    saved = list((run / "calls").glob("*/record.json"))
    assert len(saved) == 1
    assert ExtractionRecord.model_validate_json(saved[0].read_bytes()) == record
    proposals = list((run / "reading_tools").glob("*/unbound_submitted_record.json"))
    assert len(proposals) == 1
    assert ExtractionRecord.model_validate_json(proposals[0].read_bytes()).paper.id == "synthetic-proposal"
    sent = next(m for m in transport.sent if m.get("method") == "turn/start")
    assert set(sent["params"]["outputSchema"]["properties"]) == {"submission_id"}


class IncrementalReplay(ReplayTransport):
    """Model protocol replay that obtains hashes only from actual tool results."""
    def __init__(self, *, return_latest=True):
        super().__init__()
        self.return_latest = return_latest
        self.staged_ids = []

    def send(self, message):
        identifier = message.get("id")
        if identifier not in {900, 901, 902} or "result" not in message:
            super().send(message)
            if message.get("method") == "turn/start":
                self.messages[-1]["params"].update(tool="wt_revision_state", arguments={})
            return
        self.sent.append(message)
        assert message["result"]["success"]
        data = json.loads(message["result"]["contentItems"][0]["text"])
        tid = f"turn-{self.turn}"
        if identifier in {900, 901}:
            if identifier == 901:
                self.staged_ids.append(data["submission_id"])
            next_id = identifier + 1
            edits = [{"operation": "set", "path": ["observations", 0, "value", "value"], "value": 2.5}] \
                if identifier == 900 else [{"operation": "append", "path": ["observations", 0, "limitations"],
                                           "value": "Synthetic source-based qualification."}]
            self.messages.append({"id": next_id, "method": "item/tool/call", "params": {
                "threadId": "thread-test", "turnId": tid, "callId": f"patch-{next_id}",
                "tool": "wt_patch_record", "arguments": {"base_hash": data["base_hash"], "edits": edits}}})
            return
        self.staged_ids.append(data["submission_id"])
        final_id = self.staged_ids[-1] if self.return_latest else self.staged_ids[0]
        self.messages.extend([
            {"method": "item/completed", "params": {"threadId": "thread-test", "turnId": tid,
             "item": {"type": "agentMessage", "phase": "final_answer",
                      "text": json.dumps({"submission_id": final_id})}}},
            {"method": "thread/tokenUsage/updated", "params": {"threadId": "thread-test",
             "tokenUsage": {"last": {"inputTokens": 12}}}},
            {"method": "turn/completed", "params": {"threadId": "thread-test",
             "turn": {"id": tid, "status": "completed"}}},
        ])


@pytest.mark.parametrize("return_latest", [True, False])
def test_native_incremental_revision_freezes_contract_and_returns_only_latest_complete_record(active, return_latest):
    from watertreatment.domain import ExtractionRecord
    from watertreatment.storage import digest_json, hash_file, write_new_json
    workspace, run, bundle, plan = active
    initial = ExtractionRecord.model_validate({
        "paper": {"id": "synthetic-revision", "dataset_role": "development",
                  "title": {"value": "Preserved title", "status": "reported"},
                  "source_ids": [document["source"]["source_id"] for document in bundle["documents"]]},
        "materials": [{"id": "final", "role": "primary_final"}],
        "experiments": [{"id": "joint", "purpose": "coupled", "material_ids": ["final"]}],
        "observations": [{"id": "rate", "experiment_id": "joint", "material_id": "final",
            "metric_kind": "evaporation_rate", "value": {"value": 2.4, "status": "reported"}},
            {"id": "not-reported", "experiment_id": "joint", "material_id": "final",
             "metric_kind": "mineralization_efficiency", "value": {"status": "not_reported"}}],
    })
    original = initial.model_dump(mode="json")
    source_record = run / "revision-input.json"
    write_new_json(source_record, original)
    source_hash = hash_file(source_record)
    tools = toolset(active)
    tools.prepare_revision(initial)
    transport = IncrementalReplay(return_latest=return_latest)
    client = AppServerClient(workspace.settings.codex, live=True,
        transport_factory=lambda *a: transport, config_factory=lambda: {})
    client.bind_tools(tools)
    kwargs = {"prompt": "Synthetic bounded revision", "input_paths": [source_record] + [Path(i["path"]) for i in plan["input_paths"]],
              "workdir": run, "audit_root": run / "calls", "identity": {"stage": "revision_1"}}
    try:
        if return_latest:
            record = client.complete(ExtractionRecord, **kwargs)
        else:
            with pytest.raises(ValueError, match="stale"):
                client.complete(ExtractionRecord, **kwargs)
    finally:
        client.close()
    call = next((run / "calls").glob("call-*"))
    envelope = json.loads((call / "request.json").read_text(encoding="utf-8"))
    request = envelope["request"]
    assert envelope["fingerprint"] == digest_json(request)
    assert "INCREMENTAL REVISION CONTRACT" in request["prompt"]
    assert "wt_revision_state first" in request["prompt"] and "wt_patch_record" in request["prompt"]
    assert "RECORD DELIVERY CONTRACT" not in request["prompt"]
    assert any(source["path"] == str(source_record.resolve()) and source["sha256"] == source_hash
               for source in request["sources"])
    turn = next(message for message in transport.sent if message.get("method") == "turn/start")
    assert turn["params"]["input"][0]["text"] == request["prompt"]
    assert set(turn["params"]["outputSchema"]["properties"]) == {"submission_id"}
    tool_requests = [json.loads(path.read_text(encoding="utf-8"))["tool"]
                     for path in sorted(tools.audit_root.glob("*/request.json"))]
    assert tool_requests == ["wt_revision_state", "wt_patch_record", "wt_patch_record"]
    assert len(transport.staged_ids) == 2 and tools.latest_submission_id == transport.staged_ids[-1]
    assert hash_file(source_record) == source_hash and initial.model_dump(mode="json") == original
    result = json.loads((call / "result.json").read_text(encoding="utf-8"))
    if return_latest:
        expected = copy.deepcopy(original)
        expected["observations"][0]["value"]["value"] = 2.5
        expected["observations"][0]["limitations"].append("Synthetic source-based qualification.")
        assert record.model_dump(mode="json") == expected
        assert json.loads((call / "record.json").read_text(encoding="utf-8")) == expected
        assert result["status"] == "schema_valid_candidate"
        assert result["submission_id"] == transport.staged_ids[-1]
    else:
        assert result["status"] == "failed" and "stale" in result["error"]
        assert not (call / "record.json").exists()


class CandidateAuditReplay(ReplayTransport):
    def __init__(self, tool, arguments):
        super().__init__()
        self.candidate_tool = tool
        self.candidate_arguments = arguments

    def send(self, message):
        super().send(message)
        if message.get("method") == "turn/start":
            self.messages[-1]["params"].update(tool=self.candidate_tool, arguments=self.candidate_arguments)
        elif message.get("id") == 900 and "result" in message:
            from watertreatment.reading import AUDIT_DIMENSIONS, ReadingAudit
            audit = ReadingAudit(findings=[], potential_omissions=[],
                coverage_limitations=["Synthetic protocol replay; not a scientific review."],
                checked_dimensions=list(AUDIT_DIMENSIONS))
            for event in self.messages:
                item = event.get("params", {}).get("item", {})
                if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                    item["text"] = audit.model_dump_json()


@pytest.mark.parametrize("tool_name,arguments,accepted", [
    ("wt_inventory", {}, False),
    ("wt_query_candidate", {"section": "summary"}, False),
    ("wt_query_candidate", {"section": "observations", "ids": ["not-an-id"]}, False),
    ("wt_query_candidate", {"path": ["missing-field"]}, False),
    ("wt_query_candidate", {"section": "paper"}, True),
    ("wt_query_candidate", {"path": ["paper", "title"]}, True),
])
def test_bound_audit_replaces_large_inline_candidate_and_requires_actual_full_field_read(
        active, tool_name, arguments, accepted):
    from watertreatment.domain import ExtractionRecord
    from watertreatment.reading import ReadingAudit
    from watertreatment.storage import digest_json, hash_file, write_new_json
    workspace, run, bundle, plan = active
    candidate = ExtractionRecord.model_validate({
        "paper": {"id": "synthetic-large-candidate", "dataset_role": "development",
                  "title": {"value": "A complete bound paper title", "status": "reported"},
                  "source_ids": [document["source"]["source_id"] for document in bundle["documents"]]},
        "materials": [{"id": "final", "role": "primary_final"}],
        "experiments": [{"id": "joint", "purpose": "coupled", "material_ids": ["final"]}],
        "observations": [{"id": "rate", "experiment_id": "joint", "material_id": "final",
            "metric_kind": "evaporation_rate", "value": {"value": 2.4, "status": "reported",
                "raw": "UNTRUNCATED_CANDIDATE_RAW:" + "source-detail;" * 85000}}],
    })
    compact = candidate.model_dump_json(exclude_defaults=True)
    assert len(compact) > 1000000
    original = candidate.model_dump(mode="json")
    candidate_path = run / "candidate-to-audit.json"
    write_new_json(candidate_path, original)
    original_hash = hash_file(candidate_path)
    tools = toolset(active)
    binding = tools.bind_candidate(candidate, candidate_path)
    transport = CandidateAuditReplay(tool_name, arguments)
    client = AppServerClient(workspace.settings.codex, live=True,
        transport_factory=lambda *args: transport, config_factory=lambda: {})
    client.bind_tools(tools)
    kwargs = {"prompt": "PRE_CANDIDATE_INSTRUCTIONS\n" + compact + "\nPOST_CANDIDATE_INSTRUCTIONS",
        "input_paths": [Path(item["path"]) for item in plan["input_paths"]],
        "workdir": run, "audit_root": run / "calls", "identity": {"stage": "audit_1"}}
    try:
        if accepted:
            audit = client.complete(ReadingAudit, **kwargs)
            assert audit.coverage_limitations and not audit.human_validated
        else:
            with pytest.raises(ModelCallError, match="did not inspect any full fields"):
                client.complete(ReadingAudit, **kwargs)
    finally:
        client.close()
    call = next((run / "calls").glob("call-*"))
    envelope = json.loads((call / "request.json").read_text(encoding="utf-8"))
    request = envelope["request"]
    assert envelope["fingerprint"] == digest_json(request)
    assert request["candidate_binding"] == binding
    source_ids = sorted(doc["source"]["source_id"] for doc in bundle["documents"])
    assert request["schema"]["$defs"]["SourceAnchor"]["properties"]["source_id"]["enum"] == source_ids
    turn = next(m for m in transport.sent if m.get("method") == "turn/start")
    assert turn["params"]["outputSchema"] == request["schema"]
    assert len(request["prompt"]) < 900000
    assert "PRE_CANDIDATE_INSTRUCTIONS" in request["prompt"] and "POST_CANDIDATE_INSTRUCTIONS" in request["prompt"]
    assert compact not in request["prompt"] and "UNTRUNCATED_CANDIDATE_RAW:" not in request["prompt"]
    assert "CANDIDATE_SNAPSHOT " + binding["content_hash"] in request["prompt"]
    assert '"complete_record_inlined":false' in request["prompt"]
    assert any(source["path"] == binding["path"] and source["sha256"] == binding["sha256"]
               for source in request["sources"])
    assert {item["path"] for item in plan["input_paths"]} <= {item["path"] for item in request["sources"]}
    turn = next(message for message in transport.sent if message.get("method") == "turn/start")
    assert turn["params"]["input"][0]["text"] == request["prompt"]
    assert candidate.model_dump(mode="json") == original and hash_file(candidate_path) == original_hash
    assert tools.candidate_for_prompt().model_dump(mode="json") == original
    assert tools.has_candidate_read(binding) is accepted
    result = json.loads((call / "result.json").read_text(encoding="utf-8"))
    assert result["scientific_validation"] == "not_established"
    assert result["status"] == ("schema_valid_candidate" if accepted else "failed")
    if accepted:
        logged = json.loads((tools.audit_root / "tool-0001" / "result.json").read_text(encoding="utf-8"))
        assert logged["success"] and logged["data"]["candidate_binding"] == binding
        assert logged["data"]["mode"] in {"section", "path"} and logged["data"]["returned_count"] == 1


def test_full_audit_selects_only_this_papers_sources_without_rewriting_older_schemas():
    from watertreatment.app_server import _source_bound_output_schema
    from watertreatment.reading import ReadingAudit

    def field(sources):
        schema = _source_bound_output_schema(ReadingAudit, sources)
        return schema["$defs"]["SourceAnchor"]["properties"]["source_id"]

    assert field(["second-manuscript", "first-manuscript"])["enum"] == ["first-manuscript", "second-manuscript"]
    assert field(["independent-paper"])["enum"] == ["independent-paper"]
    assert "enum" not in field([])
    assert "enum" not in field([f"source-{i}" for i in range(201)])
    assert "enum" not in ReadingAudit.model_json_schema()["$defs"]["SourceAnchor"]["properties"]["source_id"]
