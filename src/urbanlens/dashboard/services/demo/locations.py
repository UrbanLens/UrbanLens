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
2. **REData**, via an endpoint published for exactly this purpose. Not yet
   wired: :func:`redata_demo_locations` is the seam it will arrive through, and
   is deliberately a stub that returns nothing rather than a guess at a URL.

Both land in the same manifest, and seeding pins every entry into every new demo
account - which is what gives that account access to each place's wiki, since
wiki visibility is earned by holding a pin on the location.

An empty pool is a correct state, not a failure: production has no passed
candidates yet. The demo is sparse until it does, and that is a truer
representation of the site than fabricated places would be.
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


def write_manifest(entries: list[dict[str, Any]]) -> Path | None:
    """Persist the manifest that seeding will read.

    Args:
        entries: Location entries, in export format.

    Returns:
        Where it was written, or None when no path is configured.
    """
    path = manifest_path()
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"locations": entries}, indent=2), encoding="utf-8")
    return path


def redata_demo_locations() -> list[dict[str, Any]]:
    """Public demo locations published by REData.

    Placeholder for the endpoint that will supply coordinates beyond this site's
    own public pins. Left returning nothing on purpose: inventing a URL now
    would mean either a dead request on every import or, worse, a plausible
    wrong one that quietly returns somebody else's data.

    Returns:
        Location entries in the same shape as the export, once implemented.
    """
    return []


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
