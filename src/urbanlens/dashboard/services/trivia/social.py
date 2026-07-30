"""Ratings visibility: your own rating + friends' ratings with opt-out.

Mirrors ``services.spotguessr.social``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.trivia.model import PlayerTriviaRating, TriviaPreference
from urbanlens.dashboard.services.connections import get_connections

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def visible_friend_ratings(profile: Profile) -> list[dict]:
    """Friends' Trivia ratings, excluding anyone who has opted out.

    Returns:
        A list of ``{"profile": Profile, "rating": PlayerTriviaRating | None}``
        dicts, one per visible friend - friends who haven't played yet still
        appear, with ``rating=None``, since the opt-out is about visibility,
        not about hiding the fact that a friend hasn't played.
    """
    visible = []
    for friend in get_connections(profile):
        try:
            preference = friend.trivia_preference
        except TriviaPreference.DoesNotExist:
            preference = None
        if preference is not None and not preference.show_ratings_to_friends:
            continue
        rating = PlayerTriviaRating.objects.filter(profile=friend).first()
        visible.append({"profile": friend, "rating": rating})
    return visible
