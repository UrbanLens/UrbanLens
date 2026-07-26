"""QuerySet and manager for PinMergeSuggestion."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import Q

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.pin_merge_suggestions.model import PinMergeSuggestion, PinMergeSuggestionOrigin
    from urbanlens.dashboard.models.profile.model import Profile


def _default_survivor(pin_a: Pin, pin_b: Pin) -> Pin:
    """Recommend which pin should survive a merge, absent an origin-specific choice.

    More visit history wins (a pin with more logged visits has more of the
    user's data riding on its identity); tie-broken by more photos; tie-broken
    by older creation date. Always overridable by the user at accept time.

    Args:
        pin_a: One candidate.
        pin_b: The other candidate.

    Returns:
        Whichever pin scores higher.
    """

    def score(pin: Pin) -> tuple[int, int, float]:
        return (pin.visit_history.count(), pin.images.count(), -pin.created.timestamp())

    return max((pin_a, pin_b), key=score)


class PinMergeSuggestionQuerySet(abstract.DashboardQuerySet):
    """QuerySet for PinMergeSuggestion records."""

    def for_profile(self, profile: Profile) -> Self:
        """Filter to suggestions belonging to a given profile.

        Args:
            profile: Owner profile.

        Returns:
            Filtered queryset.
        """
        return self.filter(profile=profile)

    def for_pin(self, pin: Pin) -> Self:
        """Filter to suggestions that mention a given pin, on either side.

        Args:
            pin: The pin to look for.

        Returns:
            Filtered queryset.
        """
        return self.filter(Q(pin_a=pin) | Q(pin_b=pin))

    def pending(self) -> Self:
        """Filter to suggestions still awaiting a response.

        Returns:
            Filtered queryset.
        """
        from urbanlens.dashboard.models.pin_merge_suggestions.model import PinMergeSuggestionStatus

        return self.filter(status=PinMergeSuggestionStatus.PENDING)


class PinMergeSuggestionManager(abstract.DashboardManager.from_queryset(PinMergeSuggestionQuerySet)):
    """Manager for PinMergeSuggestion."""

    def upsert(
        self,
        *,
        profile: Profile,
        pin_a: Pin,
        pin_b: Pin,
        origin: PinMergeSuggestionOrigin,
        reason: str = "",
        suggested_survivor: Pin | None = None,
    ) -> PinMergeSuggestion:
        """Find-or-create a pending suggestion for this pair of pins.

        The pair is looked up order-independently - a pending suggestion for
        ``(pin_b, pin_a)`` counts as already covering ``(pin_a, pin_b)`` - so a
        trigger that re-fires on the same collision (e.g. a repeated re-import
        run) never creates duplicate rows. Only a PENDING row dedupes; once a
        suggestion has been accepted or rejected, a fresh occurrence of the
        same collision creates a new one.

        Args:
            profile: Owner of both pins.
            pin_a: One of the two pins under consideration.
            pin_b: The other pin under consideration.
            origin: What raised this suggestion.
            reason: Short, human-readable explanation for the review-queue card.
            suggested_survivor: Explicit recommendation (e.g. the
                already-correctly-placed pin, for a legacy CID collision).
                Defaults to the general heuristic in :func:`_default_survivor`
                when omitted.

        Returns:
            The existing pending suggestion for this pair, or a newly-created one.
        """
        from urbanlens.dashboard.models.pin_merge_suggestions.model import PinMergeSuggestionStatus

        existing = (
            self.filter(profile=profile, status=PinMergeSuggestionStatus.PENDING)
            .filter((Q(pin_a=pin_a) & Q(pin_b=pin_b)) | (Q(pin_a=pin_b) & Q(pin_b=pin_a)))
            .first()
        )
        if existing is not None:
            return existing
        return self.create(
            profile=profile,
            pin_a=pin_a,
            pin_b=pin_b,
            origin=origin,
            reason=reason,
            suggested_survivor=suggested_survivor or _default_survivor(pin_a, pin_b),
        )
