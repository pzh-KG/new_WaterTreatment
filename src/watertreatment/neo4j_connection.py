"""Explicit, read-only Neo4j health check. No graph import or database fallback."""

from __future__ import annotations

import os
from typing import Any

from .settings import Neo4jSettings


class Neo4jConnectionError(RuntimeError):
    """A deliberately sanitized error safe for CLI output."""


def public_status(settings: Neo4jSettings) -> dict[str, Any]:
    """Only configuration metadata; do not read a secret or connect."""
    return {"enabled": settings.enabled, "uri": settings.uri, "database": settings.database,
            "username": settings.username,
            "password_source": "file" if settings.password_file else ("environment" if settings.password_env else None),
            "connection_checked": False, "graph_writes": False}


def _password(settings: Neo4jSettings) -> str:
    try:
        if settings.password_file is not None:
            value = settings.password_file.read_text(encoding="utf-8").rstrip("\r\n")
        elif settings.password_env is not None:
            value = os.environ.get(settings.password_env, "")
        else:
            raise Neo4jConnectionError("No explicit Neo4j password source is configured.")
    except (OSError, UnicodeError):
        raise Neo4jConnectionError("The configured Neo4j password file could not be read as UTF-8 text.") from None
    if not value or not value.strip() or any(char in value for char in ("\r", "\n", "\x00", "\ufeff")):
        raise Neo4jConnectionError("The configured Neo4j password source is missing or is not a single nonempty UTF-8 value.")
    return value


def _driver_factory(*args, **kwargs):
    # Import only on an explicitly requested live check.
    try:
        from neo4j import GraphDatabase
    except ImportError:
        raise Neo4jConnectionError("The official Neo4j Python driver is not installed.") from None
    return GraphDatabase.driver(*args, **kwargs)


def check_connection(settings: Neo4jSettings) -> dict[str, Any]:
    """Verify connectivity and the actual configured business-database name.

    This performs only a READ procedure, using an explicit database at both
    connection and session boundaries. Driver/server exception text is never
    propagated because it can contain connection or authentication details.
    Connection/acquisition and server transaction timeouts are separate bounds,
    not an overall wall-clock deadline for this function.
    """
    settings = Neo4jSettings.model_validate(settings.model_dump())
    if not settings.enabled:
        raise Neo4jConnectionError("Neo4j is disabled; enable its explicit configuration before checking.")
    password = _password(settings)
    try:
        with _driver_factory(settings.uri, auth=(settings.username, password),
                             connection_timeout=settings.timeout_seconds,
                             connection_acquisition_timeout=settings.timeout_seconds) as driver:
            driver.verify_connectivity(database=settings.database)
            with driver.session(database=settings.database, default_access_mode="READ") as session:
                from neo4j import Query
                query = Query("CALL db.info() YIELD name RETURN name AS database", timeout=settings.timeout_seconds)
                record = session.run(query).single(strict=True)
                actual_database = record["database"] if record is not None else None
                if actual_database != settings.database:
                    raise Neo4jConnectionError("Neo4j returned a different database identity; no fallback is allowed.")
    except Neo4jConnectionError:
        raise
    except Exception:
        raise Neo4jConnectionError("Neo4j read-only connection check failed; verify the configured endpoint, credentials and database.") from None
    return {**public_status(settings), "connection_checked": True, "connected": True,
            "actual_database": actual_database, "database_identity_confirmed": True}
