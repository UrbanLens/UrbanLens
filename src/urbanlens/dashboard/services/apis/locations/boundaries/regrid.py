"""Gateway for Regrid's Parcel API (https://support.regrid.com/api).

Regrid is the one service in this set of four that's a genuine token-authed
REST/JSON API, so it maps directly onto ``Gateway``: every call goes
through ``self.session`` and gets rate limited via ``service_key``.

Regrid parcels are property (tax parcel) boundaries, not building footprints
-- but if the account has "Matched Building Footprints" enabled (an add-on),
every parcel response also includes a ``buildings`` FeatureCollection of the
building footprints tied to that parcel. That's controlled per-request with
``return_matched_buildings`` (default True when the account supports it), so
for the "building + property boundary" use case this single API can answer
both questions at once.

Docs used to write this: https://support.regrid.com/api/parcel-api-endpoints
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.locations.base import BBox, BoundaryProvider, _best_containing_polygon, bbox_to_polygon_geojson, validate_bbox
from urbanlens.dashboard.services.gateway import Gateway, GatewayRequestError

if TYPE_CHECKING:
    from django.contrib.gis.geos import Polygon


def _features_from_payload(payload: dict) -> list[dict]:
    features: list[dict] = []
    for key in ("buildings", "parcels"):
        collection = payload.get(key)
        if isinstance(collection, dict) and isinstance(collection.get("features"), list):
            features.extend(collection["features"])
    if isinstance(payload.get("features"), list):
        features.extend(payload["features"])
    return features


@dataclass(slots=True, kw_only=True)
class RegridGateway(Gateway, BoundaryProvider):
    """Query Regrid's nationwide (US + Canada, plus limited international) parcel data.

    Attributes:
        token: Regrid API token. Falls back to the ``REGRID_TOKEN`` env var.
            Generate one at https://app.regrid.com/account/api.
    """

    service_key: ClassVar[str | None] = "regrid"
    paid_service: ClassVar[bool] = True

    BASE_URL: ClassVar[str] = "https://app.regrid.com"

    token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # NOTE: zero-arg super() breaks here because @dataclass(slots=True)
        # rebuilds the class object, invalidating the implicit __class__ cell.
        # Call the parent explicitly instead.
        Gateway.__post_init__(self)
        if self.token is None:
            object.__setattr__(self, "token", os.environ.get("REGRID_TOKEN"))
        if not self.token:
            raise ValueError(
                "RegridGateway requires an API token: pass token=... or set the "
                "REGRID_TOKEN environment variable. Generate one at "
                "https://app.regrid.com/account/api",
            )

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 json_body: dict[str, Any] | None = None) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        params.setdefault("token", self.token)
        response = self.session.request(
            method, f"{self.BASE_URL}{path}", params=params, json=json_body, timeout=60,
        )
        if response.status_code >= 400:
            raise GatewayRequestError(
                f"Regrid API error {response.status_code} for {path}: {response.text[:500]}",
            )
        return response.json()

    # -- Parcels by identifier ------------------------------------------------

    def get_parcel_by_point(self, lat: float, lon: float, *, radius: float = 0,
                             limit: int = 20, **extra: Any) -> dict:
        """Reverse-geocode a lat/lon to the parcel(s) containing (or near) it."""
        params = {"lat": lat, "lon": lon, "radius": radius, "limit": limit, **extra}
        return self._request("GET", "/api/v2/parcels/point", params=params)

    def get_parcel_by_apn(self, parcelnumb: str, *, path: str | None = None,
                           limit: int = 20, **extra: Any) -> dict:
        """Look up parcel(s) by Assessor's Parcel Number (APN/PIN)."""
        params = {"parcelnumb": parcelnumb, "path": path, "limit": limit, **extra}
        return self._request("GET", "/api/v2/parcels/apn", params=params)

    def get_parcel_by_address(self, query: str, *, path: str | None = None,
                               limit: int = 20, **extra: Any) -> dict:
        """Look up parcel(s) by street address (fuzzy-ish, ranked by relevance)."""
        params = {"query": query, "path": path, "limit": limit, **extra}
        return self._request("GET", "/api/v2/parcels/address", params=params)

    def search_by_owner(self, owner: str, *, path: str | None = None,
                         limit: int = 20, **extra: Any) -> dict:
        """Search parcels by owner name, "Last, First" prefix match. US only."""
        if len(owner) < 4:
            raise ValueError("Regrid owner search requires at least 4 characters.")
        params = {"owner": owner, "path": path, "limit": limit, **extra}
        return self._request("GET", "/api/v2/parcels/owner", params=params)

    def get_parcel_by_path(self, path: str, **extra: Any) -> dict:
        """Fetch a single parcel by its canonical Regrid path, e.g. '/us/mi/wayne/detroit/364491'."""
        return self._request("GET", "/api/v2/parcel", params={"path": path, **extra})

    def get_parcel_by_uuid(self, ll_uuid: str, **extra: Any) -> dict:
        """Fetch a single parcel by its stable Regrid UUID (``ll_uuid``)."""
        return self._request("GET", f"/api/v2/parcels/{ll_uuid}", params=extra)

    # -- Geometry / area search -------------------------------------------------

    def get_parcels_by_bbox(self, bbox: BBox, *, limit: int = 1000, **extra: Any) -> dict:
        """Every parcel intersecting a bounding box (US limit: ~80 sq mi per request)."""
        validate_bbox(bbox)
        return self.query_by_geometry(bbox_to_polygon_geojson(bbox), limit=limit, **extra)

    def query_by_geometry(self, geojson: dict, *, radius: float = 0,
                           limit: int = 1000, **extra: Any) -> dict:
        """Parcel search over an arbitrary GeoJSON geometry (Polygon, MultiPolygon, ...).

        Response includes an ``area`` block (acres/sq_meters/sq_miles) for the
        searched geometry alongside the matched ``parcels`` FeatureCollection.
        """
        body = {"token": self.token, "geojson": geojson, "radius": radius, "limit": limit, **extra}
        response = self.session.post(f"{self.BASE_URL}/api/v2/area", json=body, timeout=90)
        if response.status_code >= 400:
            raise GatewayRequestError(
                f"Regrid area query failed ({response.status_code}): {response.text[:500]}",
            )
        return response.json()

    # -- Nationwide field query ---------------------------------------------

    def query_by_fields(self, fields: dict[str, dict[str, Any]], *, geojson: dict | None = None,
                         limit: int = 20, offset_id: int | None = None, count: bool = False,
                         path: str | None = None, **extra: Any) -> dict:
        """Query parcels nationwide by up to 4 schema fields at once.

        ``fields`` maps field name -> {operator: value}, e.g.::

            gateway.query_by_fields({
                "geoid": {"eq": "06037"},
                "ll_gisacre": {"gt": 2},
            })

        Supported operators: eq, ne, isnull, between, gt, gte, lt, lte, in,
        nin, ilike (text only), order (ASC/DESC).
        """
        params: dict[str, Any] = {
            "limit": limit, "offset_id": offset_id, "count": count, "path": path, **extra,
        }
        for field_name, operators in fields.items():
            for operator, value in operators.items():
                params[f"fields[{field_name}][{operator}]"] = (
                    json.dumps(value) if isinstance(value, (list, tuple)) else value
                )
        if geojson is not None:
            params["geojson"] = json.dumps(geojson)
        return self._request("GET", "/api/v2/parcels/query", params=params)

    # -- Metadata -------------------------------------------------------------

    def get_parcel_schema(self, *, premium_only: bool | None = None) -> dict:
        """Fetch the current Regrid Parcel Schema field definitions."""
        params = {} if premium_only is None else {"premium_only": premium_only}
        return self._request("GET", "/api/v2/us/schemas/parcel", params=params)

    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        payload = self.get_parcel_by_point(
            latitude,
            longitude,
            radius=0,
            limit=5,
            return_matched_buildings=True,
        )
        return _best_containing_polygon(_features_from_payload(payload), latitude, longitude)
