"""Tests have no model-provider or database network access."""
import socket
import subprocess
import inspect

import pytest


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network connections are forbidden in offline tests")

    original_connect = socket.socket.connect
    socketpair_code = getattr(socket.socketpair, "__code__", None)

    def guarded_connect(connection, address):
        # Windows implements the stdlib socketpair with a private ephemeral
        # loopback listener. asyncio uses it for its own wake-up pipe. Permit
        # only that exact call site, its own client object and listener address;
        # ordinary loopback, Neo4j and Internet connections remain forbidden.
        frame = inspect.currentframe().f_back
        try:
            if socketpair_code is not None and frame.f_code is socketpair_code:
                listener = frame.f_locals.get("lsock")
                client = frame.f_locals.get("csock")
                if listener is not None and connection is client:
                    private_address = listener.getsockname()[:2]
                    if private_address[0] in {"127.0.0.1", "::1"} and address == private_address:
                        return original_connect(connection, address)
        finally:
            del frame
        return denied()

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    original_popen = subprocess.Popen

    def guarded_popen(command, *args, **kwargs):
        parts = [str(x) for x in command] if isinstance(command, (list, tuple)) else [str(command)]
        if "codex" in parts[0].lower() or "neo4j" in parts[0].lower() or "--live" in parts:
            raise AssertionError("model/Neo4j subprocesses are forbidden in offline tests")
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)

