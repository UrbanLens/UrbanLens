"""Re-resolve Locations the campus sweep pointed at a building place with no outline (P181).

Until P181 the sweep attached a coordinate's shared Location to the building place it was mirroring, overriding
containment. Containment can never reach a place with no geometry, and every path still writing ``Location.place``
attaches only places that have one, so a Location on such a building got there that way. It then reads as that
building for every account pinned there. This puts each back on whatever containment answers.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import PlaceKind
from urbanlens.dashboard.services.places.resolution import resolve_location_place


class Command(BaseCommand):
    """Put Locations attached by fiat to an outline-less building place back on the place containment gives."""

    help = "Re-resolve Locations attached to a building place that has no outline, by containment."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        stale = Location.objects.filter(place__kind=PlaceKind.BUILDING, place__geometry__isnull=True).order_by("pk")
        count = 0
        for location in stale.iterator():
            before = location.place_id
            after = resolve_location_place(location, save=not dry_run)
            self.stdout.write(f"  location {location.pk}: place {before} -> {after.pk if after is not None else None}")
            count += 1
        self.stdout.write(f"{'Would re-resolve' if dry_run else 'Re-resolved'} {count} location(s).")
