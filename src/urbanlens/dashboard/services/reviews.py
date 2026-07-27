"""Upsert/clear logic for a profile's own star rating on a pin.

A :class:`~urbanlens.dashboard.models.reviews.model.Review` is always the
caller's own opinion of their own pin - there is exactly one per
``(profile, pin)`` pair, enforced by a ``unique_together`` constraint. Both the
internal star-rating widget and the external API act on that pair rather than
on a Review id, so the "create it the first time, update it afterwards" rule
lives here instead of being written twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.reviews.model import Review

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile


def upsert_review(profile: Profile, pin: Pin, rating: int) -> tuple[Review, bool]:
    """Set *profile*'s rating for *pin*, creating the Review if needed.

    Args:
        profile: The rating profile. Callers are responsible for having
            established that *pin* belongs to them.
        pin: The pin being rated.
        rating: The star rating (0-5, per the model's validators).

    Returns:
        A ``(review, created)`` tuple, where ``created`` is True only when
        this was the profile's first rating for the pin.
    """
    review, created = Review.objects.update_or_create(profile=profile, pin=pin, defaults={"rating": rating})
    return review, created


def clear_review(profile: Profile, pin: Pin) -> bool:
    """Remove *profile*'s rating for *pin*, if one exists.

    Args:
        profile: The profile whose rating to remove.
        pin: The pin being unrated.

    Returns:
        True when a rating was actually deleted, False when there was none -
        letting a caller distinguish "cleared" from "nothing to clear" without
        a second query.
    """
    deleted_count, _per_model = Review.objects.for_pair(profile, pin).delete()
    return bool(deleted_count)
