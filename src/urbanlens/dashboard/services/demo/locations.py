"""Where a demo account's pins come from.

Never from invented coordinates. A pin is a claim that a real place exists at a
point, and the whole application takes that claim seriously: opening a pin's
detail page resolves boundaries, asks REData about the parcel, looks for a wiki,
and offers to organise buildings that should be standing there. Point it at a
field nobody has ever surveyed and every one of those answers is empty or wrong
- which reads as the product being broken rather than as the demo being a demo.

So the pool is real places only, from two sources:

1. **The site's own public locations**, exported from production by
   ``export_public_locations`` and loaded here by ``import_public_locations``.
   Only a ``PASSED`` ``PublicPinCandidate`` qualifies - see that command for why
   "has a wiki" is not the same question.
2. **REData's own ``/public-locations/`` catalog** (state capitols, county
   seats, national capitals) - :func:`redata_demo_locations`, via
   :class:`~urbanlens.dashboard.services.apis.locations.redata_public_locations_gateway.RedataPublicLocationsGateway`.
   As of 2026-08-20 this is real, unreleased REData code, not deployed
   anywhere UrbanLens can reach yet - see that function's docstring for why
   every failure mode (unconfigured, unreachable, 404 because it isn't
   deployed yet) degrades to an empty list rather than raising.

Both land in the same manifest (merged by ``import_redata_public_locations``,
which tops up rather than overwrites what ``import_public_locations`` already
wrote, and vice versa), and seeding pins every entry into every new demo
account - which is what gives that account access to each place's wiki, since
wiki visibility is earned by holding a pin on the location.

An empty pool is a correct state, not a failure: production has no passed
candidates yet, and REData's endpoint isn't deployed yet either. The demo is
sparse until one of them has data, and that is a truer representation of the
site than fabricated places would be.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)


def manifest_path() -> Path | None:
    """The configured manifest path, or None when none is set."""
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    return Path(app_settings.demo_locations_file) if app_settings.demo_locations_file else None


def read_manifest() -> list[dict[str, Any]]:
    """The location entries a demo account should be given pins on.

    Returns:
        Entries as written by ``import_public_locations``. Empty when no
        manifest is configured, the file is absent, or it cannot be parsed - a
        demo instance must still come up and serve when its manifest has not
        been delivered yet.
    """
    path = manifest_path()
    if path is None or not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("demo: could not read the location manifest at %s - seeding with no pins", path)
        return []
    entries = raw.get("locations")
    return entries if isinstance(entries, list) else []


def _entry_key(entry: dict[str, Any]) -> tuple[Any, Any] | None:
    """The (latitude, longitude) pair identifying an entry, or None if it has none."""
    latitude, longitude = entry.get("latitude"), entry.get("longitude")
    return None if latitude is None or longitude is None else (str(latitude), str(longitude))


def merge_into_manifest(entries: list[dict[str, Any]]) -> Path | None:
    """Add ``entries`` to the manifest that seeding reads, keyed on coordinates.

    Merges rather than overwrites: two independent importers
    (``import_public_locations`` for this site's own public pins,
    ``import_redata_public_locations`` for REData's catalog) both write here,
    and each must be able to run - in either order, any number of times - without
    erasing what the other already contributed. An entry already present (by
    coordinate) is left as-is rather than replaced, so a later import can never
    demote a richer entry (e.g. one carrying cached wiki photos) to a plainer
    one naming the same point.

    Args:
        entries: Location entries to add, in export format.

    Returns:
        Where the manifest was written, or None when no path is configured.
    """
    path = manifest_path()
    if path is None:
        return None

    existing = read_manifest()
    known = {key for entry in existing if (key := _entry_key(entry)) is not None}
    merged = existing + [entry for entry in entries if _entry_key(entry) not in known]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"locations": merged}, indent=2), encoding="utf-8")
    return path


def import_location_entries(entries: list[dict[str, Any]]) -> tuple[int, int]:
    """Create or refresh a Location (and its Wiki) for each entry, in one transaction.

    Shared by ``import_public_locations`` and ``import_redata_public_locations``
    - both hand it entries in the same export shape, so the actual write logic
    (and its idempotency: re-running tops up rather than duplicates) lives in
    exactly one place regardless of which catalog an entry came from.

    Args:
        entries: Entries in export format (``latitude``, ``longitude``,
            ``official_name``, ``wiki``).

    Returns:
        ``(created, updated)`` location counts.
    """
    from django.db import transaction

    from urbanlens.dashboard.models.aliases.model import WikiAlias
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki

    created = updated = 0
    with transaction.atomic():
        for entry in entries:
            location, was_created = Location.objects.get_exact_or_create(
                entry["latitude"],
                entry["longitude"],
                defaults={"official_name": entry.get("official_name") or ""},
            )
            if was_created:
                created += 1
            else:
                updated += 1
                if entry.get("official_name") and not location.official_name:
                    location.official_name = entry["official_name"]
                    location.save(update_fields=["official_name"])

            wiki_data = entry.get("wiki")
            if not wiki_data:
                continue

            wiki, _ = Wiki.objects.get_or_create(
                location=location,
                defaults={"name": wiki_data.get("name") or entry.get("official_name") or "", "officially_created": True},
            )
            # Top up rather than replace: an alias a demo visitor added during
            # their session is theirs, and a refresh should not delete it.
            existing = set(wiki.aliases.values_list("name", flat=True))
            for alias in wiki_data.get("aliases") or []:
                if alias and alias not in existing:
                    WikiAlias.objects.create(wiki=wiki, name=alias)

    return created, updated


def redata_demo_locations() -> list[dict[str, Any]]:
    """Public demo locations published by REData's ``/public-locations/`` catalog.

    State capitols, county seats and national capitals - a pure local-DB read
    on REData's own side, no per-source attribution. Degrades to an empty list
    rather than raising when REData is unconfigured, unreachable, or (as of
    2026-08-20) simply does not have this endpoint deployed yet: "REData
    doesn't have this yet" and "the site's own public-pin pool is empty" must
    both leave seeding with no pins to give out, not a stack trace - see
    :func:`pool_locations`.

    Returns:
        Entries in export format (``latitude``, ``longitude``,
        ``official_name``, ``wiki``) - possibly empty.
    """
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
    from urbanlens.dashboard.services.apis.locations.redata_public_locations_gateway import RedataPublicLocationsGateway

    if not redata_configured():
        return []

    gateway = RedataPublicLocationsGateway()
    entries: list[dict[str, Any]] = []
    for record in gateway.list_public_locations(limit=100):
        latitude, longitude, name = record.get("latitude"), record.get("longitude"), record.get("name")
        if latitude is None or longitude is None:
            continue
        entries.append(
            {
                "latitude": latitude,
                "longitude": longitude,
                "official_name": name or "",
                # No wiki payload of its own - unlike a PASSED PublicPinCandidate,
                # REData's catalog carries no cached photos or aliases, only the
                # coordinate and name. import_public_locations still gives it a
                # minimal Wiki so the location isn't pinned into a dead end.
                "wiki": {"name": name or "", "aliases": [], "photos": []},
            },
        )
    return entries


def pool_locations() -> list[Location]:
    """Resolve the manifest to Location rows that actually exist here.

    Entries are matched, never created: ``import_public_locations`` is what
    creates them, and it is also what pulls each one's wiki across. Seeding a
    pin against a coordinate with no imported Location would produce exactly the
    empty detail page this module exists to avoid.

    Returns:
        The Locations to pin into a new demo account, in manifest order.
    """
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.location.queryset import quantize_coordinate

    resolved: list[Location] = []
    for entry in read_manifest():
        latitude, longitude = entry.get("latitude"), entry.get("longitude")
        if latitude is None or longitude is None:
            continue
        location = Location.objects.filter(
            latitude=quantize_coordinate(latitude, "latitude"),
            longitude=quantize_coordinate(longitude, "longitude"),
        ).first()
        if location is None:
            logger.warning("demo: manifest names a location that was never imported (%s, %s)", latitude, longitude)
            continue
        resolved.append(location)
    return resolved
