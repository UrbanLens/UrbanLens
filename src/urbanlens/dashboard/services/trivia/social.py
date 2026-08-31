"""Ratings visibility: your own rating + friends' ratings with opt-out.

Mirrors ``services.spotguessr.social``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.trivia.model import PlayerTriviaRating, TriviaPreference
from urbanlens.dashboard.services.social.connections import get_connections

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def visible_friend_ratings(profile: Profile) -> list[dict]:
    """Friends' Trivia ratings, excluding anyone who has opted out.

    Batches the opt-out check and the rating lookup into one query each
    (rather than two queries per friend) - an unbatched `TriviaPreference`
    lookup and `PlayerTriviaRating` lookup for every friend cost 2N+1 queries
    on this page for N friends.

    Returns:
        A list of ``{"profile": Profile, "rating": PlayerTriviaRating | None}``
        dicts, one per visible friend - friends who haven't played yet still
        appear, with ``rating=None``, since the opt-out is about visibility,
        not about hiding the fact that a friend hasn't played.
    """
    friends = list(get_connections(profile))
    if not friends:
        return []
    friend_ids = [friend.pk for friend in friends]

    opted_out_ids = set(TriviaPreference.objects.filter(profile_id__in=friend_ids, show_ratings_to_friends=False).values_list("profile_id", flat=True))
    ratings_by_profile = {rating.profile_id: rating for rating in PlayerTriviaRating.objects.filter(profile_id__in=friend_ids)}

    return [{"profile": friend, "rating": ratings_by_profile.get(friend.pk)} for friend in friends if friend.pk not in opted_out_ids]
