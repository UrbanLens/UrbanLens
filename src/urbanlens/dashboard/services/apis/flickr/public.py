"""Public (unauthenticated) Flickr album import - any user's public photoset by URL.

Distinct from ``gateway.py``'s ``FlickrGateway``, which always signs requests
with *one user's own* OAuth1 token to search that user's own library (the
Settings > Connect Flickr / "Import from Flickr" picker). This module only
ever uses the site's Flickr API key (no OAuth, no stored per-user token) to
read a *public* photoset belonging to *any* Flickr user, given the album's own
public URL - the "Import a Flickr Album" action on pin/wiki Media.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.flickr.oauth import _consumer_credentials
from urbanlens.dashboard.services.gateway import Gateway, GatewayRequestError

logger = logging.getLogger(__name__)

REST_ENDPOINT = "https://api.flickr.com/services/rest/"
_REQUEST_TIMEOUT = 30
MAX_ALBUM_PHOTOS = 100
_ALBUM_URL_RE = re.compile(r"flickr\.com/photos/(?P<user>[^/]+)/(?:albums|sets)/(?P<photoset_id>\d+)", re.IGNORECASE)
_NSID_RE = re.compile(r"\A\d+@N\d{2}\Z")
_EXTRAS = "url_o,url_l,url_c,url_z,owner_name,date_taken"


@dataclass(frozen=True, slots=True)
class FlickrAlbumPhoto:
    """One photo in a public Flickr photoset."""

    id: str
    title: str
    thumbnail_url: str | None
    download_url: str | None
    author: str | None
    taken_at: str | None


@dataclass(frozen=True, slots=True)
class FlickrAlbum:
    """A public Flickr photoset's metadata plus its (capped) photo list."""

    photoset_id: str
    owner_nsid: str
    title: str
    owner_username: str | None
    total: int
    photos: list[FlickrAlbumPhoto]


def photo_web_url(owner_nsid: str, photo_id: str) -> str:
    """Return the Flickr web URL for one photo.

    Used both as the "view on Flickr" attribution link and as the de-dup key
    stored on ``Image.source_url`` - mirrors ``FlickrAccount.photo_web_url``'s
    format for the per-user OAuth import, so both features recognise the same
    photo as "already imported" via the identical URL shape.

    Args:
        owner_nsid: The photo owner's Flickr NSID.
        photo_id: The Flickr photo id.

    Returns:
        The photo's URL in the Flickr web UI.
    """
    return f"https://www.flickr.com/photos/{owner_nsid}/{photo_id}/"


def parse_album_url(url: str) -> tuple[str, str] | None:
    """Extract (user path segment, photoset id) from a Flickr album/photoset URL.

    Accepts both the current ``/albums/<id>`` and legacy ``/sets/<id>`` paths,
    the user segment being either a raw NSID or a custom path-alias username.

    Args:
        url: The URL as pasted by the user.

    Returns:
        (user path segment, photoset id), or None when the URL doesn't match.
    """
    match = _ALBUM_URL_RE.search(url.strip())
    if not match:
        return None
    return match.group("user"), match.group("photoset_id")


@dataclass(slots=True, kw_only=True)
class FlickrPublicGateway(Gateway):
    """Unauthenticated REST client for reading any public Flickr photoset.

    Shares the ``flickr`` rate-limit/usage-tracking service key with the
    per-user :class:`~urbanlens.dashboard.services.apis.flickr.gateway.FlickrGateway`
    - both count against the same site-wide Flickr quota.
    """

    service_key: ClassVar[str] = "flickr"
    paid_service: ClassVar[bool] = False

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)

    def _call(self, method: str, extra_params: dict[str, Any]) -> dict[str, Any]:
        """Call one public ``flickr.*`` REST method and return its decoded JSON body.

        Args:
            method: The Flickr API method name (e.g. ``flickr.photosets.getInfo``).
            extra_params: Additional method-specific parameters.

        Returns:
            The decoded JSON response body.

        Raises:
            FlickrNotConfiguredError: When the site has no Flickr API key.
            GatewayRequestError: On a network error, non-2xx response, or a
                Flickr-level error (``stat != "ok"``) - including a private or
                nonexistent album, which Flickr reports as an error here too.
        """
        api_key, _secret = _consumer_credentials()
        params = {"method": method, "format": "json", "nojsoncallback": "1", "api_key": api_key, **extra_params}
        try:
            response = self.session.get(REST_ENDPOINT, params=params, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach Flickr: {exc}") from exc
        if not response.ok:
            logger.warning("Flickr public API %s failed (%s): %s", method, response.status_code, response.text[:500])
            raise GatewayRequestError(f"Flickr API request failed with status {response.status_code}.")
        body = response.json()
        if body.get("stat") != "ok":
            logger.warning("Flickr public API %s returned an error: %s", method, body)
            raise GatewayRequestError(body.get("message") or "That Flickr album could not be found. It may be private.")
        return body

    def _resolve_user_nsid(self, user_path_segment: str) -> str:
        """Resolve a Flickr URL path segment (NSID or path-alias) to a real NSID.

        Args:
            user_path_segment: The path segment right after ``/photos/`` in
                the album URL - either already an NSID, or a custom alias.

        Returns:
            The user's NSID.

        Raises:
            FlickrNotConfiguredError: When the site has no Flickr API key.
            GatewayRequestError: When the alias doesn't resolve to any user.
        """
        if _NSID_RE.fullmatch(user_path_segment):
            return user_path_segment
        body = self._call("flickr.urls.lookupUser", {"url": f"https://www.flickr.com/photos/{user_path_segment}/"})
        return body["user"]["id"]

    def get_album(self, url: str, limit: int = MAX_ALBUM_PHOTOS) -> FlickrAlbum:
        """Resolve a public Flickr album URL into its metadata and photo list.

        Args:
            url: The album/photoset URL as pasted by the user.
            limit: Maximum number of photos to return (this feature caps
                imports at :data:`MAX_ALBUM_PHOTOS` regardless of caller input).

        Returns:
            The album's metadata and up to ``limit`` photos.

        Raises:
            ValueError: When the URL isn't a recognizable Flickr album URL.
            FlickrNotConfiguredError: When the site has no Flickr API key.
            GatewayRequestError: On a network/API error, or when the album is
                private or doesn't exist.
        """
        parsed = parse_album_url(url)
        if parsed is None:
            raise ValueError("That doesn't look like a Flickr album URL.")
        user_segment, photoset_id = parsed
        nsid = self._resolve_user_nsid(user_segment)

        capped_limit = min(limit, MAX_ALBUM_PHOTOS)
        photos_body = self._call(
            "flickr.photosets.getPhotos",
            {"photoset_id": photoset_id, "user_id": nsid, "extras": _EXTRAS, "per_page": str(capped_limit)},
        )
        photoset = photos_body["photoset"]
        owner_username = photoset.get("ownername")
        raw_photos = photoset.get("photo", [])[:capped_limit]
        photos = [
            FlickrAlbumPhoto(
                id=photo["id"],
                title=photo.get("title") or "",
                thumbnail_url=photo.get("url_z") or photo.get("url_c") or photo.get("url_l") or photo.get("url_o"),
                download_url=photo.get("url_o") or photo.get("url_l") or photo.get("url_c") or photo.get("url_z"),
                author=photo.get("ownername") or owner_username,
                taken_at=photo.get("datetaken"),
            )
            for photo in raw_photos
        ]

        info_body = self._call("flickr.photosets.getInfo", {"photoset_id": photoset_id, "user_id": nsid})
        photoset_info = info_body["photoset"]
        title = (photoset_info.get("title") or {}).get("_content") or "Untitled album"

        return FlickrAlbum(
            photoset_id=photoset_id,
            owner_nsid=nsid,
            title=title,
            owner_username=photoset_info.get("username") or owner_username,
            total=int(photoset.get("total") or len(photos)),
            photos=photos,
        )

    def download_photo(self, photo: FlickrAlbumPhoto) -> tuple[bytes, str, str]:
        """Download one album photo's file bytes.

        Args:
            photo: The photo to download (from a prior :meth:`get_album` call).

        Returns:
            Tuple of (file bytes, filename, content-type).

        Raises:
            GatewayRequestError: When the photo has no downloadable URL, or
                the download fails.
        """
        if not photo.download_url:
            raise GatewayRequestError(f"Flickr photo {photo.id} has no downloadable size available.")
        try:
            response = self.session.get(photo.download_url, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise GatewayRequestError(f"Could not download Flickr photo {photo.id}: {exc}") from exc
        if not response.ok:
            raise GatewayRequestError(f"Downloading Flickr photo {photo.id} failed with status {response.status_code}.")
        filename = photo.download_url.rsplit("/", 1)[-1] or f"{photo.id}.jpg"
        content_type = response.headers.get("Content-Type", "image/jpeg")
        return response.content, filename, content_type
