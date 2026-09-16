import socket
import sys

import pytest

import server


def test_bound_backend_socket_reserves_ephemeral_port_until_uvicorn_uses_it():
    listener = server._bind_server_socket(0)
    contender = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert listener.getsockname()[0] == "127.0.0.1"
        port = listener.getsockname()[1]
        assert port > 0
        with pytest.raises(OSError):
            contender.bind(("127.0.0.1", port))
    finally:
        contender.close()
        listener.close()


def test_backend_cli_does_not_accept_a_non_loopback_host(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", [
        "server.py", "--port-file", str(tmp_path / "backend.json"),
        "--host", "0.0.0.0",
    ])
    with pytest.raises(SystemExit):
        server.main()
