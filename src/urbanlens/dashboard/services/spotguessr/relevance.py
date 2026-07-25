"""Recording SpotGuessr's photo-quality signal (``GamePhotoFeedback``).

Photos-mode rounds are the only source of this signal (Named Place/Street
View rounds have no ``round.image``). See ``services.media_relevance`` for
how these events are weighted into a photo's overall effective relevance,
and ``docs/designs/spotguessr.md`` ("Photo relevance feedback") for the
full design.
"""

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

    Always overwrites whatever was previously recorded for this
    ``(round, profile)`` pair - including a prior ``NO_REACTION`` backfill,
    or an earlier change of mind (thumbs up -> report, say). A no-op,
    returning None, for a round with no visual content to react to (Named
    Place) - callers should treat that as "nothing to record", not an
    error. Street View feedback is recorded like Photos feedback, but has
    no effect on ``services.media_relevance`` (that scoring path is keyed
    off ``Image``, and Street View rounds have none).

    Args:
        round_: The round whose photo is being reacted to.
        profile: The reacting participant.
        kind: One of ``EXPLICIT_KINDS``.
    """
    if not modes.shows_imagery(round_.session.mode):
        return None
    feedback, _ = GamePhotoFeedback.objects.update_or_create(round=round_, profile=profile, defaults={"kind": kind})
    return feedback


def backfill_no_reaction(round_: GameRound, profiles: Iterable[Profile]) -> None:
    """Record a weak default-positive signal for every guesser who never explicitly reacted.

    Called once a round is fully revealed (see ``services.spotguessr.session.submit_guess``).
    Uses ``get_or_create`` (not ``update_or_create``) specifically so this
    can never clobber an explicit reaction a fast player already submitted
    before the round finished for everyone else - only fills the gap for
    profiles with no feedback row at all yet.
    """
    if not modes.shows_imagery(round_.session.mode):
        return
    for profile in profiles:
        GamePhotoFeedback.objects.get_or_create(round=round_, profile=profile, defaults={"kind": GamePhotoFeedbackKind.NO_REACTION})
