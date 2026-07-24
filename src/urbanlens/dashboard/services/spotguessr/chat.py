"""Session-scoped live text chat (UL-392).

See ``docs/designs/spotguessr.md`` ("Session chat") - WebSocket-only send,
HTTP-served history for reconnects. ``GameSessionConsumer`` is the only
caller of ``send_chat_message``; the HTTP chat-history endpoint is the only
caller of ``recent_messages``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.spotguessr.model import GameSessionChatMessage
from urbanlens.dashboard.services.spotguessr import realtime
from urbanlens.dashboard.services.spotguessr.serializers import serialize_chat_message

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.spotguessr.model import GameSession

#: See docs/designs/spotguessr.md's config table.
CHAT_HISTORY_LIMIT = 50
MAX_MESSAGE_LENGTH = 1000


def send_chat_message(session: GameSession, profile: Profile, body: str) -> GameSessionChatMessage:
    """Save a chat message and broadcast it to every connected participant.

    Args:
        session: The session this message belongs to.
        profile: Who sent it - caller is responsible for confirming they're
            an actual participant (``controllers.spotguessr``/
            ``GameSessionConsumer`` both check this before calling).
        body: Raw message text, truncated to ``MAX_MESSAGE_LENGTH``.
    """
    message = GameSessionChatMessage.objects.create(session=session, profile=profile, body=body.strip()[:MAX_MESSAGE_LENGTH])
    realtime.broadcast(session.pk, "chat.message", {"message": serialize_chat_message(message)})
    return message


def recent_messages(session: GameSession, *, limit: int = CHAT_HISTORY_LIMIT) -> list[GameSessionChatMessage]:
    """The most recent ``limit`` chat messages in ``session``, oldest first."""
    messages = list(GameSessionChatMessage.objects.for_session(session).select_related("profile__user").order_by("-created")[:limit])
    messages.reverse()
    return messages
