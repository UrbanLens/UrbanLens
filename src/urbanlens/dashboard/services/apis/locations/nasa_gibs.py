"""NASA GIBS (Global Imagery Browse Services) gateway for satellite imagery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider, create_bbox_str
from urbanlens.dashboard.services.gateway import Gateway

if TYPE_CHECKING:
    from collections.abc import Generator

_DEFAULT_YEARS: tuple[int, ...] = (2019, 2016, 2013, 2011)

_WMS_BASE = "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&LAYERS=Landsat_WELD_CorrectedReflectance_TrueColor_Global_Annual&CRS=CRS:84&WIDTH=640&HEIGHT=400&FORMAT=image/jpeg"


@dataclass(slots=True, kw_only=True)
class NasaGibsGateway(SatelliteViewProvider):
    """Gateway for NASA GIBS WMS satellite imagery.

    Provides access to annual Landsat true-colour composites at approximately
    30 m resolution with global coverage.  No API key is required.
    """

    service_key: ClassVar[str] = "nasa_gibs"
    paid_service: ClassVar[bool] = False

    def get_landsat_slides(
        self,
        bbox: str,
        years: tuple[int, ...] = _DEFAULT_YEARS,
    ) -> list[SatelliteSlide]:
        """Return Landsat annual composite slides for the given bounding box.

        Args:
            bbox: Bounding box in ``lng_min,lat_min,lng_max,lat_max`` format (EPSG:4326).
            years: Calendar years to include, newest first.

        Returns:
            List of SatelliteSlide, one per year, in the order given.
            Images are fetched directly by the browser via WMS ``TIME`` parameter.
        """
        return [
            SatelliteSlide(
                img_src=f"{_WMS_BASE}&BBOX={bbox}&TIME={year}",
                source="NASA GIBS / Landsat",
                date=str(year),
                detail="30 m resolution - annual composite",
            )
            for year in years
        ]

    def _generate_satellite_slides(
        self,
        latitude: float,
        longitude: float,
        *,
        zoom: int = 18,
        width: int = 640,
        height: int = 400,
        limit: int = -1,
    ) -> Generator[SatelliteSlide]:
        bbox = create_bbox_str(latitude, longitude)
        yield from self.get_landsat_slides(bbox)
