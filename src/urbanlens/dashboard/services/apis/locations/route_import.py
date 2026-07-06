"""Streaming import orchestration for Route records.

Route candidates are gathered by the caller from any source (GPX tracks/routes,
Google Takeout semantic activitySegments, ...) and saved here as a second SSE
pass, mirroring how ``location_history.import_location_history_streaming`` is
run as its own pass after the main pin-import loop. Each saved Route
immediately runs dwell-detection to auto-create GEOLOCATION-sourced PinVisit
rows.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.db import DatabaseError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.import_formats.gpx_tracks import ParsedRoute

logger = logging.getLogger(__name__)


def import_routes_streaming(parsed_routes: list[ParsedRoute], profile: Profile) -> Iterator[str]:
    r"""Stream SSE events while saving parsed Route candidates.

    SSE event shapes emitted:

    - ``{type: "start",    total, subtype: "route"}``
    - ``{type: "progress", current, total, percent, created, skipped, subtype: "route"}``
    - ``{type: "complete", total, created, skipped, subtype: "route"}``

    Args:
        parsed_routes: Unsaved Route instances paired with their raw points,
            as returned by ``gpx_tracks_to_routes``/``semantic_history_to_routes``.
        profile: The profile these routes belong to (used for dwell-detection).

    Yields:
        SSE-formatted strings (``data: {...}\\n\\n``).
    """
    from urbanlens.dashboard.services.import_formats.gpx_tracks import detect_dwells_and_create_visits

    def sse(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    total = len(parsed_routes)
    if total == 0:
        return

    yield sse({"type": "start", "total": total, "subtype": "route"})

    created = 0
    skipped = 0

    for i, parsed in enumerate(parsed_routes, 1):
        try:
            parsed.route.save()
            detect_dwells_and_create_visits(parsed.route, parsed.raw_points, profile)
            created += 1
        except (DatabaseError, ValueError) as exc:
            logger.warning("Failed to save route '%s': %s", parsed.route.name, exc)
            skipped += 1

        yield sse(
            {
                "type": "progress",
                "current": i,
                "total": total,
                "percent": min(100, int(i / total * 100)),
                "created": created,
                "skipped": skipped,
                "subtype": "route",
            },
        )

    yield sse(
        {
            "type": "complete",
            "total": total,
            "created": created,
            "skipped": skipped,
            "subtype": "route",
        },
    )
