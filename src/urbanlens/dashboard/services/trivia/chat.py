"""Session-scoped live text chat. Mirrors ``services.spotguessr.chat``.

WebSocket-only send, HTTP-served history for reconnects. ``TriviaSessionConsumer``
is the only caller of ``send_chat_message``; the HTTP chat-history endpoint is
the only caller of ``recent_messages``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.trivia.model import TriviaSessionChatMessage
from urbanlens.dashboard.services.trivia import realtime
from urbanlens.dashboard.services.trivia.serializers import serialize_chat_message

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trivia.model import TriviaSession

CHAT_HISTORY_LIMIT = 50
MAX_MESSAGE_LENGTH = 1000


def send_chat_message(session: TriviaSession, profile: Profile, body: str) -> TriviaSessionChatMessage:
    """Save a chat message and broadcast it to every connected participant.

    Args:
        session: The session this message belongs to.
        profile: Who sent it - caller is responsible for confirming they're
            an actual participant (``controllers.trivia``/
            ``TriviaSessionConsumer`` both check this before calling).
        body: Raw message text, truncated to ``MAX_MESSAGE_LENGTH``.
    """
    message = TriviaSessionChatMessage.objects.create(session=session, profile=profile, body=body.strip()[:MAX_MESSAGE_LENGTH])
    realtime.broadcast(session.pk, "chat.message", {"message": serialize_chat_message(message)})
    return message


def recent_messages(session: TriviaSession, *, limit: int = CHAT_HISTORY_LIMIT) -> list[TriviaSessionChatMessage]:
    """The most recent ``limit`` chat messages in ``session``, oldest first."""
    messages = list(TriviaSessionChatMessage.objects.for_session(session).select_related("profile__user").order_by("-created")[:limit])
    messages.reverse()
    return messages
