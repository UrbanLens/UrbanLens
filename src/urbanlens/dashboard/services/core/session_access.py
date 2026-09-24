"""Who may act inside a participant-session game, shared by every such game.

"Participant" here means an *active* one, as each game's participant queryset defines ``active()``: a row that
still exists after its owner left or was removed is history, not access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from django.db.models import Exists, Model, OuterRef

if TYPE_CHECKING:
    from django.db.models import QuerySet


class NotAnActiveParticipantError(Exception):
    """The profile has no active participant row in the session."""


class ActiveParticipantManager(Protocol):
    """The slice of a participant manager ``SessionAccess`` depends on."""

    def active(self) -> QuerySet[Any]:
        """Participant rows that still grant access to their session."""
        ...


class SessionAccess[SessionT: Model]:
    """Resolves and enforces active participation in one game's sessions.

    Args:
        name: Which game this is, e.g. ``"trivia"``.
        session_model: The game's session model.
        participants: The participant model's manager; its rows carry ``session_id`` and ``profile_id``.
    """

    def __init__(self, *, name: str, session_model: type[SessionT], participants: ActiveParticipantManager) -> None:
        self.name = name
        self.session_model = session_model
        self.participants = participants

    def active_participants(self, session_id: int) -> QuerySet[Any]:
        """Every active participant row of ``session_id``."""
        return self.participants.active().filter(session_id=session_id)

    def is_active_participant(self, session_id: int, profile_id: int) -> bool:
        """Whether ``profile_id`` actively participates in ``session_id``."""
        return self.active_participants(session_id).filter(profile_id=profile_id).exists()

    def session_for(self, session_id: int, profile_id: int) -> SessionT | None:
        """The session, only if ``profile_id`` actively participates in it.

        A missing session and someone else's session both return None, so callers cannot tell them apart.
        """
        membership = self.participants.active().filter(session_id=OuterRef("pk"), profile_id=profile_id)
        return self.session_model._default_manager.filter(Exists(membership), pk=session_id).first()  # noqa: SLF001

    def require(self, session_id: int, profile_id: int) -> None:
        """Raise unless ``profile_id`` actively participates in ``session_id``.

        Raises:
            NotAnActiveParticipantError: It does not.
        """
        if not self.is_active_participant(session_id, profile_id):
            raise NotAnActiveParticipantError(f"Profile {profile_id} is not an active participant of {self.name} session {session_id}.")
