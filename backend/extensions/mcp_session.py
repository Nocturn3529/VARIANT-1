"""Cancellation propagation on the canonical MCP SDK session."""

import asyncio

from mcp import types
from mcp.client.session import ClientSession


class CancellableClientSession(ClientSession):
    async def send_request(self, request, result_type, *args, **kwargs):
        # The SDK assigns and increments this counter before its first await.
        # Capture the wire ID at that same boundary; caller request IDs are not
        # MCP protocol IDs. The protocol regression pins this SDK assumption.
        wire_id = self._request_id
        try:
            return await super().send_request(request, result_type, *args, **kwargs)
        except asyncio.CancelledError:
            # Initialization may not be cancelled by a protocol notification.
            if not isinstance(request.root, types.InitializeRequest):
                notification = types.ClientNotification(types.CancelledNotification(
                    params=types.CancelledNotificationParams(
                        requestId=wire_id, reason="Caller cancelled the pending request",
                    ),
                ))
                try:
                    await asyncio.shield(asyncio.wait_for(self.send_notification(notification), 2.0))
                except (Exception, asyncio.CancelledError):
                    # Acceptance of local cancellation is not proof of remote
                    # settlement; retain the original cancellation outcome.
                    pass
            raise
