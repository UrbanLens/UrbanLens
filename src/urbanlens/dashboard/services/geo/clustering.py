"""Finding the densest group of points without comparing every pair.

The obvious formulation - "which point has the most neighbours within R" - is one
great-circle calculation per *pair*, which at 20,000 pins was about seven minutes
of a process serving nothing (P108). A spatial histogram answers the same
question in one pass plus a bounded number of dictionary lookups.

The seed cell is an approximation; cluster membership and the centroid are exact.
Compared against the pairwise scan they agree wherever the points have a densest
region at all, and diverge only where the question has no single answer - two
equal concentrations, or points spread evenly. See `test_geo_clustering.py`.
"""

from __future__ import annotations

from collections import Counter
import math
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.geo import distance
from urbanlens.dashboard.services.geo.longitude import circular_mean_longitude

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

#: Mean Earth radius in kilometres, matching ``distance.EARTH_RADIUS_METERS``.
EARTH_RADIUS_KM = distance.EARTH_RADIUS_METERS / 1000.0

#: Cell side as a fraction of the radius' chord length. Half means the block
#: summed below spans roughly one and a half radii, so a concentration sitting
#: across a cell boundary is still counted as one.
_CELL_FRACTION = 0.5

#: Cell offsets summed around a candidate, per axis. One is enough to make the
#: histogram boundary-insensitive; more would widen the neighbourhood past the
#: radius it is meant to approximate.
_BLOCK_RADIUS = 1

#: Passes of "take the points within the radius, move to their centre". Two is
#: enough to leave the histogram's cell geometry behind; the cost is one
#: great-circle calculation per point per pass, so this is the constant the
#: scaling test in ``test_map_center_scaling.py`` measures.
_REFINEMENT_PASSES = 2

Point = tuple[float, float]


def densest_cluster_centroid(points: Sequence[Point], radius_km: float) -> Point | None:
    """Centre of the largest concentration of points within ``radius_km``.

    Args:
        points: ``(latitude, longitude)`` pairs in degrees. May be empty.
        radius_km: How far apart two points can be and still count as part of
            the same concentration. Must be positive.

    Returns:
        The concentration's ``(latitude, longitude)`` centroid, or None when
        ``points`` is empty. Latitude is averaged arithmetically and longitude
        as a direction, so a cluster straddling the antimeridian centres on the
        cluster rather than in the Atlantic.

    Raises:
        ValueError: If ``radius_km`` is not positive.
    """
    if radius_km <= 0:
        raise ValueError(f"radius_km must be positive, got {radius_km}")
    if not points:
        return None
    if len(points) == 1:
        return points[0]

    cells = [_cell_of(point, radius_km) for point in points]
    # The block, not every point: an unrefined answer should still be where the
    # pins are, not the average of two continents.
    cluster = _densest_block(points, cells)
    seed = _centroid(cluster)

    for _ in range(_REFINEMENT_PASSES):
        nearby = [point for point in points if distance.haversine_km(seed[0], seed[1], point[0], point[1]) <= radius_km]
        if not nearby:
            # Seed drifted off every point; keep the previous membership.
            break
        cluster = nearby
        seed = _centroid(cluster)

    return _centroid(cluster)


def _densest_block(points: Sequence[Point], cells: Sequence[tuple[int, int, int]]) -> list[Point]:
    """The points in the most occupied cell neighbourhood.

    Args:
        points: The points being clustered.
        cells: Each point's cell index, in the same order.

    Returns:
        Every point in the winning block. Never empty, since the winning cell
        is one that holds at least one point.
    """
    occupancy = Counter(cells)
    # Tie-break on the cell index, so row order cannot change the answer.
    best = max(occupancy, key=lambda cell: (_block_total(occupancy, cell), cell))
    block = {(best[0] + dx, best[1] + dy, best[2] + dz) for dx in range(-_BLOCK_RADIUS, _BLOCK_RADIUS + 1) for dy in range(-_BLOCK_RADIUS, _BLOCK_RADIUS + 1) for dz in range(-_BLOCK_RADIUS, _BLOCK_RADIUS + 1)}
    return [point for point, cell in zip(points, cells, strict=True) if cell in block]


def _block_total(occupancy: Counter[tuple[int, int, int]], cell: tuple[int, int, int]) -> int:
    """How many points sit in ``cell`` and the cells immediately around it.

    Args:
        occupancy: Point count per cell.
        cell: The cell at the centre of the block.

    Returns:
        The block's total occupancy.
    """
    x, y, z = cell
    return sum(occupancy.get((x + dx, y + dy, z + dz), 0) for dx in range(-_BLOCK_RADIUS, _BLOCK_RADIUS + 1) for dy in range(-_BLOCK_RADIUS, _BLOCK_RADIUS + 1) for dz in range(-_BLOCK_RADIUS, _BLOCK_RADIUS + 1))


def _cell_of(point: Point, radius_km: float) -> tuple[int, int, int]:
    """Which cell of the lattice a point falls in.

    Cells are cut from a cubic lattice in the unit sphere's own coordinates
    rather than from a latitude/longitude grid, because lat/lng cells shrink
    towards the poles and would make polar accounts look artificially dense.

    Args:
        point: ``(latitude, longitude)`` in degrees.
        radius_km: The clustering radius, which sets the cell size.

    Returns:
        The cell's integer index on each axis.
    """
    side = _CELL_FRACTION * _chord_length(radius_km)
    latitude, longitude = math.radians(point[0]), math.radians(point[1])
    cos_latitude = math.cos(latitude)
    return (
        math.floor(cos_latitude * math.cos(longitude) / side),
        math.floor(cos_latitude * math.sin(longitude) / side),
        math.floor(math.sin(latitude) / side),
    )


def _chord_length(radius_km: float) -> float:
    """Straight-line distance across the sphere for a given surface distance.

    Args:
        radius_km: Surface distance in kilometres.

    Returns:
        The corresponding chord on a unit sphere, capped at 2 (the diameter),
        which is what a radius past the far side of the planet amounts to.
    """
    angle = min(radius_km / EARTH_RADIUS_KM, math.pi)
    return 2.0 * math.sin(angle / 2.0)


def _centroid(points: Iterable[Point]) -> Point:
    """Average a set of points, treating longitude as a direction.

    Args:
        points: ``(latitude, longitude)`` pairs. Must not be empty.

    Returns:
        The average ``(latitude, longitude)``.
    """
    collected = list(points)
    latitude = sum(point[0] for point in collected) / len(collected)
    return latitude, circular_mean_longitude([point[1] for point in collected])
