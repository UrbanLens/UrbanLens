from __future__ import annotations

import base64
import csv
from dataclasses import dataclass, field
import json
import logging
import math
import re
from typing import TYPE_CHECKING, Any, ClassVar

# Only used to catch exceptions
from xml.etree.ElementTree import ParseError as XMLParseError  # nosec B405

from django.core.cache import cache
from django.db import DatabaseError
from fastkml import kml
from gpxpy.gpx import GPXException
from pyogrio.errors import DataSourceError as ShapefileDataSourceError
import requests
from shapely.errors import ShapelyError
from shapely.geometry import shape as shapely_shape

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.dashboard.models.badges.meta import KIND_TAG
from urbanlens.dashboard.models.badges.model import Badge
from urbanlens.dashboard.models.location import Location
from urbanlens.dashboard.models.pin import Pin
from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider, StreetViewProvider, StreetViewSlide
from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway
from urbanlens.dashboard.services.apis.locations.google.place_info import GooglePlaceService
from urbanlens.dashboard.services.badges.style_suggestions import suggest_badge_style
from urbanlens.dashboard.services.import_formats.heuristics import (
    DEFAULT_LATITUDE_KEYS,
    DEFAULT_LONGITUDE_KEYS,
    pick_latlon,
    pick_name_and_description,
)
from urbanlens.dashboard.services.redact import redact_coordinate, redact_text
from urbanlens.UrbanLens.settings.app import settings

_CID_RE = re.compile(r"!1s0x[0-9a-fA-F]+:0x([0-9a-fA-F]+)")


def _notify_pin_import_parse_failure(fmt: str) -> None:
    """Alert the site admin that a pin-import file failed to parse.

    Only the file's detected format and the current time are included - never the
    filename, contents, or the underlying parse error, since those may reflect
    user-supplied data. Admins can consult the app logs for full details.

    Args:
        fmt: The detected file format (e.g. "csv", "kml"), or "shapefile" for a
            shapefile bundle.
    """
    from django.utils import timezone

    from urbanlens.dashboard.services.notifications import NotificationEvent, notify

    notify(
        NotificationEvent.PIN_IMPORT_ERROR,
        subject="Pin import failed to process a file",
        message=(
            f"A pin import attempt failed to process an uploaded {fmt} file at "
            f"{timezone.now().isoformat()}. Check the app logs for details."
        ),
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
    """
    Enforces that the Google Maps API key is set.
    """
    if not settings.google_unrestricted_api_key:
        raise ValueError("Google Maps API key is not set")
    return settings.google_unrestricted_api_key


@dataclass(kw_only=True)
class GoogleMapsGateway(SatelliteViewProvider, StreetViewProvider):
    """
    Gateway for the Google Maps API.

    Defaults to the settings.google_unrestricted_api_key, but the app
    occasionally passes a different api key (e.g. street_view_api_key)
    """

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

        The image is retrieved server-side (rather than via a browser URL) so that the
        API key is never exposed to the client.  The encoded result is cached for 30 days.

        Args:
            latitude: WGS-84 latitude of the target location.
            longitude: WGS-84 longitude of the target location.

        Returns:
            SatelliteSlide with a ``data:`` URI image source, or ``None`` when no API
            key is configured or the request fails.
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
        """
        Get the closest Street View image to the given latitude and longitude.
        """
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
                image_params = params.copy()
                image_params.pop("radius")
                image_params["heading"] = self.calculate_heading(
                    metadata["location"]["lat"],
                    metadata["location"]["lng"],
                    latitude,
                    longitude,
                )
                image_url = "https://maps.googleapis.com/maps/api/streetview"
                image_response = self.session.get(image_url, params=image_params)
                image_response.raise_for_status()
                return image_response.content, metadata.get("date")

            if status in {"REQUEST_DENIED", "INVALID_REQUEST", "UNKNOWN_ERROR"}:
                raise ValueError(f"Street View API error: {status}")

            radius += radius_increment
            logger.debug("Street view not found at radius %s, increasing to %s", radius - radius_increment, radius)

        raise ValueError("No Street View imagery found within the maximum search radius.")

    def _street_view_slide(self, image_bytes: bytes, capture_date: str) -> StreetViewSlide:
        """Return a StreetViewSlide from the given image bytes and capture date."""
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        return StreetViewSlide(
            img_src=f"data:image/jpeg;base64,{image_b64}",
            source="Google Street View",
            date=capture_date or "Unknown",
        )

    def _generate_street_view_slides(self, latitude: float, longitude: float, *, radius: float = 50, limit: int = 5) -> Generator[StreetViewSlide]:
        """Yield Street View slides for the given latitude and longitude."""
        image_bytes, capture_date = self.get_street_view_single(latitude, longitude, radius=int(radius))
        yield self._street_view_slide(image_bytes, capture_date)

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

    def _csv_row_iter(self, file_contents: str, user_profile: Profile) -> Generator[dict[str, Any] | None, None, None]:
        """Generator yielding one pin_data dict per CSV row. Yields None for rows that fail.

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

        Yields:
            dict with pin fields, or None when a row cannot be resolved to coordinates.
        """
        gateway = GoogleGeocodingGateway()
        reader = csv.DictReader(file_contents.splitlines())
        for row in reader:
            url = row.get("URL", "")
            if url:
                try:
                    latitude, longitude = gateway.extract_coordinates_from_url(url)
                except ValueError as exc:
                    logger.warning("Failed to extract coordinates from URL %s: %s", url, exc)
                    yield None
                    continue

                if latitude is None or longitude is None:
                    logger.warning("Could not resolve coordinates for URL: %s", url)
                    yield None
                    continue

                cid_match = _CID_RE.search(url)
                cid = int(cid_match.group(1), 16) if cid_match else None

                yield {
                    "latitude": latitude,
                    "longitude": longitude,
                    "profile": user_profile,
                    "name": row.get("Title", "")[:255],
                    "description": (row.get("Note", "") + " " + row.get("Comment", "")).strip(),
                    "cid": cid,
                }
                continue

            coords = pick_latlon(row)
            if coords is None:
                if any(v.strip() for v in row.values() if v):
                    logger.warning("Skipping CSV row with no URL or latitude/longitude columns: %s", row)
                    yield None
                else:
                    logger.debug("Skipping blank CSV row")
                continue

            latitude, longitude = coords
            # Exclude the matched coordinate columns so they don't also get
            # serialised into the description by the "no description column
            # found" fallback in pick_name_and_description.
            latlon_keys = {*DEFAULT_LATITUDE_KEYS, *DEFAULT_LONGITUDE_KEYS}
            remaining = {k: v for k, v in row.items() if k is not None and k.strip().lower() not in latlon_keys}
            name, description = pick_name_and_description(remaining)
            yield {
                "latitude": latitude,
                "longitude": longitude,
                "profile": user_profile,
                "name": name[:255],
                "description": description,
                "cid": None,
            }

    def import_pins_streaming(
        self,
        files: list[tuple[str, bytes]],
        user_profile: Profile,
        tags: list | None = None,
        tag_by_filename: bool = False,
    ):
        r"""Generator that yields SSE data strings while importing pins from a list of files.

        Each yielded string is a complete SSE event in the format ``data: {...}\\n\\n``.

        Event shapes:

        - ``{type: "start", total: N}``
        - ``{type: "progress", current, total, percent, created, exists, skipped, name}``
        - ``{type: "complete", total, created, exists, skipped}``
        - ``{type: "error", message}``

        Files whose content does not match a supported format are skipped silently.
        A parse failure on one file does not abort the remaining files.

        Args:
            files: List of ``(filename, raw_bytes)`` pairs to import.
                   Archives must already be expanded by the caller.
            user_profile: The profile to associate with imported pins.
            tags: Optional list of Tag objects to apply to every imported pin
                  (both newly created and pre-existing).
            tag_by_filename: When True, each source file that produces at least one
                pin gets a tag created (or reused) from the file's stem name and
                applied to every pin from that file.  Tag lookup is case-insensitive.

        Yields:
            str: SSE-formatted data lines.
        """

        from urbanlens.dashboard.services.apis.locations.google.location_history import (
            detect_location_history_format,
            import_location_history_streaming,
            semantic_history_to_routes,
        )
        from urbanlens.dashboard.services.apis.locations.route_import import import_routes_streaming
        from urbanlens.dashboard.services.archive_extractor import validate_content_type
        from urbanlens.dashboard.services.import_formats.gpx import gpx_to_dict
        from urbanlens.dashboard.services.import_formats.gpx_tracks import ParsedRoute, gpx_tracks_to_routes
        from urbanlens.dashboard.services.import_formats.osm_xml import osm_xml_to_dict
        from urbanlens.dashboard.services.import_formats.shapefile import extract_shapefile_bundles, shapefile_to_dict
        from urbanlens.dashboard.services.import_formats.wkt_wkb import wkb_to_dict, wkt_to_dict

        def sse(data: dict) -> str:
            return f"data: {json.dumps(data)}\n\n"

        # Separate files into pin-data files and location-history files so each
        # category can be reported with an accurate total.
        location_history_files: list[tuple[str, bytes]] = []
        # GPX tracks/routes and Google Takeout activitySegments both produce
        # Route candidates, saved in a separate pass after pins (see below).
        parsed_routes: list[ParsedRoute] = []

        # Shapefiles ship as a set of same-stem sidecar files (.shp/.dbf/.shx/...)
        # rather than one file, so they must be grouped before the per-file loop
        # below (which assumes one format per file).
        shapefile_bundles, files = extract_shapefile_bundles(files)

        # First pass: validate and parse every file so we can report an accurate
        # grand total upfront.  CSV rows are counted by line (cheap); every other
        # format is parsed fully and the results cached for the second pass.
        # Files that fail validation or parsing are skipped with a warning.
        parsed: list[tuple[str, str, Any, int]] = []  # (filename, fmt, data_or_text, file_total)
        grand_total = 0

        for bundle in shapefile_bundles:
            try:
                data_list = shapefile_to_dict(bundle, user_profile)
                parsed.append((f"{bundle.stem}.shp", "shapefile", data_list, len(data_list)))
                grand_total += len(data_list)
            except (OSError, ValueError, ShapefileDataSourceError) as exc:
                logger.warning("Failed to parse shapefile bundle '%s', skipping: %s", bundle.stem, exc)
                _notify_pin_import_parse_failure("shapefile")

        for filename, raw_bytes in files:
            fmt = validate_content_type(filename, raw_bytes)
            if fmt is None:
                logger.info("Skipping unrecognised file during import: %s", filename)
                continue

            if fmt == "location_history":
                location_history_files.append((filename, raw_bytes))
                continue

            try:
                if fmt == "json":
                    data_list = self.geojson_to_dict(raw_bytes.decode("utf-8"), user_profile)
                    parsed.append((filename, fmt, data_list, len(data_list)))
                    grand_total += len(data_list)
                elif fmt == "kml":
                    data_list = self.takeout_kml_to_dict(raw_bytes, user_profile)
                    parsed.append((filename, fmt, data_list, len(data_list)))
                    grand_total += len(data_list)
                elif fmt == "csv":
                    text = raw_bytes.decode("utf-8")
                    file_total = max(0, len(text.splitlines()) - 1)
                    parsed.append((filename, fmt, text, file_total))
                    grand_total += file_total
                elif fmt == "gpx":
                    data_list = gpx_to_dict(raw_bytes, user_profile)
                    parsed.append((filename, fmt, data_list, len(data_list)))
                    grand_total += len(data_list)
                    parsed_routes.extend(gpx_tracks_to_routes(raw_bytes, user_profile, filename))
                elif fmt == "wkt":
                    data_list = wkt_to_dict(raw_bytes, user_profile)
                    parsed.append((filename, fmt, data_list, len(data_list)))
                    grand_total += len(data_list)
                elif fmt == "wkb":
                    data_list = wkb_to_dict(raw_bytes, user_profile)
                    parsed.append((filename, fmt, data_list, len(data_list)))
                    grand_total += len(data_list)
                elif fmt == "osm_xml":
                    data_list = osm_xml_to_dict(raw_bytes, user_profile)
                    parsed.append((filename, fmt, data_list, len(data_list)))
                    grand_total += len(data_list)
            except (UnicodeDecodeError, ValueError, KeyError, AttributeError, GPXException, ShapelyError, XMLParseError) as exc:
                logger.warning("Failed to parse '%s', skipping: %s", filename, exc)
                _notify_pin_import_parse_failure(fmt)

        if not parsed:
            yield sse({"type": "error", "message": "No valid location files found in the upload."})
            return

        yield sse({"type": "start", "total": grand_total})

        created_count = 0
        exists_count = 0
        skipped_count = 0
        current = 0

        try:
            for filename, fmt, file_data, _file_total in parsed:
                # Accumulate pins per file only when needed for filename tagging.
                file_pins: list[Pin] | None = [] if tag_by_filename else None
                pin_iter = self._csv_row_iter(file_data, user_profile) if fmt == "csv" else iter(file_data)

                for pin_data in pin_iter:
                    current += 1
                    pin_name = ""

                    if pin_data is None:
                        skipped_count += 1
                    else:
                        cid = pin_data.pop("cid", None)
                        location = Location.objects.by_cid(cid).first() if cid is not None else None
                        if location:
                            pin_data["location"] = location
                            # Clear the coordinate override - pin inherits location's coords.
                            pin_data.pop("latitude", None)
                            pin_data.pop("longitude", None)

                        pin_name = pin_data.get("name") or (location.name if location else "")
                        lookup_lat = pin_data.get("latitude") or (location.latitude if location else None)
                        lookup_lon = pin_data.get("longitude") or (location.longitude if location else None)
                        try:
                            pin, created = Pin.objects.get_nearby_or_create(
                                latitude=lookup_lat,
                                longitude=lookup_lon,
                                profile=user_profile,
                                defaults=pin_data,
                            )
                            if pin:
                                if created:
                                    created_count += 1
                                else:
                                    exists_count += 1
                                if tags:
                                    pin.badges.add(*tags)
                                if file_pins is not None:
                                    file_pins.append(pin)
                                # Backfill: if the import carried a CID but no existing
                                # Location was found by that CID, the nearby-match may
                                # have returned a Location that still lacks one.  Set it
                                # now so future imports resolve via CID instead of coords.
                                if cid is not None and location is None and pin.location_id and not pin.location.cid:
                                    GooglePlaceService().set_cid_for_entity(pin.location, cid)
                            else:
                                skipped_count += 1
                        except (DatabaseError, ValueError, OSError) as exc:
                            logger.warning("Failed to create pin '%s': %s", redact_text(pin_name), exc)
                            skipped_count += 1

                    percent = min(100, int(current / grand_total * 100)) if grand_total > 0 else 100
                    yield sse(
                        {
                            "type": "progress",
                            "current": current,
                            "total": grand_total,
                            "percent": percent,
                            "created": created_count,
                            "exists": exists_count,
                            "skipped": skipped_count,
                            "name": pin_name,
                        },
                    )

                # Apply a per-file tag to every pin produced from this file.
                if file_pins:
                    try:
                        from urbanlens.dashboard.models.badges.model import Badge

                        tag_name = _filename_stem(filename)
                        file_tag = Badge.objects.filter(
                            profile=user_profile,
                            name__iexact=tag_name,
                            kind=KIND_TAG,
                        ).first()
                        if file_tag is None:
                            style = suggest_badge_style(tag_name, user_profile)
                            file_tag = Badge.objects.create(
                                profile=user_profile,
                                kind=KIND_TAG,
                                name=tag_name,
                                icon=style.icon,
                                color=style.color,
                            )
                        for pin in file_pins:
                            pin.badges.add(file_tag)
                    except Exception as exc:
                        # TODO: Catch specific exception
                        logger.exception("Unable to add badge to pins: %s", exc)
        except (DatabaseError, OSError, ValueError, RuntimeError) as exc:
            logger.exception("Unexpected error during streaming import: %s", exc)
            yield sse({"type": "error", "message": "Import failed unexpectedly."})
            return

        yield sse(
            {
                "type": "complete",
                "total": grand_total,
                "created": created_count,
                "exists": exists_count,
                "skipped": skipped_count,
            },
        )

        # Process any Semantic Location History files found in the same upload.
        # These are streamed as a second pass with subtype="location_history" so
        # the frontend can distinguish them from the pin-import events above.
        if location_history_files:
            yield from import_location_history_streaming(location_history_files, user_profile)

            for filename, raw_bytes in location_history_files:
                try:
                    data = json.loads(raw_bytes.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if detect_location_history_format(data) == "semantic":
                    parsed_routes.extend(semantic_history_to_routes(data, user_profile, filename))

        # Save any Route candidates gathered above (GPX tracks/routes, Google
        # Takeout activitySegments) as a third pass, subtype="route".
        if parsed_routes:
            yield from import_routes_streaming(parsed_routes, user_profile)

    def parse_for_preview(
        self,
        files: list[tuple[str, bytes]],
        user_profile: Profile,
    ) -> list[dict[str, Any]]:
        """Parse uploaded files without importing, returning serialisable preview data.

        Args:
            files: List of ``(filename, raw_bytes)`` pairs (archives already expanded).
            user_profile: The profile associated with the import (used by CSV geocoding).

        Returns:
            List of dicts, one per file, each with keys:
                - ``stem`` (str): filename without extension, used as list/category name.
                - ``pins`` (list[dict]): serialisable pin dicts with keys
                  ``name``, ``lat``, ``lng``, ``description``, ``cid``.
        """
        from urbanlens.dashboard.services.archive_extractor import validate_content_type
        from urbanlens.dashboard.services.import_formats.gpx import gpx_to_dict
        from urbanlens.dashboard.services.import_formats.osm_xml import osm_xml_to_dict
        from urbanlens.dashboard.services.import_formats.shapefile import extract_shapefile_bundles, shapefile_to_dict
        from urbanlens.dashboard.services.import_formats.wkt_wkb import wkb_to_dict, wkt_to_dict

        result: list[dict[str, Any]] = []

        # Shapefiles ship as a set of same-stem sidecar files rather than one file,
        # so they must be grouped before the per-file loop below.
        shapefile_bundles, files = extract_shapefile_bundles(files)
        for bundle in shapefile_bundles:
            try:
                raw_pins = shapefile_to_dict(bundle, user_profile)
            except (OSError, ValueError, ShapefileDataSourceError) as exc:
                logger.warning("Failed to parse shapefile bundle '%s' for preview: %s", bundle.stem, exc)
                _notify_pin_import_parse_failure("shapefile")
                continue
            pins = self._preview_pins(raw_pins)
            if pins:
                result.append({"stem": bundle.stem, "pins": pins})

        for filename, raw_bytes in files:
            fmt = validate_content_type(filename, raw_bytes)
            if fmt is None or fmt == "location_history":
                continue

            stem = _filename_stem(filename)
            try:
                if fmt == "json":
                    raw_pins = self.geojson_to_dict(raw_bytes.decode("utf-8"), user_profile)
                elif fmt == "kml":
                    raw_pins = self.takeout_kml_to_dict(raw_bytes, user_profile)
                elif fmt == "csv":
                    text = raw_bytes.decode("utf-8")
                    raw_pins = [row for row in self._csv_row_iter(text, user_profile) if row is not None]
                elif fmt == "gpx":
                    raw_pins = gpx_to_dict(raw_bytes, user_profile)
                elif fmt == "wkt":
                    raw_pins = wkt_to_dict(raw_bytes, user_profile)
                elif fmt == "wkb":
                    raw_pins = wkb_to_dict(raw_bytes, user_profile)
                elif fmt == "osm_xml":
                    raw_pins = osm_xml_to_dict(raw_bytes, user_profile)
                else:
                    continue
            except (UnicodeDecodeError, ValueError, KeyError, AttributeError, GPXException, ShapelyError, XMLParseError) as exc:
                logger.warning("Failed to parse '%s' for preview: %s", filename, exc)
                _notify_pin_import_parse_failure(fmt)
                continue

            pins = self._preview_pins(raw_pins)
            if pins:
                result.append({"stem": stem, "pins": pins})

        return result

    @staticmethod
    def _preview_pins(raw_pins: Iterable[dict[str, Any] | None]) -> list[dict[str, Any]]:
        """Convert internal pin dicts into the serialisable preview shape.

        Args:
            raw_pins: Pin dicts as returned by any of the format parsers (``None``
                entries, e.g. from a failed CSV row, are skipped).

        Returns:
            List of dicts with keys ``name``, ``lat``, ``lng``, ``description``, ``cid``.
        """
        pins: list[dict[str, Any]] = []
        for p in raw_pins:
            if p is None:
                continue
            lat = p.get("latitude")
            lng = p.get("longitude")
            if lat is None or lng is None:
                continue
            pins.append(
                {
                    "name": (p.get("name") or "")[:255],
                    "lat": float(lat),
                    "lng": float(lng),
                    "description": (p.get("description") or "")[:500],
                    "cid": p.get("cid"),
                },
            )
        return pins

    def import_preview_streaming(
        self,
        confirmed_lists: list[dict[str, Any]],
        user_profile: Profile,
        auto_tag: bool = True,
    ):
        r"""Stream import events for user-confirmed pin selections from the preview step.

        Each ``confirmed_lists`` entry must have:
            - ``stem`` (str): list name used for category creation.
            - ``create_category`` (bool): create a ``kind="category"`` badge from *stem*.
            - ``badge_ids`` (list[int]): badge IDs to apply to every pin in the list.
            - ``pins`` (list[dict]): dicts with ``name``, ``lat``, ``lng``,
              ``description``, ``cid``, ``badge_ids`` (list[int]), and optionally
              ``is_private`` (bool) fields.  Private pins are never linked to a
              shared Location and do not create a community wiki entry.

        Yields:
            str: SSE-formatted data lines (same event shapes as ``import_pins_streaming``).

        Args:
            confirmed_lists: User-confirmed selection from the preview step.
            user_profile: Profile to import pins for.
        """

        def sse(data: dict) -> str:
            return f"data: {json.dumps(data)}\n\n"

        total = sum(len(lst.get("pins", [])) for lst in confirmed_lists)
        if total == 0:
            yield sse({"type": "error", "message": "No pins selected for import."})
            return

        yield sse({"type": "start", "total": total})

        created_count = 0
        exists_count = 0
        skipped_count = 0
        current = 0

        try:
            for lst in confirmed_lists:
                stem = lst.get("stem", "")
                list_badge_ids = lst.get("badge_ids") or []
                create_category = bool(lst.get("create_category", False))

                list_badges = list(Badge.objects.filter(id__in=list_badge_ids)) if list_badge_ids else []

                category_badge = None
                if create_category and stem:
                    category_badge, _ = Badge.objects.get_or_create(
                        profile=user_profile,
                        name__iexact=stem,
                        defaults={"name": stem, "kind": "category"},
                    )

                for pin_dict in lst.get("pins", []):
                    current += 1
                    pin_name = (pin_dict.get("name") or "")[:255]
                    lat = pin_dict.get("lat")
                    lng = pin_dict.get("lng")
                    description = pin_dict.get("description") or ""
                    cid = pin_dict.get("cid")
                    pin_badge_ids = pin_dict.get("badge_ids") or []
                    is_private = bool(pin_dict.get("is_private", False))

                    try:
                        # Private pins are never linked to a shared Location.
                        location = None if is_private else (Location.objects.by_cid(cid).first() if cid else None)

                        pin_defaults: dict[str, Any] = {
                            "profile": user_profile,
                            "name": pin_name,
                            "description": description,
                            "is_private": is_private,
                        }

                        if location:
                            pin_defaults["location"] = location
                            lookup_lat = location.latitude
                            lookup_lon = location.longitude
                        else:
                            pin_defaults["latitude"] = lat
                            pin_defaults["longitude"] = lng
                            lookup_lat = lat
                            lookup_lon = lng

                        pin, created = Pin.objects.get_nearby_or_create(
                            latitude=lookup_lat,
                            longitude=lookup_lon,
                            profile=user_profile,
                            defaults=pin_defaults,
                        )

                        if pin:
                            if created:
                                created_count += 1
                                if auto_tag:
                                    from urbanlens.dashboard.services.celery import safely_enqueue_task
                                    from urbanlens.dashboard.tasks import suggest_pin_category

                                    safely_enqueue_task(suggest_pin_category, pin.pk)
                            else:
                                exists_count += 1

                            if list_badges:
                                pin.badges.add(*list_badges)
                            if category_badge:
                                pin.badges.add(category_badge)
                            if pin_badge_ids:
                                extra = list(Badge.objects.filter(id__in=pin_badge_ids))
                                if extra:
                                    pin.badges.add(*extra)

                            if cid and not location and pin.location_id and not pin.location.cid:
                                GooglePlaceService().set_cid_for_entity(pin.location, cid)
                        else:
                            skipped_count += 1

                    except (DatabaseError, ValueError, OSError) as exc:
                        logger.warning("Failed to import pin '%s': %s", pin_name, exc)
                        skipped_count += 1

                    percent = min(100, int(current / total * 100)) if total > 0 else 100
                    yield sse(
                        {
                            "type": "progress",
                            "current": current,
                            "total": total,
                            "percent": percent,
                            "created": created_count,
                            "exists": exists_count,
                            "skipped": skipped_count,
                            "name": pin_name,
                        },
                    )

        except (DatabaseError, OSError, ValueError, RuntimeError) as exc:
            logger.exception("Unexpected error during preview import: %s", exc)
            yield sse({"type": "error", "message": "Import failed unexpectedly."})
            return

        yield sse(
            {
                "type": "complete",
                "total": total,
                "created": created_count,
                "exists": exists_count,
                "skipped": skipped_count,
            },
        )

    @staticmethod
    def _iter_kml_placemarks(features: Iterable[Any]) -> Iterator[Any]:
        """Recursively yield every Placemark nested within KML Document/Folder containers.

        KML files nest Placemarks at varying depths depending on the exporting
        tool (e.g. Google MyMaps wraps every layer in a Folder, other tools may
        put Placemarks directly under the Document), so the tree must be walked
        rather than assuming a fixed depth.

        Args:
            features: An iterable of KML feature objects (Document, Folder, Placemark, etc).

        Yields:
            Placemark: Each Placemark found at any depth within *features*.
        """
        for feature in features:
            if isinstance(feature, kml.Placemark):
                yield feature
            else:
                children = getattr(feature, "features", None)
                if children:
                    yield from GoogleMapsGateway._iter_kml_placemarks(children)

    def takeout_kml_to_dict(self, file_contents: bytes, user_profile: Profile) -> list[dict[str, Any]]:
        try:
            # lxml refuses to parse a `str` containing an `<?xml ... encoding=...?>`
            # declaration (which every Google Takeout KML/KMZ has), so the raw
            # bytes must be passed through undecoded and let lxml sniff the encoding.
            k = kml.KML.from_string(file_contents)  # type: ignore[arg-type]

            pins: list[dict[str, Any]] = []
            for placemark in self._iter_kml_placemarks(k.features):
                geometry = placemark.geometry
                if geometry is None or not hasattr(geometry, "coords"):
                    continue
                coords = next(iter(geometry.coords))

                pins.append(
                    {
                        "latitude": coords[1],
                        "longitude": coords[0],
                        "profile": user_profile,
                        "name": placemark.name,
                        "description": placemark.description,
                    },
                )

            logger.debug("Converted %s pins from KML file to dicts.", len(pins))
        except (ValueError, AttributeError, UnicodeDecodeError) as e:
            logger.exception("Failed to import pins from KML: %s", e)
            raise

        return pins

    @staticmethod
    def _geojson_feature_point(geometry: dict[str, Any]) -> tuple[float, float] | None:
        """Return a representative ``(longitude, latitude)`` for any GeoJSON geometry type.

        ``Point`` coordinates are used directly; every other geometry type (as
        produced by Overpass/OSM exports, for example a building footprint
        ``Polygon``) is reduced to its centroid via shapely.
        """
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

        Google Takeout's "Saved Places" export uses a fixed ``name``/``description``/
        ``address`` property shape, which is tried first so existing Takeout imports
        are unaffected. Anything else (Overpass exports, custom scripts, arbitrary
        property names) falls back to the generic heuristic in ``import_formats.heuristics``.
        """
        takeout_bits = [str(properties[key]).strip() for key in ("description", "address") if properties.get(key)]
        if "name" in properties or takeout_bits or not properties:
            name = str(properties.get("name") or "Unknown Location").strip()
            return name, " ".join(takeout_bits)
        return pick_name_and_description(properties)

    def geojson_to_dict(self, file_contents: str, user_profile: Profile) -> list[dict[str, Any]]:
        """Convert a GeoJSON ``FeatureCollection`` into pin dicts.

        Handles Google Takeout's "Saved Places" export (``Point`` geometry,
        ``name``/``description``/``address`` properties) as well as generic
        GeoJSON produced by Overpass Turbo, OSM exports, or custom scripts
        (arbitrary geometry types and property names).
        """
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

    def takeout_csv_to_dict(self, file_contents: str, user_profile: Profile) -> list[dict[str, Any]]:
        pins: list[dict[str, Any]] = []
        gateway = GoogleGeocodingGateway()
        try:
            reader = csv.DictReader(file_contents.splitlines())

            for row in reader:
                # Extract coordinates from URL if available
                url = row.get("URL", "")
                if not url:
                    logger.error("No url to extract coordinates from: row -> %s", row)
                    continue

                latitude, longitude = gateway.extract_coordinates_from_url(url)

                pins.append(
                    {
                        "latitude": latitude,
                        "longitude": longitude,
                        "profile": user_profile,
                        "name": row.get("Title", ""),
                        "description": row.get("Note", "") + " " + row.get("Comment", "").strip(),
                    },
                )

        except (csv.Error, KeyError, ValueError) as e:
            logger.exception("Failed to import pins from CSV: %s", e)
            raise

        logger.info("Converted %s pins from CSV file to dicts.", len(pins))

        return pins
