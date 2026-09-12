"""Session-scoped live text chat, shared by every participant-session game."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from django.db.models import Model

from urbanlens.dashboard.services.core.text_limits import MAX_SESSION_CHAT_MESSAGE_LENGTH

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

#: How many past messages a reconnecting client is served.
CHAT_HISTORY_LIMIT = 50


class SessionRealtime(Protocol):
    """The broadcast entry point of a game's ``realtime`` module.
    Deliberately the *module* rather than the ``SessionBroadcaster`` it wraps: the attribute is looked up at call time, so ``patch("services.<game>.realtime.broadcast")`` - the idiom every existing game test uses - still intercepts the call."""

    def broadcast(self, session_id: int, event_type: str, payload: dict[str, Any]) -> None:
        """Send ``payload`` to every connected participant of ``session_id``."""
        ...


class ChatMessageManager[MessageT](Protocol):
    """The slice of a chat-message manager ``SessionChat`` depends on.
    Structural rather than a base class: the three managers are already built by ``DashboardManager.from_queryset(...)`` over unrelated querysets, and each needs only to create a message and list a session's messages."""

    def create(self, *, session: Any, profile: Any, body: str) -> MessageT:
        """Persist one chat message."""
        ...

    def for_session(self, session: Any) -> QuerySet[Any]:
        """Every chat message in ``session``, oldest first."""
        ...


class SessionChat[SessionT: Model, MessageT]:
    """Send and read back the live text chat for one game's sessions.

    Args:
        name: Which game this is, e.g. ``"trivia"``.
        manager: The chat-message model's manager (e.g. ``GameSessionChatMessage.objects``).
        realtime: The game's ``realtime`` module, used to push new messages to connected participants.
        serialize: Turns a saved message into the payload broadcast to clients.
        history_limit: Default number of past messages ``recent`` returns.
        max_message_length: Bodies are truncated to this before saving."""

    def __init__(
        self,
        *,
        name: str,
        manager: ChatMessageManager[MessageT],
        realtime: SessionRealtime,
        serialize: Callable[[MessageT], dict[str, Any]],
        history_limit: int = CHAT_HISTORY_LIMIT,
        max_message_length: int = MAX_SESSION_CHAT_MESSAGE_LENGTH,
    ) -> None:
        self.name = name
        self.manager = manager
        self.realtime = realtime
        self.serialize = serialize
        self.history_limit = history_limit
        self.max_message_length = max_message_length

    def send(self, session: SessionT, profile: Profile, body: str) -> MessageT:
        """Save a chat message and broadcast it to every connected participant.

        Returns:
            The saved message.

        Raises:
            MessageRateLimitedError: This participant's budget for this session is spent."""
        from urbanlens.dashboard.services.core.message_limits import charge_message, session_chat_identity

        # The rate limit this class's own docstring says belongs here rather
        # than three times over. Charged before the insert, and after the caller
        # has confirmed participation.
        charge_message(session_chat_identity(self.name, session.pk, profile.pk))
        message = self.manager.create(session=session, profile=profile, body=body.strip()[: self.max_message_length])
        self.realtime.broadcast(session.pk, "chat.message", {"message": self.serialize(message)})
        return message

    def recent(self, session: SessionT, *, limit: int | None = None) -> list[MessageT]:
        """The most recent messages in ``session``, returned oldest first.

        Returns:
            Up to ``limit`` messages, oldest first."""
        count = self.history_limit if limit is None else limit
        messages: list[MessageT] = list(self.manager.for_session(session).select_related("profile__user").order_by("-created")[:count])
        messages.reverse()
        return messages
