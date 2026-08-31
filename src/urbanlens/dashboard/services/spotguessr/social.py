"""Ratings visibility: your own ratings + friends' ratings with opt-out.

See ``docs/designs/drafts/spotguessr.md`` ("Social: ratings visibility").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.spotguessr.model import PlayerModeRating, SpotGuessrPreference

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def friend_profiles(profile: Profile) -> list[Profile]:
    """Every profile ``profile`` has an accepted friendship with."""
    rows = Friendship.objects.profile(profile).is_friend().select_related("from_profile", "to_profile")
    return [row.to_profile if row.from_profile_id == profile.pk else row.from_profile for row in rows]


def visible_friend_ratings(profile: Profile) -> list[dict]:
    """Each visible friend's most-recently-played mode rating, excluding anyone who has opted out.

    Each friend's own most-recently-played mode is used independently
    (rather than a single fixed mode for everyone) - a Named Place/Street
    View-only friend previously showed no rating at all, since the lookup
    was hardcoded to Photos mode.

    Batches the opt-out check and the latest-rating lookup into one query
    each (rather than two queries per friend) - a `SpotGuessrPreference`
    lookup and a `PlayerModeRating` lookup for every friend, unbatched, cost
    2N+1 queries on this page for N friends.

    Returns:
        A list of ``{"profile": Profile, "rating": PlayerModeRating | None}``
        dicts, one per visible friend - friends who haven't played yet still
        appear, with ``rating=None``, since the opt-out is about visibility,
        not about hiding the fact that a friend hasn't played.
    """
    friends = friend_profiles(profile)
    if not friends:
        return []
    friend_ids = [friend.pk for friend in friends]

    opted_out_ids = set(SpotGuessrPreference.objects.filter(profile_id__in=friend_ids, show_ratings_to_friends=False).values_list("profile_id", flat=True))
    # DISTINCT ON (profile_id) with a matching leading ORDER BY: the latest
    # PlayerModeRating per friend in one query rather than one query each.
    latest_ratings = {rating.profile_id: rating for rating in PlayerModeRating.objects.filter(profile_id__in=friend_ids).order_by("profile_id", "-last_played_at").distinct("profile_id")}

    return [{"profile": friend, "rating": latest_ratings.get(friend.pk)} for friend in friends if friend.pk not in opted_out_ids]
