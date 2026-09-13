"""How much the media volume holds, measured off the request.

Measuring means stat-ing every file the site stores, and the site-admin system panel
polls every 60 seconds. The panel reads the last measurement and asks for a new one
when it is stale; one maintenance task walks the tree at a time.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta
import os

from django.conf import settings
from django.utils import timezone

from urbanlens.dashboard.services.core import single_flight
from urbanlens.dashboard.services.core.bounded_cache import get_or_none, set_or_skip

CACHE_KEY = "site_admin:media_usage"
GUARD_KEY = "site_admin:media_usage:measuring"

#: How old a measurement may be before the panel asks for another.
FRESH_FOR = timedelta(hours=1)

#: How long a measurement is kept to show while the next one is taken.
KEEP_SECONDS = 7 * 24 * 3600

#: Outlives the maintenance queue's hard limit, so a slow walk is not started twice.
GUARD_TTL_SECONDS = 75 * 60

_LABEL = "media usage"


@dataclass(frozen=True)
class MediaUsage:
    """One measurement of the media volume.

    Attributes:
        megabytes: Size of everything under ``MEDIA_ROOT``, in MiB.
        measured_at: When the walk finished.
    """

    megabytes: float
    measured_at: datetime

    @property
    def is_fresh(self) -> bool:
        """Whether it is recent enough not to ask for another."""
        return timezone.now() - self.measured_at <= FRESH_FOR


def directory_size_mb(path: str) -> float:
    """Return the disk usage of *path* in megabytes.

    Args:
        path: Directory to walk. A missing one measures as zero.

    Returns:
        MiB, to one decimal place.
    """
    total = 0
    with contextlib.suppress(OSError):
        for dirpath, _dirs, files in os.walk(path):
            for fname in files:
                with contextlib.suppress(OSError):
                    total += os.path.getsize(os.path.join(dirpath, fname))
    return round(total / 1_048_576, 1)


def measured_media_usage() -> MediaUsage | None:
    """The last measurement, asking for a new one when it is stale or missing.

    Never walks the tree itself.

    Returns:
        The last measurement, or None when there has not been one.
    """
    usage = _parse(get_or_none(CACHE_KEY, label=_LABEL))
    if usage is None or not usage.is_fresh:
        _request_measurement()
    return usage


def measure_media_usage() -> MediaUsage:
    """Walk ``MEDIA_ROOT`` and store the result for the panel.

    Releases the measuring guard however it ends.

    Returns:
        The new measurement.
    """
    try:
        usage = MediaUsage(megabytes=directory_size_mb(str(settings.MEDIA_ROOT)), measured_at=timezone.now())
        set_or_skip(CACHE_KEY, {"megabytes": usage.megabytes, "measured_at": usage.measured_at.isoformat()}, KEEP_SECONDS, label=_LABEL)
        return usage
    finally:
        single_flight.release(GUARD_KEY)


def _request_measurement() -> None:
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import measure_media_usage_task

    if single_flight.claim(GUARD_KEY, GUARD_TTL_SECONDS) and safely_enqueue_task(measure_media_usage_task) is None:
        single_flight.release(GUARD_KEY)


def _parse(stored: object) -> MediaUsage | None:
    if not isinstance(stored, dict):
        return None
    try:
        return MediaUsage(megabytes=float(stored["megabytes"]), measured_at=datetime.fromisoformat(stored["measured_at"]))
    except (KeyError, TypeError, ValueError):
        return None
