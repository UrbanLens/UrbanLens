"""Load a public-location export into a demo instance.

Run on the **demo** instance, against the JSON ``export_public_locations``
produced on the real site. Refuses to run anywhere else: the export is a set of
real coordinates, and importing it into a database that also holds real user
pins would silently merge the two - ``get_exact_or_create`` matches on stored
coordinates, so an imported row would attach itself to whatever real Location
already sits at that point.

Idempotent: re-running updates names and tops up aliases rather than duplicating
anything, so a demo instance can be refreshed from a newer export on a schedule.
Companion to ``import_redata_public_locations`` - both write into the same
manifest via ``services.demo.locations.merge_into_manifest``, so running either
(in any order, any number of times) never erases what the other contributed.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Import public locations and their cached wiki data."""

    help = "Import public locations exported from the real site. Demo instances only."

    def add_arguments(self, parser) -> None:
        """Register CLI arguments."""
        parser.add_argument("path", help="Path to the JSON export.")
        parser.add_argument(
            "--allow-non-demo",
            action="store_true",
            help="Import even though UL_DEMO_MODE is off. Only for a scratch database you are certain holds no real data.",
        )

    def handle(self, *args, **options) -> None:
        """Load the export.

        Raises:
            CommandError: Not a demo instance, or the file is missing/invalid.
        """
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        if not app_settings.demo_mode and not options["allow_non_demo"]:
            raise CommandError(
                "UL_DEMO_MODE is off. This import writes real coordinates, which would merge with any real "
                "Location at the same point. Pass --allow-non-demo only for a database you know holds no real data.",
            )

        source = Path(options["path"])
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CommandError(f"Could not read {source}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise CommandError(f"{source} is not valid JSON: {exc}") from exc

        entries = raw.get("locations") or []
        if not entries:
            self.stdout.write("The export contains no public locations - nothing to import.")
            return

        from urbanlens.dashboard.services.demo.locations import import_location_entries, merge_into_manifest

        created, updated = import_location_entries(entries)
        self.stdout.write(f"Imported {len(entries)} public location(s): {created} created, {updated} updated.")

        # The manifest is what seeding reads to decide which places every new
        # demo account gets pinned. Written after the import rather than before,
        # so it can only ever name locations that exist here - a manifest entry
        # with no Location behind it would seed a pin whose detail page has
        # nothing to show, which is the failure this whole path avoids.
        written = merge_into_manifest(entries)
        if written is None:
            self.stdout.write(
                "UL_DEMO_LOCATIONS_FILE is not set, so no manifest was written and demo accounts will be seeded "
                "with no pins. Set it to persist this import for seeding.",
            )
        else:
            self.stdout.write(f"Wrote the seeding manifest to {written}")
