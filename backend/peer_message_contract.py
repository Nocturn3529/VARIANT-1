"""Shared envelope guidance; no peer-service or runtime imports."""

LEGACY_PEER_REPLY_HINT = "\n\nReply only when useful with peers.inspect_message(...).reply(...)."
PEER_REPLY_HINT = (
    "\n\nReply only when useful. Select ipython(category='operate') first; "
    "then use the mounted peers object, not a Python import or session.context(). "
    "Set message_id to the exact Message ID above; "
    "message = peers.inspect_message(message_id=message_id); "
    "message.reply(text=reply_text), where reply_text is your actual result. "
    "The message handle preserves the original exchange. "
    "Use toolbelt.search('peer reply') or peers.describe('inspect_message') "
    "if the current mount or call shape is unclear."
)
