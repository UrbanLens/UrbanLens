"""Shared data types for the satellite imagery and street-view carousels."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Protocol

from django.core.cache import cache

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.dashboard.services.gateway import Gateway

if TYPE_CHECKING:
    from collections.abc import Generator


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


class SatelliteViewProvider(Gateway, ABC):
    @abstractmethod
    def _generate_satellite_slides(self, latitude: float, longitude: float, *, zoom: int = 17, width: int = 640, height: int = 400, limit: int = -1) -> Generator[SatelliteSlide]:
        ...
    
    def get_satellite_slides(self, latitude: float, longitude: float, *, zoom: int = 17, width: int = 640, height: int = 400, limit: int = 5) -> list[SatelliteSlide]:
        cache_key = make_cache_key(f"satellite_view_{self.service_key}", f"{latitude:.5f}", f"{longitude:.5f}")
        if slides := cache.get(cache_key):
            return slides

        slides = []
        for slide in self._generate_satellite_slides(latitude, longitude, zoom=zoom, width=width, height=height):
            slides.append(slide)
            if limit > 0 and len(slides) >= limit:
                break

        cache.set(cache_key, slides, 24 * 3600)
        return slides


class StreetViewProvider(Gateway, ABC):
    @abstractmethod
    def _generate_street_view_slides(self, latitude: float, longitude: float, *, radius: float = 50, limit: int = 5) -> Generator[StreetViewSlide]:
        ...

    def get_street_view_slides(self, latitude: float, longitude: float, *, radius: float = 50, limit: int = 5) -> list[StreetViewSlide]:
        cache_key = make_cache_key(f"street_view_{self.service_key}", f"{latitude:.5f}", f"{longitude:.5f}")
        if slides := cache.get(cache_key):
            return slides

        slides = []
        for slide in self._generate_street_view_slides(latitude, longitude, radius=radius):
            slides.append(slide)
            if limit > 0 and len(slides) >= limit:
                break

        return slides
