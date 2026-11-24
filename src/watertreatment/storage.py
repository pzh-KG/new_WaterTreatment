"""Immutable artifacts, content-addressed sources and an append-only run history."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


_receipt_cohort_time: ContextVar[str | None] = ContextVar("receipt_cohort_time", default=None)


@contextmanager
def cohort_at_receipt(created_at: str):
    """Replay nested cohort lookups at the registered date, local to this read."""
    instant = datetime.fromisoformat(created_at)
    if instant.tzinfo is None:
        raise ValueError("receipt replay requires a timezone-aware registration time")
    cutoff = instant.astimezone(timezone.utc).isoformat(timespec="microseconds")
    enclosing = _receipt_cohort_time.get()
    token = _receipt_cohort_time.set(min(enclosing, cutoff) if enclosing else cutoff)
    try:
        yield
    finally:
        _receipt_cohort_time.reset(token)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_new_json(path: Path, value: Any) -> None:
    """Publish a complete file without ever replacing a pre-existing artifact."""
    write_new_bytes(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))


def write_new_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) or value in {".", ".."}:
        raise ValueError("identifier must contain only ASCII letters, digits, '.', '_' or '-'")
    return value


class DataStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / "store.json"
        if marker.exists():
            data = json.loads(marker.read_text(encoding="utf-8"))
            if data.get("format") != "watertreatment-store" or data.get("version") != 1:
                raise ValueError("unrecognized data store; no automatic migration performed")
        else:
            if any(self.root.iterdir()):
                raise ValueError("refusing to initialize a nonempty unrecognized data root")
            write_new_json(marker, {"format": "watertreatment-store", "version": 1, "created_at": utc_now()})
        for name in ("originals", "runs", "records", "exports"):
            (self.root / name).mkdir(exist_ok=True)
        with self.connection(initializing=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"unsupported catalog schema {version}")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, original_path TEXT NOT NULL,
                    stored_path TEXT NOT NULL, size_bytes INTEGER NOT NULL, imported_at TEXT NOT NULL,
                    UNIQUE(sha256, original_path)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL,
                    manifest_path TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(run_id),
                    event TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cohort_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL, role TEXT NOT NULL, reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connection(self, *, initializing: bool = False) -> Iterator[sqlite3.Connection]:
        marker = self.root / "store.json"
        if not marker.is_file():
            raise ValueError("data store not initialized; run 'wt init' first")
        metadata = json.loads(marker.read_text(encoding="utf-8"))
        if metadata.get("format") != "watertreatment-store" or metadata.get("version") != 1:
            raise ValueError("unrecognized data store marker")
        if not initializing and not (self.root / "catalog.sqlite3").is_file():
            raise ValueError("catalog is missing; run 'wt init' to finish initialization")
        db = sqlite3.connect(self.root / "catalog.sqlite3", timeout=30)
        try:
            if not initializing and db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("unsupported or incomplete catalog schema")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def import_file(self, path: Path) -> dict[str, Any]:
        with self.connection():
            pass
        path = path.resolve(strict=True)
        if not path.is_file():
            raise ValueError("only regular files may be imported")
        digest = hash_file(path)
        target = self.root / "originals" / digest[:2] / (digest + path.suffix.lower())
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.parent / (uuid.uuid4().hex + ".tmp")
            try:
                shutil.copyfile(path, temporary)
                if hash_file(temporary) != digest:
                    raise ValueError("source changed during import")
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        if hash_file(target) != digest:
            raise ValueError("content-addressed original failed integrity check")
        source_id = "src-" + digest_json({"path": str(path), "sha256": digest})[:24]
        record = {"source_id": source_id, "sha256": digest, "original_path": str(path), "stored_path": str(target), "size_bytes": target.stat().st_size, "imported_at": utc_now()}
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO sources VALUES (:source_id,:sha256,:original_path,:stored_path,:size_bytes,:imported_at)", record)
            db.row_factory = sqlite3.Row
            saved = db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
        return dict(saved)

    def new_run(self, kind: str, manifest: dict[str, Any]) -> Path:
        with self.connection() as db:
            return self.new_run_in_transaction(db, kind, manifest)

    def new_run_in_transaction(self, db: sqlite3.Connection, kind: str, manifest: dict[str, Any]) -> Path:
        """Register a run and its creation event in the caller's transaction."""
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
        folder = self.root / "runs" / run_id
        folder.mkdir(parents=False)
        payload = {**manifest, "run_id": run_id, "kind": kind, "created_at": utc_now()}
        write_new_json(folder / "manifest.json", payload)
        db.execute("INSERT INTO runs VALUES (?,?,?,?)", (run_id, kind, payload["created_at"], str(folder / "manifest.json")))
        db.execute("INSERT INTO run_events(run_id,event,payload,created_at) VALUES(?,?,?,?)",
                   (run_id, "created", "{}", utc_now()))
        return folder

    def event(self, run_id: str, event: str, payload: dict[str, Any]) -> None:
        with self.connection() as db:
            db.execute("INSERT INTO run_events(run_id,event,payload,created_at) VALUES (?,?,?,?)", (run_id, event, canonical_json(payload).decode("utf-8"), utc_now()))

    def last_event(self, run_id: str, event: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM run_events WHERE run_id=? AND event=? ORDER BY id DESC LIMIT 1", (run_id, event)).fetchone()
        return json.loads(row[0]) if row else None

    def record_development_use(self, run_id: str, paper_id: str, source_hashes: list[str], reason: str) -> None:
        """Record rule/debug exposure without rewriting an immutable cohort identity.

        Such a paper can still be resumed as its original unassigned candidate,
        but it and identical source bytes cannot later become independent heldout
        validation. Existing cohort_events and frozen plans remain untouched.
        """
        safe_name(run_id)
        if not isinstance(paper_id, str) or not paper_id.strip():
            raise ValueError("development use requires an explicit paper identity")
        if (not isinstance(source_hashes, list) or not source_hashes
                or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
                       for value in source_hashes)):
            raise ValueError("development use requires exact source SHA-256 hashes")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("development use requires a concrete reason")
        payload = {"version": 1, "paper_id": paper_id, "source_hashes": sorted(set(source_hashes)),
                   "reason": reason.strip(), "interpretation": "Development use excludes independent heldout validation; "
                   "the original plan/candidate cohort identity is unchanged."}
        with self.connection() as db:
            if db.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
                raise ValueError("development use requires a registered run")
            db.execute("INSERT INTO run_events(run_id,event,payload,created_at) VALUES (?,?,?,?)",
                       (run_id, "development_use", canonical_json(payload).decode("utf-8"), utc_now()))

    @staticmethod
    def _has_recorded_development_use(db, paper_id: str, hashes: list[str]) -> bool:
        """Use the existing append-only ledger; no SQLite JSON extension needed."""
        requested = set(hashes)
        for (text,) in db.execute("SELECT payload FROM run_events WHERE event='development_use'"):
            payload = json.loads(text)
            if payload.get("paper_id") == paper_id or requested.intersection(payload.get("source_hashes", [])):
                return True
        return False

    def register_cohort(self, paper_id: str, hashes: list[str], role: str) -> str:
        """Exposure is shared by paper identity and identical source bytes."""
        if role not in {"development", "heldout", "unassigned"}:
            raise ValueError("invalid cohort role")
        with self.connection() as db:
            # Keep exposure checks and registration atomic with concurrent
            # append-only development-use events.
            db.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in hashes) or "NULL"
            rows = db.execute(f"SELECT role FROM cohort_events WHERE paper_id=? OR source_sha256 IN ({placeholders})", [paper_id, *hashes]).fetchall()
            previously_development = any(row[0] == "development" for row in rows)
            if role == "heldout" and (previously_development or self._has_recorded_development_use(db, paper_id, hashes)):
                raise ValueError("development exposure cannot be relabeled as independent heldout validation")
            effective_role = "development" if previously_development else role
            for digest in hashes:
                db.execute("INSERT INTO cohort_events(paper_id,source_sha256,role,reason,created_at) VALUES (?,?,?,?,?)", (paper_id, digest, effective_role, "explicit extraction-plan registration", utc_now()))
        return effective_role

    def has_development_exposure(self, paper_id: str, hashes: list[str]) -> bool:
        """Read the cohort ledger, using its registration date only inside receipt replay."""
        cutoff = _receipt_cohort_time.get()
        with self.connection() as db:
            placeholders = ",".join("?" for _ in hashes) or "NULL"
            row = db.execute(
                f"SELECT 1 FROM cohort_events WHERE role='development' AND "
                f"(paper_id=? OR source_sha256 IN ({placeholders})) "
                "AND (? IS NULL OR created_at <= ?) LIMIT 1",
                [paper_id, *hashes, cutoff, cutoff],
            ).fetchone()
        return row is not None

    def runs(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY created_at DESC")]

