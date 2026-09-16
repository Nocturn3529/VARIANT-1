"""Durable peer-agent communication across native chats and adapters."""

from .repository import PeerRepository
from .service import PeerCommunicationService, PeerError

__all__ = ["PeerCommunicationService", "PeerError", "PeerRepository"]
