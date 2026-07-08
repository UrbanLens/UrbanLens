from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import ClassVar

from urbanlens.dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)


@dataclass(slots=True, kw_only=True)
class NPSMapGateway(Gateway):
    service_key: ClassVar[str] = "nps"
    paid_service: ClassVar[bool] = False

    base_url: str = "https://mapservices.nps.gov/arcgis/rest/services/ParkBoundaries/FeatureServer/0/query"

    def check_coordinates_within_park(self, latitude: float, longitude: float) -> str | None:
        """Return the park code of the NPS unit whose boundary contains the point.

        Resolves containment server-side with a point-in-polygon query against
        the NPS ParkBoundaries feature service, so only the containing unit (if
        any) comes back rather than the entire boundary dataset. The returned
        code is lower-cased to match the developer API's ``parkCode``
        (e.g. ArcGIS ``YELL`` -> ``yell``).

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            The lower-cased park code of the containing unit, or ``None`` when
            the point falls outside every NPS boundary.
        """
        params = {
            "geometry": f"{longitude},{latitude}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "where": "1=1",
            "outFields": "UNIT_CODE,UNIT_NAME",
            "returnGeometry": "false",
            "f": "json",
        }

        response = self.session.get(self.base_url, params=params, timeout=60)
        response.raise_for_status()

        for feature in response.json().get("features", []):
            attributes = feature.get("attributes") or {}
            code = attributes.get("UNIT_CODE") or attributes.get("unit_code") or attributes.get("parkCode")
            if code:
                return str(code).strip().lower()

        return None
