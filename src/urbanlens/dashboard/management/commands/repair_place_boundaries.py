"""Re-resolve parcels whose geometry predates REData's boundary ranking.

Two populations carry wrong geometry, for different reasons, and both need the
same repair:

- Parcels created by ``0027_places_backfill`` from pre-places location
  boundaries. They carry ``geometry_generated_at=None``, and until recently
  ``geometry_stale`` read that null as "pending, not stale", so the provider
  chain never ran for them again.
- Parcels whose geometry *is* a provider answer, but from before the chain
  consulted ``/parcels/{uuid}/boundaries/``. For any New York parcel that answer
  was the convex hull of every building REData returned, unfiltered - which on
  the reported campus was the hull of a ~1,040-acre archaeological sensitivity
  zone's contents.

The subtle part is not re-fetching the boundary; it is repairing what the wrong
boundary already did to *other* rows. ``provision_places_for_coordinate`` calls
``resolve_locations_in(place.geometry)`` on every new outline, so pins across a
wide area were re-homed onto the oversized parcel.

``resolve_locations_in`` re-resolves each location it visits authoritatively -
whatever place now contains it wins - but its scope is
``Location.objects.filter(point__within=polygon)``. So the sweep has to run
against the **old, larger** geometry, captured before re-provisioning. Sweeping
with the corrected polygon would visit only the locations that are still inside
it and silently leave every wrongly re-homed location outside it attached to the
wrong place, which looks like a half-applied fix.

Cheap by design: REData's boundary sources are cache-first, and the building
list is already cached locally, so the only genuinely new call per parcel is
``/parcels/{uuid}/boundaries/`` - an endpoint that has never been called, so
there is no earlier response to reuse.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import DatabaseError

from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.services.places import resolution
from urbanlens.dashboard.services.places.provisioning import ensure_place_for_location


class Command(BaseCommand):
    """Re-run the boundary chain for parcels resolved before it could rank candidates."""

    help = "Re-resolve parcel boundaries that predate REData's scored /boundaries/ ranking, and re-home the pins the old geometry captured."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would be re-resolved without fetching or writing.")
        parser.add_argument("--limit", type=int, default=0, help="Stop after this many parcels (0 = no limit).")
        parser.add_argument(
            "--all",
            action="store_true",
            help="Include parcels the chain has already generated. Without this, only never-generated (backfilled) parcels are repaired.",
        )

    def _targets(self, *, include_generated: bool, limit: int):
        """The parcels to repair, largest first.

        Largest first because area is the symptom: an oversized boundary is
        both the most visible defect and the one whose sweep re-homes the most
        wrongly-attached locations, so a partial run (``--limit``) does the most
        good.

        Args:
            include_generated: Whether to include parcels that already have a
                generation timestamp.
            limit: Maximum number to return, or 0 for all.

        Returns:
            A list of places.
        """
        queryset = Place.objects.current().of_kind(PlaceKind.PARCEL).filter(geometry__isnull=False)
        if not include_generated:
            queryset = queryset.filter(geometry_generated_at__isnull=True)
        queryset = queryset.order_by("-area_sqm")
        return list(queryset[:limit] if limit else queryset)

    def handle(self, *args, **options):
        dry_run, limit = options["dry_run"], options["limit"]
        targets = self._targets(include_generated=options["all"], limit=limit)
        self.stdout.write(f"Found {len(targets)} parcel(s) to re-resolve.")

        repaired = skipped = failed = 0
        for place in targets:
            location = place.locations.exclude(latitude__isnull=True).exclude(longitude__isnull=True).first()
            if location is None:
                # Nothing to re-provision *from*: the chain answers a
                # coordinate, and this parcel has no located row attached.
                skipped += 1
                continue

            old_geometry = place.geometry
            label = place.name or f"place {place.pk}"
            if dry_run:
                self.stdout.write(f"  would re-resolve {label} ({place.area_sqm or 0:,.0f} m2) from location {location.pk}")
                repaired += 1
                continue

            try:
                ensure_place_for_location(location, force=True)
                # Deliberately the *old* geometry: the locations that need
                # re-homing are the ones the oversized outline captured, and
                # most of them are outside the corrected one.
                moved = resolution.resolve_locations_in(old_geometry)
            except (DatabaseError, OSError) as exc:
                self.stderr.write(f"  failed {label}: {exc}")
                failed += 1
                continue

            place.refresh_from_db()
            self.stdout.write(f"  re-resolved {label}: {place.area_sqm or 0:,.0f} m2, {moved} location(s) re-homed")
            repaired += 1

        verb = "would repair" if dry_run else "repaired"
        self.stdout.write(f"Done: {verb} {repaired}, skipped {skipped} (no located row), failed {failed}.")
