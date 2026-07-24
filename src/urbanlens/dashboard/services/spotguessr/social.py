"""Ratings visibility: your own ratings + friends' ratings with opt-out.

See ``docs/designs/spotguessr.md`` ("Social: ratings visibility").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.spotguessr.model import PlayerModeRating, SpotGuessrMode, SpotGuessrPreference

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def friend_profiles(profile: Profile) -> list[Profile]:
    """Every profile ``profile`` has an accepted friendship with."""
    rows = Friendship.objects.profile(profile).is_friend().select_related("from_profile", "to_profile")
    return [row.to_profile if row.from_profile_id == profile.pk else row.from_profile for row in rows]


def visible_friend_ratings(profile: Profile, mode: str = SpotGuessrMode.PHOTOS) -> list[dict]:
    """Friends' ratings for ``mode``, excluding anyone who has opted out.

    Returns:
        A list of ``{"profile": Profile, "rating": PlayerModeRating | None}``
        dicts, one per visible friend - friends who haven't played yet still
        appear, with ``rating=None``, since the opt-out is about visibility,
        not about hiding the fact that a friend hasn't played.
    """
    visible = []
    for friend in friend_profiles(profile):
        try:
            preference = friend.spotguessr_preference
        except SpotGuessrPreference.DoesNotExist:
            preference = None
        if preference is not None and not preference.show_ratings_to_friends:
            continue
        rating = PlayerModeRating.objects.filter(profile=friend, mode=mode).first()
        visible.append({"profile": friend, "rating": rating})
    return visible
