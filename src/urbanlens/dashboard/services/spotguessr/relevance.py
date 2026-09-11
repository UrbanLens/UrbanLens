"""Recording SpotGuessr's photo-quality signal (``GamePhotoFeedback``).
Photos-mode rounds are the only source of this signal (Named Place/Street View rounds have no ``round.image``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.spotguessr.model import GamePhotoFeedback, GamePhotoFeedbackKind
from urbanlens.dashboard.services.spotguessr import modes

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.spotguessr.model import GameRound

#: Kinds a player may explicitly report, as opposed to NO_REACTION, which is
#: only ever backfilled server-side (see backfill_no_reaction()).
EXPLICIT_KINDS = (GamePhotoFeedbackKind.THUMBS_UP, GamePhotoFeedbackKind.THUMBS_DOWN, GamePhotoFeedbackKind.REPORTED)


def record_feedback(round_: GameRound, profile: Profile, kind: str) -> GamePhotoFeedback | None:
    """Record (or change) ``profile``'s explicit reaction to ``round_``'s photo.

    Args:
        round_: The round whose photo is being reacted to.
        profile: The reacting participant.
        kind: One of ``EXPLICIT_KINDS``."""
    if not modes.shows_imagery(round_.session.mode):
        return None
    feedback, _ = GamePhotoFeedback.objects.update_or_create(round=round_, profile=profile, defaults={"kind": kind})
    return feedback


def backfill_no_reaction(round_: GameRound, profiles: Iterable[Profile]) -> None:
    """Record a weak default-positive signal for every guesser who never explicitly reacted."""
    if not modes.shows_imagery(round_.session.mode):
        return
    for profile in profiles:
        GamePhotoFeedback.objects.get_or_create(round=round_, profile=profile, defaults={"kind": GamePhotoFeedbackKind.NO_REACTION})
