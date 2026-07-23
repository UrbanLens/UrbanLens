"""Pin export writers: GeoJSON, KML, GPX, and generic CSV.

Mirrors the existing import side (``services/import_formats/``) using the
same libraries those readers already depend on (``fastkml``, ``gpxpy``) -
UL-382. Each writer takes an iterable of ``Pin`` instances (already scoped
to whatever the caller wants exported - a search/filter match or a specific
list, see ``controllers.pin_bulk.PinBulkExportView`` for UL-377's targeted
export) and returns the file content as a string.

Only a pin's name, coordinates, and description are portable across every
one of these formats - richer UrbanLens-specific fields (ratings, security
indicators, labels, ...) stay in the full JSON account export
(``services.export._export_pins``), which remains the source of truth for
a complete round-trippable backup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol


class _ExportablePin(Protocol):
    """The subset of ``Pin`` these writers actually read - lets tests use a
    lightweight stand-in instead of a real, database-backed ``Pin``.

    ``effective_name``/``effective_latitude``/``effective_longitude`` are
    read-only properties on ``Pin``, so they're declared as properties here
    too - a plain attribute in a Protocol is treated as read-write, which a
    read-only property can never satisfy.
    """

    @property
    def effective_name(self) -> str: ...

    @property
    def effective_latitude(self) -> float: ...

    @property
    def effective_longitude(self) -> float: ...

    description: str | None


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


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
        writer.writerow([pin.effective_name, pin.effective_latitude, pin.effective_longitude, pin.description or ""])
    return buf.getvalue()


#: Maps a URL-facing format key to (writer, file extension, MIME type).
EXPORT_FORMATS: dict[str, tuple[Callable[[Iterable[_ExportablePin]], str], str, str]] = {
    "geojson": (pins_to_geojson, "geojson", "application/geo+json"),
    "kml": (pins_to_kml, "kml", "application/vnd.google-earth.kml+xml"),
    "gpx": (pins_to_gpx, "gpx", "application/gpx+xml"),
    "csv": (pins_to_csv, "csv", "text/csv"),
}
