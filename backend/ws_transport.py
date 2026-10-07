"""Recognize a lost observer without misclassifying it as a provider failure."""
from starlette.websockets import WebSocketDisconnect


def transport_disconnected(error: BaseException) -> bool:
    if isinstance(error, (WebSocketDisconnect, ConnectionError)):
        return True
    if not isinstance(error, RuntimeError):
        return False
    text = str(error).lower()
    return any(marker in text for marker in (
        'once a close message has been sent',
        "after sending 'websocket.close'",
        'websocket is not connected',
        'cannot call "receive" once a disconnect message',
    ))
