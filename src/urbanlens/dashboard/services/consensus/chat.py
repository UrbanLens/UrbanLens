"""Session-scoped live text chat for competitive Consensus sessions.

Mirrors ``services.spotguessr.chat`` - WebSocket-only send, HTTP-served
history for reconnects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.consensus.model import ConsensusSessionChatMessage
from urbanlens.dashboard.services.consensus import realtime
from urbanlens.dashboard.services.consensus.serializers import serialize_chat_message

if TYPE_CHECKING:
    from urbanlens.dashboard.models.consensus.model import ConsensusSession
    from urbanlens.dashboard.models.profile.model import Profile

CHAT_HISTORY_LIMIT = 50
MAX_MESSAGE_LENGTH = 1000


def send_chat_message(session: ConsensusSession, profile: Profile, body: str) -> ConsensusSessionChatMessage:
    """Save a chat message and broadcast it to every connected participant.

    Args:
        session: The session this message belongs to.
        profile: Who sent it - caller is responsible for confirming they're
            an actual participant.
        body: Raw message text, truncated to ``MAX_MESSAGE_LENGTH``.
    """
    message = ConsensusSessionChatMessage.objects.create(session=session, profile=profile, body=body.strip()[:MAX_MESSAGE_LENGTH])
    realtime.broadcast(session.pk, "chat.message", {"message": serialize_chat_message(message)})
    return message


def recent_messages(session: ConsensusSession, *, limit: int = CHAT_HISTORY_LIMIT) -> list[ConsensusSessionChatMessage]:
    """The most recent ``limit`` chat messages in ``session``, oldest first."""
    messages = list(ConsensusSessionChatMessage.objects.for_session(session).select_related("profile__user").order_by("-created")[:limit])
    messages.reverse()
    return messages
