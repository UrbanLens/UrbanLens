from __future__ import annotations

import base64
import csv
from dataclasses import dataclass, field
import io
import json
import logging
import math
import re
import time
from typing import TYPE_CHECKING, Any, ClassVar

from defusedxml.ElementTree import ParseError as XMLParseError, iterparse as iterparse_xml_defused
from django.db import DatabaseError
from fastkml.exceptions import KMLParseError
from gpxpy.gpx import GPXException
from lxml.etree import XMLSyntaxError
from pyogrio.errors import DataSourceError as ShapefileDataSourceError
import requests
from shapely.errors import ShapelyError
from shapely.geometry import GeometryCollection as ShapelyGeometryCollection, LineString as ShapelyLineString, Point as ShapelyPoint, Polygon as ShapelyPolygon, shape as shapely_shape

from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location import Location
from urbanlens.dashboard.models.pin import Pin
from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider, StreetViewProvider, StreetViewSlide
from urbanlens.dashboard.services.apis.locations.google.geocoding import CoordinatesNeedNetworkError, GoogleGeocodingGateway
from urbanlens.dashboard.services.apis.locations.google.place_info import GooglePlaceService

# TEMPORARY: legacy CID coordinate repair - remove this import together with the
# blocks it feeds (each marked with a matching TEMPORARY comment below) once
# every user has re-imported. See legacy_cid_coordinate_fix's module docstring.
from urbanlens.dashboard.services.apis.locations.legacy_cid_coordinate_fix import is_legacy_location, preview_needs_legacy_repair, repair_legacy_pin_coordinates, repoint_cid_to_corrected_location
from urbanlens.dashboard.services.core.capacity import CapacityExceededError
from urbanlens.dashboard.services.core.text_limits import MAX_PIN_DESCRIPTION_LENGTH
from urbanlens.dashboard.services.import_formats.heuristics import (
    DEFAULT_LATITUDE_KEYS,
    DEFAULT_LONGITUDE_KEYS,
    normalize_header_key,
    pick_latlon,
    pick_name_and_description,
)
from urbanlens.dashboard.services.import_formats.html_description import extract_image_urls, extract_link_urls, strip_html
from urbanlens.dashboard.services.labels.style_suggestions import resolve_or_create_styled_label
from urbanlens.dashboard.services.pins.history_import import ImportedHistory
from urbanlens.dashboard.services.sandbox import untrusted_parse
from urbanlens.dashboard.services.security.redact import redact_coordinate, redact_text
from urbanlens.UrbanLens.settings.app import settings

#: Every error that means "this uploaded file is unusable, skip it and carry on".
#: Named rather than inlined because two handlers need the same list - the KML parser's own and the
#: bulk importer's per-file guard - and they drifted apart once already: fastkml's ``KMLParseError``
#: (a ``FastKMLError``) and lxml's ``XMLSyntaxError`` (a ``SyntaxError``) are neither ``ValueError``
IMPORT_PARSE_ERRORS: tuple[type[Exception], ...] = (
    UnicodeDecodeError,
    ValueError,
    KeyError,
    AttributeError,
    GPXException,
    ShapelyError,
    XMLParseError,
    KMLParseError,
    XMLSyntaxError,
    TypeError,
)

if TYPE_CHECKING:
    from decimal import Decimal
    from xml.etree.ElementTree import Element

    from shapely.geometry.base import BaseGeometry

    from urbanlens.dashboard.models.profile.model import Profile

_CID_RE = re.compile(r"!1s0x[0-9a-fA-F]+:0x([0-9a-fA-F]+)")

#: Column names (case-insensitive) that hold a Google Maps URL to extract coordinates/CID from.
#: Google Takeout's various per-category CSV exports don't agree on a header name for this:
#: starred/saved-place list exports use "URL", but the Timeline "Parking" export uses "Parking
#: location" - UL-203: every row in Parking.csv silently failed to import (no coordinate column
_TAKEOUT_URL_COLUMN_KEYS: tuple[str, ...] = ("url", "parking location")


def _attach_description_extras(pin: Pin, image_urls: list[str], link_urls: list[str], profile: Profile) -> None:
    """Best-effort: attach a freshly-created pin's extracted image/link URLs.
    Only meant for pins the import just created - an existing pin merged into by ``get_nearby_or_create`` is never touched, matching how its description itself is left alone on a merge.

    Args:
        pin: The newly created pin.
        image_urls: ``<img src="...">`` URLs pulled from its raw description.
        link_urls: ``<a href="...">``/bare URLs pulled from its raw description.
        profile: The importing user - becomes each photo's uploader."""
    from urbanlens.dashboard.models.images.model import ImageSource
    from urbanlens.dashboard.models.links.model import MAX_LINK_URL_LENGTH, PinLink
    from urbanlens.dashboard.services.media.media_materialize import MaterializeError, materialize_media_item
    from urbanlens.dashboard.services.security.link_urls import is_link_url

    for url in link_urls:
        if not is_link_url(url, max_length=MAX_LINK_URL_LENGTH):
            continue
        try:
            PinLink.objects.create(pin=pin, url=url)
        except DatabaseError:
            logger.warning("Skipping malformed link %r extracted from import description for pin %s", url, pin.pk, exc_info=True)

    for url in image_urls:
        try:
            image = materialize_media_item(location=pin.location, profile=profile, source=ImageSource.GOOGLE_MAPS, url=url)
        except MaterializeError as exc:
            logger.warning("Skipping image %r extracted from import description for pin %s: %s", url, pin.pk, exc)
            continue
        if image.pin_id is None:
            image.pin = pin
            image.save(update_fields=["pin", "updated"])


def _create_pin_from_confirmed(
    pin_dict: dict[str, Any],
    *,
    location: Location | None,
    latitude: float | None,
    longitude: float | None,
    user_profile: Profile,
    list_labels: list[Label],
    category_label: Label | None,
    auto_tag: bool,
) -> tuple[Pin | None, bool]:
    """Create (or merge into) a Pin from one confirmed-import pin dict.

    Shared by the synchronous confirm-import loop (``iter_confirmed_import_events``,
    for pins that are already accurate - literal coords, or a cid already
    cached/linked) and the background CID-resolution Celery task
    (``tasks.resolve_deferred_pin_locations``, for pins whose cid needed a live
    lookup) - both reach this once a pin's coordinates are known, so their
    behavior (tagging, category/label application, description extras, cid
    backfill) can never drift apart between the fast and slow paths.


    Args:
        pin_dict: dict with ``name``, ``description`` (raw, not yet stripped/truncated), ``cid``, ``label_ids``.
        location: an existing Location to attach to, when the cid/coords matched one; None to create the pin from bare coordinates.
        latitude: used when ``location`` is None.
        longitude: used when ``location`` is None.
        user_profile: importing profile.
        list_labels: labels from the owning list's ``label_ids``.
        category_label: the list's category label, if ``create_category`` was set.
        auto_tag: whether to enqueue AI category suggestion for a newly-created pin.
        TEMPORARY: When that matches, the existing pin is moved onto the corrected coordinates and returned as ``created=False`` instead of a second pin being created nearby.

    Returns:
        ``(pin, created)`` - ``pin`` is None if creation failed or was skipped."""
    pin_name = (pin_dict.get("name") or "")[:255]
    raw_description = pin_dict.get("description") or ""
    cid = pin_dict.get("cid")
    pin_label_ids = pin_dict.get("label_ids") or []

    image_urls = extract_image_urls(raw_description)
    link_urls = extract_link_urls(raw_description)
    description = strip_html(raw_description)[:MAX_PIN_DESCRIPTION_LENGTH]

    # --- TEMPORARY (legacy CID coordinate repair) ------------------------- A Location created
    # before the CID->coordinate fix may itself be sitting on an S2-decoded guess, so it can't be
    # trusted to place this pin when the caller has already resolved the real coordinates.
    # Drop the match and let `latitude`/`longitude` below find (or create) the right Location
    legacy_cid_location = location if (location is not None and latitude is not None and longitude is not None and is_legacy_location(location)) else None
    if legacy_cid_location is not None:
        location = None
    # end TEMPORARY

    pin_defaults: dict[str, Any] = {"name": pin_name, "description": description}
    lookup_lat: float | Decimal | None
    lookup_lon: float | Decimal | None
    if location:
        pin_defaults["location"] = location
        lookup_lat, lookup_lon = location.latitude, location.longitude
    else:
        pin_defaults["latitude"] = latitude
        pin_defaults["longitude"] = longitude
        lookup_lat, lookup_lon = latitude, longitude

    # If so, move that pin onto its corrected coordinates rather than leaving it stranded and
    # creating a second pin nearby.
    # `lookup_lat`/`lookup_lon` are safe to pass: every caller reaching here has real coordinates (a
    # resolved or cached CID lookup, or literal coordinates from the import file), and the one
    repaired = repair_legacy_pin_coordinates(
        profile=user_profile,
        cid=cid,
        name=pin_name,
        latitude=lookup_lat,
        longitude=lookup_lon,
    )
    # end TEMPORARY

    if repaired is not None:
        pin, created = repaired, False
    else:
        try:
            pin, created = Pin.objects.get_nearby_or_create(
                latitude=lookup_lat,
                longitude=lookup_lon,
                profile=user_profile,
                defaults=pin_defaults,
            )
        except (DatabaseError, ValueError, OSError) as exc:
            logger.warning("Failed to import pin '%s': %s", redact_text(pin_name), exc)
            return None, False

    if not pin:
        return None, False

    if created:
        if image_urls or link_urls:
            _attach_description_extras(pin, image_urls, link_urls, user_profile)
        if auto_tag:
            from urbanlens.dashboard.services.core.bulk_followup import enqueue_follow_on
            from urbanlens.dashboard.services.core.celery import follow_on_queue
            from urbanlens.dashboard.tasks import suggest_pin_categories, suggest_pin_category

            enqueue_follow_on(suggest_pin_category, suggest_pin_categories, pin.pk, queue=follow_on_queue())
    # Fill in a still-blank, non-user-provided name from this later import
    # (UL-207) - get_nearby_or_create's `defaults` are only ever applied
    # when creating a new row, never to an existing one it merges into.
    elif pin_name and not pin.name and not pin.name_is_user_provided:
        pin.name = pin_name
        pin.save(update_fields=["name"])

    if list_labels:
        pin.labels.add(*list_labels)
    if category_label:
        pin.labels.add(category_label)
    if pin_label_ids:
        extra = list(Label.objects.pin_assignable_by(user_profile).filter(id__in=pin_label_ids))
        if extra:
            pin.labels.add(*extra)

    # TEMPORARY: `legacy_cid_location` still holds this cid on GooglePlace (unique), so the
    # corrected Location can't just claim it - repoint_cid_to_corrected_location clears the old
    # row first so by_cid() resolves to the corrected Location for every user going forward.
    if cid and not location and pin.location_id and not pin.location.cid:
        if legacy_cid_location is not None:
            repoint_cid_to_corrected_location(legacy_cid_location, pin.location, cid)
        else:
            # fetch_if_missing=False: never block the import loop on a live
            # Places call per pin.
            GooglePlaceService().set_cid_for_entity(pin.location, cid, fetch_if_missing=False)

    return pin, created


def _notify_pin_import_parse_failure(fmt: str) -> None:
    """Alert the site admin that a pin-import file failed to parse.
    Only the file's detected format and the current time are included - never the filename, contents, or the underlying parse error, since those may reflect user-supplied data.

    Args:
        fmt: The detected file format (e.g. "csv", "kml"), or "shapefile" for a shapefile bundle."""
    from django.utils import timezone

    from urbanlens.dashboard.services.notifications.notifications import NotificationEvent, notify

    notify(
        NotificationEvent.PIN_IMPORT_ERROR,
        subject="Pin import failed to process a file",
        message=(f"A pin import attempt failed to process an uploaded {fmt} file at {timezone.now().isoformat()}. Check the app logs for details."),
    )


def _filename_stem(filename: str) -> str:
    """Return the filename without its extension or directory path.

    Examples:
        "Demolished Structures.csv" -> "Demolished Structures"
        "path/to/Saved Places.json" -> "Saved Places"
    """
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name.strip()


if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Iterator

    from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)


def _google_maps_api_key() -> str:
    """Reads the configured Google Maps API key, if any.
    Deliberately does not raise when unset - most of GoogleMapsGateway's own methods (file-format parsing in particular) never touch the network, and the ones that do (e.g."""
    return settings.google_unrestricted_api_key or ""


@dataclass
class PreviewParse:
    """What parsing a preview's files produced, and what is left for a process with network access.

    Attributes:
        lists: ``{"stem", "pins"}`` per file, pins in the preview shape.
        unresolved: CSV rows only a lookup can place, each carrying its file's ``stem``.
        failed_formats: The format of each file that failed to parse, for the admin notice.
        history: Location History, My Activity and GPS tracks, for the confirmed import.
    """

    lists: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    failed_formats: list[str] = field(default_factory=list)
    history: ImportedHistory = field(default_factory=ImportedHistory)


@dataclass(kw_only=True)
class GoogleMapsGateway(SatelliteViewProvider, StreetViewProvider):
    """Gateway for the Google Maps API."""

    service_key: ClassVar[str] = "google_maps"
    paid_service: ClassVar[bool] = True

    api_key: str = field(
        default_factory=_google_maps_api_key,
    )

    def get_directions(self, origin, destination, mode="driving"):
        """
        Get directions from origin to destination.
        """
        directions_url = "https://maps.googleapis.com/maps/api/directions/json"
        params = {
            "origin": origin,
            "destination": destination,
            "mode": mode,
            "key": self.api_key,
        }
        response = self.session.get(directions_url, params=params)
        response.raise_for_status()
        return response.json()

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
        """Return a server-fetched Google Maps Static satellite image as a SatelliteSlide.
        The image is retrieved server-side (rather than via a browser URL) so that the API key is never exposed to the client.

        Args:
            latitude: WGS-84 latitude of the target location.
            longitude: WGS-84 longitude of the target location.

        Returns:
            SatelliteSlide with a ``data:`` URI image source, or ``None`` when no API key is configured or the request fails.
        """
        if not self.api_key:
            return

        try:
            resp = self.session.get(
                "https://maps.googleapis.com/maps/api/staticmap",
                params={
                    "center": f"{latitude},{longitude}",
                    "zoom": "18",
                    "size": "640x400",
                    "maptype": "satellite",
                    "key": self.api_key,
                },
                timeout=15,
            )
            resp.raise_for_status()
            google_b64 = base64.b64encode(resp.content).decode("ascii")
        except requests.exceptions.RequestException as exc:
            logger.warning("Google satellite image unavailable for %s, %s: %s", redact_coordinate(latitude), redact_coordinate(longitude), exc)
            return

        yield SatelliteSlide(
            img_src=f"data:image/jpeg;base64,{google_b64}",
            source="Google Maps",
            date="Current",
            detail="High resolution - current imagery",
        )

    def get_satellite_image_bytes(self, latitude: float, longitude: float) -> bytes | None:
        """Return the raw JPEG bytes of a Google Maps Static satellite image, or None if unavailable.

        Args:
            latitude: WGS-84 latitude of the target location.
            longitude: WGS-84 longitude of the target location.

        Returns:
            Raw JPEG bytes, or None when no API key is configured or the request fails.
        """
        slide = next(self._generate_satellite_slides(latitude, longitude), None)
        if slide is None:
            return None
        _prefix, _sep, b64_data = slide.img_src.partition(",")
        return base64.b64decode(b64_data)

    def get_street_view_single(
        self,
        latitude,
        longitude,
        *,
        fov=90,
        pitch=0,
        size="600x300",
        radius=50,
        max_radius=1000,
        radius_increment=50,
    ):
        """Get the closest Street View image to the given latitude and longitude.

        Returns:
            Tuple of ``(image_bytes, capture_date, pano_latitude, pano_longitude)`` - the pano's own coordinates are returned alongside the image (rather than just echoing back the input) since a widened search radius can resolve to a pano some distance from the requested point.

        Raises:
            ValueError: No Street View imagery was found within ``max_radius``, or the API returned a non-recoverable status."""
        street_view_url = "https://maps.googleapis.com/maps/api/streetview/metadata"
        logger.debug("Getting street view for %s, %s", redact_coordinate(latitude), redact_coordinate(longitude))

        while radius <= max_radius:
            params = {
                "location": f"{latitude},{longitude}",
                "fov": fov,
                "pitch": pitch,
                "size": size,
                "radius": radius,
                "key": self.api_key,
            }

            # Checking for metadata first to avoid unnecessary data usage
            metadata_response = self.session.get(street_view_url, params=params)
            metadata_response.raise_for_status()
            metadata = metadata_response.json()

            status = metadata.get("status", "")
            if status == "OK":
                logger.debug("Found street view at radius %s", radius)
                # Keep `radius` in image_params (don't pop it) - metadata may have only found a pano
                # by searching out to the current, possibly-expanded radius.
                # Dropping it here would let the image request re-search with Google's own smaller
                # default radius, miss that same pano, and silently return Google's "Sorry, we have
                image_params = params.copy()
                image_params["heading"] = self.calculate_heading(
                    metadata["location"]["lat"],
                    metadata["location"]["lng"],
                    latitude,
                    longitude,
                )
                image_url = "https://maps.googleapis.com/maps/api/streetview"
                image_response = self.session.get(image_url, params=image_params)
                image_response.raise_for_status()
                # Treat a suspiciously small response as unavailable rather than trusting the 200
                # status alone, so a mismatch the radius fix doesn't catch still degrades to "try
                # another location" instead of showing the placeholder.
                if len(image_response.content) < 2000:
                    radius += radius_increment
                    continue
                return image_response.content, metadata.get("date"), metadata["location"]["lat"], metadata["location"]["lng"]

            if status not in {"ZERO_RESULTS", "NOT_FOUND"}:
                # Anything other than "genuinely no pano here yet" (e.g. OVER_QUERY_LIMIT,
                # REQUEST_DENIED, INVALID_REQUEST, UNKNOWN_ERROR) is an account/request-level
                # failure a wider radius can never fix - looping through the whole radius range
                # would just repeat the identical failure up to (max_radius -
                raise ValueError(f"Street View API error: {status}")

            radius += radius_increment
            logger.debug("Street view not found at radius %s, increasing to %s", radius - radius_increment, radius)

        raise ValueError("No Street View imagery found within the maximum search radius.")

    def _street_view_slide(self, image_bytes: bytes, capture_date: str, pano_latitude: float, pano_longitude: float) -> StreetViewSlide:
        """Return a StreetViewSlide from the given image bytes, capture date, and the pano's actual coordinates."""
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        return StreetViewSlide(
            img_src=f"data:image/jpeg;base64,{image_b64}",
            source="Google Street View",
            date=capture_date or "Unknown",
            latitude=pano_latitude,
            longitude=pano_longitude,
        )

    def _generate_street_view_slides(self, latitude: float, longitude: float, *, radius: float = 50, limit: int = 5) -> Generator[StreetViewSlide]:
        """Yield Street View slides for the given latitude and longitude."""
        image_bytes, capture_date, pano_latitude, pano_longitude = self.get_street_view_single(latitude, longitude, radius=int(radius))
        yield self._street_view_slide(image_bytes, capture_date, pano_latitude, pano_longitude)

    def calculate_heading(self, lat1, lng1, lat2, lng2):
        """
        Calculate the heading from the first coordinate (lat1, lng1) to the second coordinate (lat2, lng2).
        """
        lat1 = math.radians(lat1)
        lng1 = math.radians(lng1)
        lat2 = math.radians(lat2)
        lng2 = math.radians(lng2)
        diff_lng = lng2 - lng1
        x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(diff_lng)
        y = math.sin(diff_lng) * math.cos(lat2)
        heading = math.degrees(math.atan2(y, x))
        return (heading + 360) % 360

    def _csv_row_iter(self, file_contents: str, user_profile: Profile, *, offline: bool = False) -> Generator[dict[str, Any] | None, None, None]:
        """Generator yielding one pin_data dict per CSV row.

        Supports two CSV shapes:

        - Google Takeout exports, identified by a ``URL`` column: coordinates and
          the Google CID are extracted from the Maps URL, with ``Title``/``Note``/
          ``Comment`` columns used for the name and description.
        - Generic spreadsheet exports (Airtable, Google Sheets, Excel, etc.) that
          have their own latitude/longitude columns: see
          ``import_formats.heuristics.pick_latlon`` and ``pick_name_and_description``
          for the recognised column names.

        Args:
            file_contents: Raw CSV text.
            user_profile: The profile to associate with each pin.
            offline: Make no network request. A Takeout row only a lookup could place is
                yielded with ``needs_lookup`` set and no coordinates.

        Yields:
            dict with pin fields, or None when a row cannot be resolved to coordinates.
        """
        gateway = GoogleGeocodingGateway()
        # utf-8-sig decode at the call site strips a file-level BOM; also guard
        # here so a BOM left on the first header (Excel "CSV UTF-8") still
        # matches latitude/URL column names.
        reader = csv.DictReader(file_contents.lstrip("\ufeff").splitlines())
        for row in reader:
            lowered_row = {normalize_header_key(k): v for k, v in row.items() if k is not None}
            url = next((lowered_row[key] for key in _TAKEOUT_URL_COLUMN_KEYS if lowered_row.get(key)), "")
            if url:
                cid_match = _CID_RE.search(url)
                takeout = {
                    "name": row.get("Title", "")[:255],
                    "description": (row.get("Note", "") + " " + row.get("Comment", "")).strip(),
                    "cid": int(cid_match.group(1), 16) if cid_match else None,
                    # Carried through to a deferred cid lookup (see
                    # cid_resolution.resolve_cids) - REData resolves faster and
                    # more reliably from a place's own URL than from cid alone.
                    "maps_url": url,
                    # TEMPORARY (see _preview_pins below): this row's cid came out of the same
                    # !1s0x{s2_cell}:0x{cid} URL segment that extract_coordinates_from_url decodes
                    # via the imprecise _imprecise_guess_s2_cell() first - wrong roughly a third of
                    # the time.
                    "s2_guess": bool(cid_match),
                }
                try:
                    latitude, longitude = gateway.extract_coordinates_from_url(url, offline=offline)
                except CoordinatesNeedNetworkError:
                    yield {**takeout, "needs_lookup": True}
                    continue
                except ValueError as exc:
                    logger.warning("Failed to extract coordinates from a Takeout URL: %s", type(exc).__name__)
                    yield None
                    continue

                if latitude is None or longitude is None:
                    logger.warning("Could not resolve coordinates for a Takeout URL")
                    yield None
                    continue

                yield {"latitude": latitude, "longitude": longitude, "profile": user_profile, **takeout}
                continue

            coords = pick_latlon(row)
            if coords is None:
                if any(v.strip() for v in row.values() if v):
                    logger.warning("Skipping CSV row with no URL or latitude/longitude columns (columns: %s)", sorted(k for k in row if k))
                    yield None
                else:
                    logger.debug("Skipping blank CSV row")
                continue

            latitude, longitude = coords
            # Exclude the matched coordinate columns so they don't also get
            # serialised into the description by the "no description column
            # found" fallback in pick_name_and_description.
            latlon_keys = {*DEFAULT_LATITUDE_KEYS, *DEFAULT_LONGITUDE_KEYS}
            remaining = {k: v for k, v in row.items() if k is not None and normalize_header_key(k) not in latlon_keys}
            name, description = pick_name_and_description(remaining)
            yield {
                "latitude": latitude,
                "longitude": longitude,
                "profile": user_profile,
                "name": name[:255],
                "description": description,
                "cid": None,
            }

    #: Most pins one preview may hold across every file in the upload, and one confirmed import may carry:
    #: the preview's pins reach the dialog as one JSON document.
    MAX_PREVIEW_PINS = 20_000

    def parse_for_preview(
        self,
        files: Iterable[tuple[str, bytes]],
        user_profile: Profile,
    ) -> PreviewParse:
        """Parse uploaded files without importing, and without any network request.

        Runs in the sandbox worker, which has no route out: a CSV row only a lookup could
        place is set aside for :meth:`resolve_preview_rows`, and a file that fails to parse
        is recorded rather than reported, because the admin notice sends mail.

        Args:
            files: ``(filename, raw_bytes)`` pairs (archives already expanded), read one at a time:
                only Shapefile parts are kept until the rest have been read.
            user_profile: The profile the import is for.

        Returns:
            The lists - one ``{"stem", "pins"}`` per file, pins with ``name``, ``lat``,
            ``lng``, ``description`` and ``cid`` - the history and tracks the files hold, and
            what a networked process has left to do.
        """
        from urbanlens.dashboard.services.import_export.archive_extractor import validate_content_type
        from urbanlens.dashboard.services.import_formats.gpx import gpx_to_dict
        from urbanlens.dashboard.services.import_formats.gpx_tracks import gpx_tracks_to_routes
        from urbanlens.dashboard.services.import_formats.osm_xml import osm_xml_to_dict
        from urbanlens.dashboard.services.import_formats.shapefile import extract_shapefile_bundles, is_shapefile_part, shapefile_to_dict
        from urbanlens.dashboard.services.import_formats.wkt_wkb import wkb_to_dict, wkt_to_dict

        parse = PreviewParse()
        previewed = 0
        # The one kind of file held until the rest are read: a Shapefile is a set of same-stem sidecar files.
        shapefile_parts: list[tuple[str, bytes]] = []

        for filename, raw_bytes in files:
            if is_shapefile_part(filename):
                shapefile_parts.append((filename, raw_bytes))
                continue
            fmt = validate_content_type(filename, raw_bytes)
            if fmt is None:
                continue

            stem = _filename_stem(filename)
            try:
                if fmt == "location_history":
                    parse.history.add_location_history(raw_bytes, user_profile, filename)
                    continue
                if fmt == "my_activity":
                    parse.history.add_my_activity(raw_bytes)
                    continue
                if fmt == "json":
                    raw_pins = self.geojson_to_dict(raw_bytes.decode("utf-8"), user_profile)
                elif fmt == "kml":
                    raw_pins = self.takeout_kml_to_dict(raw_bytes, user_profile)
                elif fmt == "csv":
                    rows = [row for row in self._csv_row_iter(raw_bytes.decode("utf-8-sig"), user_profile, offline=True) if row is not None]
                    parse.unresolved.extend({**row, "stem": stem} for row in rows if row.get("needs_lookup"))
                    raw_pins = [row for row in rows if not row.get("needs_lookup")]
                elif fmt == "gpx":
                    raw_pins = gpx_to_dict(raw_bytes, user_profile)
                    parse.history.add_routes(gpx_tracks_to_routes(raw_bytes, user_profile, filename))
                elif fmt == "wkt":
                    raw_pins = wkt_to_dict(raw_bytes, user_profile)
                elif fmt == "wkb":
                    raw_pins = wkb_to_dict(raw_bytes, user_profile)
                elif fmt == "osm_xml":
                    raw_pins = osm_xml_to_dict(raw_bytes, user_profile)
                else:
                    continue
            except IMPORT_PARSE_ERRORS as exc:
                logger.warning("Failed to parse '%s' for preview: %s", filename, exc)
                parse.failed_formats.append(fmt)
                continue

            pins = self._preview_pins(raw_pins, user_profile)[: self.MAX_PREVIEW_PINS - previewed]
            if pins:
                previewed += len(pins)
                parse.lists.append({"stem": stem, "pins": pins})
            if previewed >= self.MAX_PREVIEW_PINS:
                return parse

        shapefile_bundles, _ = extract_shapefile_bundles(shapefile_parts)
        for bundle in shapefile_bundles:
            try:
                raw_pins = shapefile_to_dict(bundle, user_profile)
            except (OSError, ValueError, ShapefileDataSourceError) as exc:
                logger.warning("Failed to parse shapefile bundle '%s' for preview: %s", bundle.stem, exc)
                parse.failed_formats.append("shapefile")
                continue
            pins = self._preview_pins(raw_pins, user_profile)[: self.MAX_PREVIEW_PINS - previewed]
            if pins:
                previewed += len(pins)
                parse.lists.append({"stem": bundle.stem, "pins": pins})
            if previewed >= self.MAX_PREVIEW_PINS:
                return parse
        return parse

    def resolve_preview_rows(self, rows: list[dict[str, Any]], user_profile: Profile, *, room: int, deadline: float | None = None) -> tuple[list[dict[str, Any]], int]:
        """Place the CSV rows :meth:`parse_for_preview` set aside, making the lookups it could not.

        A lookup service that cannot answer - disabled, rate-limited or unreachable - ends the
        pass, because every remaining row would fail the same way. So does reaching ``deadline``.

        Args:
            rows: :attr:`PreviewParse.unresolved`.
            user_profile: The profile the import is for.
            room: How many more pins the preview may hold.
            deadline: A ``time.monotonic()`` value after which no further lookup starts.

        Returns:
            ``{"stem", "pins"}`` for each stem that gained pins, in the order its rows came, and
            how many rows were left unplaced because the lookup was unavailable.
        """
        from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError

        geocoder = GoogleGeocodingGateway()
        placed: dict[str, list[dict[str, Any]]] = {}
        unavailable = 0
        for index, row in enumerate(rows):
            if room <= 0:
                break
            if deadline is not None and time.monotonic() >= deadline:
                unavailable = len(rows) - index
                break
            try:
                latitude, longitude = geocoder.extract_coordinates_from_url(row["maps_url"])
            except (RequestCancelledError, OSError) as exc:
                logger.warning("Location lookups are unavailable for this preview: %s", exc)
                unavailable = len(rows) - index
                break
            except ValueError as exc:
                logger.warning("Failed to extract coordinates from URL %s: %s", row["maps_url"], exc)
                continue
            if latitude is None or longitude is None:
                continue
            raw = {key: value for key, value in row.items() if key not in {"stem", "needs_lookup"}}
            pins = self._preview_pins([{**raw, "latitude": latitude, "longitude": longitude}], user_profile)
            if pins:
                placed.setdefault(row["stem"], []).extend(pins)
                room -= len(pins)
        return [{"stem": stem, "pins": pins} for stem, pins in placed.items()], unavailable

    @staticmethod
    def _preview_pins(raw_pins: Iterable[dict[str, Any] | None], user_profile: Profile) -> list[dict[str, Any]]:
        """Convert internal pin dicts into the serialisable preview shape.

        Args:
            raw_pins: Pin dicts as returned by any of the format parsers (``None``
                entries, e.g. from a failed CSV row, are skipped).
            user_profile: The profile the import is for - used only to flag
                legacy-repair candidates, see the TEMPORARY block below.

        Returns:
            List of dicts with keys ``name``, ``lat``, ``lng``, ``description``, ``cid``, and - on records the TEMPORARY legacy CID repair would apply to, or whose own cid came from the imprecise S2-cell URL guess - ``needs_repair``.
        """
        pins: list[dict[str, Any]] = []
        for p in raw_pins:
            if p is None:
                continue
            lat = p.get("latitude")
            lng = p.get("longitude")
            if lat is None or lng is None:
                continue
            name = (p.get("name") or "")[:255]
            cid = p.get("cid")
            pin: dict[str, Any] = {
                "name": name,
                "lat": float(lat),
                "lng": float(lng),
                # The preview UI never displays this - it's only carried through so the confirm step
                # (which re-uses this exact dict, not a fresh parse of the file) has the real
                # description to save.
                "description": (p.get("description") or "")[:MAX_PIN_DESCRIPTION_LENGTH],
                "cid": cid,
                # Also just carried through to the confirm step - not displayed -
                # so a deferred lookup can pass it to REData. See _csv_row_iter.
                "maps_url": p.get("maps_url"),
            }
            # --- TEMPORARY (legacy CID coordinate repair) ----------------------- This record's
            # (lat, lng) may be the same S2-derived guess that mis-placed one of this profile's own
            # pre-cutoff pins - the client's "already on your map" proximity check would then match
            # that legacy pin and pre-deselect the very record that would fix it.
            if preview_needs_legacy_repair(user_profile, cid=cid, name=name) or p.get("s2_guess"):
                pin["needs_repair"] = True
            # end TEMPORARY
            pins.append(pin)
        return pins

    def iter_confirmed_import_events(
        self,
        confirmed_lists: list[dict[str, Any]],
        user_profile: Profile,
        auto_tag: bool = True,
    ) -> Generator[dict[str, Any], None, None]:
        r"""Import user-confirmed pin selections from the preview step, one event per pin.

        Each ``confirmed_lists`` entry must have:
            - ``stem`` (str): list name used for category creation.
            - ``create_category`` (bool): create a ``kind="category"`` label from *stem*.
            - ``label_ids`` (list[int]): label IDs to apply to every pin in the list.
            - ``pins`` (list[dict]): dicts with ``name``, ``lat``, ``lng``,
              ``description``, ``cid``, ``maps_url`` (the source Google Maps
              URL, when the pin came from one - passed to REData for a more
              reliable deferred lookup), and ``label_ids`` (list[int]) fields.
              Imports never hit external APIs synchronously; each created pin's
              ``Pin`` post_save signal (``models.pin.signals``) does queue a
              background task that creates the Wiki row for its Location
              (see ``tasks.ensure_wiki_for_location``), enriched in the
              background rather than on the import's critical path.

        A pin whose ``cid`` has no existing ``Location`` *and* no cached
        Places lookup is never placed from the client-supplied ``lat``/``lng``
        here - those preview-time coordinates come from a free heuristic
        (decoding the Maps URL's embedded S2 cell) that's wrong roughly a
        third of the time (see ``docs/designs/redata-cid-resolution.md``). Instead
        it's queued and handed off to ``tasks.resolve_deferred_pin_locations``
        once every list has been walked, so it only ever gets placed once its real
        coordinates are known. This keeps this generator itself free of any
        live REData/Google call - every pin it places here came from data
        already on hand (a matched ``Location``, or a cached lookup).

        Yields:
            dict: ``{type: "start", total}``; one ``{type: "progress", current,
            total, percent, created, exists, skipped, outcome, name}`` per pin;
            then ``{type: "complete", total, created, exists, skipped, deferred}``
            and, when pins were queued for background resolution,
            ``{type: "deferred", count}``. ``{type: "error", message}`` ends it
            early.


        Args:
            confirmed_lists: User-confirmed selection from the preview step.
            user_profile: Profile to import pins for.
            auto_tag: Whether to enqueue AI category suggestion for newly-created pins.

        """
        from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway

        total = sum(len(lst.get("pins", [])) for lst in confirmed_lists)
        if total == 0:
            yield {"type": "error", "message": "No pins selected for import."}
            return

        yield {"type": "start", "total": total}

        created_count = 0
        exists_count = 0
        skipped_count = 0
        deferred_count = 0
        current = 0
        # Mirrors confirmed_lists' shape, but only the pins that need a live
        # CID lookup - handed to resolve_deferred_pin_locations once streaming
        # is done. A list only appears here if it has at least one such pin.
        deferred_lists: list[dict[str, Any]] = []

        try:
            for lst in confirmed_lists:
                stem = (lst.get("stem") or "").strip()
                list_label_ids = lst.get("label_ids") or []
                create_category = bool(lst.get("create_category", False))

                list_labels = list(Label.objects.pin_assignable_by(user_profile).filter(id__in=list_label_ids)) if list_label_ids else []

                category_label = None
                if create_category and stem:
                    try:
                        category_label, _ = resolve_or_create_styled_label(user_profile, stem, KIND_CATEGORY)
                    except CapacityExceededError as exc:
                        logger.info("Confirmed import for profile %s: no category %r: %s", user_profile.pk, stem, exc)

                list_deferred_pins: list[dict[str, Any]] = []

                for pin_dict in lst.get("pins", []):
                    current += 1
                    pin_name = (pin_dict.get("name") or "")[:255]
                    cid = pin_dict.get("cid")

                    location = Location.objects.by_cid(cid).first() if cid else None

                    # Drop the match so this pin takes the cached-lookup/deferred path below and
                    # gets placed from a real resolution - which then also repairs whichever legacy
                    # pin of this user's is sitting on the bad Location.
                    # Remove with services.apis.locations.legacy_cid_coordinate_fix.
                    if location is not None and is_legacy_location(location):
                        location = None
                    # end TEMPORARY

                    cached_coords = GoogleGeocodingGateway.get_cached_coordinates_by_cid(cid) if cid and not location else None

                    if cid and not location and cached_coords is None:
                        # Needs a live lookup - queue it rather than trusting
                        # the preview's own (unverified) lat/lng.
                        list_deferred_pins.append(pin_dict)
                        deferred_count += 1
                        percent = min(100, int(current / total * 100)) if total > 0 else 100
                        yield {
                            "type": "progress",
                            "current": current,
                            "total": total,
                            "percent": percent,
                            "created": created_count,
                            "exists": exists_count,
                            "skipped": skipped_count,
                            "outcome": "deferred",
                            "name": pin_name,
                        }
                        continue

                    latitude = cached_coords[0] if cached_coords else pin_dict.get("lat")
                    longitude = cached_coords[1] if cached_coords else pin_dict.get("lng")

                    pin, created = _create_pin_from_confirmed(
                        pin_dict,
                        location=location,
                        latitude=latitude,
                        longitude=longitude,
                        user_profile=user_profile,
                        list_labels=list_labels,
                        category_label=category_label,
                        auto_tag=auto_tag,
                    )

                    if pin:
                        if created:
                            created_count += 1
                            outcome = "created"
                        else:
                            exists_count += 1
                            outcome = "exists"
                    else:
                        skipped_count += 1
                        outcome = "skipped"

                    percent = min(100, int(current / total * 100)) if total > 0 else 100
                    yield {
                        "type": "progress",
                        "current": current,
                        "total": total,
                        "percent": percent,
                        "created": created_count,
                        "exists": exists_count,
                        "skipped": skipped_count,
                        "outcome": outcome,
                        "name": pin_name,
                    }

                if list_deferred_pins:
                    deferred_lists.append(
                        {
                            "stem": stem,
                            "create_category": create_category,
                            "label_ids": list_label_ids,
                            "pins": list_deferred_pins,
                        },
                    )

        except (DatabaseError, OSError, ValueError, RuntimeError) as exc:
            logger.exception("Unexpected error during preview import: %s", exc)
            yield {"type": "error", "message": "Import failed unexpectedly."}
            return

        yield {
            "type": "complete",
            "total": total,
            "created": created_count,
            "exists": exists_count,
            "skipped": skipped_count,
            "deferred": deferred_count,
        }

        if deferred_lists:
            from urbanlens.dashboard.services.core.celery import safely_enqueue_task
            from urbanlens.dashboard.tasks import resolve_deferred_pin_locations

            safely_enqueue_task(resolve_deferred_pin_locations, user_profile.pk, deferred_lists, auto_tag)
            yield {"type": "deferred", "count": deferred_count}

    @untrusted_parse("geo.kml")
    def takeout_kml_to_dict(self, file_contents: bytes, user_profile: Profile) -> list[dict[str, Any]]:
        """Read every Placemark with a location out of a KML document, at any depth, one placemark at a time.

        Streams rather than building the document tree, which cost about fifteen times the file (P95), and matches
        elements by local name, so an ``https://`` KML namespace some exporters write reads like ``http://``.

        Args:
            file_contents: The KML bytes; the encoding is taken from their XML declaration.
            user_profile: Who the pins are for.

        Returns:
            One pin dict per placemark with a readable geometry.

        Raises:
            One of ``IMPORT_PARSE_ERRORS`` for a malformed document, bad coordinates, or a DTD or entity.
        """
        pins: list[dict[str, Any]] = []
        try:
            for placemark in _iter_kml_placemarks(file_contents):
                point = _kml_placemark_point(placemark)
                if point is None:
                    continue
                pins.append(
                    {
                        "latitude": point[1],
                        "longitude": point[0],
                        "profile": user_profile,
                        "name": _kml_child_text(placemark, "name"),
                        "description": _kml_child_text(placemark, "description"),
                    },
                )
            logger.debug("Converted %s pins from KML file to dicts.", len(pins))
        except IMPORT_PARSE_ERRORS as e:
            logger.exception("Failed to import pins from KML: %s", e)
            raise
        return pins

    @staticmethod
    def _geojson_feature_point(geometry: dict[str, Any]) -> tuple[float, float] | None:
        """Return a representative ``(longitude, latitude)`` for any GeoJSON geometry type."""
        geom_type = geometry.get("type")
        if geom_type == "Point":
            coordinates = geometry.get("coordinates") or []
            if len(coordinates) < 2:
                return None
            return coordinates[0], coordinates[1]
        if geom_type in {"MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection"}:
            try:
                shape = shapely_shape(geometry)
            except (ValueError, TypeError, AttributeError):
                return None
            if shape.is_empty:
                return None
            centroid = shape.centroid
            return centroid.x, centroid.y
        return None

    @staticmethod
    def _geojson_name_and_description(properties: dict[str, Any]) -> tuple[str, str]:
        """Guess a pin name/description from a GeoJSON feature's properties.
        Google Takeout's "Saved Places" export uses a fixed ``name``/``description``/ ``address`` property shape, which is tried first so existing Takeout imports are unaffected."""
        takeout_bits = [str(properties[key]).strip() for key in ("description", "address") if properties.get(key)]
        if "name" in properties or takeout_bits or not properties:
            name = str(properties.get("name") or "Unknown Location").strip()
            return name, " ".join(takeout_bits)
        return pick_name_and_description(properties)

    def geojson_to_dict(self, file_contents: str, user_profile: Profile) -> list[dict[str, Any]]:
        """Convert a GeoJSON ``FeatureCollection`` into pin dicts."""
        try:
            json_data = json.loads(file_contents)
            features = json_data.get("features", [])
            pins: list[dict[str, Any]] = []

            for feature in features:
                geometry = feature.get("geometry") or {}
                properties = feature.get("properties") or {}

                point = self._geojson_feature_point(geometry)
                if point is None:
                    logger.warning("Skipping feature with unresolvable geometry: %s", geometry)
                    continue
                longitude, latitude = point

                name, description = self._geojson_name_and_description(properties)
                pins.append(
                    {
                        "latitude": latitude,
                        "longitude": longitude,
                        "profile": user_profile,
                        "name": name,
                        "description": description,
                    },
                )

            logger.info("Converted %s pins from GeoJSON file to dicts.", len(pins))

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.exception("Failed to import pins from GeoJSON: %s", e)
            raise

        return pins


def _kml_local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _iter_kml_placemarks(content: bytes) -> Iterator[Element]:
    """Yield each Placemark element as it closes, then free it, refusing a DTD, entity or external reference."""
    stack: list[Element] = []
    for event, element in iterparse_xml_defused(io.BytesIO(content), events=("start", "end"), forbid_dtd=True):
        if event == "start":
            stack.append(element)
            continue
        stack.pop()
        if _kml_local_name(element.tag) != "Placemark":
            continue
        yield element
        if stack:
            stack[-1].remove(element)


def _kml_child_text(element: Element, name: str) -> str | None:
    for child in element:
        if _kml_local_name(child.tag) == name:
            return child.text
    return None


def _kml_coordinates(element: Element) -> list[tuple[float, float]]:
    text = _kml_child_text(element, "coordinates") or ""
    return [(float(parts[0]), float(parts[1])) for parts in (token.split(",") for token in text.split())]


def _kml_shape(element: Element) -> BaseGeometry | None:
    name = _kml_local_name(element.tag)
    if name == "Point":
        coordinates = _kml_coordinates(element)
        return ShapelyPoint(coordinates[0]) if coordinates else None
    if name in {"LineString", "LinearRing"}:
        coordinates = _kml_coordinates(element)
        return ShapelyLineString(coordinates) if len(coordinates) > 1 else (ShapelyPoint(coordinates[0]) if coordinates else None)
    if name == "Polygon":
        rings: dict[str, list[list[tuple[float, float]]]] = {"outerBoundaryIs": [], "innerBoundaryIs": []}
        for boundary in element:
            kind = _kml_local_name(boundary.tag)
            if kind in rings:
                rings[kind].extend(_kml_coordinates(ring) for ring in boundary if _kml_local_name(ring.tag) == "LinearRing")
        outer = rings["outerBoundaryIs"][0] if rings["outerBoundaryIs"] else []
        return ShapelyPolygon(outer, rings["innerBoundaryIs"]) if len(outer) >= 3 else None
    if name == "MultiGeometry":
        parts = [shape for shape in (_kml_shape(child) for child in element) if shape is not None and not shape.is_empty]
        return ShapelyGeometryCollection(parts) if parts else None
    return None


def _kml_placemark_point(placemark: Element) -> tuple[float, float] | None:
    """A placemark's ``(longitude, latitude)``: a point, line or ring's first coordinate, an area's centroid."""
    for child in placemark:
        name = _kml_local_name(child.tag)
        if name in {"Point", "LineString", "LinearRing"}:
            coordinates = _kml_coordinates(child)
            if not coordinates:
                raise ValueError(f"KML {name} has no coordinates")
            return coordinates[0]
        if name in {"Polygon", "MultiGeometry"}:
            shape = _kml_shape(child)
            if shape is None or shape.is_empty:
                return None
            centroid = shape.centroid
            return centroid.x, centroid.y
    return None
