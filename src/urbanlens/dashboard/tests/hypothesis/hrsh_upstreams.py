"""REData and Overpass for the Hudson River State Hospital campus, answered at the HTTP boundary.

Patching ``requests.Session.request`` rather than gateway methods keeps every gateway's own sharing (the coalesced
parcel lookup, the capability index cache) in play, so a count here is a count of real requests.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import copy
import json
import re
from typing import TYPE_CHECKING, Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import requests

from urbanlens.dashboard.tests.hypothesis.building_fixtures import (
    CAMPUS_LAT,
    CAMPUS_LNG,
    offset,
    parcel_square,
    record,
    rect,
)
from urbanlens.dashboard.tests.hypothesis.redata_helpers import REDATA_TEST_URL

if TYPE_CHECKING:
    from collections.abc import Iterator

REDATA_HOST = urlsplit(REDATA_TEST_URL).hostname
PARCEL_UUID = "0b7e1c52-hrsh-parcel"
OVERPASS_HOSTS = frozenset({"overpass.osm.urbanlens.org", "overpass-api.de", "maps.mail.ru"})

#: Half the parcel's side, in metres.
PARCEL_HALF_SIDE_M = 220.0
MAIN_YEAR = 1871
LAUNDRY_YEAR = 1895
GARAGE_YEAR = 1937
ASSESSOR_YEAR = 1900


def campus_buildings(extra: int = 0) -> list[dict[str, Any]]:
    """REData's reconciled building records for the campus; the campus point is on the main building, off its centre.

    Args:
        extra: Further numbered cottages along the south edge, to show a cost does not grow with the building count.
    """
    buildings = [
        record(
            "cris:02714.000051",
            10,
            20,
            geometry=rect(10, 20, 120, 40),
            name="MAIN/ADMIN",
            building_number="51",
            year_built=MAIN_YEAR,
        ),
        record(
            "osm:way/2",
            -90,
            120,
            geometry=rect(-90, 120, 30, 20),
            name="LAUNDRY",
            building_number="45",
            year_built=LAUNDRY_YEAR,
        ),
        record("cris:02714.000028", 110, -100, geometry=rect(110, -100, 25, 25), name="CATHOLIC CHAPEL"),
        record(
            "cris:02714.000166",
            -150,
            -140,
            geometry=rect(-150, -140, 20, 12),
            name="GARAGE",
            building_number="166",
            year_built=GARAGE_YEAR,
        ),
        record("cris:02714.000999", 0, 600, is_on_property=False, name="Across the road"),
    ]
    for index in range(extra):
        east = -180 + index * 30
        buildings.append(
            record(f"osm:way/{100 + index}", -190, east, geometry=rect(-190, east, 12, 10), name=f"COTTAGE {index + 1}")
        )
    return buildings


#: Buildings on the parcel - every record but the one across the road.
ON_PROPERTY_BUILDINGS = 4


def _cris_rows() -> list[dict[str, Any]]:
    rows = []
    for number, name, north, east in (
        ("51", "BLDG 51/MAIN/ADMIN (1871) - NHL", 10, 20),
        ("45", "BLDG 45/LAUNDRY (1895)", -90, 120),
    ):
        latitude, longitude = offset(north, east)
        rows.append(
            {
                "uuid": f"cris-{number}",
                "provider": "ny_cris",
                "resource_type": "building",
                "scope": "structure",
                "external_id": f"02714.0000{number}",
                "name": name,
                "source_latitude": latitude,
                "source_longitude": longitude,
                "year_built": None,
                "attributes": {"USNName": name, "USNNum": f"02714.0000{number}", "EligibilityDesc": "Listed"},
                "attachments": [],
                "linked_resources": [],
                "linked_from": [],
            }
        )
    return rows


def _nrhp_rows(year_built: int | None = None) -> list[dict[str, Any]]:
    return [
        {
            "uuid": "nrhp-89001166",
            "provider": "nps_nrhp",
            "resource_type": "national_register_listing",
            "scope": "site",
            "external_id": "89001166",
            "name": "Hudson River State Hospital",
            "status": "Listed",
            "year_built": year_built,
            "geometry": parcel_square(PARCEL_HALF_SIDE_M),
            "attributes": {},
        }
    ]


def _envelope(results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    results = results or []
    return {"count": len(results), "complete": True, "results": results, "providers": []}


def _response(url: str, status: int, body: Any) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    response.headers["Content-Type"] = "application/json"
    response.url = url
    response.encoding = "utf-8"
    return response


_UUID_SEGMENT = re.compile(r"/(cultural-resources|parcels)/(?!lookup/|fetch-details/)[^/]+/")


class HrshUpstreams:
    """REData for the campus - and Overpass, when REData is absent - counting every request by endpoint.

    Attributes:
        calls: ``(method, path)`` per request, with resource ids collapsed to ``{id}``.
        prewarm_status: What ``POST locations/prewarm/`` answers: 202, 403 for a key without the scope, or an
            outage's status.
        buildings: The building records ``parcels/{id}/buildings/`` returns.
        osm_main_building: Whether OpenStreetMap maps the main building, so the provider chain's building slot finds
            it before the building list does.
        register_year: The construction year the National Register listing drawn around the campus reports; NPS
            publishes none, but a state or city register can.
    """

    def __init__(
        self,
        *,
        prewarm_status: int = 202,
        buildings: list[dict[str, Any]] | None = None,
        parcel_year: int | None = ASSESSOR_YEAR,
        osm_main_building: bool = True,
        register_year: int | None = None,
    ) -> None:
        self.calls: Counter[tuple[str, str]] = Counter()
        self.osm_main_building = osm_main_building
        self.register_year = register_year
        self.prewarm_status = prewarm_status
        self.buildings = buildings if buildings is not None else campus_buildings()
        self.parcel_year = parcel_year
        self.other_hosts: Counter[str] = Counter()

    @property
    def redata_total(self) -> int:
        """Every REData request so far."""
        return sum(self.calls.values())

    def count(self, path_fragment: str, method: str = "GET") -> int:
        """Requests to paths containing ``path_fragment``."""
        return sum(n for (verb, path), n in self.calls.items() if verb == method and path_fragment in path)

    @contextmanager
    def serving(self) -> Iterator[HrshUpstreams]:
        """Answer every outbound request from this fake while the block runs."""
        fake = self

        def request(_session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
            return fake.answer(method.upper(), str(url), kwargs)

        with mock.patch.object(requests.Session, "request", request):
            yield self

    def answer(self, method: str, url: str, kwargs: dict[str, Any]) -> requests.Response:
        """The response to one request."""
        parts = urlsplit(url)
        host = parts.hostname or ""
        if host == REDATA_HOST:
            path = _UUID_SEGMENT.sub(lambda match: f"/{match.group(1)}/{{id}}/", parts.path)
            self.calls[(method, path)] += 1
            params = {
                **parse_qs(parts.query),
                **{
                    key: value if isinstance(value, list) else [value]
                    for key, value in (kwargs.get("params") or {}).items()
                },
            }
            status, body = self.redata(method, path, params)
            return _response(url, status, body)
        self.other_hosts[host] += 1
        if host in OVERPASS_HOSTS:
            data = kwargs.get("data") or {}
            return _response(url, 200, self.overpass(str(data.get("data") or "")))
        raise requests.ConnectionError(f"{host} is not reachable from the test")

    def redata(self, method: str, path: str, params: dict[str, list[Any]]) -> tuple[int, Any]:
        """REData's answer to one request."""
        if path == "/api/v1/locations/prewarm/":
            if self.prewarm_status != 202:
                return self.prewarm_status, {"detail": "Prewarm refused."}
            return 202, {
                "latitude": CAMPUS_LAT,
                "longitude": CAMPUS_LNG,
                "queued": [{"source": "parcels", "delay_seconds": 0.0}],
                "skipped": [],
                "recently_queued": False,
            }
        if path == "/api/v1/parcels/lookup/":
            payload: dict[str, Any] = {
                "apn": "6163-03-011149-0000",
                "situs_address": "3532 North Rd",
                "owner_name": ["NYS OFFICE OF GENERAL SERVICES"],
            }
            if self.parcel_year is not None:
                payload["year_built"] = self.parcel_year
            return 200, {
                "uuid": PARCEL_UUID,
                "display_name": "Hudson River State Hospital",
                "parcel_geometry": None,
                "building_geometry": None,
                "record_payload": payload,
            }
        if path == "/api/v1/parcels/{id}/boundaries/":
            return 200, [
                {
                    "kind": "parcel",
                    "source": "county_gis",
                    "is_suggested": True,
                    "confidence": 0.9,
                    "geometry": parcel_square(PARCEL_HALF_SIDE_M),
                }
            ]
        if path == "/api/v1/parcels/{id}/buildings/":
            return 200, copy.deepcopy(self.buildings)
        if path == "/api/v1/parcels/{id}/coverage/":
            return 200, {}
        if path == "/api/v1/capabilities/":
            return 200, {
                "domains": [{"tag": "cultural_resources", "applicable_providers": ["nps_nrhp", "ny_cris"]}],
                "text_domains": [],
            }
        if path == "/api/v1/cultural-resources/lookup/":
            providers = {str(value) for value in params.get("provider") or []}
            return 200, _envelope(_cris_rows() if providers == {"ny_cris"} else _nrhp_rows(self.register_year))
        if path == "/api/v1/cultural-resources/fetch-details/":
            return 200, {"queued": 0}
        if path == "/api/v1/cultural-resources/{id}/fetch-detail/":
            return 200, {**_cris_rows()[0], "detail_payload": {}}
        if path == "/api/v1/search/news/":
            return 200, _envelope(
                [
                    {
                        "title": "Hudson River State Hospital redevelopment approved",
                        "url": "https://news.test/hrsh",
                        "domain": "news.test",
                        "seendate": "20260901T000000Z",
                    }
                ]
            )
        if path == "/api/v1/search/web/":
            return 200, _envelope(
                [
                    {
                        "title": "Hudson River State Hospital",
                        "url": "https://photos.test/hrsh",
                        "thumbnail": "https://photos.test/hrsh.jpg",
                        "img_src": "https://photos.test/hrsh.jpg",
                    }
                ]
            )
        if path.startswith("/api/v1/parcels/{id}/"):
            return 200, {"results": []}
        return 200, _envelope()

    def overpass(self, query: str) -> dict[str, Any]:
        """OpenStreetMap around the campus: the hospital grounds and its main building, or the buildings on the grounds."""
        if "poly:" in query:
            return {
                "elements": [
                    _osm_way(1, rect(10, 20, 120, 40), {"building": "hospital", "name": "Main Building"}),
                    _osm_way(2, rect(-90, 120, 30, 20), {"building": "yes"}),
                ]
            }
        grounds = _osm_way(
            10, parcel_square(PARCEL_HALF_SIDE_M), {"amenity": "hospital", "name": "Hudson River State Hospital"}
        )
        if not self.osm_main_building:
            return {"elements": [grounds]}
        return {
            "elements": [grounds, _osm_way(1, rect(10, 20, 120, 40), {"building": "hospital", "name": "Main Building"})]
        }


def _osm_way(osm_id: int, polygon: dict[str, Any], tags: dict[str, str]) -> dict[str, Any]:
    ring = polygon["coordinates"][0]
    latitudes = [lat for _lng, lat in ring]
    longitudes = [lng for lng, _lat in ring]
    return {
        "type": "way",
        "id": osm_id,
        "tags": tags,
        "bounds": {
            "minlat": min(latitudes),
            "minlon": min(longitudes),
            "maxlat": max(latitudes),
            "maxlon": max(longitudes),
        },
        "geometry": [{"lat": lat, "lon": lng} for lng, lat in ring],
    }
