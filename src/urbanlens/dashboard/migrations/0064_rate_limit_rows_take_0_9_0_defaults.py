"""Give each rate-limit row nobody has edited the defaults 0.9.0 changed.

``rate_limiter.get_limit_config`` writes a service's registered defaults into its ``ApiRateLimit`` row only when it
creates the row, and later replaces only the generic 20/min, 500/day fallback. It never writes over a row that holds a
service's own defaults, because an admin can edit every one of these fields: the site admin's API limits page and
Django admin both do. So a deployment that upgrades keeps the values its rows were created with, and a changed default
in ``SERVICE_REGISTRY`` or a plugin's ``get_service_defaults`` never reaches them.

This migration writes 0.9.0's values into a row only when every field ``get_limit_config`` writes still holds exactly
what 0.8.0 wrote. Any other row is left alone, and so is every row's ``enabled`` switch.

- ``redata_places``: ``calls_per_day`` None -> 40. REData's Google Places budget is 160 uncached calls a UTC day, shared
  with its own CID resolution, and REData asks each client for about 40. This entry has held 0.8.0's values since it
  was registered in 0.6.0, so a row created by any earlier release takes the cap.
- Notes only: ``azure_maps``, ``google_geocoding``, ``google_maps`` and ``google_places`` (each free tier restated by
  de04fadf2), ``google_open_buildings``, ``overpass``, ``overture_maps`` and ``redata_reference_documents``. Three of
  the old notes misstate a bill. ``google_places``' still names the $200 monthly credit, which Google ended in 2025-03.
  ``google_maps``' gives Static Maps 25,000 free a month; it is 10,000. ``google_geocoding``'s gives its 10,000 to a
  "Places Details Essentials SKU", but the 10,000 is Geocoding's; the legacy Place Details ``cid:`` lookup counted
  there is billed as Places Details, a Pro SKU with 5,000 free a month.

No other service's defaults changed between 0.8.0 and 0.9.0. The monthly free-tier ceilings from de04fadf2 are read
from code on every check (``free_tier_ceiling``), not from the row, so nothing here carries them.

An edited row whose limits differ from 0.9.0's is logged, so whoever runs the upgrade can see that ``redata_places``,
say, is still uncapped by choice.

Reverse is a no-op. A row holding 0.9.0's values cannot be told from one an admin set to the same values, so putting
0.8.0's back could lift a cap somebody chose, and 0.8.0 runs as well with REData Places held to 40 a day.
"""

import logging

from django.db import migrations
from django.utils import timezone

logger = logging.getLogger(__name__)

#: The fields ``get_limit_config`` writes from a service's defaults; ``enabled`` is not one of them.
_FIELDS = ("display_name", "calls_per_minute", "calls_per_day", "calls_per_30_days", "min_interval_seconds", "usa_only", "notes")

#: Each changed service's defaults: (service, what 0.8.0 wrote, what 0.9.0 writes), frozen as they were.
CHANGED_DEFAULTS = (
    (
        "redata_places",
        {
            "display_name": "REData Places",
            "calls_per_minute": 20,
            "calls_per_day": None,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": "Places API (New) via REData - permanently cached on REData's end. See services.apis.locations.places_resolution.",
        },
        {"calls_per_day": 40},
    ),
    (
        "azure_maps",
        {
            "display_name": "Azure Maps (Search/Geocoding)",
            "calls_per_minute": 50,
            "calls_per_day": 2500,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": "Free tier: 5,000 transactions/month (Gen1 S0 / Gen2 pay-as-you-go) shared across Search and Geocoding.",
        },
        {"notes": "Free tier: 5,000 Search transactions a month (Gen2), per subscription, shared with REData."},
    ),
    (
        "google_geocoding",
        {
            "display_name": "Google Geocoding API",
            "calls_per_minute": 20,
            "calls_per_day": 500,
            "calls_per_30_days": 9999,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": "Free tier: 10,000 calls/month (Places Details Essentials SKU).",
        },
        {
            "notes": (
                "Free tier: 10,000 calls/month for Geocoding, shared with REData on the same billing account. "
                "The legacy Place Details cid: lookup is counted here too, though Google bills it as Places Details (Pro, 5,000 free a month)."
            ),
        },
    ),
    (
        "google_maps",
        {
            "display_name": "Google Maps (Static/StreetView)",
            "calls_per_minute": 20,
            "calls_per_day": 200,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": "Static Maps: 25,000 free/month. Street View: billed per call.",
        },
        {
            "notes": (
                "Static Maps and Street View Static: 10,000 free a month each, then $2 and $7 per 1,000. "
                "The coverage probes before a Street View image are counted here too, though Google does not bill them."
            ),
        },
    ),
    (
        "google_open_buildings",
        {
            "display_name": "Google Open Buildings",
            "calls_per_minute": 20,
            "calls_per_day": 500,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Downloads whole gzip CSV shards of Google's public Open Buildings dataset during boundary lookups. "
                "The dataset has no quota, so this bounds our own bandwidth; the values are the generic fallback's, not tuned. "
                "See services.apis.locations.boundaries.google_open_buildings."
            ),
        },
        {
            "notes": (
                "Downloads level-6 gzip CSV shards of Google's public Open Buildings v3 dataset during boundary lookups, "
                "only for cells it covers (none in the US), skipping any past MAX_SHARD_BYTES and remembering missing ones. "
                "The dataset has no quota, so this bounds our own bandwidth; the values are the generic fallback's, not tuned. "
                "See services.apis.locations.boundaries.google_open_buildings."
            ),
        },
    ),
    (
        "google_places",
        {
            "display_name": "Google Places API",
            "calls_per_minute": 20,
            "calls_per_day": 200,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": "Free tier: $200/month credit. Geocoding/details billed per call.",
        },
        {
            "notes": (
                "No credit since 2025-03: each SKU has its own monthly free cap, per billing account, shared with REData. "
                "Held to the smallest this budget can spend."
            ),
        },
    ),
    (
        "overpass",
        {
            "display_name": "Overpass API (OpenStreetMap)",
            "calls_per_minute": 240,
            "calls_per_day": 24000,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Free API. Load is distributed across several public Overpass instances, and any instance that errors/times out "
                "is dropped until the next day. Each logical lookup may spend more than one call when it fails over."
            ),
        },
        {
            "notes": (
                "Free API. Load is distributed across several public Overpass instances, and an instance that errors or times out "
                "is dropped for minutes, longer on each repeat, or for the wait it states. "
                "Each logical lookup may spend more than one call when it fails over."
            ),
        },
    ),
    (
        "overture_maps",
        {
            "display_name": "Overture Maps",
            "calls_per_minute": 20,
            "calls_per_day": 500,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Building/place/address/land-use GeoParquet themes via services.apis.locations.boundaries.overture_maps. "
                "Free public dataset, but its STAC index rate-limits us under load - see P110."
            ),
        },
        {
            "notes": (
                "Overture's public GeoParquet release via services.apis.locations.boundaries.overture_maps, "
                "read only where REData's mirror holds nothing (outside its synced US shards); REData answers the rest. "
                "Free public dataset, but its STAC index rate-limits us under load - see P110."
            ),
        },
    ),
    (
        "redata_reference_documents",
        {
            "display_name": "REData Reference Documents",
            "calls_per_minute": 20,
            "calls_per_day": None,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": False,
            "notes": (
                "Archival/encyclopaedic lookups via GET /reference-documents/ (near-coordinate, this panel) and "
                "GET /reference-documents/search/ (by name, the Media gallery's archive providers). "
                "See services.apis.locations.redata_reference_documents_gateway."
            ),
        },
        {
            "notes": (
                "Archival lookups by name via GET /reference-documents/search/, made by the Media gallery's archive providers. "
                "See services.apis.locations.redata_reference_documents_gateway."
            ),
        },
    ),
)


def apply_0_9_0_defaults(apps, schema_editor):
    """Write 0.9.0's values into each changed service's row that still holds exactly 0.8.0's."""
    ApiRateLimit = apps.get_model("dashboard", "ApiRateLimit")
    now = timezone.now()
    for service, old, changes in CHANGED_DEFAULTS:
        # Every written field, so a row an admin changed in any of them is theirs; a None matches only NULL.
        untouched = {field: old[field] for field in _FIELDS}
        if ApiRateLimit.objects.filter(service=service, **untouched).update(**changes, updated=now):
            continue
        limits = {field: value for field, value in changes.items() if field != "notes"}
        kept = ApiRateLimit.objects.filter(service=service).values(*limits).first() if limits else None
        if kept is not None and kept != limits:
            logger.warning("%s keeps %s, not 0.9.0's %s: its row was edited after 0.8.0 created it, so it stays as its admin left it", service, kept, limits)


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0063_apicalllog_calls"),
    ]

    operations = [
        migrations.RunPython(code=apply_0_9_0_defaults, reverse_code=migrations.RunPython.noop),
    ]
