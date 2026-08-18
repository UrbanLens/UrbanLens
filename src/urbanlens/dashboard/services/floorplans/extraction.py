"""Blueprint tracing: turn a georeferenced schematic into floorplan geometry.

The pipeline the editor drives:

1. The user uploads a blueprint/schematic as a ``MapImageOverlay`` on their
   pin and drags its corners until it lines up with the imagery - the same
   overlay mechanism historical map sheets already use, so alignment is a
   solved, familiar interaction.
2. This module reads the aligned sheet and asks a vision model for the
   *structure* it shows - rooms with their names, walls, doors, windows - in
   normalized image coordinates.
3. Those coordinates map through the overlay's corner georeference (a
   bilinear interpolation - adequate for the near-rectilinear crops people
   actually align; a heavily warped sheet should be rectified before upload)
   into WGS-84, and come back as document fragments the editor merges as
   *suggestions the user edits*, never as silently-committed rows.

AI being unconfigured or unable to read the sheet degrades to manual tracing
- the editor works identically either way, this is an accelerant.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = (
    "This image is a building floorplan, blueprint, or schematic. Identify its structure "
    "and answer with ONLY a JSON object, no prose, shaped exactly like:\n"
    '{"rooms": [{"name": "Ward B", "polygon": [[x, y], ...]}], '
    '"walls": [{"line": [[x, y], [x, y]]}], '
    '"doors": [{"point": [x, y]}], "windows": [{"point": [x, y]}]}\n'
    "All coordinates are fractions of the image size: x from 0.0 (left) to 1.0 (right), "
    "y from 0.0 (top) to 1.0 (bottom). Trace room outlines as closed polygons in drawing "
    "order. Include a room's name only when it is legibly written on the plan. Trace the "
    "major walls as straight segments. Mark door and window openings as single points. "
    "If the image is not a floorplan, answer with an empty JSON object {}."
)


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting, or None when singular."""
    size = len(rhs)
    rows = [[*matrix[index], rhs[index]] for index in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) < 1e-12:
            return None
        rows[column], rows[pivot] = rows[pivot], rows[column]
        divisor = rows[column][column]
        rows[column] = [value / divisor for value in rows[column]]
        for row in range(size):
            if row == column:
                continue
            factor = rows[row][column]
            if factor:
                rows[row] = [value - factor * rows[column][index] for index, value in enumerate(rows[row])]
    return [row[size] for row in rows]


def _corner_transform(overlay: MapImageOverlay):
    """Map normalized image coordinates through the overlay's georeference.

    A *projective* transform, not a bilinear one, because that is what the
    browser already uses to draw the sheet (``matrix3dForCorners`` in
    ``shared/map-image-overlays.ts`` solves the same eight-parameter system
    for its CSS ``matrix3d``). Tracing has to agree with what the user sees:
    under bilinear interpolation a sheet whose corners are not an affine
    parallelogram - which is exactly what dragging corners onto oblique
    imagery produces - renders one way and traces another, so the geometry
    lands beside the walls it was traced from.

    Falls back to bilinear only when the corner arrangement is degenerate
    (three corners collinear), where the projective system has no solution.

    Args:
        overlay: The georeferenced sheet.

    Returns:
        Callable ``(x, y) -> (longitude, latitude)`` with x/y in 0..1, the
        image's own top-left origin.
    """
    nw = (overlay.nw_longitude, overlay.nw_latitude)
    ne = (overlay.ne_longitude, overlay.ne_latitude)
    se = (overlay.se_longitude, overlay.se_latitude)
    sw = (overlay.sw_longitude, overlay.sw_latitude)
    source = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    target = (nw, ne, se, sw)

    matrix: list[list[float]] = []
    rhs: list[float] = []
    for (sx, sy), (tx, ty) in zip(source, target, strict=True):
        matrix.append([sx, sy, 1, 0, 0, 0, -sx * tx, -sy * tx])
        rhs.append(tx)
        matrix.append([0, 0, 0, sx, sy, 1, -sx * ty, -sy * ty])
        rhs.append(ty)
    solution = _solve(matrix, rhs)

    if solution is None:
        def bilinear(x: float, y: float) -> tuple[float, float]:
            top = (nw[0] + (ne[0] - nw[0]) * x, nw[1] + (ne[1] - nw[1]) * x)
            bottom = (sw[0] + (se[0] - sw[0]) * x, sw[1] + (se[1] - sw[1]) * x)
            return (top[0] + (bottom[0] - top[0]) * y, top[1] + (bottom[1] - top[1]) * y)

        logger.debug("floorplan extraction: degenerate corners on overlay %s, falling back to bilinear", overlay.pk)
        return bilinear

    a, b, c, d, e, f, g, h = solution

    def projective(x: float, y: float) -> tuple[float, float]:
        denominator = g * x + h * y + 1
        if abs(denominator) < 1e-12:
            return (nw[0], nw[1])
        return ((a * x + b * y + c) / denominator, (d * x + e * y + f) / denominator)

    return projective


def _clamp01(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, number))


def _points(raw: Any, transform) -> list[list[float]]:
    """Normalized point pairs mapped to [lng, lat]; malformed entries skipped."""
    points: list[list[float]] = []
    for pair in raw if isinstance(raw, list) else []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        x, y = _clamp01(pair[0]), _clamp01(pair[1])
        if x is None or y is None:
            continue
        lng, lat = transform(x, y)
        points.append([lng, lat])
    return points


def _overlay_image_bytes(overlay: MapImageOverlay) -> bytes | None:
    """The overlay's stored sheet as bytes, or None when there isn't one.

    ``Image``'s file field is ``image`` (an ImageField); an overlay may
    instead reference an external URL or a tile pyramid, neither of which has
    local bytes to read.
    """
    if overlay.image_id is None:
        return None
    stored = getattr(overlay.image, "image", None)
    if not stored:
        return None
    try:
        with stored.open("rb") as handle:
            return handle.read()
    except (OSError, ValueError):
        logger.debug("floorplan extraction: could not read overlay %s image", overlay.pk, exc_info=True)
        return None


def _structure_from_model(image_bytes: bytes) -> dict[str, Any] | None:
    """Ask the configured vision model for the sheet's structure, or None."""
    from urbanlens.dashboard.services.ai.vision import describe_image_json

    return describe_image_json(image_bytes, _EXTRACTION_PROMPT)


def extract_overlay_structure(overlay: MapImageOverlay) -> dict[str, Any] | None:
    """Extract suggested floorplan geometry from one aligned blueprint overlay.

    Args:
        overlay: The georeferenced sheet. Must have a stored image (an
            external ``image_url``/tile overlay has no bytes to read).

    Returns:
        ``{"rooms": [...], "elements": [...]}`` as document fragments in
        world coordinates, ``{}`` when the model saw no floorplan, or None
        when extraction isn't possible (no stored image, AI unconfigured,
        or the model's answer was unusable).
    """
    image_bytes = _overlay_image_bytes(overlay)
    if image_bytes is None:
        return None

    structure = _structure_from_model(image_bytes)
    if structure is None:
        return None
    if not structure:
        return {}

    transform = _corner_transform(overlay)
    rooms = []
    for room in structure.get("rooms") or []:
        polygon = _points(room.get("polygon") if isinstance(room, dict) else None, transform)
        if len(polygon) < 3:
            continue
        if polygon[0] != polygon[-1]:
            polygon.append(list(polygon[0]))
        rooms.append({"name": str(room.get("name") or "") if isinstance(room, dict) else "", "geometry": {"type": "Polygon", "coordinates": [polygon]}})

    elements = []
    for wall in structure.get("walls") or []:
        line = _points(wall.get("line") if isinstance(wall, dict) else None, transform)
        if len(line) >= 2:
            elements.append({"kind": "wall", "geometry": {"type": "LineString", "coordinates": line}})
    for kind, key in (("door", "doors"), ("window", "windows")):
        for record in structure.get(key) or []:
            point = _points([record.get("point")] if isinstance(record, dict) else None, transform)
            if point:
                elements.append({"kind": kind, "geometry": {"type": "Point", "coordinates": point[0]}, "locks": []})

    return {"rooms": rooms, "elements": elements}
