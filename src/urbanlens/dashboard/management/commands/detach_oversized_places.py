"""Retire places too large to be what they claim, and release every domain they joined (P148, UrbanLens#285).

Offline and idempotent: it asks no provider. Detached locations are marked unresolved, so the fixed provider chain
places them again the next time they are viewed.

Dry-run by default; ``--apply`` writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand

from urbanlens.dashboard.services.places.oversized import detach_oversized_place, locations_standing_on, places_to_detach

if TYPE_CHECKING:
    from argparse import ArgumentParser


class Command(BaseCommand):
    """Detach implausibly large places from every access domain."""

    help = "Retire places larger than their kind can be (a county-sized 'parcel'), re-home their locations and un-nest the wikis they merged. Dry-run unless --apply."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Register the command's flags.

        Args:
            parser: The argument parser.
        """
        parser.add_argument("--apply", action="store_true", help="Detach the places; without it, only report what would be detached.")

    def handle(self, *args: Any, **options: Any) -> None:
        """List every place to detach, and detach each one with ``--apply``.

        Args:
            *args: Unused.
            **options: ``apply``.
        """
        apply: bool = options["apply"]
        targets = places_to_detach()
        self.stdout.write(f"Found {len(targets)} place(s) to detach.")
        for place in targets:
            label = f"{place.get_kind_display().lower()} {place.pk} {place.name!r} ({(place.area_sqm or 0) / 1_000_000:,.1f} km², {place.status})"
            if not apply:
                held = locations_standing_on(place).count()
                self.stdout.write(f"  would detach {label}: {place.children.count()} child place(s), {held} location(s) on it")
                continue
            outcome = detach_oversized_place(place)
            self.stdout.write(
                f"  detached {label}: {outcome.children} child place(s), {outcome.locations} location(s) ({outcome.unplaced} left unplaced), {outcome.wikis_unnested} wiki(s) un-nested",
            )
        if targets and not apply:
            self.stdout.write("Nothing was written. Run with --apply to detach them.")
