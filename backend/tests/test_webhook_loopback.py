"""Webhook automations accept only loopback callers, checked before the token."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import server_http


@pytest.mark.asyncio
@pytest.mark.parametrize("client", [SimpleNamespace(host="10.0.0.5"), None])
async def test_non_loopback_webhook_is_refused_before_token_lookup(client):
    lookups = []
    srv = SimpleNamespace(automations=SimpleNamespace(
        get_by_webhook=lambda token: lookups.append(token) or {"id": "task"},
    ))
    request = SimpleNamespace(
        headers={}, body=AsyncMock(return_value=b""), client=client,
    )
    response = await server_http.handle_webhook(srv, "secret", request)
    assert response.status_code == 403
    assert json.loads(response.body)["error"] == "loopback only"
    assert lookups == []
