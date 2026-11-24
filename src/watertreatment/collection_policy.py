"""Explicit, content-bound user statements about a verified literature collection.

This acquisition policy never decides that a scientific field is not reported.
Its only SI assertion is the user's statement about the absence of a supplement
in a matching, unchanged local collection. The role classifier must separately
establish that no unresolved attachment or combined main/SI file remains.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import stat
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .storage import digest_json, hash_file


DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".doc"})
IGNORED_INVENTORY_NAMES = frozenset({"thumbs.db", "desktop.ini", ".ds_store"})
MAX_POLICY_BYTES = 16 * 1024 * 1024


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class VerifiedDocument(PolicyModel):
    name: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("name")
    @classmethod
    def plain_document_name(cls, value):
        if (value in {".", ".."} or any(char in value for char in ("/", "\\", ":", "\x00"))
                or value.startswith("~$") or Path(value).suffix.casefold() not in DOCUMENT_EXTENSIONS):
            raise ValueError("verified document requires a plain supported filename")
        return value


class VerifiedFolder(PolicyModel):
    folder: str = Field(min_length=1)
    documents: list[VerifiedDocument] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_documents(self):
        if len({doc.name.casefold() for doc in self.documents}) != len(self.documents):
            raise ValueError("verified folder has duplicate document names")
        return self


class CollectionStatement(PolicyModel):
    version: Literal[1]
    policy_id: str = Field(min_length=1, max_length=200)
    source_root: str = Field(min_length=1)
    confirmed_by: str = Field(min_length=1, max_length=200)
    confirmed_at: str = Field(min_length=1, max_length=80)
    statement: str = Field(min_length=1, max_length=16000)
    absent_si_meaning: Literal["author_not_provided"]
    entries: list[VerifiedFolder] = Field(min_length=1, max_length=10000)

    @field_validator("policy_id", "confirmed_by", "statement")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("collection statement metadata must be nonblank")
        return value

    @field_validator("confirmed_at")
    @classmethod
    def dated_confirmation(cls, value):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("collection confirmation must have an ISO date or timestamp") from error
        return value


def _absolute(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("verified collection paths must be absolute without traversal")
    return path


def is_inventory_noise(path: Path) -> bool:
    """Ignore ordinary OS metadata/Word locks, never a directory of attachments."""
    return path.is_file() and (path.name.startswith("~$") or path.name.casefold() in IGNORED_INVENTORY_NAMES)


def _no_links(path: Path, root: Path) -> Path:
    """Check lexical and resolved containment without following a linked entry."""
    path, root = _absolute(path), _absolute(root)
    if path != root and root not in path.parents:
        raise ValueError("verified collection path lies outside its declared root")
    for node in (path, *path.parents):
        info = node.lstat()
        if node.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
            raise ValueError("verified collection does not accept symbolic links or junctions")
        if node == root:
            break
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)):
        raise ValueError("verified collection path escapes its declared root")
    return resolved


@dataclass(frozen=True)
class VerifiedCollectionPolicy:
    path: Path
    manifest_sha256: str
    statement: CollectionStatement
    source_root: Path
    entries: dict[Path, VerifiedFolder]

    def match(self, folder: Path, files: list[dict]) -> dict | None:
        """Return user-confirmation provenance only for this exact current inventory.

        A missing/changed/added document expires applicability rather than
        silently extending the assertion to a new source version. Names and
        hashes are both checked; the caller's cached classified files cannot
        conceal a new attachment. No scientific facts are inferred here.
        """
        folder = _absolute(folder)
        if folder not in self.entries:
            return None
        if hash_file(self.path) != self.manifest_sha256:
            raise ValueError("verified collection manifest changed during use; reload the policy")
        entry = self.entries[folder]
        expected = {doc.name: doc.sha256 for doc in entry.documents}
        try:
            actual_folder = _no_links(folder, self.source_root)
            if not actual_folder.is_dir():
                return None
            supplied = {}
            for file in files:
                name, checksum = file["name"], file["sha256"]
                if name in supplied or ("path" in file and _absolute(file["path"]) != folder / name):
                    return None
                supplied[name] = checksum
            if supplied != expected:
                return None
            # An unsupported archive, separate nested attachment directory or
            # other unexplained file may contain SI. It invalidates absence
            # even if the classifier can read only PDF/Word documents.
            paths = [p for p in folder.iterdir() if not is_inventory_noise(p)]
            if {p.name for p in paths} != set(expected):
                return None
            for path in paths:
                resolved = _no_links(path, self.source_root)
                if not resolved.is_file() or hash_file(resolved) != expected[path.name]:
                    return None
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return {"origin": "user_verified_collection", "policy_id": self.statement.policy_id,
            "manifest_path": str(self.path), "manifest_sha256": self.manifest_sha256,
            "entry_sha256": digest_json(entry.model_dump(mode="json")), "folder": str(folder),
            "confirmed_by": self.statement.confirmed_by, "confirmed_at": self.statement.confirmed_at,
            "statement": self.statement.statement, "absent_si_meaning": self.statement.absent_si_meaning,
            "verification_scope": "exact_current_document_inventory_not_scientific_field_validation"}


class StatementReference(PolicyModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StatementIndex(PolicyModel):
    version: Literal[2]
    manifests: list[StatementReference] = Field(min_length=1, max_length=64)


@dataclass(frozen=True)
class IndexedCollectionPolicy:
    path: Path
    manifest_sha256: str
    policies_by_folder: dict[Path, VerifiedCollectionPolicy]

    def match(self, folder: Path, files: list[dict]) -> dict | None:
        policy = self.policies_by_folder.get(_absolute(folder))
        if policy is None:
            return None
        if hash_file(self.path) != self.manifest_sha256:
            raise ValueError("verified collection manifest changed during use; reload the policy")
        # Retain the original statement and its hash in the source fingerprint.
        return policy.match(folder, files)


def _read_manifest(path: Path, data_root: Path):
    path = _no_links(path, data_root)
    if not path.is_file() or path.suffix.casefold() != ".json" or path.stat().st_size > MAX_POLICY_BYTES:
        raise ValueError("verified collection policy must be a bounded JSON file")
    before = hash_file(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, TypeError) as error:
        raise ValueError("invalid verified collection policy; no absence rule applied") from error
    if hash_file(path) != before:
        raise ValueError("verified collection manifest changed while loading")
    return path, before, raw


def _statement_policy(settings, path, before, raw):
    statement = CollectionStatement.model_validate(raw)
    root = _absolute(statement.source_root)
    if root not in settings.source_roots:
        raise ValueError("verified collection source_root must exactly match a configured source root")
    _no_links(root, root)
    entries = {}
    for entry in statement.entries:
        folder = _absolute(entry.folder)
        if folder.parent != root or folder in entries:
            raise ValueError("verified collection folders must be unique direct children of its source root")
        # A moved or removed paper merely loses applicability. Do not touch a
        # missing entry or open its source data while loading the manifest.
        entries[folder] = entry
    return VerifiedCollectionPolicy(path, before, statement, root, entries)


def load_collection_policy(settings) -> VerifiedCollectionPolicy | IndexedCollectionPolicy | None:
    """Load the configured statement or a hash-bound index of separate confirmations."""
    if settings.collection_policy_path is None:
        return None
    path, before, raw = _read_manifest(settings.collection_policy_path, settings.data_root)
    if not isinstance(raw, dict) or raw.get("version") != 2:
        return _statement_policy(settings, path, before, raw)
    index = StatementIndex.model_validate(raw)
    policies = {}
    for ref in index.manifests:
        child, checksum, content = _read_manifest(Path(ref.path), settings.data_root)
        if checksum != ref.sha256:
            raise ValueError("indexed collection statement differs from its pinned hash")
        policy = _statement_policy(settings, child, checksum, content)
        if policies.keys() & policy.entries.keys():
            raise ValueError("ambiguous folder in collection statement index")
        policies.update(dict.fromkeys(policy.entries, policy))
    return IndexedCollectionPolicy(path, before, policies)
