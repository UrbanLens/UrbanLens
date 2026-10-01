"""Fill this deployment's basemap tile cache with placeholder tiles, for a load run.

A capacity run must measure what this deployment costs to serve a tile it already holds. Left to
warm itself, every tile in the run's grid would instead be an upstream fetch - measuring the
vendor, at a rate no one should point at someone else's service - and the tiles that lost the race
for a slot would measure 503s, which are fast for the wrong reason.

Not for a deployment with users: every coordinate written here answers with a grey square until it
ages out or something overwrites it.
"""

from __future__ import annotations

import base64

from django.conf import settings
from django.core.cache import cache, caches
from django.core.management.base import BaseCommand, CommandError

from urbanlens.dashboard.services.integration_testing.guards import is_production, production_unlocked
from urbanlens.dashboard.services.map.basemap_catalogue import CATALOGUE_CACHE_KEY, CATALOGUE_CACHE_TTL, tile_url_template
from urbanlens.dashboard.services.map.tile_cache_keys import basemap_tile_cache_key

#: A 1x1 grey PNG. Tile bytes decide bandwidth, not the per-request cost this exists to measure, and
#: a run is judged on latency at the proxy rather than on how long a picture takes to paint.
_PLACEHOLDER_TILE = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

#: Long enough for a run that climbs through several levels, short enough that a seeded deployment
#: returns to the truth on its own.
_SEED_TTL = 6 * 3600


class Command(BaseCommand):
    """Seed a square of basemap tiles so a load run measures the served path."""

    help = "Fill the basemap tile cache with placeholder tiles over one square of coordinates, for a load run."

    def add_arguments(self, parser) -> None:
        """Register CLI arguments."""
        parser.add_argument("--layer", default="street", help="Layer id the run draws (default: street).")
        parser.add_argument("--zoom", type=int, default=13, help="Zoom level the run draws at (default: 13).")
        parser.add_argument("--origin-x", type=int, required=True, help="Lowest tile column in the grid.")
        parser.add_argument("--origin-y", type=int, required=True, help="Lowest tile row in the grid.")
        parser.add_argument("--size", type=int, default=32, help="Grid side, in tiles (default: 32).")
        parser.add_argument("--force", action="store_true", help="Required, with the override variable, to touch production.")
        parser.add_argument(
            "--catalogue",
            action="store_true",
            help="Also publish a catalogue naming this layer, so the map draws through the proxy rather than a vendor.",
        )

    def handle(self, *args: object, **options: object) -> None:
        """Write the grid into the cache.

        Raises:
            CommandError: If this is production, which has real viewers who would be shown grey squares.
        """
        if is_production() and not production_unlocked(force=bool(options["force"])):
            raise CommandError("Refusing to seed a production tile cache: every seeded coordinate becomes a grey square.")

        layer = str(options["layer"])
        zoom = int(options["zoom"])  # type: ignore[call-overload]
        origin_x = int(options["origin_x"])  # type: ignore[call-overload]
        origin_y = int(options["origin_y"])  # type: ignore[call-overload]
        size = int(options["size"])  # type: ignore[call-overload]
        if size < 1:
            raise CommandError("--size must be at least 1.")

        # Through the proxy's own store rather than the default cache: proxied bytes live apart from
        # sessions, and a seeder writing to the wrong one leaves a run measuring upstream fetches.
        store = caches[settings.PROXIED_BYTES_CACHE]
        written = 0
        for x in range(origin_x, origin_x + size):
            for y in range(origin_y, origin_y + size):
                store.set(basemap_tile_cache_key(layer, zoom, x, y), (_PLACEHOLDER_TILE, "image/png"), _SEED_TTL)
                written += 1

        if options["catalogue"]:
            cache.set(
                CATALOGUE_CACHE_KEY,
                [
                    {
                        "id": layer,
                        "name": layer,
                        "source_type": "raster",
                        "attribution": "Seeded for a capacity run",
                        "min_zoom": 2,
                        "max_zoom": 19,
                        "url_template": tile_url_template(layer),
                    }
                ],
                CATALOGUE_CACHE_TTL,
            )

        self.stdout.write(f"seeded {written} tiles for {layer} at zoom {zoom} from ({origin_x}, {origin_y}) over {size}x{size}")
