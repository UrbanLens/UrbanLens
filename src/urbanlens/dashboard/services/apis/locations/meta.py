"""Shared data types for the satellite imagery and street-view carousels."""

from __future__ import annotations

from dataclasses import dataclass, field


def create_bbox(latitude: float, longitude: float, delta: float = 0.005) -> str:
    return f"{longitude - delta},{latitude - delta},{longitude + delta},{latitude + delta}"


@dataclass(frozen=True)
class SatelliteSlide:
    """A single slide in the satellite imagery carousel.

    Attributes:
        img_src: Absolute URL or ``data:`` URI for the image.
        source: Human-readable provider name (e.g. ``"Google Maps"``).
        date: Human-readable date or year (e.g. ``"Current"`` or ``"2019"``).
        detail: One-line description of resolution / coverage.
    """

    img_src: str
    source: str
    date: str
    detail: str


@dataclass(frozen=True)
class StreetViewSlide:
    """A single street-level image result.

    Attributes:
        img_src: Absolute URL or ``data:`` URI for the image.
        source: Human-readable provider name (e.g. ``"Google Street View"``).
        date: Human-readable capture date (e.g. ``"2022-06"`` or ``"Unknown"``).
        heading: Camera heading in degrees (0-360, 0 = north). ``None`` if unknown.
        latitude: Actual image capture latitude. ``None`` if unknown.
        longitude: Actual image capture longitude. ``None`` if unknown.
    """

    img_src: str
    source: str
    date: str
    heading: float | None = field(default=None)
    latitude: float | None = field(default=None)
    longitude: float | None = field(default=None)
