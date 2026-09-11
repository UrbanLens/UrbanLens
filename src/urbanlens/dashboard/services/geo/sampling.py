"""Choose a bounded subset of points that still shows the whole area.

A photo map layer that plots everything costs one serialized row per photo, and
a popular location accumulates thousands. Capping is the fix; capping with a
slice is not, because photographs cluster - the first N rows of a well-visited
site are usually the same doorway, and everything else disappears from the map.

This picks for coverage first and density second: lay a grid over the points,
take one from each occupied cell in turn, and only then go round again for the
crowded cells. An outlying photo therefore survives a cap that would have
dropped it, which is the difference between a capped map and a broken one.

Deterministic by construction - cells and the points inside them are both
visited in sorted order - so the same request returns the same photos and a
marker does not move when the layer reloads.
"""

from __future__ import annotations

import collections
import math
from typing import TYPE_CHECKING, Any

from django.db.models import QuerySet

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Most photos any map layer will plot. A photo map is read at a glance, so the
#: value of the five-hundredth marker is already near zero while the cost of the
#: five-thousandth is a serialized row - and, on the gallery layers, an
#: uploader-visibility resolution - inside the request.
MAX_MAP_PHOTOS = 500

#: A point as the callers have it: an identifier and its coordinates.
Point = tuple[int, float, float]


def _grid_size(count: int, limit: int) -> int:
    """How many cells per axis to lay over the points.

    Aims for rather more cells than the limit, so most cells hold one point and
    the first pass alone nearly fills the quota - which is what makes coverage
    win over density.

    Args:
        count: How many points there are.
        limit: How many may be kept.

    Returns:
        Cells per axis, at least one.
    """
    del count
    return max(1, math.ceil(math.sqrt(limit)) * 2)


def spread_across_space(points: Sequence[Point], limit: int) -> list[int]:
    """Pick at most *limit* ids, spread over the area *points* covers.

    Args:
        points: ``(id, latitude, longitude)`` for each candidate.
        limit: The most ids to return. Zero returns nothing.

    Returns:
        The chosen ids. Every one comes from *points*, none is repeated, and the
        same input always gives the same output.
    """
    if limit <= 0 or not points:
        return []
    if len(points) <= limit:
        return [point[0] for point in points]

    lats = [point[1] for point in points]
    lons = [point[2] for point in points]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    # A zero span means every point shares a coordinate, which is an ordinary
    # case (a hundred photos of one doorway) rather than an edge case. Any
    # non-zero divisor puts them all in one cell, which is the right answer.
    lat_span = (max_lat - min_lat) or 1.0
    lon_span = (max_lon - min_lon) or 1.0

    size = _grid_size(len(points), limit)
    cells: dict[tuple[int, int], list[int]] = collections.defaultdict(list)
    for identifier, lat, lon in points:
        row = min(size - 1, int((lat - min_lat) / lat_span * size))
        column = min(size - 1, int((lon - min_lon) / lon_span * size))
        cells[(row, column)].append(identifier)

    for bucket in cells.values():
        bucket.sort()

    chosen: list[int] = []
    order = sorted(cells)
    depth = 0
    while len(chosen) < limit:
        took_any = False
        for key in order:
            bucket = cells[key]
            if depth < len(bucket):
                chosen.append(bucket[depth])
                took_any = True
                if len(chosen) == limit:
                    return chosen
        if not took_any:
            break
        depth += 1
    return chosen


def bound_map_layer[Images: QuerySet[Any]](images: Images, limit: int | None = None) -> tuple[Images, bool, int]:
    """Cap what a map layer plots, keeping the area covered.

    Counts first and reads coordinates only when the count is over the limit, so
    the ordinary case pays one cheap query rather than a projection of the whole
    gallery.

    Args:
        images: Geotagged, already access-scoped images.
        limit: The most to plot. Defaults to :data:`MAX_MAP_PHOTOS`, read at call
            time rather than bound as a default so a test can lower it without
            seeding five hundred photographs.

    Returns:
        ``(images, truncated, total)`` - what to render, whether anything was
        left out, and how many there were before capping.
    """
    limit = MAX_MAP_PHOTOS if limit is None else limit
    total = images.count()
    if total <= limit:
        return images, False, total

    # The None check is not only for the type checker: it also means a caller
    # that forgot to filter for coordinates drops unplottable rows rather than
    # crashing.
    points = [(pk, float(lat), float(lon)) for pk, lat, lon in images.values_list("pk", "latitude", "longitude") if lat is not None and lon is not None]
    return images.filter(pk__in=spread_across_space(points, limit)), True, total
