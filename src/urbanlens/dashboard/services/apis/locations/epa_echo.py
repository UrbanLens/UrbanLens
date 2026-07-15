"""EPA ECHO gateway - free, keyless lookup of regulated facilities near a coordinate.

https://echo.epa.gov/ (Enforcement and Compliance History Online) - the EPA's
public facility search, covering RCRA (hazardous waste), CAA (air), CWA
(water), and other federally-regulated facilities with their current
compliance/violation status. Directly useful "urbex signal" data: industrial
sites with active violations or a defunct-looking compliance history are
exactly the kind of place this project's users are trying to find.

The REST API is a two-step query: ``get_facilities`` runs the spatial search
and returns a ``QueryID``; ``get_qid`` pages through that query's actual rows.
No API key is required for either call.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

import requests

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.redact import redact_coordinate

logger = logging.getLogger(__name__)

_BASE_URL = "https://echodata.epa.gov/echo"


def _normalize_facility(facility: dict[str, Any]) -> dict[str, Any]:
    """Flatten one ECHO facility record into a display-friendly dict."""
    address_parts = [facility.get("FacStreet"), facility.get("FacCity"), facility.get("FacState"), facility.get("FacZip")]
    return {
        "name": facility.get("FacName") or "",
        "address": ", ".join(part for part in address_parts if part),
        "compliance_status": facility.get("FacComplianceStatus") or "",
        "significant_violator": facility.get("FacSNCFlg") == "Y",
        "quarters_with_violation": facility.get("FacQtrsWithNC") or "",
        "inspection_count": facility.get("FacInspectionCount") or "",
        "last_inspection_date": facility.get("FacDateLastInspection") or "",
        "active": facility.get("FacActiveFlag") == "Y",
        "registry_id": facility.get("RegistryID") or "",
    }


@dataclass(slots=True, kw_only=True)
class EpaEchoGateway(Gateway):
    """Gateway for the EPA ECHO facility search REST API. USA only."""

    service_key: ClassVar[str] = "epa_echo"
    paid_service: ClassVar[bool] = False

    def get_nearby_facilities(self, latitude: float, longitude: float, *, radius_miles: float = 1.0, limit: int = 10) -> list[dict[str, Any]]:
        """Return EPA-regulated facilities within a radius of a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_miles: Search radius in miles.
            limit: Maximum number of facilities to return (the underlying
                query may match many more; this only caps the page fetched).

        Returns:
            Normalized facility dicts as ECHO orders them; empty when nothing
            is nearby or either request fails. Order is not guaranteed to be
            distance-sorted - ECHO's default fields include no facility
            longitude to sort by client-side, only latitude.
        """
        try:
            search_params: dict[str, str | float] = {"output": "JSON", "p_lat": latitude, "p_long": longitude, "p_radius": radius_miles}
            search_response = self.session.get(f"{_BASE_URL}/echo_rest_services.get_facilities", params=search_params, timeout=15)
            search_response.raise_for_status()
            results = search_response.json().get("Results") or {}
            query_id = results.get("QueryID")
            if not query_id:
                return []

            rows_params: dict[str, str | int] = {"qid": query_id, "output": "JSON", "rows": max(1, min(int(limit), 100))}
            rows_response = self.session.get(f"{_BASE_URL}/echo_rest_services.get_qid", params=rows_params, timeout=15)
            rows_response.raise_for_status()
            facilities = (rows_response.json().get("Results") or {}).get("Facilities") or []
        except requests.exceptions.RequestException:
            logger.warning("EPA ECHO facility search failed for %s, %s", redact_coordinate(latitude), redact_coordinate(longitude), exc_info=True)
            return []

        return [_normalize_facility(facility) for facility in facilities]
