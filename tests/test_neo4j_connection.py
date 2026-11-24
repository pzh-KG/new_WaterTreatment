"""Offline mocks only: these checks never open sockets or read real credentials."""

import json
from contextlib import nullcontext

import pytest
from neo4j import Query
from pydantic import ValidationError

from watertreatment import cli, neo4j_connection
from watertreatment.neo4j_connection import Neo4jConnectionError, check_connection, public_status
from watertreatment.settings import Neo4jSettings, Settings


def configured(tmp_path, **overrides):
    secret = tmp_path / "new-password.txt"
    secret.write_text("synthetic secret\n", encoding="utf-8")
    return Neo4jSettings(enabled=True, uri="bolt://127.0.0.1:7688", database="watertreatment",
                         username="neo4j", **{"password_file": secret, **overrides})


def test_connection_defaults_are_disabled_and_have_no_legacy_endpoint():
    settings = Neo4jSettings()
    assert not settings.enabled and settings.uri is None and settings.database is None
    assert settings.username is None and settings.password_file is None and settings.password_env is None


@pytest.mark.parametrize("kwargs", [
    {"enabled": True},
    {"uri": "bolt://127.0.0.1"},
    {"uri": "bolt://neo4j:synthetic-secret@127.0.0.1:7688"},
    {"uri": "http://127.0.0.1:7475"},
    {"database": ""},
    {"database": "system"},
    {"database": "SYSTEM"},
    {"username": " "},
    {"password_file": "relative-password.txt"},
    {"password_env": ""},
])
def test_invalid_connection_configuration_is_rejected_without_echoing_input(kwargs):
    with pytest.raises(ValidationError) as caught:
        Neo4jSettings(**kwargs)
    assert "synthetic-secret" not in str(caught.value)


def test_two_secret_sources_are_rejected(tmp_path):
    with pytest.raises(ValidationError, match="exactly one"):
        configured(tmp_path, password_env="NEW_WT_SECRET")


@pytest.mark.parametrize("state", ["missing_file", "empty_file", "multiline_file", "non_utf8_file", "missing_env"])
def test_missing_or_invalid_secret_is_rejected_before_driver_creation(tmp_path, monkeypatch, state):
    settings = configured(tmp_path)
    if state == "missing_file":
        settings.password_file.unlink()
    elif state == "empty_file":
        settings.password_file.write_text("\n", encoding="utf-8")
    elif state == "multiline_file":
        settings.password_file.write_text("synthetic secret\nsecond line", encoding="utf-8")
    elif state == "non_utf8_file":
        settings.password_file.write_bytes(b"\xff\xfe")
    else:
        settings.password_file = None
        settings.password_env = "NEW_WT_MISSING_TEST_SECRET"
        monkeypatch.delenv(settings.password_env, raising=False)
    monkeypatch.setattr(neo4j_connection, "_driver_factory", lambda *a, **k: pytest.fail("must not connect"))
    with pytest.raises(Neo4jConnectionError) as caught:
        check_connection(settings)
    assert "synthetic secret" not in str(caught.value)


class FakeDriver:
    def __init__(self, *, database="watertreatment", failure=None):
        self.database = database
        self.failure = failure
        self.closed = False
        self.connectivity_args = None
        self.session_args = None
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def verify_connectivity(self, **kwargs):
        self.connectivity_args = kwargs
        if self.failure:
            raise RuntimeError(self.failure)

    def session(self, **kwargs):
        self.session_args = kwargs
        return nullcontext(self)

    def run(self, query):
        self.queries.append(query)
        return self

    def single(self, *, strict):
        assert strict
        return {"database": self.database}


@pytest.mark.parametrize("secret_source,timeout", [("file", 7), ("environment", 13)])
def test_read_only_check_uses_explicit_database_and_returns_no_secret(tmp_path, monkeypatch, secret_source, timeout):
    settings = configured(tmp_path, timeout_seconds=timeout)
    if secret_source == "environment":
        settings.password_file = None
        settings.password_env = "NEW_WT_SYNTHETIC_SECRET"
        monkeypatch.setenv(settings.password_env, "synthetic secret")
    driver = FakeDriver()
    factory_calls = []
    def factory(*args, **kwargs):
        factory_calls.append((args, kwargs))
        return driver
    monkeypatch.setattr(neo4j_connection, "_driver_factory", factory)
    result = check_connection(settings)
    assert result["connected"] and result["actual_database"] == "watertreatment"
    assert result["database_identity_confirmed"] and not result["graph_writes"]
    assert "synthetic secret" not in json.dumps(result)
    assert factory_calls[0][0] == ("bolt://127.0.0.1:7688",)
    assert factory_calls[0][1]["auth"] == ("neo4j", "synthetic secret")
    assert factory_calls[0][1]["connection_timeout"] == timeout
    assert factory_calls[0][1]["connection_acquisition_timeout"] == timeout
    assert driver.connectivity_args == {"database": "watertreatment"}
    assert driver.session_args == {"database": "watertreatment", "default_access_mode": "READ"}
    assert len(driver.queries) == 1 and isinstance(driver.queries[0], Query)
    assert driver.queries[0].text == "CALL db.info() YIELD name RETURN name AS database"
    assert driver.queries[0].timeout == timeout
    assert driver.closed


def test_wrong_database_identity_is_rejected_without_retry_or_fallback(tmp_path, monkeypatch):
    driver = FakeDriver(database="other-database")
    monkeypatch.setattr(neo4j_connection, "_driver_factory", lambda *a, **k: driver)
    with pytest.raises(Neo4jConnectionError, match="different database identity"):
        check_connection(configured(tmp_path))
    assert len(driver.queries) == 1 and driver.closed


def test_driver_exception_text_cannot_leak_authentication_values(tmp_path, monkeypatch):
    driver = FakeDriver(failure="password=synthetic secret and private server details")
    monkeypatch.setattr(neo4j_connection, "_driver_factory", lambda *a, **k: driver)
    with pytest.raises(Neo4jConnectionError) as caught:
        check_connection(configured(tmp_path))
    assert "synthetic secret" not in str(caught.value)
    assert "private server" not in str(caught.value)
    assert driver.closed


def test_disabled_check_and_public_status_never_read_secret(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    settings.enabled = False
    monkeypatch.setattr(neo4j_connection, "_password", lambda *a: pytest.fail("must not read secret"))
    assert not public_status(settings)["connection_checked"]
    with pytest.raises(Neo4jConnectionError, match="disabled"):
        check_connection(settings)


def cli_config(tmp_path, neo4j):
    settings = Settings(project_root=tmp_path / "project", data_root=tmp_path / "data", neo4j=neo4j)
    path = tmp_path / "project.local.json"
    path.write_text(settings.model_dump_json(), encoding="utf-8")
    return path


@pytest.mark.parametrize("command", ["doctor", "neo4j-check"])
def test_cli_without_live_never_reads_password_or_connects(tmp_path, monkeypatch, capsys, command):
    path = cli_config(tmp_path, configured(tmp_path))
    monkeypatch.setattr(neo4j_connection, "_password", lambda *a: pytest.fail("must not read secret"))
    monkeypatch.setattr(neo4j_connection, "_driver_factory", lambda *a, **k: pytest.fail("must not connect"))
    code = cli.main(["--config", str(path), command])
    captured = capsys.readouterr()
    assert code == (0 if command == "doctor" else 2)
    assert "synthetic secret" not in captured.out + captured.err
    if command == "doctor":
        assert not json.loads(captured.out)["neo4j"]["connection_checked"]


def test_cli_live_uses_the_check_and_sanitizes_driver_errors(tmp_path, monkeypatch, capsys):
    path = cli_config(tmp_path, configured(tmp_path))
    driver = FakeDriver(failure="synthetic secret")
    monkeypatch.setattr(neo4j_connection, "_driver_factory", lambda *a, **k: driver)
    assert cli.main(["--config", str(path), "neo4j-check", "--live"]) == 2
    error = capsys.readouterr().err
    assert "synthetic secret" not in error
    assert "read-only connection check failed" in error


def test_cli_invalid_uri_never_echoes_embedded_password(tmp_path, capsys):
    path = cli_config(tmp_path, Neo4jSettings())
    value = json.loads(path.read_text(encoding="utf-8"))
    value["neo4j"]["uri"] = "bolt://neo4j:synthetic-secret@127.0.0.1:7688"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert cli.main(["--config", str(path), "doctor"]) == 2
    captured = capsys.readouterr()
    assert "synthetic-secret" not in captured.out + captured.err
