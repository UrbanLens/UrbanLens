"""Flickr API gateway.

All calls operate on *one user's own* Flickr account using the OAuth 1.0a
token pair stored on that user's
:class:`~urbanlens.dashboard.models.flickr.FlickrAccount` row - there is no
site-wide Flickr account. Unlike Immich, Flickr's ``flickr.photos.search``
filters by geo radius **server-side** (``lat``/``lon``/``radius``), so no
local haversine post-filtering is needed here.

Each request is signed per-call with :class:`requests_oauthlib.OAuth1` (a
``requests`` ``auth=`` callable) rather than routing through
``OAuth1Session`` as the transport, so ``self.session`` stays the ordinary
rate-limited session every other :class:`Gateway` uses - the OAuth1 flow's
own request/access-token exchange (a one-off handshake, not a per-user data
call) lives separately in ``oauth.py`` and does use ``OAuth1Session`` there,
where its convenience methods are the right tool.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from requests_oauthlib import OAuth1

from urbanlens.dashboard.services.apis.flickr.oauth import FlickrNotConfiguredError, _consumer_credentials
from urbanlens.dashboard.services.gateway import Gateway, GatewayRequestError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.flickr.model import FlickrAccount

logger = logging.getLogger(__name__)

REST_ENDPOINT = "https://api.flickr.com/services/rest/"
_REQUEST_TIMEOUT = 30
# Flickr's radial geo search requires a "limiting agent" (a scoping filter
# besides lat/lon/radius) or it silently narrows to the last 12 hours of
# uploads. Passing our own user_id already scopes the whole search to one
# person's library, which satisfies that requirement.
_SEARCH_EXTRAS = "url_s,url_o,geo,date_taken"
_DEFAULT_RECENT_LIMIT = 100


@dataclass(frozen=True, slots=True)
class FlickrPhoto:
    """One photo returned by ``flickr.photos.search``."""

    id: str
    thumbnail_url: str | None
    original_url: str | None
    lat: float | None
    lon: float | None


@dataclass(slots=True, kw_only=True)
class FlickrGateway(Gateway):
    """REST client for one user's Flickr account.

    Attributes:
        account: The user's stored Flickr OAuth token pair.
    """

    service_key: ClassVar[str] = "flickr"
    paid_service: ClassVar[bool] = False

    account: FlickrAccount

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)

    def _auth(self) -> OAuth1:
        """Build the per-request OAuth1 signer for this account.

        Returns:
            An ``OAuth1`` auth callable for use as ``requests``' ``auth=``.

        Raises:
            FlickrNotConfiguredError: When the site has no Flickr consumer key/secret.
        """
        api_key, api_secret = _consumer_credentials()
        return OAuth1(
            api_key,
            client_secret=api_secret,
            resource_owner_key=self.account.oauth_token,
            resource_owner_secret=self.account.oauth_token_secret,
        )

    def _call(self, method: str, *, extra_params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call one ``flickr.*`` REST method and return its decoded JSON body.

        Args:
            method: The Flickr API method name (e.g. ``flickr.photos.search``).
            extra_params: Additional method-specific parameters.

        Returns:
            The decoded JSON response body.

        Raises:
            FlickrNotConfiguredError: When the site has no Flickr consumer key/secret.
            GatewayRequestError: On a network error, non-2xx response, or a
                Flickr-level error (``stat != "ok"``).
        """
        params = {"method": method, "format": "json", "nojsoncallback": "1", **(extra_params or {})}
        try:
            response = self.session.get(REST_ENDPOINT, params=params, auth=self._auth(), timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach Flickr: {exc}") from exc
        if not response.ok:
            logger.warning("Flickr API %s failed (%s): %s", method, response.status_code, response.text[:500])
            raise GatewayRequestError(f"Flickr API request failed with status {response.status_code}.")
        body = response.json()
        if body.get("stat") != "ok":
            logger.warning("Flickr API %s returned an error: %s", method, body)
            raise GatewayRequestError(body.get("message") or "Flickr API request failed.")
        return body

    def whoami(self) -> tuple[str, str | None]:
        """Verify the stored token and return the connected Flickr user's identity.

        Returns:
            Tuple of (user NSID, username or None).

        Raises:
            FlickrNotConfiguredError: When the site has no Flickr consumer key/secret.
            GatewayRequestError: When the token is invalid or the call fails.
        """
        body = self._call("flickr.test.login")
        user = body["user"]
        username = user.get("username", {}).get("_content")
        return user["id"], username

    def _search(self, extra_params: dict[str, Any]) -> list[FlickrPhoto]:
        """Run one ``flickr.photos.search`` query scoped to the connected user.

        Args:
            extra_params: Method-specific search parameters (geo radius, date
                range, or sort order) layered onto the shared defaults below.

        Returns:
            Matching photos, each with thumbnail/original URLs and coordinates
            when Flickr reports them.

        Raises:
            FlickrNotConfiguredError: When the site has no Flickr consumer key/secret.
            GatewayRequestError: On a network error or non-2xx/error response.
        """
        body = self._call(
            "flickr.photos.search",
            extra_params={"user_id": "me", "extras": _SEARCH_EXTRAS, "per_page": "250", **extra_params},
        )
        photos = body.get("photos", {}).get("photo", [])
        return [
            FlickrPhoto(
                id=photo["id"],
                thumbnail_url=photo.get("url_s"),
                original_url=photo.get("url_o"),
                lat=float(photo["latitude"]) if photo.get("latitude") not in (None, "0") else None,
                lon=float(photo["longitude"]) if photo.get("longitude") not in (None, "0") else None,
            )
            for photo in photos
        ]

    def search_near(self, lat: float, lon: float, radius_km: float) -> list[FlickrPhoto]:
        """Search the connected user's own photos within a radius of a point.

        Args:
            lat: Latitude of the search center.
            lon: Longitude of the search center.
            radius_km: Search radius in kilometers (Flickr caps this at 32km).

        Returns:
            Matching photos, each with thumbnail/original URLs and coordinates
            when Flickr reports them.

        Raises:
            FlickrNotConfiguredError: When the site has no Flickr consumer key/secret.
            GatewayRequestError: On a network error or non-2xx/error response.
        """
        return self._search({"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "radius": f"{min(radius_km, 32):.3f}", "radius_units": "km"})

    def search_by_dates(self, dates: Sequence[datetime.date]) -> list[FlickrPhoto]:
        """Return the user's own photos taken on any of the given calendar dates.

        Issues one search per date (Flickr's ``min_taken_date``/``max_taken_date``
        take a single range, not a set of discrete days) and merges/dedupes the
        results - callers should keep ``dates`` short (see
        ``photo_import.MAX_VISIT_DATES``).

        Args:
            dates: Calendar dates to search, in the account's local time.

        Returns:
            Matching photos, deduplicated by id.

        Raises:
            FlickrNotConfiguredError: When the site has no Flickr consumer key/secret.
            GatewayRequestError: On a network error or non-2xx/error response.
        """
        seen: dict[str, FlickrPhoto] = {}
        for day in dates:
            next_day = day + datetime.timedelta(days=1)
            for photo in self._search({"min_taken_date": day.isoformat(), "max_taken_date": next_day.isoformat()}):
                seen.setdefault(photo.id, photo)
        return list(seen.values())

    def list_recent(self, limit: int = _DEFAULT_RECENT_LIMIT) -> list[FlickrPhoto]:
        """Return the user's most recently taken photos, with no filter applied.

        Args:
            limit: Maximum number of photos to return (Flickr caps a single
                page at 500).

        Returns:
            Up to ``limit`` photos, most recently taken first.

        Raises:
            FlickrNotConfiguredError: When the site has no Flickr consumer key/secret.
            GatewayRequestError: On a network error or non-2xx/error response.
        """
        return self._search({"sort": "date-taken-desc", "per_page": str(min(limit, 500))})

    def get_original(self, photo_id: str, fallback_url: str | None = None) -> tuple[bytes, str, str]:
        """Download a photo's original file.

        Uses ``fallback_url`` (the search result's ``url_o``) when given, to
        avoid an extra API call; falls back to ``flickr.photos.getSizes`` when
        the owner has disabled original downloads and no ``url_o`` was returned.

        Args:
            photo_id: The Flickr photo id.
            fallback_url: The photo's ``url_o`` from a prior search result, if any.

        Returns:
            Tuple of (file bytes, filename, content-type).

        Raises:
            GatewayRequestError: When no downloadable size is available, or
                the download fails.
        """
        url = fallback_url or self._largest_available_url(photo_id)
        try:
            response = self.session.get(url, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise GatewayRequestError(f"Could not download Flickr photo {photo_id}: {exc}") from exc
        if not response.ok:
            raise GatewayRequestError(f"Downloading Flickr photo {photo_id} failed with status {response.status_code}.")
        filename = url.rsplit("/", 1)[-1] or f"{photo_id}.jpg"
        content_type = response.headers.get("Content-Type", "image/jpeg")
        return response.content, filename, content_type

    def _largest_available_url(self, photo_id: str) -> str:
        """Return the largest size URL Flickr will serve for a photo.

        Args:
            photo_id: The Flickr photo id.

        Returns:
            The source URL of the largest available size.

        Raises:
            GatewayRequestError: When Flickr reports no downloadable sizes.
        """
        body = self._call("flickr.photos.getSizes", extra_params={"photo_id": photo_id})
        sizes = body.get("sizes", {}).get("size", [])
        if not sizes:
            raise GatewayRequestError(f"Flickr photo {photo_id} has no downloadable sizes available.")
        return sizes[-1]["source"]
