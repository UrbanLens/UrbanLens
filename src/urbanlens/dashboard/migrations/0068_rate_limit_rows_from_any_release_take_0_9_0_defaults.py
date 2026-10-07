"""Give each rate-limit row still holding an earlier release's defaults the values 0.9.0 writes.

``rate_limiter.get_limit_config`` writes a service's registered defaults into its ``ApiRateLimit`` row only when it
creates the row, and later replaces only the generic 20/min, 500/day fallback. 0064 carried the services 0.9.0 changed
from 0.8.0's values to 0.9.0's, and only from 0.8.0's. A row created by any older release still holds what that
release wrote: Overpass rows from v0.3.0b0 and v0.4.0b3 allow 2 calls a minute and 500 a day, where 0.9.0 allows 240
and 24,000. Jess ruled on 2026-10-07 ("Yes") that these rows take the current defaults.

The rule generalises 0064's. A row is rewritten only when every field ``get_limit_config`` writes (``_FIELDS``) still
holds exactly one of the defaults its service has had at some point in git history; it then takes all of 0.9.0's
values, ``updated`` with them. An admin can edit every one of those fields, and a row off every earlier default in any
of them is theirs, so it is left alone and logged. ``enabled`` is never read or written. A row already at 0.9.0's
values is not rewritten.

``DEFAULTS`` was generated, not typed: each release's ``ServiceDefaults`` were read from every commit on the release
lineage (tags v0.3.0b0 to v0.8.0, the ``@features``/``@release``/``release`` branches) that touched
``services/core/rate_limiter.py`` (``services/rate_limiter.py`` before 0.7.0) or a plugin declaring
``get_service_defaults``, with each dataclass default filled in as that commit defined it. Both the core registry and
the plugins count, since a disabled plugin leaves its key to the registry. Reading every ref instead found the same
snapshots, and the 0.9.0 values were checked against ``all_service_defaults()`` for all 86 services. Columns added
later, ``calls_per_30_days`` (0.5.0) and ``min_interval_seconds`` (0.7.0), are NULL on a row made before them. Each
earlier snapshot is commented with the commit it first appears in and the release tags that shipped it. Of the 86
services 0.9.0 registers, the 17 below have had any other default; a service that has left the registry (``loopnet``,
``nps``, ``yelp``, ``openhistoricalmap`` and others) has no current default to take, so its row is not touched.

Most rows only loosen or take reworded notes. Three earlier defaults tighten instead, listed so a reviewer sees them:

- ``google_geocoding`` before 0.6.0 had no 30-day cap; it takes 9,999. It does not bind at any share above 0.0005:
  ``free_tier_ceiling`` holds the service to 10,000 x 0.4 x this deployment's share a calendar month, so a rolling
  30 days holds at most 8,000 x share: two months' ceilings, or, where it touches three months, one month's and a
  day's 500 x share at each end. That is below the 30-day cap's 9,999 x share.
- ``nominatim`` at 60 a minute with a 1.0 s spacing, from f6df0c851 (2026-09-29, on ``release/v_0_8_0`` only). It was
  reverted the same day by deb9d93dd, per D25, so the row takes 1 a minute, as every release wrote.
- ``virustotal`` at 4 a minute and 490 a day, from 19daf23be (2026-09-01, ``release/v_0_8_0`` only, replaced the same
  day by 3d0195b47); it takes 3 and 480, as 0.8.0 wrote.

0.8.0's own values are included, so this migration alone brings any row up; 0064 has already moved those it covered.
That includes the one other tightening, ``redata_places``' only earlier default, 0.8.0's uncapped one, which 0064
already holds to 40 a day.
``google_calendar``'s only earlier default is the 30 a minute every release from v0.4.0b3 to 0.8.0 wrote. 0067, which
runs first, already moves a row holding it to 120 a minute; it is listed here so the earlier defaults stay complete.

Reverse is a no-op. A row holding 0.9.0's values cannot be told from one an admin set to the same values, so putting
an older default back could undo a choice somebody made, and older releases run with 0.9.0's limits.
"""

import logging

from django.db import migrations
from django.utils import timezone

logger = logging.getLogger(__name__)

#: The fields ``get_limit_config`` writes from a service's defaults; ``enabled`` is not one of them.
_FIELDS = ("display_name", "calls_per_minute", "calls_per_day", "calls_per_30_days", "min_interval_seconds", "usa_only", "notes")
#: The fields named with their values when a row is left alone; notes are named, never quoted.
_LIMITS = ("calls_per_minute", "calls_per_day", "calls_per_30_days", "min_interval_seconds", "usa_only")

#: (service, what 0.9.0 writes, every other default it has had), frozen as git history has them.
DEFAULTS = (
    (
        "azure_maps",
        # 0.9.0
        {
            "display_name": "Azure Maps (Search/Geocoding)",
            "calls_per_minute": 50,
            "calls_per_day": 2500,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": "Free tier: 5,000 Search transactions a month (Gen2), per subscription, shared with REData.",
        },
        (
            # From e2c5e0554 (2026-07-14), shipped in v0.5.0b0, v0.6.0b0.
            {
                "display_name": "Azure Maps (Search/Geocoding/Render)",
                "calls_per_minute": 50,
                "calls_per_day": 2500,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": (
                    "Free tier: 5,000 transactions/month (Gen1 S0 / Gen2 pay-as-you-go) shared across Search, Geocoding, "
                    "and Render."
                ),
            },
            # From baa41b292 (2026-08-04), shipped in v0.7.0b0, v0.8.0.
            {
                "display_name": "Azure Maps (Search/Geocoding)",
                "calls_per_minute": 50,
                "calls_per_day": 2500,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": (
                    "Free tier: 5,000 transactions/month (Gen1 S0 / Gen2 pay-as-you-go) shared across Search and "
                    "Geocoding."
                ),
            },
        ),
    ),
    (
        "basemap_vendor_tiles",
        # 0.9.0
        {
            "display_name": "Basemap Vendor Tiles",
            "calls_per_minute": 600,
            "calls_per_day": None,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": "Raster basemap tiles fetched straight from the vendor - see services.map.basemap_vendors.",
        },
        (
            # From ebe547fd3 (2026-09-22), in no release tag; on release/v_0_8_0.
            {
                "display_name": "Basemap Vendor Tiles",
                "calls_per_minute": 600,
                "calls_per_day": None,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": "Raster basemap tiles fetched straight from Esri/OpenTopoMap - see services.map.basemap_vendors.",
            },
        ),
    ),
    (
        "google_calendar",
        # 0.9.0
        {
            "display_name": "Google Calendar API",
            "calls_per_minute": 120,
            "calls_per_day": 2000,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Free API; Google's quota is per user (600 a minute by default), so this is our own politeness limit."
                " A minute holds one whole trip export: an event per activity (max_trip_activities, 100 by default) "
                "plus the trip's own."
            ),
        },
        (
            # From 0574cc805 (2026-07-11), shipped in v0.4.0b3, v0.5.0b0, v0.6.0b0, v0.7.0b0, v0.8.0.
            {
                "display_name": "Google Calendar API",
                "calls_per_minute": 30,
                "calls_per_day": 2000,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": "Free API; Google quota is per-user (default 600 queries/min/user across the project).",
            },
        ),
    ),
    (
        "google_geocoding",
        # 0.9.0
        {
            "display_name": "Google Geocoding API",
            "calls_per_minute": 20,
            "calls_per_day": 500,
            "calls_per_30_days": 9999,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Free tier: 10,000 calls/month for Geocoding, shared with REData on the same billing account. The "
                "legacy Place Details cid: lookup is counted here too, though Google bills it as Places Details (Pro,"
                " 5,000 free a month)."
            ),
        },
        (
            # From 549c22537 (2026-07-04), shipped in v0.3.0b0, v0.4.0b3, v0.5.0b0.
            {
                "display_name": "Google Geocoding API",
                "calls_per_minute": 20,
                "calls_per_day": 500,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": "Free tier: $200/month credit (~40,000 calls/month).",
            },
            # From 8896b6a9b (2026-07-24), shipped in v0.6.0b0, v0.7.0b0, v0.8.0.
            {
                "display_name": "Google Geocoding API",
                "calls_per_minute": 20,
                "calls_per_day": 500,
                "calls_per_30_days": 9999,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": "Free tier: 10,000 calls/month (Places Details Essentials SKU).",
            },
            # From de04fadf2 (2026-10-05), in no release tag; on release/v_0_9_0.
            {
                "display_name": "Google Geocoding API",
                "calls_per_minute": 20,
                "calls_per_day": 500,
                "calls_per_30_days": 9999,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": (
                    "Free tier: 10,000 calls/month for Geocoding and for Place Details Essentials, shared with REData on "
                    "the same billing account."
                ),
            },
        ),
    ),
    (
        "google_maps",
        # 0.9.0
        {
            "display_name": "Google Maps (Static/StreetView)",
            "calls_per_minute": 20,
            "calls_per_day": 200,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Static Maps and Street View Static: 10,000 free a month each, then $2 and $7 per 1,000. The coverage"
                " probes before a Street View image are counted here too, though Google does not bill them."
            ),
        },
        (
            # From 549c22537 (2026-07-04), shipped in v0.3.0b0, v0.4.0b3, v0.5.0b0, v0.6.0b0, v0.7.0b0, v0.8.0.
            {
                "display_name": "Google Maps (Static/StreetView)",
                "calls_per_minute": 20,
                "calls_per_day": 200,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": "Static Maps: 25,000 free/month. Street View: billed per call.",
            },
        ),
    ),
    (
        "google_open_buildings",
        # 0.9.0
        {
            "display_name": "Google Open Buildings",
            "calls_per_minute": 20,
            "calls_per_day": 500,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Downloads level-6 gzip CSV shards of Google's public Open Buildings v3 dataset during boundary "
                "lookups, only for cells it covers (none in the US), skipping any past MAX_SHARD_BYTES and "
                "remembering missing ones. The dataset has no quota, so this bounds our own bandwidth; the values are"
                " the generic fallback's, not tuned. See services.apis.locations.boundaries.google_open_buildings."
            ),
        },
        (
            # From eeb50efe5 (2026-09-14), shipped in v0.8.0.
            {
                "display_name": "Google Open Buildings",
                "calls_per_minute": 20,
                "calls_per_day": 500,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": (
                    "Downloads whole gzip CSV shards of Google's public Open Buildings dataset during boundary lookups. "
                    "The dataset has no quota, so this bounds our own bandwidth; the values are the generic fallback's, "
                    "not tuned. See services.apis.locations.boundaries.google_open_buildings."
                ),
            },
            # From 52e1241e4 (2026-10-05), in no release tag; on release/v_0_9_0.
            {
                "display_name": "Google Open Buildings",
                "calls_per_minute": 20,
                "calls_per_day": 500,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": (
                    "Downloads gzip CSV shards of Google's public Open Buildings dataset during boundary lookups, "
                    "skipping any past MAX_SHARD_BYTES and remembering missing ones. The dataset has no quota, so this "
                    "bounds our own bandwidth; the values are the generic fallback's, not tuned. See "
                    "services.apis.locations.boundaries.google_open_buildings."
                ),
            },
        ),
    ),
    (
        "google_places",
        # 0.9.0
        {
            "display_name": "Google Places API",
            "calls_per_minute": 20,
            "calls_per_day": 200,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "No credit since 2025-03: each SKU has its own monthly free cap, per billing account, shared with "
                "REData. Held to the smallest this budget can spend."
            ),
        },
        (
            # From 549c22537 (2026-07-04), shipped in v0.3.0b0, v0.4.0b3, v0.5.0b0, v0.6.0b0, v0.7.0b0, v0.8.0.
            {
                "display_name": "Google Places API",
                "calls_per_minute": 20,
                "calls_per_day": 200,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": "Free tier: $200/month credit. Geocoding/details billed per call.",
            },
        ),
    ),
    (
        "nominatim",
        # 0.9.0
        {
            "display_name": "Nominatim (OpenStreetMap)",
            "calls_per_minute": 1,
            "calls_per_day": 500,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": "Free API. Hard limit: 1 req/second per OSM ToS.",
        },
        (
            # From f6df0c851 (2026-09-29), in no release tag; on release/v_0_8_0.
            {
                "display_name": "Nominatim (OpenStreetMap)",
                "calls_per_minute": 60,
                "calls_per_day": 500,
                "calls_per_30_days": None,
                "min_interval_seconds": 1.0,
                "usa_only": False,
                "notes": "Free API. Hard limit: 1 req/second per OSM ToS.",
            },
        ),
    ),
    (
        "overpass",
        # 0.9.0
        {
            "display_name": "Overpass API (OpenStreetMap)",
            "calls_per_minute": 240,
            "calls_per_day": 24000,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Free API. Load is distributed across several public Overpass instances, and an instance that errors "
                "or times out is dropped for minutes, longer on each repeat, or for the wait it states. Each logical "
                "lookup may spend more than one call when it fails over."
            ),
        },
        (
            # From 549c22537 (2026-07-04), shipped in v0.3.0b0, v0.4.0b3.
            {
                "display_name": "Overpass API (OpenStreetMap)",
                "calls_per_minute": 2,
                "calls_per_day": 500,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": "Free API. Be conservative; public Overpass instances are shared community infrastructure.",
            },
            # From 3f12e8751 (2026-07-22), shipped in v0.5.0b0, v0.6.0b0, v0.7.0b0, v0.8.0.
            {
                "display_name": "Overpass API (OpenStreetMap)",
                "calls_per_minute": 240,
                "calls_per_day": 24000,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": (
                    "Free API. Load is distributed across several public Overpass instances, and any instance that "
                    "errors/times out is dropped until the next day. Each logical lookup may spend more than one call "
                    "when it fails over."
                ),
            },
        ),
    ),
    (
        "overpass_public_mirror",
        # 0.9.0
        {
            "display_name": "Overpass API public mirrors (overpass-api.de, maps.mail.ru)",
            "calls_per_minute": 10,
            "calls_per_day": 2000,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Free API: the public Overpass instances the self-hosted primary fails over to. Their limits are per "
                "address, shared by every deployment and REData."
            ),
        },
        (
            # From 09663a56a (2026-10-06), in no release tag; on release/v_0_9_0.
            {
                "display_name": "Overpass API public mirrors (overpass-api.de, maps.mail.ru)",
                "calls_per_minute": 10,
                "calls_per_day": 2000,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": (
                    "The public Overpass instances the self-hosted primary fails over to. Their limits are per address, "
                    "shared by every deployment and REData."
                ),
            },
        ),
    ),
    (
        "overture_maps",
        # 0.9.0
        {
            "display_name": "Overture Maps",
            "calls_per_minute": 20,
            "calls_per_day": 500,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Overture's public GeoParquet release via services.apis.locations.boundaries.overture_maps, read only"
                " where REData's mirror holds nothing (outside its synced US shards); REData answers the rest. Free "
                "public dataset, but its STAC index rate-limits us under load - see P110."
            ),
        },
        (
            # From f7d8c046e (2026-09-17), shipped in v0.8.0.
            {
                "display_name": "Overture Maps",
                "calls_per_minute": 20,
                "calls_per_day": 500,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": (
                    "Building/place/address/land-use GeoParquet themes via "
                    "services.apis.locations.boundaries.overture_maps. Free public dataset, but its STAC index "
                    "rate-limits us under load - see P110."
                ),
            },
            # From 95e44ed4d (2026-10-05), in no release tag; on release/v_0_9_0.
            {
                "display_name": "Overture Maps",
                "calls_per_minute": 20,
                "calls_per_day": 500,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": (
                    "Overture's public GeoParquet release via services.apis.locations.boundaries.overture_maps, read only"
                    " outside the US; REData's mirror answers inside it. Free public dataset, but its STAC index "
                    "rate-limits us under load - see P110."
                ),
            },
        ),
    ),
    (
        "redata_api",
        # 0.9.0
        {
            "display_name": "REData (property records service)",
            "calls_per_minute": 120,
            "calls_per_day": 10000,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": True,
            "notes": "Our own standalone property-records service - not a third-party budget, just a sanity ceiling.",
        },
        (
            # From dfdadab83 (2026-07-20), shipped in v0.5.0b0.
            {
                "display_name": "REData (property records service)",
                "calls_per_minute": 120,
                "calls_per_day": 10000,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": True,
                "notes": (
                    "Our own standalone property-records service (see docs/redata.md) - not a third-party budget, just a "
                    "sanity ceiling."
                ),
            },
        ),
    ),
    (
        "redata_buildings",
        # 0.9.0
        {
            "display_name": "REData Buildings",
            "calls_per_minute": 20,
            "calls_per_day": None,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Overture building footprints near a point via GET /buildings/, from REData's own Overture mirror, "
                "where its synced US shards cover the point. Read by the boundary chain and the Building "
                "Characteristics panel. Shares REData's one 1,000/hour lookup pool per key. See "
                "services.apis.locations.boundaries.overture."
            ),
        },
        (
            # From 95e44ed4d (2026-10-05), in no release tag; on release/v_0_9_0.
            {
                "display_name": "REData Buildings",
                "calls_per_minute": 20,
                "calls_per_day": None,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": (
                    "Overture building footprints near a point via GET /buildings/, from REData's own Overture mirror (US"
                    " only). Read by the boundary chain and the Building Characteristics panel. Shares REData's one "
                    "1,000/hour lookup pool per key. See services.apis.locations.boundaries.overture."
                ),
            },
        ),
    ),
    (
        "redata_cid_lookup",
        # 0.9.0
        {
            "display_name": "REData CID Resolution",
            "calls_per_minute": 10,
            "calls_per_day": None,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Batch CID->coordinate resolution via POST /places/resolve-cids/ (see "
                "docs/designs/redata-cid-resolution.md), plus reading (GET /places/cid/{cid}/) and downloading (GET "
                "/places/cid/{cid}/media/{id}/download/) a resolved CID's deep-scraped place detail - see "
                "plugins.builtin.redata_place_details."
            ),
        },
        (
            # From 8896b6a9b (2026-07-24), in no release tag; on @release/v0.6.0.
            {
                "display_name": "REData CID Resolution",
                "calls_per_minute": 10,
                "calls_per_day": None,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": "Batch CID->coordinate resolution. Not yet live - see docs/redata-cid-resolution.md.",
            },
            # From a913c0c1d (2026-07-24), shipped in v0.6.0b0.
            {
                "display_name": "REData CID Resolution",
                "calls_per_minute": 10,
                "calls_per_day": None,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": "Batch CID->coordinate resolution via POST /places/resolve-cids/. See docs/redata-cid-resolution.md.",
            },
            # From 610f97be1 (2026-08-07), shipped in v0.7.0b0.
            {
                "display_name": "REData CID Resolution",
                "calls_per_minute": 10,
                "calls_per_day": None,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": (
                    "Batch CID->coordinate resolution via POST /places/resolve-cids/. See "
                    "docs/designs/redata-cid-resolution.md."
                ),
            },
        ),
    ),
    (
        "redata_places",
        # 0.9.0
        {
            "display_name": "REData Places",
            "calls_per_minute": 20,
            "calls_per_day": 40,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Places API (New) via REData - permanently cached on REData's end. See "
                "services.apis.locations.places_resolution."
            ),
        },
        (
            # From 8d3036257 (2026-07-25), shipped in v0.6.0b0, v0.7.0b0, v0.8.0.
            {
                "display_name": "REData Places",
                "calls_per_minute": 20,
                "calls_per_day": None,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": (
                    "Places API (New) via REData - permanently cached on REData's end. See "
                    "services.apis.locations.places_resolution."
                ),
            },
        ),
    ),
    (
        "redata_reference_documents",
        # 0.9.0
        {
            "display_name": "REData Reference Documents",
            "calls_per_minute": 20,
            "calls_per_day": None,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Archival lookups by name via GET /reference-documents/search/, made by the Media gallery's archive "
                "providers. See services.apis.locations.redata_reference_documents_gateway."
            ),
        },
        (
            # From a34e1c197 (2026-09-08), shipped in v0.8.0.
            {
                "display_name": "REData Reference Documents",
                "calls_per_minute": 20,
                "calls_per_day": None,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": (
                    "Archival/encyclopaedic lookups via GET /reference-documents/ (near-coordinate, this panel) and GET "
                    "/reference-documents/search/ (by name, the Media gallery's archive providers). See "
                    "services.apis.locations.redata_reference_documents_gateway."
                ),
            },
        ),
    ),
    (
        "virustotal",
        # 0.9.0
        {
            "display_name": "VirusTotal",
            "calls_per_minute": 3,
            "calls_per_day": 480,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Free public API tier, hash-lookup only. Fast path before ClamAV on externally-fetched image assets -"
                " never sent a user upload or a user's own cloud photo library. See "
                "services.security.virustotal_scan."
            ),
        },
        (
            # From 19daf23be (2026-09-01), in no release tag; on release/v_0_8_0.
            {
                "display_name": "VirusTotal",
                "calls_per_minute": 4,
                "calls_per_day": 490,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "usa_only": False,
                "notes": (
                    "Free public API tier, hash-lookup only. Fast path before ClamAV on externally-fetched image assets -"
                    " never sent a user upload or a user's own cloud photo library. See "
                    "services.security.virustotal_scan."
                ),
            },
        ),
    ),
)


def bring_earlier_defaults_up_to_0_9_0(apps, schema_editor):
    """Write 0.9.0's values into each covered service's row that still holds exactly one of its earlier defaults."""
    ApiRateLimit = apps.get_model("dashboard", "ApiRateLimit")
    now = timezone.now()
    for service, current, earlier in DEFAULTS:
        for old in earlier:
            # Every written field, so a row an admin changed in any of them is theirs; a None matches only NULL.
            if ApiRateLimit.objects.filter(service=service, **old).update(**current, updated=now):
                logger.info("%s held an earlier release's defaults (%s/min, %s/day); it now holds 0.9.0's", service, old["calls_per_minute"], old["calls_per_day"])
        kept = ApiRateLimit.objects.filter(service=service).values(*_FIELDS).first()
        if kept is None or kept == current:
            continue
        differing = [field for field in _FIELDS if kept[field] != current[field]]
        logger.warning(
            "%s keeps its own %s (limits %s), not 0.9.0's (%s): it holds no default any release wrote, so it stays as its admin left it",
            service,
            ", ".join(differing),
            {field: kept[field] for field in _LIMITS},
            {field: current[field] for field in _LIMITS},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0067_calendar_export_resumable"),
    ]

    operations = [
        migrations.RunPython(code=bring_earlier_defaults_up_to_0_9_0, reverse_code=migrations.RunPython.noop),
    ]
