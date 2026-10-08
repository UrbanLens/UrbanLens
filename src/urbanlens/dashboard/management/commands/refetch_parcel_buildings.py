"""Clear the parcel building rows a pending REData answer may have left, so they are fetched again.

REData 0.3.7 to 0.3.9 answer a parcel's buildings with a 503 naming ``refresh_queued`` or ``compute_timeout`` in
``error`` while they compute it. UrbanLens 0.8.0 knows neither code and kept the 503 as REData's settled answer: the
Building Attributes source cached ``{}``, and the parcel-buildings list cached its OpenStreetMap or CRIS fallback, or
``{}``. An enrichment source never refreshes a row it has written, so those rows stay until a panel finds them stale.
0.8.0's parcel-buildings list falls back on any REData failure, so it keeps writing such rows against REData 0.3.10 too,
until this release replaces it.

This deletes, from the window given, each parcel-buildings row that is empty or a fallback's, and each Building
Attributes row that is empty or stands on one of those locations (0.8.0's Building Attributes reads the cached list
first, so a fallback list fed it too). Enrichment and the panels then fetch them again. A list REData answered, a row
from outside the window and every other source are left alone. So are child pins built from a fallback list: a later
sweep adds pins for buildings REData knows of and removes none.

Dry-run by default; ``--apply`` deletes. The window runs from REData 0.3.7's deploy to this release's: before that
deploy, 0.8.0 is still writing such rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

if TYPE_CHECKING:
    from argparse import ArgumentParser

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.cache.location_cache import LocationCache

#: The providers ``fetch_parcel_buildings`` falls back to when REData gives no list.
_FALLBACK_PROVIDERS = ("osm", "cris")


def _instant(value: str) -> datetime:
    """An ISO 8601 instant with its offset, such as ``2026-10-07T08:24:00+00:00``.

    Args:
        value: The command-line value.

    Returns:
        The aware datetime.

    Raises:
        CommandError: Not ISO 8601, or no offset, which would leave the window to the server's time zone.
    """
    try:
        instant = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"{value!r} is not an ISO 8601 time.") from exc
    if instant.tzinfo is None:
        raise CommandError(f"{value!r} names no UTC offset; add one, such as +00:00.")
    return instant


class Command(BaseCommand):
    """Delete the parcel building rows a pending REData answer may have left, within a window."""

    help = "Clear empty Building Attributes rows and empty or fallback parcel-buildings rows cached in a window, so they are fetched again. Dry-run unless --apply."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Register the command's flags.

        Args:
            parser: The argument parser.
        """
        parser.add_argument("--since", required=True, help="Start of the window, ISO 8601 with an offset (REData 0.3.7's deploy).")
        parser.add_argument("--until", required=True, help="End of the window, exclusive, ISO 8601 with an offset (the deploy of the UrbanLens release that replaced 0.8.0).")
        parser.add_argument("--apply", action="store_true", help="Delete the rows; without it, only count them.")

    def handle(self, *args: Any, **options: Any) -> None:
        """Count, or delete, each source's rows in the window.

        Args:
            *args: Unused.
            **options: ``since``, ``until`` and ``apply``.

        Raises:
            CommandError: The window is malformed or ends before it starts.
        """
        since = _instant(options["since"])
        until = _instant(options["until"])
        if until <= since:
            raise CommandError("--until must be after --since.")

        for source, rows in self._candidates(since, until).items():
            count = rows.count()
            if options["apply"]:
                rows.delete()
                self.stdout.write(f"{source}: {count} cleared")
            else:
                self.stdout.write(f"{source}: {count}")
        if not options["apply"]:
            self.stdout.write("Nothing was deleted. Run with --apply to clear these rows.")

    @staticmethod
    def _candidates(since: datetime, until: datetime) -> dict[str, QuerySet[LocationCache]]:
        """Each source's rows a pending answer could have left, cached within ``[since, until)``."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.plugins.builtin.redata_building_attributes import _CACHE_SOURCE as ATTRIBUTES_SOURCE
        from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE

        window = LocationCache.objects.filter(updated__gte=since, updated__lt=until)
        lists = window.filter(source=PARCEL_BUILDINGS_CACHE_SOURCE).filter(Q(data={}) | Q(data__provider__in=_FALLBACK_PROVIDERS))
        # Read before anything is deleted: the attributes a fallback list fed are found through that list's row.
        fed = list(lists.filter(~Q(data={})).values_list("location_id", flat=True))
        return {
            ATTRIBUTES_SOURCE: window.filter(source=ATTRIBUTES_SOURCE).filter(Q(data={}) | Q(location_id__in=fed)),
            PARCEL_BUILDINGS_CACHE_SOURCE: lists,
        }
