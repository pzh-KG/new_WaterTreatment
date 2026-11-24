"""Auditable Codex CLI adapter. No model execution occurs without live=True."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

from .settings import CodexSettings
from .storage import digest_json, hash_file, utc_now, write_new_json

T = TypeVar("T", bound=BaseModel)


class ModelCallError(RuntimeError):
    pass


def strict_schema(value: Any) -> Any:
    """Require all defined keys; nullable values represent missing information."""
    if isinstance(value, list):
        return [strict_schema(v) for v in value]
    if not isinstance(value, dict):
        return value
    result = {key: strict_schema(v) for key, v in value.items() if key not in {"default", "examples"}}
    if result.get("type") == "object" or "properties" in result:
        if "properties" not in result and result.get("additionalProperties") is not False:
            raise ValueError("unbounded dictionaries are not supported in model response schemas")
        result["additionalProperties"] = False
        result["required"] = list(result.get("properties", {}))
    return result


def source_snapshot(paths: list[Path]) -> list[dict[str, Any]]:
    return [{"path": str(path.resolve(strict=True)), "sha256": hash_file(path), "size_bytes": path.stat().st_size} for path in paths]


def code_identity(project_root: Path) -> str:
    """Fingerprint current code even in an uncommitted or non-Git workspace."""
    items = []
    for subdirectory in ("src", "config", "prompts"):
        folder = project_root / subdirectory
        if folder.exists():
            for path in sorted(folder.rglob("*")):
                if path.is_file() and path.suffix in {".py", ".json", ".txt", ".toml", ".md", ".html", ".css", ".js"} and "__pycache__" not in path.parts and ".local." not in path.name:
                    items.append({"path": path.relative_to(project_root).as_posix(), "sha256": hash_file(path)})
    for name in ("pyproject.toml", "AGENTS.md", "requirements.lock.txt"):
        manifest = project_root / name
        if manifest.is_file():
            items.append({"path": name, "sha256": hash_file(manifest)})
    return digest_json(items)


def _environment() -> dict[str, str]:
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "HOME", "CODEX_HOME", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def _run(command: list[str], *, prompt: bytes, cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt")
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired as original:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired as cleanup:
            stdout = cleanup.output or original.output or b""
            stderr = cleanup.stderr or original.stderr or b""
        raise subprocess.TimeoutExpired(command[0], timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


class CodexClient:
    def __init__(self, settings: CodexSettings, *, live: bool = False, runner: Callable = _run):
        self.settings = settings
        self.live = live
        self.runner = runner
        self.calls_started = 0

    def complete(self, model: type[T], *, prompt: str, input_paths: list[Path], workdir: Path, audit_root: Path, identity: dict[str, Any], image_paths: list[Path] | None = None) -> T:
        if not self.live:
            raise ModelCallError("model execution is disabled; use an explicit --live run")
        if not self.settings.model:
            raise ModelCallError("live extraction requires an explicit model identifier")
        if self.calls_started >= self.settings.max_calls_per_run:
            raise ModelCallError("run call budget exhausted; no automatic retries")
        executable = self.settings.executable or shutil.which("codex")
        if not executable:
            raise ModelCallError("Codex CLI not found; configure codex.executable")
        executable_path = Path(executable).resolve(strict=True)
        if os.name == "nt" and executable_path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise ModelCallError("configure the native codex.exe for auditable Windows subprocess calls")
        schema = strict_schema(model.model_json_schema())
        snapshot = source_snapshot(input_paths)
        images = [path.resolve(strict=True) for path in (image_paths or [])]
        if any(str(path) not in {item["path"] for item in snapshot} for path in images):
            raise ModelCallError("image attachments must be included in the audited input files")
        request_identity = {**identity, "adapter_version": "2", "requested_model": self.settings.model, "reasoning_effort": self.settings.reasoning_effort, "timeout_seconds": self.settings.timeout_seconds, "schema": schema, "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "sources": snapshot, "image_attachments": [str(p) for p in images], "cli_sha256": hash_file(executable_path), "sandbox": "read-only", "ignore_user_config": True}
        audit_dir = audit_root / (f"call-{self.calls_started + 1:02d}-" + uuid.uuid4().hex[:8])
        audit_dir.mkdir(parents=True)
        write_new_json(audit_dir / "request.json", {"identity": request_identity, "fingerprint": digest_json(request_identity), "prompt": prompt, "created_at": utc_now()})
        write_new_json(audit_dir / "schema.json", schema)
        output_path = audit_dir / "answer.json"
        command = [str(executable_path), "exec", "--ephemeral", "--ignore-user-config", "--json", "--color", "never", "--sandbox", "read-only", "--skip-git-repo-check", "-C", str(workdir), "--model", self.settings.model, "-c", f'model_reasoning_effort="{self.settings.reasoning_effort}"', "-c", 'web_search="disabled"', "--output-schema", str(audit_dir / "schema.json"), "--output-last-message", str(output_path), "-"]
        for path in images:
            command[-1:-1] = ["--image", str(path)]
        if os.name == "nt" and len(subprocess.list2cmdline(command)) >= 30000:
            raise ModelCallError("image arguments exceed the Windows command limit; use a separately designed chunked reading plan, never silently truncate images")
        self.calls_started += 1
        started = time.monotonic()
        result_metadata: dict[str, Any] = {"requested_model": self.settings.model, "effective_model": None, "status": "failed", "calls_started": self.calls_started, "usage": [], "error": None, "scientific_validation": "not_established"}
        stdout = b""
        stderr = b""
        try:
            result = self.runner(command, prompt=prompt.encode("utf-8"), cwd=workdir, timeout=self.settings.timeout_seconds)
            stdout, stderr = result.stdout or b"", result.stderr or b""
            result_metadata["returncode"] = result.returncode
            if result.returncode != 0:
                raise ModelCallError(f"Codex exited with {result.returncode}; see retained stderr")
            events = []
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
                    if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                        result_metadata["usage"].append(event["usage"])
            if any(e.get("type") in {"error", "turn.failed"} for e in events):
                raise ModelCallError("Codex emitted a failure event")
            if not any(e.get("type") == "turn.completed" for e in events):
                raise ModelCallError("missing successful turn completion event")
            if source_snapshot(input_paths) != snapshot:
                raise ModelCallError("input changed during model run; candidate cannot be accepted")
            if not output_path.is_file():
                raise ModelCallError("Codex did not produce the response artifact")
            parsed = model.model_validate_json(output_path.read_bytes())
            result_metadata["status"] = "schema_valid_candidate"
            return parsed
        except subprocess.TimeoutExpired as error:
            stdout, stderr = error.output or b"", error.stderr or b""
            result_metadata["error"] = "timeout"
            raise ModelCallError("Codex timed out; partial output retained, no automatic retry") from error
        except Exception as error:
            result_metadata["error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            (audit_dir / "events.jsonl").write_bytes(stdout)
            (audit_dir / "stderr.txt").write_bytes(stderr)
            result_metadata["elapsed_seconds"] = round(time.monotonic() - started, 3)
            write_new_json(audit_dir / "result.json", result_metadata)

