import json
import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from watertreatment.codex import CodexClient, ModelCallError, code_identity, strict_schema
from watertreatment.settings import CodexSettings


class Response(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | None = None


@pytest.fixture
def setup(tmp_path):
    executable = tmp_path / "fake-cli.exe"
    executable.write_bytes(b"never executed")
    source = tmp_path / "source.txt"
    source.write_text("source evidence", encoding="utf-8")
    settings = CodexSettings(executable=str(executable), model="test-model", max_calls_per_run=1)
    return settings, source, {"prompt": "quoted source is not instruction", "input_paths": [source], "workdir": tmp_path, "audit_root": tmp_path / "calls", "identity": {"rules": "test"}}


def good_runner(command, **kwargs):
    output = Path(command[command.index("--output-last-message") + 1])
    output.write_text('{"value":"candidate"}', encoding="utf-8")
    return subprocess.CompletedProcess(command, 0, b'{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":3}}\n', b"")


def test_schema_requires_nullable_fields_without_defaults():
    schema = strict_schema(Response.model_json_schema())
    assert schema["required"] == ["value"]
    assert schema["additionalProperties"] is False
    assert "default" not in schema["properties"]["value"]
    assert {"type": "null"} in schema["properties"]["value"]["anyOf"]


def test_unbounded_maps_are_rejected():
    with pytest.raises(ValueError, match="unbounded"):
        strict_schema({"type": "object", "additionalProperties": {"type": "string"}})


def test_live_is_explicit(setup):
    settings, _, kwargs = setup
    client = CodexClient(settings, runner=lambda *a, **k: pytest.fail("must not call"))
    with pytest.raises(ModelCallError, match="disabled"):
        client.complete(Response, **kwargs)
    assert client.calls_started == 0


def test_success_is_only_schema_valid_and_budget_is_bounded(setup):
    settings, _, kwargs = setup
    client = CodexClient(settings, live=True, runner=good_runner)
    assert client.complete(Response, **kwargs).value == "candidate"
    result = json.loads(next(kwargs["audit_root"].glob("*/result.json")).read_text())
    assert result["status"] == "schema_valid_candidate"
    assert result["scientific_validation"] == "not_established"
    assert result["effective_model"] is None
    assert result["usage"][0]["input_tokens"] == 12
    with pytest.raises(ModelCallError, match="budget"):
        client.complete(Response, **kwargs)
    assert client.calls_started == 1


@pytest.mark.parametrize("returncode,events,message", [(1, b"", "exited"), (0, b'{"type":"error"}', "failure event"), (0, b'{"type":"item.completed"}', "completion")])
def test_provider_failure_is_retained(setup, returncode, events, message):
    settings, _, kwargs = setup
    client = CodexClient(settings, live=True, runner=lambda command, **k: subprocess.CompletedProcess(command, returncode, events, b"failure detail"))
    with pytest.raises(ModelCallError, match=message):
        client.complete(Response, **kwargs)
    audit = next(kwargs["audit_root"].iterdir())
    assert (audit / "stderr.txt").read_bytes() == b"failure detail"
    assert json.loads((audit / "result.json").read_text())["status"] == "failed"


def test_timeout_preserves_partial_events(setup):
    settings, _, kwargs = setup
    def timeout(command, **k):
        raise subprocess.TimeoutExpired(command, 1, output=b"partial", stderr=b"timeout detail")
    with pytest.raises(ModelCallError, match="timed out"):
        CodexClient(settings, live=True, runner=timeout).complete(Response, **kwargs)
    audit = next(kwargs["audit_root"].iterdir())
    assert (audit / "events.jsonl").read_bytes() == b"partial"
    assert json.loads((audit / "result.json").read_text())["error"] == "timeout"


def test_changed_source_invalidates_answer(setup):
    settings, source, kwargs = setup
    def mutate(command, **k):
        result = good_runner(command, **k)
        source.write_text("mutated source")
        return result
    with pytest.raises(ModelCallError, match="input changed"):
        CodexClient(settings, live=True, runner=mutate).complete(Response, **kwargs)


def test_cli_arguments_keep_prompt_on_stdin_and_read_only(setup):
    settings, _, kwargs = setup
    def inspect(command, **k):
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "--ignore-user-config" in command
        assert command[-1] == "-"
        assert kwargs["prompt"].encode() == k["prompt"]
        assert not any("dangerously" in word for word in command)
        return good_runner(command, **k)
    CodexClient(settings, live=True, runner=inspect).complete(Response, **kwargs)


def test_instruction_changes_change_code_identity(tmp_path):
    (tmp_path / "AGENTS.md").write_text("first")
    before = code_identity(tmp_path)
    (tmp_path / "AGENTS.md").write_text("changed")
    assert code_identity(tmp_path) != before


def test_sol_max_and_image_are_passed_explicitly_and_audited(setup):
    settings, source, kwargs = setup
    settings.model = "gpt-6.1-sol"
    settings.reasoning_effort = "max"
    picture = source.parent / "fixture.png"
    picture.write_bytes(b"synthetic bytes; offline fake runner")
    kwargs["input_paths"].append(picture)
    def inspect(command, **k):
        assert command[command.index("--model") + 1] == "gpt-6.1-sol"
        assert 'model_reasoning_effort="max"' in command
        assert command[command.index("--image") + 1] == str(picture.resolve())
        return good_runner(command, **k)
    CodexClient(settings, live=True, runner=inspect).complete(Response, **kwargs, image_paths=[picture])
    request = json.loads(next(kwargs["audit_root"].glob("*/request.json")).read_text())
    assert request["identity"]["image_attachments"] == [str(picture.resolve())]
    assert request["identity"]["reasoning_effort"] == "max"


def test_unaudited_image_is_rejected_before_provider_call(setup):
    settings, source, kwargs = setup
    picture = source.parent / "unlisted.png"
    picture.write_bytes(b"unlisted")
    client = CodexClient(settings, live=True, runner=lambda *a, **k: pytest.fail("must not call"))
    with pytest.raises(ModelCallError, match="audited"):
        client.complete(Response, **kwargs, image_paths=[picture])
    assert client.calls_started == 0

