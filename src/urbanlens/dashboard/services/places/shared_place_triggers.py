"""Debounced enqueueing of the shared-place evaluation. Cheap and non-blocking: the checks themselves only ever run in the worker."""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache


def schedule_pin_evaluation(pin_id: int) -> None:
    """Queue one evaluation for the pin unless one was queued within ``UL_SHARED_PLACE_DEBOUNCE_SECONDS``."""
    from urbanlens.dashboard.tasks import evaluate_shared_places_for_pin

    debounce = settings.UL_SHARED_PLACE_DEBOUNCE_SECONDS
    try:
        first = cache.add(f"ul:shared-place:eval:{pin_id}", 1, timeout=debounce)
    except Exception:  # a cache outage must not stop the evaluation, only its debounce
        first = True
    if first:
        evaluate_shared_places_for_pin.apply_async(args=(pin_id,), countdown=debounce)


def schedule_membership_revalidation(profile_id: int) -> None:
    """Queue a check that the profile still holds pins near the places it belongs to."""
    from urbanlens.dashboard.tasks import revalidate_shared_place_memberships

    revalidate_shared_place_memberships.apply_async(args=(profile_id,), countdown=settings.UL_SHARED_PLACE_DEBOUNCE_SECONDS)
