"""Retire places too large to be what they claim, and release every domain they joined (P148).

Offline and idempotent: it asks no provider. Detached locations are marked unresolved, so the fixed provider chain
places them again the next time they are viewed.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from urbanlens.dashboard.services.places.oversized import detach_oversized_place, locations_standing_on, places_to_detach


class Command(BaseCommand):
    """Detach implausibly large places from every access domain."""

    help = "Retire places larger than their kind can be (a county-sized 'parcel'), re-home their locations and un-nest the wikis they merged."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would be detached without writing.")

    def handle(self, *args, **options):
        targets = places_to_detach()
        self.stdout.write(f"Found {len(targets)} place(s) to detach.")
        for place in targets:
            label = f"{place.get_kind_display().lower()} {place.pk} {place.name!r} ({(place.area_sqm or 0) / 1_000_000:,.1f} km², {place.status})"
            if options["dry_run"]:
                held = locations_standing_on(place).count()
                self.stdout.write(f"  would detach {label}: {place.children.count()} child place(s), {held} location(s) on it")
                continue
            outcome = detach_oversized_place(place)
            self.stdout.write(
                f"  detached {label}: {outcome.children} child place(s), {outcome.locations} location(s) ({outcome.unplaced} left unplaced), {outcome.wikis_unnested} wiki(s) un-nested",
            )
