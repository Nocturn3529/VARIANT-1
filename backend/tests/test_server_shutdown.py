from types import SimpleNamespace

import pytest

import server_http


TOKEN = "shutdown-secret"


def _request(host: str, authorization: str | None = None):
    headers = {}
    if authorization is not None:
        headers["authorization"] = authorization
    return SimpleNamespace(
        client=SimpleNamespace(host=host),
        headers=headers,
    )


def test_shutdown_rejects_non_loopback_before_mutating_server():
    server = SimpleNamespace(should_exit=False)

    response = server_http.handle_shutdown(
        {"server": server}, TOKEN, _request("192.0.2.10", f"Bearer {TOKEN}")
    )

    assert response.status_code == 403
    assert server.should_exit is False


@pytest.mark.parametrize("authorization", [None, "", "Bearer wrong-secret"])
def test_shutdown_rejects_missing_or_wrong_bearer(authorization):
    server = SimpleNamespace(should_exit=False)

    response = server_http.handle_shutdown(
        {"server": server}, TOKEN, _request("127.0.0.1", authorization)
    )

    assert response.status_code == 401
    assert server.should_exit is False


def test_shutdown_requires_a_live_uvicorn_server():
    response = server_http.handle_shutdown(
        {}, TOKEN, _request("::1", f"Bearer {TOKEN}")
    )

    assert response.status_code == 503


def test_shutdown_sets_should_exit_for_valid_loopback_bearer_only():
    server = SimpleNamespace(should_exit=False)

    response = server_http.handle_shutdown(
        {"server": server}, TOKEN, _request("127.0.0.1", f"Bearer {TOKEN}")
    )

    assert response.status_code == 202
    assert server.should_exit is True
