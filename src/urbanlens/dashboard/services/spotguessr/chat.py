"""Session-scoped live text chat for spotguessr sessions.

WebSocket-only send, HTTP-served history for reconnects. Binds
``services.core.session_chat.SessionChat``, shared by every participant-session game;
existing import paths and call signatures are unchanged.
"""

from __future__ import annotations

from urbanlens.dashboard.models.spotguessr.model import GameSession, GameSessionChatMessage
from urbanlens.dashboard.services.core.session_chat import CHAT_HISTORY_LIMIT, SessionChat
from urbanlens.dashboard.services.core.text_limits import MAX_SESSION_CHAT_MESSAGE_LENGTH
from urbanlens.dashboard.services.spotguessr import realtime
from urbanlens.dashboard.services.spotguessr.serializers import serialize_chat_message

#: Re-exported for callers that imported these from here before the shared module existed.
MAX_MESSAGE_LENGTH = MAX_SESSION_CHAT_MESSAGE_LENGTH
__all__ = ["CHAT_HISTORY_LIMIT", "MAX_MESSAGE_LENGTH", "recent_messages", "send_chat_message"]

_chat: SessionChat[GameSession, GameSessionChatMessage] = SessionChat(
    manager=GameSessionChatMessage.objects,
    realtime=realtime,
    serialize=serialize_chat_message,
)

send_chat_message = _chat.send
recent_messages = _chat.recent
