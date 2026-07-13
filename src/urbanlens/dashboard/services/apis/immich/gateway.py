"""Immich API gateway.

All calls operate on *one user's own* self-hosted Immich server using the API
key stored on that user's :class:`~urbanlens.dashboard.models.immich.ImmichAccount`
row - there is no site-wide Immich instance. Immich's REST API is documented
at https://immich.app/docs/api/ and, unlike Google Photos, returns raw GPS
coordinates per asset (``GET /api/map/markers``), which is what makes
"photos near this pin" possible at all.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.gateway import Gateway, GatewayRequestError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from urbanlens.dashboard.models.immich.model import ImmichAccount

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30
_DEFAULT_RECENT_LIMIT = 100
_DEFAULT_PAGE_SIZE = 1000
#: Runaway-loop guard for ``iter_library_assets`` - 500 pages at the default
#: page size covers a 500k-asset library, far beyond any real Immich install.
_MAX_LIBRARY_PAGES = 500


@dataclass(frozen=True, slots=True)
class MapMarker:
    """One geolocated asset returned by ``GET /api/map/markers``."""

    id: str
    lat: float
    lon: float
    city: str | None = None


@dataclass(frozen=True, slots=True)
class SearchAsset:
    """One asset returned by ``POST /api/search/metadata``.

    Unlike :class:`MapMarker`, this endpoint doesn't require (or guarantee)
    GPS coordinates - a match found this way isn't necessarily near any
    particular point. ``lat``/``lon``/``city`` come from the same ``exifInfo``
    block ``map/markers`` and ``taken_at`` already draw from, so a single
    paginated sweep of this endpoint (see ``iter_library_assets``) is enough
    to recover location + capture date together without a second API call
    per asset.
    """

    id: str
    taken_at: datetime.datetime | None = None
    lat: float | None = None
    lon: float | None = None
    city: str | None = None


@dataclass(slots=True, kw_only=True)
class ImmichGateway(Gateway):
    """REST client for one user's Immich server.

    Attributes:
        account: The user's stored Immich server URL and API key.
    """

    service_key: ClassVar[str] = "immich"
    paid_service: ClassVar[bool] = False

    account: ImmichAccount

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)

    @property
    def _base_url(self) -> str:
        return f"{self.account.server_url.rstrip('/')}/api"

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.account.api_key, "Accept": "application/json"}

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """Perform an authenticated GET and return the decoded JSON body.

        Args:
            path: API path beginning with ``/`` (e.g. ``/server/ping``).
            params: Optional query parameters.

        Returns:
            The decoded JSON response body.

        Raises:
            GatewayRequestError: On a network error or non-2xx response.
        """
        try:
            response = self.session.get(f"{self._base_url}{path}", params=params, headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach Immich server: {exc}") from exc
        if not response.ok:
            logger.warning("Immich API GET %s failed (%s): %s", path, response.status_code, response.text[:500])
            raise GatewayRequestError(f"Immich API request failed with status {response.status_code}.")
        return response.json()

    def _get_binary(self, path: str, *, params: dict[str, Any] | None = None) -> tuple[bytes, str, str]:
        """Perform an authenticated GET and return the raw response body.

        Args:
            path: API path beginning with ``/``.
            params: Optional query parameters.

        Returns:
            Tuple of (content bytes, content-type, filename derived from the
            response's Content-Disposition header, or the asset id when absent).

        Raises:
            GatewayRequestError: On a network error or non-2xx response.
        """
        try:
            response = self.session.get(f"{self._base_url}{path}", params=params, headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach Immich server: {exc}") from exc
        if not response.ok:
            logger.warning("Immich API GET %s failed (%s): %s", path, response.status_code, response.text[:200])
            raise GatewayRequestError(f"Immich API request failed with status {response.status_code}.")
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        filename = _filename_from_content_disposition(response.headers.get("Content-Disposition"))
        return response.content, content_type, filename

    def _post(self, path: str, *, json: dict[str, Any]) -> Any:
        """Perform an authenticated POST and return the decoded JSON body.

        Args:
            path: API path beginning with ``/`` (e.g. ``/search/metadata``).
            json: The JSON request body.

        Returns:
            The decoded JSON response body.

        Raises:
            GatewayRequestError: On a network error or non-2xx response.
        """
        try:
            response = self.session.post(f"{self._base_url}{path}", json=json, headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach Immich server: {exc}") from exc
        if not response.ok:
            logger.warning("Immich API POST %s failed (%s): %s", path, response.status_code, response.text[:500])
            raise GatewayRequestError(f"Immich API request failed with status {response.status_code}.")
        return response.json()

    def ping(self) -> bool:
        """Verify the stored server URL and API key are valid.

        Returns:
            True when the server responds and the key is accepted.
        """
        try:
            result = self._get("/server/ping")
        except GatewayRequestError:
            return False
        return bool(result)

    def get_map_markers(self, *, is_archived: bool = False) -> list[MapMarker]:
        """Return every geolocated asset in the user's library.

        Args:
            is_archived: When False (default), excludes archived/trashed assets.

        Returns:
            One MapMarker per geolocated asset. Assets without GPS coordinates
            are never returned by this endpoint, so no filtering is needed here.

        Raises:
            GatewayRequestError: On a network error or non-2xx response.
        """
        markers = self._get("/map/markers", params={"isArchived": is_archived})
        return [MapMarker(id=marker["id"], lat=float(marker["lat"]), lon=float(marker["lon"]), city=marker.get("city")) for marker in markers if marker.get("lat") is not None and marker.get("lon") is not None]

    def _search_metadata_page(self, filters: dict[str, Any], *, page: Any = None, size: int = _DEFAULT_RECENT_LIMIT) -> tuple[list[SearchAsset], Any, int]:
        """Run one page of a ``POST /api/search/metadata`` query.

        Args:
            filters: Immich metadata-search filters (e.g. ``takenAfter``/``takenBefore``).
            page: Pagination token from a previous page's response, or None for the first page.
            size: Maximum number of assets to return in this page.

        Returns:
            Tuple of (assets on this page, the token to request the next page - falsy
            when this was the last page, total assets matching the filter).

        Raises:
            GatewayRequestError: On a network error or non-2xx response.
        """
        request: dict[str, Any] = {**filters, "size": size}
        if page is not None:
            request["page"] = page
        body = self._post("/search/metadata", json=request)
        assets_body = body.get("assets", {})
        items = assets_body.get("items", [])
        return [_parse_asset(item) for item in items], assets_body.get("nextPage"), int(assets_body.get("total") or 0)

    def _search_metadata(self, filters: dict[str, Any], *, size: int = _DEFAULT_RECENT_LIMIT) -> list[SearchAsset]:
        """Run one ``POST /api/search/metadata`` query and parse the results.

        Args:
            filters: Immich metadata-search filters (e.g. ``takenAfter``/``takenBefore``).
            size: Maximum number of assets to return (a single page).

        Returns:
            Matching assets, most recently taken first (Immich's default order).

        Raises:
            GatewayRequestError: On a network error or non-2xx response.
        """
        assets, _next_page, _total = self._search_metadata_page(filters, size=size)
        return assets

    def iter_library_assets(self, *, page_size: int = _DEFAULT_PAGE_SIZE) -> Iterator[tuple[list[SearchAsset], int]]:
        """Page through every asset in the user's library, yielding one page at a time.

        Used for a full-library location sweep, where downloading every asset
        would be far too expensive - this only fetches the lightweight metadata
        (id, GPS, capture date, city) already present in the search response.

        Args:
            page_size: Assets requested per page.

        Yields:
            One (assets on this page, total assets in the library) tuple per
            page, in Immich's default (most recent first) order. Stops when
            Immich reports no further page, or after ``_MAX_LIBRARY_PAGES`` as
            a runaway-loop guard.

        Raises:
            GatewayRequestError: On a network error or non-2xx response.
        """
        page: Any = None
        for _ in range(_MAX_LIBRARY_PAGES):
            assets, next_page, total = self._search_metadata_page({}, page=page, size=page_size)
            if assets:
                yield assets, total
            if not next_page:
                return
            page = next_page

    def search_by_dates(self, dates: Sequence[datetime.date]) -> list[SearchAsset]:
        """Return the user's own assets taken on any of the given calendar dates.

        Issues one metadata search per date (Immich's search takes a single
        ``takenAfter``/``takenBefore`` range, not a set of discrete days) and
        merges/dedupes the results - callers should keep ``dates`` short (see
        ``photo_import.MAX_VISIT_DATES``).

        Args:
            dates: Calendar dates to search, in the account's local time.

        Returns:
            Matching assets, deduplicated by id, most recently taken first.

        Raises:
            GatewayRequestError: On a network error or non-2xx response.
        """
        seen: dict[str, SearchAsset] = {}
        for day in dates:
            start = datetime.datetime.combine(day, datetime.time.min, tzinfo=datetime.UTC)
            end = start + datetime.timedelta(days=1)
            for asset in self._search_metadata({"takenAfter": start.isoformat(), "takenBefore": end.isoformat()}):
                seen.setdefault(asset.id, asset)
        return sorted(seen.values(), key=lambda asset: asset.taken_at or datetime.datetime.min.replace(tzinfo=datetime.UTC), reverse=True)

    def list_recent(self, limit: int = _DEFAULT_RECENT_LIMIT) -> list[SearchAsset]:
        """Return the user's most recently taken assets, with no filter applied.

        Args:
            limit: Maximum number of assets to return (a single page).

        Returns:
            Up to ``limit`` assets, most recently taken first.

        Raises:
            GatewayRequestError: On a network error or non-2xx response.
        """
        return self._search_metadata({}, size=limit)

    def get_asset_thumbnail(self, asset_id: str) -> tuple[bytes, str]:
        """Return a preview-sized image for one asset.

        Args:
            asset_id: The Immich asset id.

        Returns:
            Tuple of (image bytes, content-type).

        Raises:
            GatewayRequestError: On a network error or non-2xx response.
        """
        content, content_type, _filename = self._get_binary(f"/assets/{asset_id}/thumbnail", params={"size": "thumbnail"})
        return content, content_type

    def get_asset_original(self, asset_id: str) -> tuple[bytes, str, str]:
        """Return the full-resolution original file for one asset.

        Args:
            asset_id: The Immich asset id.

        Returns:
            Tuple of (file bytes, filename, content-type).

        Raises:
            GatewayRequestError: On a network error or non-2xx response.
        """
        content, content_type, filename = self._get_binary(f"/assets/{asset_id}/original")
        return content, filename, content_type


def _parse_taken_at(item: dict[str, Any]) -> datetime.datetime | None:
    """Extract the capture timestamp from a ``/search/metadata`` result item.

    Args:
        item: One raw asset object from the search response.

    Returns:
        The parsed ``exifInfo.dateTimeOriginal`` (falling back to
        ``fileCreatedAt``), or None when neither is present or parseable.
    """
    value = (item.get("exifInfo") or {}).get("dateTimeOriginal") or item.get("fileCreatedAt")
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_asset(item: dict[str, Any]) -> SearchAsset:
    """Parse one raw asset object from a ``/search/metadata`` result item.

    Args:
        item: One raw asset object from the search response.

    Returns:
        A SearchAsset with GPS/city populated from ``exifInfo`` when present.
    """
    exif_info = item.get("exifInfo") or {}
    lat = exif_info.get("latitude")
    lon = exif_info.get("longitude")
    return SearchAsset(
        id=item["id"],
        taken_at=_parse_taken_at(item),
        lat=float(lat) if lat is not None else None,
        lon=float(lon) if lon is not None else None,
        city=exif_info.get("city"),
    )


def _filename_from_content_disposition(header: str | None) -> str:
    """Extract a filename from a Content-Disposition header, if present.

    Args:
        header: The raw Content-Disposition header value, or None.

    Returns:
        The filename, or "download" when it cannot be determined.
    """
    if not header:
        return "download"
    for raw_part in header.split(";"):
        part = raw_part.strip()
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip('"')
    return "download"
