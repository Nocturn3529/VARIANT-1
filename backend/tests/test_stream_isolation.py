"""Stream isolation: start/done carry client_id + source for multi-surface Deck."""

from __future__ import annotations

from chat_pipeline import bind_turn_identity, infer_turn_source, stream_meta
from chat_session import ConnectionSession


def test_infer_turn_source_from_client_prefix():
    assert infer_turn_source("deck-react-1") == "chat"
    assert infer_turn_source("voice-1") == "voice"
    assert infer_turn_source("", "voice") == "voice"
    assert infer_turn_source("") == ""


def test_stream_meta_from_session():
    # Identity lives on session.active (nested ActiveTurn), not flat session.
    sess = ConnectionSession()
    bind_turn_identity(sess, client_id="voice-xyz", source="voice")
    sess.active.turn_session_id = "chat-voice"
    assert sess.active.turn_client_id == "voice-xyz"
    meta = stream_meta(sess)
    assert meta["client_id"] == "voice-xyz"
    assert meta["source"] == "voice"
    assert meta["session_id"] == "chat-voice"


def test_stream_meta_explicit_overrides():
    meta = stream_meta(None, client_id="deck-react-9", source="")
    assert meta["client_id"] == "deck-react-9"
    assert meta["source"] == "chat"


def test_stream_meta_without_active_is_empty():
    class Bare:
        pass
    assert stream_meta(Bare()) == {}
