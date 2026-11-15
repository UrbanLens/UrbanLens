"""Debounced enqueueing of the shared-place evaluation. Cheap and non-blocking: the checks themselves only ever run in the worker."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone


def schedule_pin_evaluation(pin_id: int, *, created: datetime | None = None) -> bool:
    """
    Queue one evaluation for the pin unless one was queued within ``UL_SHARED_PLACE_DEBOUNCE_SECONDS``.

    Args:
        pin_id: The pin to evaluate.
        created: When the pin was created; a pin younger than ``UL_SHARED_PLACE_MIN_PIN_AGE_DAYS`` cannot pass the maturity gate yet, so
            no task is spent on it (the nightly sweep evaluates it once it has matured).

    Returns:
        Whether a task was enqueued.
    """
    from urbanlens.dashboard.tasks import evaluate_shared_places_for_pin

    if created is not None and created > timezone.now() - timedelta(days=settings.UL_SHARED_PLACE_MIN_PIN_AGE_DAYS):
        return False

    debounce = settings.UL_SHARED_PLACE_DEBOUNCE_SECONDS
    try:
        first = cache.add(f"ul:shared-place:eval:{pin_id}", 1, timeout=debounce)
    except Exception:  # a cache outage must not stop the evaluation, only its debounce
        first = True
    if first:
        evaluate_shared_places_for_pin.apply_async(args=(pin_id,), countdown=debounce)
    return bool(first)


def schedule_membership_revalidation(profile_id: int) -> None:
    """Queue a check that the profile still holds pins near the places it belongs to."""
    from urbanlens.dashboard.tasks import revalidate_shared_place_memberships

    revalidate_shared_place_memberships.apply_async(args=(profile_id,), countdown=settings.UL_SHARED_PLACE_DEBOUNCE_SECONDS)
