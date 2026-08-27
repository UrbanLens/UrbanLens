"""Pull REData's public-locations catalog into a demo instance.

Run on the **demo** instance. Companion to ``import_public_locations`` - both
write into the same manifest via ``services.demo.locations.merge_into_manifest``,
so running either, in any order, any number of times, never erases what the
other contributed.

REData's ``/public-locations/`` is a real, documented endpoint
(``GET /api/v1/public-locations/``, scope ``public_locations:read``) but as of
2026-08-20 is not yet deployed anywhere UrbanLens can reach - this command is
meant to be run (or scheduled) safely before that is true. It reports "0
locations" rather than erroring when REData is unreachable, unconfigured, or
simply doesn't have the endpoint yet, matching
``services.demo.locations.redata_demo_locations``'s own degrade-to-empty
contract.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Import REData's public-locations catalog."""

    help = "Import REData's public-locations catalog (state capitols, county seats, national capitals). Demo instances only."

    def add_arguments(self, parser) -> None:
        """Register CLI arguments."""
        parser.add_argument(
            "--allow-non-demo",
            action="store_true",
            help="Import even though UL_DEMO_MODE is off. Only for a scratch database you are certain holds no real data.",
        )

    def handle(self, *args, **options) -> None:
        """Fetch REData's catalog and merge it into the demo instance.

        Raises:
            CommandError: Not a demo instance and not explicitly overridden.
        """
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        if not app_settings.demo_mode and not options["allow_non_demo"]:
            raise CommandError(
                "UL_DEMO_MODE is off. This import writes real coordinates, which would merge with any real Location at the same point. Pass --allow-non-demo only for a database you know holds no real data.",
            )

        from urbanlens.dashboard.services.demo.locations import import_location_entries, merge_into_manifest, redata_demo_locations

        entries = redata_demo_locations()
        if not entries:
            self.stdout.write(
                "REData returned no public locations - it may not have this endpoint deployed yet, may be "
                "unconfigured (UL_REDATA_API_URL/UL_REDATA_API_KEY), or the configured key may lack the "
                "public_locations:read scope. Nothing was imported; this is not an error.",
            )
            return

        created, updated = import_location_entries(entries)
        self.stdout.write(f"Imported {len(entries)} public location(s) from REData: {created} created, {updated} updated.")

        written = merge_into_manifest(entries)
        if written is None:
            self.stdout.write(
                "UL_DEMO_LOCATIONS_FILE is not set, so no manifest was written and demo accounts will be seeded with no pins. Set it to persist this import for seeding.",
            )
        else:
            self.stdout.write(f"Wrote the seeding manifest to {written}")
