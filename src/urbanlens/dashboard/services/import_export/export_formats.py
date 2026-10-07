"""Pin export writers: GeoJSON, KML, GPX, and generic CSV."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol


class _ExportablePin(Protocol):
    """The subset of ``Pin`` these writers actually read - lets tests use a lightweight stand-in instead of a real, database-backed ``Pin``."""

    @property
    def effective_name(self) -> str: ...

    @property
    def effective_latitude(self) -> float: ...

    @property
    def effective_longitude(self) -> float: ...

    description: str | None


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


#: First characters a spreadsheet reads as the start of a formula (OWASP CSV injection).
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_text_cell(value: str | None) -> str:
    """Return user-supplied text safe to write into a CSV cell.

    A cell starting with ``=``, ``+``, ``-``, ``@``, tab or CR is evaluated as a formula when the file is opened in a
    spreadsheet, so it gets a leading single quote, which the spreadsheet shows as plain text.

    Args:
        value: The user-supplied text, or ``None`` for an empty cell.

    Returns:
        The text, quoted if it could be read as a formula. Numbers go to the writer unchanged rather than through here.
    """
    text = value or ""
    return f"'{text}" if text.startswith(_CSV_FORMULA_PREFIXES) else text


def pins_to_geojson(pins: Iterable[_ExportablePin]) -> str:
    """Serialize pins as a GeoJSON FeatureCollection of Point features."""
    import json

    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [pin.effective_longitude, pin.effective_latitude]},
            "properties": {
                "name": pin.effective_name,
                "description": pin.description or "",
            },
        }
        for pin in pins
    ]
    return json.dumps({"type": "FeatureCollection", "features": features}, indent=2, ensure_ascii=False)


def pins_to_kml(pins: Iterable[_ExportablePin]) -> str:
    """Serialize pins as a KML document of Placemark points."""
    from fastkml import kml
    from pygeoif.geometry import Point

    document = kml.Document()
    for pin in pins:
        # geometry is constructor-only (read-only property) in fastkml 1.x.
        placemark = kml.Placemark(
            name=pin.effective_name,
            description=pin.description or "",
            geometry=Point(pin.effective_longitude, pin.effective_latitude),
        )
        document.append(placemark)
    root = kml.KML()
    root.append(document)
    return root.to_string(prettyprint=True)


def pins_to_gpx(pins: Iterable[_ExportablePin]) -> str:
    """Serialize pins as GPX waypoints."""
    import gpxpy
    import gpxpy.gpx

    gpx = gpxpy.gpx.GPX()
    for pin in pins:
        gpx.waypoints.append(
            gpxpy.gpx.GPXWaypoint(
                latitude=pin.effective_latitude,
                longitude=pin.effective_longitude,
                name=pin.effective_name,
                description=pin.description or None,
            ),
        )
    return gpx.to_xml()


def pins_to_csv(pins: Iterable[_ExportablePin]) -> str:
    """Serialize pins as a generic CSV (name, latitude, longitude, description)."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "latitude", "longitude", "description"])
    for pin in pins:
        writer.writerow([csv_text_cell(pin.effective_name), pin.effective_latitude, pin.effective_longitude, csv_text_cell(pin.description)])
    return buf.getvalue()


#: Maps a URL-facing format key to (writer, file extension, MIME type).
EXPORT_FORMATS: dict[str, tuple[Callable[[Iterable[_ExportablePin]], str], str, str]] = {
    "geojson": (pins_to_geojson, "geojson", "application/geo+json"),
    "kml": (pins_to_kml, "kml", "application/vnd.google-earth.kml+xml"),
    "gpx": (pins_to_gpx, "gpx", "application/gpx+xml"),
    "csv": (pins_to_csv, "csv", "text/csv"),
}
