"""Explicit isolated configuration; importing this module has no side effects."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CodexSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    executable: str | None = None
    model: str | None = None
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "max"
    timeout_seconds: int = Field(default=600, ge=1, le=3600)
    max_calls_per_run: int = Field(default=2, ge=1, le=20)


class ReadingSettings(BaseModel):
    """Versioned reading policy; turn limits are not reasoning-token limits."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    max_revisions: int = Field(default=2, ge=0, le=5)
    max_tool_calls: int = Field(default=160, ge=1, le=300)
    total_timeout_seconds: int = Field(default=7200, ge=60, le=21600)


class Neo4jSettings(BaseModel):
    """An opt-in endpoint with one explicit secret source; no legacy defaults."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    enabled: bool = False
    uri: str | None = None
    database: str | None = None
    username: str | None = None
    password_file: Path | None = None
    password_env: str | None = None
    timeout_seconds: int = Field(default=10, ge=1, le=120)

    @model_validator(mode="after")
    def explicit_connection(self) -> "Neo4jSettings":
        if self.uri is not None:
            try:
                endpoint = urlsplit(self.uri)
                valid = (endpoint.scheme in {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"}
                         and endpoint.hostname and endpoint.port is not None
                         and endpoint.username is None and endpoint.password is None
                         and not endpoint.query and not endpoint.fragment and endpoint.path in {"", "/"}
                         and not any(char.isspace() for char in self.uri))
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("neo4j.uri requires a supported scheme, host and explicit port without embedded credentials")
        for name in ("database", "username"):
            value = getattr(self, name)
            if value is not None and (not value.strip() or value != value.strip()):
                raise ValueError(f"neo4j.{name} must be nonempty and have no surrounding whitespace")
        if self.database is not None and self.database.casefold() == "system":
            raise ValueError("neo4j.database must be an explicit business database, not system")
        if self.password_file is not None and not self.password_file.is_absolute():
            raise ValueError("neo4j.password_file must be an explicit absolute path")
        if self.password_env is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.password_env):
            raise ValueError("neo4j.password_env must be an environment variable name")
        if self.password_file is not None and self.password_env is not None:
            raise ValueError("configure exactly one Neo4j password source")
        if self.enabled and (self.uri is None or self.database is None or self.username is None
                             or (self.password_file is None and self.password_env is None)):
            raise ValueError("enabled Neo4j requires uri, database, username and one explicit password source")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schema_version: Literal["1"] = "1"
    project_root: Path
    data_root: Path
    source_roots: list[Path] = Field(default_factory=list)
    collection_policy_path: Path | None = None
    rules_version: str = "0.5"
    taxonomy_path: Path = Path("config/taxonomy.v0.5.json")
    codex: CodexSettings = Field(default_factory=CodexSettings)
    reading: ReadingSettings = Field(default_factory=ReadingSettings)
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)

    @model_validator(mode="after")
    def isolated_roots(self) -> "Settings":
        self.project_root = self.project_root.expanduser().resolve()
        self.data_root = self.data_root.expanduser().resolve()
        self.source_roots = [p.expanduser().resolve() for p in self.source_roots]
        if self.project_root == self.data_root or self.project_root in self.data_root.parents or self.data_root in self.project_root.parents:
            raise ValueError("project_root and data_root must be separate, non-nested directories")
        for root in self.source_roots:
            if root == self.data_root or root in self.data_root.parents or self.data_root in root.parents:
                raise ValueError("source_roots must not overlap the writable data_root")
        if self.collection_policy_path is not None:
            policy_path = self.collection_policy_path.expanduser()
            if (not policy_path.is_absolute() or ".." in policy_path.parts
                    or policy_path.suffix.casefold() != ".json"
                    or not policy_path.resolve().is_relative_to(self.data_root)):
                raise ValueError("collection_policy_path must be an explicit JSON path inside data_root")
            self.collection_policy_path = policy_path
        if not self.taxonomy_path.is_absolute():
            self.taxonomy_path = self.project_root / self.taxonomy_path
        return self

    def assert_source_allowed(self, path: Path) -> Path:
        resolved = path.resolve(strict=True)
        if not any(resolved == root or root in resolved.parents for root in self.source_roots):
            raise ValueError(f"source is outside configured source_roots: {resolved}")
        return resolved


def load_settings(path: str | Path) -> Settings:
    """Never auto-load old .env, login tokens, global defaults or passwords."""
    config_path = Path(path).resolve(strict=True)
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    for key in ("project_root", "data_root"):
        if key not in raw or not Path(raw[key]).is_absolute():
            raise ValueError(f"{key} must be an explicit absolute path")
    if any(not Path(p).is_absolute() for p in raw.get("source_roots", [])):
        raise ValueError("source_roots must be absolute paths")
    return Settings.model_validate(raw)

