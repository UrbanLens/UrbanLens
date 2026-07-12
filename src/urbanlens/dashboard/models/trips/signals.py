"""Auto-sync push for trips linked to a user's Google Calendar.

When a trip is imported from a Google Calendar event with "keep in sync"
checked (:attr:`~urbanlens.dashboard.models.calendar_sync.model.TripCalendarLink.auto_sync`),
any later change to the trip or one of its activities should be reflected on
the linked calendar event. This is one-way only - edits made on Google
Calendar are never pulled back into UrbanLens.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.calendar_sync.model import TripCalendarLink
from urbanlens.dashboard.models.trips.model import Trip, TripActivity


def _queue_calendar_push(trip_id: int | None) -> None:
    """Enqueue a calendar push for a trip, if it has an auto-sync link.

    The existence check avoids scheduling a Celery task (and its DB lookups)
    for the overwhelming majority of trips that were never imported with
    "keep in sync" enabled.

    Args:
        trip_id: PK of the trip that changed, or None (unsaved FK).
    """
    if trip_id is None:
        return
    if not TripCalendarLink.objects.filter(trip_id=trip_id, activity__isnull=True, auto_sync=True).exists():
        return

    def _enqueue() -> None:
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import push_trip_to_calendar

        safely_enqueue_task(push_trip_to_calendar, trip_id)

    transaction.on_commit(_enqueue)


@receiver(post_save, sender=Trip, dispatch_uid="trip_calendar_auto_sync_on_trip_save")
def sync_trip_on_save(sender: type[Trip], instance: Trip, **kwargs: Any) -> None:
    """Push a saved trip to its auto-synced calendar event, if any.

    Args:
        sender: The Trip model class.
        instance: The trip that was saved.
        **kwargs: Remaining signal arguments (unused).
    """
    _queue_calendar_push(instance.pk)


@receiver(post_save, sender=TripActivity, dispatch_uid="trip_calendar_auto_sync_on_activity_save")
def sync_trip_on_activity_save(sender: type[TripActivity], instance: TripActivity, **kwargs: Any) -> None:
    """Push a saved activity's trip to its auto-synced calendar event, if any.

    Args:
        sender: The TripActivity model class.
        instance: The activity that was saved.
        **kwargs: Remaining signal arguments (unused).
    """
    _queue_calendar_push(instance.trip_id)
