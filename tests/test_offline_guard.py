"""The asyncio wake-up pipe exception must never permit service connections."""
import socket
import subprocess

import pytest


@pytest.mark.parametrize("address", [("127.0.0.1", 7688), ("127.0.0.1", 7475), ("203.0.113.1", 443)])
def test_offline_guard_still_blocks_loopback_database_and_external_connections(address):
    with pytest.raises(AssertionError, match="network connections"):
        socket.create_connection(address)
    with socket.socket() as connection:
        with pytest.raises(AssertionError, match="network connections"):
            connection.connect(address)
        with pytest.raises(AssertionError, match="network connections"):
            connection.connect_ex(address)


def test_offline_guard_permits_only_stdlib_owned_control_socket_pair():
    left, right = socket.socketpair()
    try:
        left.sendall(b"internal control event")
        assert right.recv(64) == b"internal control event"
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("command", [["codex.exe", "app-server"], ["neo4j", "console"], ["python", "tool.py", "--live"]])
def test_offline_guard_still_blocks_real_provider_and_graph_processes(command):
    with pytest.raises(AssertionError, match="model/Neo4j subprocesses"):
        subprocess.Popen(command)
