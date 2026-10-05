from __future__ import annotations

from array import array
import base64
import csv
from dataclasses import dataclass, field
import itertools
import logging
import math
import re
import tempfile
import time
from typing import IO, TYPE_CHECKING, Any, ClassVar

from defusedxml.ElementTree import ParseError as XMLParseError, iterparse as iterparse_xml_defused
from django.db import DatabaseError
from fastkml.exceptions import KMLParseError
from gpxpy.gpx import GPXException
from lxml.etree import XMLSyntaxError
import numpy as np
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
from urbanlens.dashboard.services.core.gateway import GatewayRequestError, is_source_outage
from urbanlens.dashboard.services.core.numbers import LATITUDE_BOUND, LONGITUDE_BOUND, coordinate_or_none
from urbanlens.dashboard.services.core.text_limits import MAX_PIN_DESCRIPTION_LENGTH
from urbanlens.dashboard.services.import_formats.geometry_readers import MAX_NESTING, geojson_nests_too_deep
from urbanlens.dashboard.services.import_formats.heuristics import (
    DEFAULT_LATITUDE_KEYS,
    DEFAULT_LONGITUDE_KEYS,
    normalize_header_key,
    pick_latlon,
    pick_name_and_description,
)
from urbanlens.dashboard.services.import_formats.html_description import extract_image_urls, extract_link_urls, strip_html
from urbanlens.dashboard.services.import_formats.json_stream import iter_geojson_features
from urbanlens.dashboard.services.import_formats.streams import as_stream, iter_decoded, iter_lines
from urbanlens.dashboard.services.labels.style_suggestions import resolve_or_create_styled_label
from urbanlens.dashboard.services.pins.history_import import ImportedHistory
from urbanlens.dashboard.services.sandbox import untrusted_parse
from urbanlens.dashboard.services.security.redact import redact_coordinate, redact_text
from urbanlens.UrbanLens.settings.app import settings

#: Every error that means "this uploaded file is unusable, skip it and carry on", for the preview's per-file guard
#: and the bulk importer's. Several are not ``ValueError``: fastkml's ``KMLParseError``, lxml's ``XMLSyntaxError``
#: (a ``SyntaxError``), and ``csv.Error``, which a cell past the csv module's 128 KiB field limit raises.
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
    csv.Error,
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

#: The longest CSV record read, header included, in characters. ``csv`` builds a record whole, a string per cell, at
#: up to 40 times its size; a real row, even with a 128 KiB WKT cell (``csv``'s own field limit), is far shorter.
MAX_CSV_RECORD_CHARS = 1024 * 1024


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


def _on_the_globe(latitude: Any, longitude: Any) -> tuple[float, float] | None:
    """A coordinate pair as floats when both are finite numbers on the globe, else None.

    JSON cannot carry infinity or NaN, and Python's parser reads them all the same.
    """
    lat = coordinate_or_none(latitude, bound=LATITUDE_BOUND)
    lng = coordinate_or_none(longitude, bound=LONGITUDE_BOUND)
    return (lat, lng) if lat is not None and lng is not None else None


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

    @property
    def previewed(self) -> int:
        """How many pins the lists hold."""
        return sum(len(entry["pins"]) for entry in self.lists)

    def add(self, stem: str, read: _PreviewFile) -> None:
        """Add what one file held, once it has been read without error.

        Args:
            stem: The file's name without its extension, naming its list.
            read: What the file held.
        """
        if read.pins:
            self.lists.append({"stem": stem, "pins": read.pins})
        room = GoogleMapsGateway.MAX_PREVIEW_PINS - len(self.unresolved)
        self.unresolved.extend({**row, "stem": stem} for row in read.unresolved[:room])
        self.history.extend(read.history)


@dataclass
class _PreviewFile:
    """What one file holds for the preview, kept apart until the whole file has been read.

    Attributes:
        pins: Pins in the preview shape.
        unresolved: CSV rows only a lookup can place.
        history: Location History, My Activity and GPS tracks.
    """

    pins: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    history: ImportedHistory = field(default_factory=ImportedHistory)


class StreetViewNotFoundError(ValueError):
    """Google has no Street View panorama within the search radius."""


#: Street View statuses that mean Google couldn't answer for now, rather than that the request was refused.
_STREET_VIEW_TRANSIENT_STATUSES = frozenset({"OVER_QUERY_LIMIT", "UNKNOWN_ERROR"})


class StreetViewStatusError(GatewayRequestError, ValueError):
    """Street View answered with an account or request-level status instead of a panorama.

    Attributes:
        status: The API's ``status``, such as ``OVER_QUERY_LIMIT`` or ``REQUEST_DENIED``.
    """

    def __init__(self, status: str) -> None:
        super().__init__(f"Street View API error: {status}")
        self.status = status

    @property
    def is_outage(self) -> bool:
        """Whether Google couldn't answer for now (an exhausted quota or a server error), not a refusal."""
        return self.status in _STREET_VIEW_TRANSIENT_STATUSES


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

        Yields:
            One slide with a ``data:`` URI image source; none without an API key, or when Google refuses the request.

        Raises:
            requests.RequestException: Google couldn't be asked, or didn't answer.
        """
        try:
            content = self.get_satellite_image_bytes(latitude, longitude)
        except requests.exceptions.RequestException as exc:
            if is_source_outage(exc):
                raise
            logger.warning("Google refused the satellite image for %s, %s: %s", redact_coordinate(latitude), redact_coordinate(longitude), exc)
            return
        if content is None:
            return
        google_b64 = base64.b64encode(content).decode("ascii")

        yield SatelliteSlide(
            img_src=f"data:image/jpeg;base64,{google_b64}",
            source="Google Maps",
            date="Current",
            detail="High resolution - current imagery",
        )

    def get_satellite_image_bytes(self, latitude: float, longitude: float) -> bytes | None:
        """Return the raw JPEG bytes of a Google Maps Static satellite image.

        Args:
            latitude: WGS-84 latitude of the target location.
            longitude: WGS-84 longitude of the target location.

        Returns:
            Raw JPEG bytes, or None when no API key is configured.

        Raises:
            requests.exceptions.RequestException: The request failed.
        """
        if not self.api_key:
            return None
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
        return resp.content

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
            StreetViewNotFoundError: No Street View imagery was found within ``max_radius``.
            StreetViewStatusError: The API answered with an account or request-level status.
            requests.RequestException: The request failed."""
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
                # An account or request-level failure that a wider radius can't fix.
                raise StreetViewStatusError(status)

            radius += radius_increment
            logger.debug("Street view not found at radius %s, increasing to %s", radius - radius_increment, radius)

        raise StreetViewNotFoundError("No Street View imagery found within the maximum search radius.")

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
        """Yield Street View slides for the given latitude and longitude.

        Raises:
            StreetViewStatusError: Google couldn't answer for now, such as an exhausted quota.
            requests.RequestException: The request failed.
        """
        try:
            image_bytes, capture_date, pano_latitude, pano_longitude = self.get_street_view_single(latitude, longitude, radius=int(radius))
        except StreetViewNotFoundError:
            return
        except StreetViewStatusError as exc:
            if exc.is_outage:
                raise
            logger.warning("Google Street View refused the request: %s", exc.status)
            return
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

    def _csv_row_iter(self, file_contents: str | Iterable[str], user_profile: Profile, *, offline: bool = False) -> Generator[dict[str, Any] | None, None, None]:
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
            file_contents: Raw CSV text, or its lines as ``str.splitlines`` splits them.
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
        if isinstance(file_contents, str):
            lines: Iterable[str] = file_contents.lstrip("\ufeff").splitlines()
        else:
            lines = _without_leading_boms(file_contents)
        record = _CsvRecordBudget(lines)
        reader = csv.DictReader(record)
        _ = reader.fieldnames  # The header is read here, so it is a record of its own rather than part of the first row.
        record.used = 0
        for row in reader:
            record.used = 0
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
                    latitude = longitude = None

                if latitude is not None and longitude is not None:
                    yield {"latitude": latitude, "longitude": longitude, "profile": user_profile, **takeout}
                    continue
                # A spreadsheet's own URL column (a website, say) places nothing; its coordinate columns may.
                if pick_latlon(row) is None:
                    logger.warning("Could not resolve coordinates for a Takeout URL")
                    yield None
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
        files: Iterable[tuple[str, bytes | IO[bytes]]],
        user_profile: Profile,
        *,
        scratch: str | None = None,
    ) -> PreviewParse:
        """Parse uploaded files without importing, and without any network request.

        Runs in the sandbox worker, which has no route out: a CSV row only a lookup could
        place is set aside for :meth:`resolve_preview_rows`, and a file that fails to parse
        is recorded rather than reported, because the admin notice sends mail.

        A file is read a chunk at a time and its pins taken only until the preview holds
        :attr:`MAX_PREVIEW_PINS`, so what a file costs does not grow with its size, apart
        from the history it carries, which the confirmed import needs whole.

        Args:
            files: ``(filename, content)`` pairs (archives already expanded), the content as bytes or as a
                seekable binary file, read one at a time: only Shapefile parts are kept, on disk, until the
                rest have been read.
            user_profile: The profile the import is for.
            scratch: A directory on disk for the Shapefile parts; the system's temporary directory when not
                given, which in the sandbox worker is a tmpfs counted as memory.

        Returns:
            The lists - one ``{"stem", "pins"}`` per file, pins with ``name``, ``lat``,
            ``lng``, ``description`` and ``cid`` - the history and tracks the files hold, and
            what a networked process has left to do.
        """
        from urbanlens.dashboard.services.import_export.archive_extractor import validate_content_type
        from urbanlens.dashboard.services.import_formats.shapefile import ShapefileSpool, is_shapefile_part, iter_shapefile_pins

        parse = PreviewParse()
        with tempfile.TemporaryDirectory(prefix="preview-shapefiles-", dir=scratch) as parts:
            # The one kind of file held until the rest are read: a Shapefile is a set of same-stem sidecar files.
            shapefiles = ShapefileSpool(parts)
            for filename, content in files:
                stream = as_stream(content)
                if is_shapefile_part(filename):
                    shapefiles.add(filename, stream)
                    continue
                fmt = validate_content_type(filename, stream)
                if fmt is None:
                    continue
                try:
                    read = self._read_preview_file(fmt, filename, stream, user_profile, room=self.MAX_PREVIEW_PINS - parse.previewed)
                except IMPORT_PARSE_ERRORS as exc:
                    logger.warning("Failed to parse '%s' for preview: %s", filename, exc)
                    parse.failed_formats.append(fmt)
                    continue
                parse.add(_filename_stem(filename), read)
                if parse.previewed >= self.MAX_PREVIEW_PINS:
                    return parse

            for stem, shp_path in shapefiles.bundles():
                try:
                    pins = self._take_preview_pins(iter_shapefile_pins(shp_path, stem, user_profile), user_profile, room=self.MAX_PREVIEW_PINS - parse.previewed)
                except (OSError, ValueError, ShapefileDataSourceError) as exc:
                    logger.warning("Failed to parse shapefile bundle '%s' for preview: %s", stem, exc)
                    parse.failed_formats.append("shapefile")
                    continue
                parse.add(stem, _PreviewFile(pins=pins))
                if parse.previewed >= self.MAX_PREVIEW_PINS:
                    return parse
        return parse

    def _read_preview_file(self, fmt: str, filename: str, stream: IO[bytes], user_profile: Profile, *, room: int) -> _PreviewFile:
        """Read one file of a known format for the preview, taking at most *room* pins.

        Args:
            fmt: What :func:`validate_content_type` made of the file.
            filename: The file's name.
            stream: The file, positioned at its start.
            user_profile: The profile the import is for.
            room: How many more pins the preview may hold.

        Returns:
            What the file holds for the preview.

        Raises:
            One of ``IMPORT_PARSE_ERRORS`` for a file that cannot be read, up to where reading stopped.
        """
        from urbanlens.dashboard.services.import_formats.gpx_tracks import read_gpx
        from urbanlens.dashboard.services.import_formats.osm_xml import iter_osm_xml_pins
        from urbanlens.dashboard.services.import_formats.wkt_wkb import iter_wkb_pins, iter_wkt_pins

        read = _PreviewFile()
        if fmt == "location_history":
            read.history.add_location_history(stream, user_profile, filename)
        elif fmt == "my_activity":
            read.history.add_my_activity(stream)
        elif fmt == "json":
            read.pins = self._take_preview_pins(self.iter_geojson_pins(stream, user_profile), user_profile, room=room)
        elif fmt == "kml":
            read.pins = self._take_preview_pins(self.iter_kml_pins(stream, user_profile), user_profile, room=room)
        elif fmt == "csv":
            self._read_preview_csv(stream, user_profile, read, room=room)
        elif fmt == "gpx":
            gpx = read_gpx(stream, user_profile, filename, max_waypoints=room)
            read.pins = self._take_preview_pins(gpx.waypoints, user_profile, room=room)
            read.history.add_routes(gpx.routes)
        elif fmt == "wkt":
            read.pins = self._take_preview_pins(iter_wkt_pins(stream, user_profile), user_profile, room=room)
        elif fmt == "wkb":
            read.pins = self._take_preview_pins(iter_wkb_pins(stream, user_profile), user_profile, room=room)
        elif fmt == "osm_xml":
            read.pins = self._take_preview_pins(iter_osm_xml_pins(stream, user_profile), user_profile, room=room)
        return read

    def _read_preview_csv(self, stream: IO[bytes], user_profile: Profile, read: _PreviewFile, *, room: int) -> None:
        """Take a CSV's pins until *room* is filled, setting aside the rows only a lookup can place."""
        lines = iter_lines(iter_decoded(stream, "utf-8-sig"), max_line=MAX_CSV_RECORD_CHARS)
        for row in self._csv_row_iter(lines, user_profile, offline=True):
            if len(read.pins) >= room:
                return
            if row is None:
                continue
            if row.get("needs_lookup"):
                # No more are kept than a preview could place: the lookups stop once it is full.
                if len(read.unresolved) < self.MAX_PREVIEW_PINS:
                    read.unresolved.append(row)
                continue
            read.pins.extend(self._iter_preview_pins([row], user_profile))

    def _take_preview_pins(self, raw_pins: Iterable[dict[str, Any] | None], user_profile: Profile, *, room: int) -> list[dict[str, Any]]:
        """The first *room* pins of a parser's output in the preview shape, reading no further."""
        return list(itertools.islice(self._iter_preview_pins(raw_pins, user_profile), room))

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
        return list(GoogleMapsGateway._iter_preview_pins(raw_pins, user_profile))

    @staticmethod
    def _iter_preview_pins(raw_pins: Iterable[dict[str, Any] | None], user_profile: Profile) -> Iterator[dict[str, Any]]:
        """:meth:`_preview_pins`, one pin at a time, reading no more of *raw_pins* than is asked for."""
        for p in raw_pins:
            if p is None:
                continue
            if p.get("latitude") is None or p.get("longitude") is None:
                continue
            coordinates = _on_the_globe(p["latitude"], p["longitude"])
            if coordinates is None:
                logger.debug("Leaving out a place off the globe: (%s, %s)", p["latitude"], p["longitude"])
                continue
            lat, lng = coordinates
            name = (p.get("name") or "")[:255]
            cid = p.get("cid")
            pin: dict[str, Any] = {
                "name": name,
                "lat": lat,
                "lng": lng,
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
            yield pin

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

                    # The client posts these back, so they are checked as the preview checked them (P282).
                    coordinates = _on_the_globe(*cached_coords) if cached_coords else _on_the_globe(pin_dict.get("lat"), pin_dict.get("lng"))
                    if location is None and coordinates is None:
                        pin, created = None, False
                    else:
                        pin, created = _create_pin_from_confirmed(
                            pin_dict,
                            location=location,
                            latitude=coordinates[0] if coordinates else None,
                            longitude=coordinates[1] if coordinates else None,
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
    def takeout_kml_to_dict(self, file_contents: bytes | IO[bytes], user_profile: Profile) -> list[dict[str, Any]]:
        """Read every Placemark with a location out of a KML document, at any depth.

        Args:
            file_contents: The KML, as bytes or a binary file; the encoding is taken from its XML declaration.
            user_profile: Who the pins are for.

        Returns:
            One pin dict per placemark with a readable geometry.

        Raises:
            One of ``IMPORT_PARSE_ERRORS`` for a malformed document, bad coordinates, or a DTD or entity.
        """
        try:
            pins = list(self.iter_kml_pins(file_contents, user_profile))
        except IMPORT_PARSE_ERRORS as e:
            logger.exception("Failed to import pins from KML: %s", e)
            raise
        logger.debug("Converted %s pins from KML file to dicts.", len(pins))
        return pins

    @untrusted_parse("geo.kml")
    def iter_kml_pins(self, file_contents: bytes | IO[bytes], user_profile: Profile) -> Iterator[dict[str, Any]]:
        """:meth:`takeout_kml_to_dict`, one placemark at a time, reading no more of the file than that takes.

        Holds at most one placemark's subtree at a time, and matches elements by local name, so an ``https://`` KML
        namespace some exporters write reads like ``http://``.

        Args:
            file_contents: The KML, as bytes or a binary file; the encoding is taken from its XML declaration.
            user_profile: Who the pins are for.

        Yields:
            One pin dict per placemark with a readable geometry.

        Raises:
            One of ``IMPORT_PARSE_ERRORS`` for a malformed document, bad coordinates, or a DTD or entity, once
            reading reaches it.
        """
        for placemark in _iter_kml_placemarks(as_stream(file_contents)):
            point = _kml_placemark_point(placemark)
            if point is None:
                continue
            yield {
                "latitude": point[1],
                "longitude": point[0],
                "profile": user_profile,
                "name": _kml_child_text(placemark, "name"),
                "description": _kml_child_text(placemark, "description"),
            }

    @staticmethod
    def _geojson_feature_point(geometry: dict[str, Any]) -> tuple[float, float] | None:
        """Return a representative ``(longitude, latitude)`` for any GeoJSON geometry type."""
        geom_type = geometry.get("type")
        if geom_type == "Point":
            coordinates = geometry.get("coordinates")
            # A list of positions where one belongs reads as compact arrays; this branch takes it as lists, as it was.
            coordinates = (coordinates.tolist() if isinstance(coordinates, np.ndarray) else coordinates) or []
            if len(coordinates) < 2:
                return None
            return coordinates[0], coordinates[1]
        if geom_type in {"MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection"}:
            if geojson_nests_too_deep(geometry):
                return None
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

    def geojson_to_dict(self, file_contents: str | bytes | IO[bytes], user_profile: Profile) -> list[dict[str, Any]]:
        """Convert a GeoJSON ``FeatureCollection`` into pin dicts.

        Args:
            file_contents: The GeoJSON, as text, UTF-8 bytes or a binary file.
            user_profile: Who the pins are for.

        Returns:
            One pin dict per feature with a resolvable geometry.

        Raises:
            One of ``IMPORT_PARSE_ERRORS``.
        """
        content = file_contents.encode("utf-8") if isinstance(file_contents, str) else file_contents
        try:
            pins = list(self.iter_geojson_pins(content, user_profile))
        except (KeyError, ValueError) as e:
            logger.exception("Failed to import pins from GeoJSON: %s", e)
            raise
        logger.info("Converted %s pins from GeoJSON file to dicts.", len(pins))
        return pins

    def iter_geojson_pins(self, file_contents: bytes | IO[bytes], user_profile: Profile) -> Iterator[dict[str, Any]]:
        """:meth:`geojson_to_dict`, one feature at a time, reading no more of the file than that takes.

        Args:
            file_contents: The GeoJSON, as UTF-8 bytes or a seekable binary file positioned at its start.
            user_profile: Who the pins are for.

        Yields:
            One pin dict per feature with a resolvable geometry.

        Raises:
            MalformedJSONError: Malformed JSON, once reading reaches it.
            AttributeError: A feature, or its geometry or properties, is not an object.
        """
        for feature in iter_geojson_features(as_stream(file_contents)):
            geometry = feature.get("geometry") or {}
            properties = feature.get("properties") or {}

            point = self._geojson_feature_point(geometry)
            if point is None:
                logger.warning("Skipping a feature whose %r geometry has no point.", geometry.get("type"))
                continue
            longitude, latitude = point

            name, description = self._geojson_name_and_description(properties)
            yield {
                "latitude": latitude,
                "longitude": longitude,
                "profile": user_profile,
                "name": name,
                "description": description,
            }


_KML_TOKEN = re.compile(r"\S+")


class _CsvRecordBudget:
    """Lines for ``csv``, refusing a record past ``MAX_CSV_RECORD_CHARS`` before ``csv`` builds its cells.

    A quoted cell can hold line breaks, so one record can span many short lines. The reader's caller sets ``used``
    back to 0 after each record.
    """

    def __init__(self, lines: Iterable[str]) -> None:
        self._lines = iter(lines)
        self.used = 0

    def __iter__(self) -> _CsvRecordBudget:
        return self

    def __next__(self) -> str:
        line = next(self._lines)
        self.used += len(line)
        if self.used > MAX_CSV_RECORD_CHARS:
            raise csv.Error(f"a CSV record is longer than {MAX_CSV_RECORD_CHARS:,} characters")
        return line


def _without_leading_boms(lines: Iterable[str]) -> Iterator[str]:
    remaining = iter(lines)
    for first in remaining:
        yield first.lstrip("\ufeff")
        break
    yield from remaining


def _kml_local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _iter_kml_placemarks(content: IO[bytes]) -> Iterator[Element]:
    """Yield each Placemark element as it closes, refusing a DTD, entity or external reference.

    Every element outside a placemark is freed as it closes, and a placemark once it has been read, so the tree
    never holds more than one placemark and the elements enclosing it.
    """
    stack: list[Element] = []
    placemarks_open = 0
    for event, element in iterparse_xml_defused(content, events=("start", "end"), forbid_dtd=True):
        is_placemark = _kml_local_name(element.tag) == "Placemark"
        if event == "start":
            stack.append(element)
            placemarks_open += is_placemark
            continue
        stack.pop()
        placemarks_open -= is_placemark
        if is_placemark:
            yield element
        if stack and (is_placemark or not placemarks_open):
            stack[-1].remove(element)


def _kml_child_text(element: Element, name: str) -> str | None:
    for child in element:
        if _kml_local_name(child.tag) == name:
            return child.text.strip() if child.text else child.text
    return None


def _iter_kml_coordinates(element: Element) -> Iterator[tuple[float, float]]:
    """Each ``(longitude, latitude)`` of an element's ``coordinates``, parsed as it is reached.

    One placemark's geometry may be the whole file, so neither its tokens nor its pairs are listed (P95).
    """
    for child in element:
        if _kml_local_name(child.tag) == "coordinates":
            for token in _kml_tuples(child.text or ""):
                parts = token.split(",", 2)
                if len(parts) < 2:
                    raise ValueError(f"A KML coordinate needs a longitude and a latitude: {token[:40]!r}")
                yield float(parts[0]), float(parts[1])
            return


def _kml_tuples(text: str) -> Iterator[str]:
    """Each whitespace-separated tuple of a ``coordinates`` text, whitespace beside a comma taken as part of it.

    The same tuples as removing the whitespace around every comma and then splitting, without a substitution: one
    builds a string for every piece between commas before joining them, about seven times the text (P95).
    """
    pieces: list[str] = []
    for match in _KML_TOKEN.finditer(text):
        token = match.group()
        if pieces and not pieces[-1].endswith(",") and not token.startswith(","):
            yield "".join(pieces)
            pieces.clear()
        pieces.append(token)
    if pieces:
        yield "".join(pieces)


def _kml_first_coordinate(element: Element) -> tuple[float, float] | None:
    """An element's first ``(longitude, latitude)``, once every one of them has parsed."""
    first = None
    for coordinate in _iter_kml_coordinates(element):
        if first is None:
            first = coordinate
    return first


def _kml_coordinate_array(element: Element) -> np.ndarray:
    """An element's coordinates as an ``(n, 2)`` array, at 16 bytes a pair rather than a tuple's hundred."""
    flat = array("d")
    for longitude, latitude in _iter_kml_coordinates(element):
        flat.append(longitude)
        flat.append(latitude)
    return np.frombuffer(flat, dtype=np.float64).reshape(-1, 2)


def _kml_shape(element: Element, depth: int = 0) -> BaseGeometry | None:
    """A KML geometry element as a shape; a ``MultiGeometry`` nested past ``MAX_NESTING`` reads as none."""
    name = _kml_local_name(element.tag)
    if name == "Point":
        first = _kml_first_coordinate(element)
        return ShapelyPoint(first) if first is not None else None
    if name in {"LineString", "LinearRing"}:
        coordinates = _kml_coordinate_array(element)
        return ShapelyLineString(coordinates) if len(coordinates) > 1 else (ShapelyPoint(coordinates[0]) if len(coordinates) else None)
    if name == "Polygon":
        rings: dict[str, list[np.ndarray]] = {"outerBoundaryIs": [], "innerBoundaryIs": []}
        for boundary in element:
            kind = _kml_local_name(boundary.tag)
            if kind in rings:
                rings[kind].extend(_kml_coordinate_array(ring) for ring in boundary if _kml_local_name(ring.tag) == "LinearRing")
        outer = rings["outerBoundaryIs"][0] if rings["outerBoundaryIs"] else None
        return ShapelyPolygon(outer, rings["innerBoundaryIs"]) if outer is not None and len(outer) >= 3 else None
    if name == "MultiGeometry":
        if depth >= MAX_NESTING:
            return None
        parts = [shape for shape in (_kml_shape(child, depth + 1) for child in element) if shape is not None and not shape.is_empty]
        return ShapelyGeometryCollection(parts) if parts else None
    return None


def _kml_placemark_point(placemark: Element) -> tuple[float, float] | None:
    """A placemark's ``(longitude, latitude)``: a point, line or ring's first coordinate, an area's centroid."""
    for child in placemark:
        name = _kml_local_name(child.tag)
        if name in {"Point", "LineString", "LinearRing"}:
            first = _kml_first_coordinate(child)
            if first is None:
                raise ValueError(f"KML {name} has no coordinates")
            return first
        if name in {"Polygon", "MultiGeometry"}:
            shape = _kml_shape(child)
            if shape is None or shape.is_empty:
                return None
            centroid = shape.centroid
            return centroid.x, centroid.y
    return None
