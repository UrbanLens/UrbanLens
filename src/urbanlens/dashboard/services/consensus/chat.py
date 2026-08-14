"""Session-scoped live text chat for consensus sessions.

WebSocket-only send, HTTP-served history for reconnects. Binds
``services.core.session_chat.SessionChat``, shared by every participant-session game;
existing import paths and call signatures are unchanged.
"""

from __future__ import annotations

from urbanlens.dashboard.models.consensus.model import ConsensusSession, ConsensusSessionChatMessage
from urbanlens.dashboard.services.consensus import realtime
from urbanlens.dashboard.services.consensus.serializers import serialize_chat_message
from urbanlens.dashboard.services.core.session_chat import CHAT_HISTORY_LIMIT, SessionChat
from urbanlens.dashboard.services.core.text_limits import MAX_SESSION_CHAT_MESSAGE_LENGTH

#: Re-exported for callers that imported these from here before the shared module existed.
MAX_MESSAGE_LENGTH = MAX_SESSION_CHAT_MESSAGE_LENGTH
__all__ = ["CHAT_HISTORY_LIMIT", "MAX_MESSAGE_LENGTH", "recent_messages", "send_chat_message"]

_chat: SessionChat[ConsensusSession, ConsensusSessionChatMessage] = SessionChat(
    manager=ConsensusSessionChatMessage.objects,
    realtime=realtime,
    serialize=serialize_chat_message,
)

send_chat_message = _chat.send
recent_messages = _chat.recent
