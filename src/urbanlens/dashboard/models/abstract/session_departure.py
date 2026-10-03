"""How a player stopped playing a multiplayer game, shared by every game that lets one leave."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from urbanlens.dashboard.models.abstract.choices import TextChoices

if TYPE_CHECKING:
    from collections.abc import Iterable


class SessionDeparture(TextChoices):
    """How a participant who was playing left a game already under way.

    Blank for anyone still playing, and for anyone who left before play began - an invitee who declined, or a
    player who left the lobby - since they never played.
    """

    LEFT = "left", "Left"
    REMOVED = "removed", "Removed"


class DepartableParticipant(Protocol):
    """A game's participant row, as far as its departure goes."""

    departure: str


def finishers_first[ParticipantT: DepartableParticipant](participants: Iterable[ParticipantT]) -> list[ParticipantT]:
    """``participants`` with everyone who departed after everyone who finished, each group in its given order.

    A departed player ranked by points alone could head the scoreboard of a game they did not finish.

    Args:
        participants: Participant rows, already in scoreboard order.

    Returns:
        The same rows, finishers first.
    """
    return sorted(participants, key=lambda participant: bool(participant.departure))
