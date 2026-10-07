"""Saving parsed Route candidates."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db import DatabaseError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.import_formats.gpx_tracks import ParsedRoute

logger = logging.getLogger(__name__)


def iter_route_import_events(parsed_routes: list[ParsedRoute], profile: Profile) -> Iterator[dict[str, Any]]:
    """Save each parsed route and record the visits its dwells imply, one event per route.

    Args:
        parsed_routes: Unsaved Route instances paired with their raw points.
        profile: The profile these routes belong to (used for dwell-detection).

    Yields:
        ``{type, subtype: "route", ...}``: ``start`` with ``total``; ``progress`` with ``current``,
        ``total``, ``percent``, ``created`` and ``skipped``; then ``complete`` with the final counts.
        A profile that does not track routes gets a lone ``complete`` with ``reason: "routes_disabled"``.
    """
    from urbanlens.dashboard.services.import_formats.gpx_tracks import detect_dwells_and_create_visits
    from urbanlens.dashboard.services.security.redact import redact_text
    from urbanlens.dashboard.services.visits.visits import route_import_allowed

    subtype = "route"
    total = len(parsed_routes)
    if total == 0:
        return

    if not route_import_allowed(profile):
        yield {"type": "complete", "total": total, "created": 0, "skipped": total, "subtype": subtype, "reason": "routes_disabled"}
        return

    yield {"type": "start", "total": total, "subtype": subtype}

    created = 0
    skipped = 0

    for i, parsed in enumerate(parsed_routes, 1):
        try:
            parsed.route.save()
            detect_dwells_and_create_visits(parsed.route, parsed.raw_points, profile)
            created += 1
        except (DatabaseError, ValueError) as exc:
            logger.warning("Failed to save route %s: %s", redact_text(parsed.route.name), exc)
            skipped += 1

        yield {"type": "progress", "current": i, "total": total, "percent": min(100, int(i / total * 100)), "created": created, "skipped": skipped, "subtype": subtype}

    yield {"type": "complete", "total": total, "created": created, "skipped": skipped, "subtype": subtype}
