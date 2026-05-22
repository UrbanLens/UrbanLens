"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    maps.py                                                                                              *
*        Path:    /dashboard/services/google/maps.py                                                                   *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2024-01-01                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-17     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import json
import logging
import math
from typing import TYPE_CHECKING, Any

from fastkml import kml
import requests
from tqdm import tqdm

from urbanlens.dashboard.models.pin import Pin
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.google.geocoding import GoogleGeocodingGateway

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class GoogleMapsGateway(Gateway):
    """
    Gateway for the Google Maps API.
    """

    api_key: str

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

    def find_place(self, input_text: str, input_type: str = "textquery"):
        """
        Find a place using the Google Maps Places API.
        """
        place_url = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
        params = {
            "input": input_text,
            "inputtype": input_type,
            "key": self.api_key,
        }
        response = self.session.get(place_url, params=params)
        response.raise_for_status()
        return response.json()

    def get_satellite_view(self, latitude, longitude, zoom=17, size="600x300", maptype="satellite"):
        """
        Get a satellite view image for the given latitude and longitude.
        """
        static_map_url = "https://maps.googleapis.com/maps/api/staticmap"
        params = {
            "center": f"{latitude},{longitude}",
            "zoom": zoom,
            "size": size,
            "maptype": maptype,
            "key": self.api_key,
        }
        response = self.session.get(static_map_url, params=params)
        response.raise_for_status()
        return response.content  # Returns the raw bytes of the image

    def get_street_view(
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
        logger.critical("Getting street view")

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

            if metadata["status"] == "OK":
                logger.critical("Found street view")
                # If image is available, get the image with the heading towards the original coordinates
                image_params = params.copy()
                image_params.pop("radius")
                image_params["heading"] = self.calculate_heading(
                    metadata["location"]["lat"],
                    metadata["location"]["lng"],
                    latitude,
                    longitude,
                )
                street_view_url = "https://maps.googleapis.com/maps/api/streetview"
                image_response = self.session.get(street_view_url, params=image_params)
                image_response.raise_for_status()
                return image_response.content  # Returns the raw bytes of the image

            radius += radius_increment
            logger.critical("Increasing radius to %s", radius)

        logger.critical("No street view found")
        raise ValueError("No Street View imagery found within the maximum search radius.")

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

    def import_pins_from_file(self, file, user_profile: Profile) -> list[Pin]:
        """
        Imports pins from a file and bulk creates Pin objects.
        """
        parts = file.name.split(".")
        if not parts or len(parts) < 2:
            raise ValueError("No file extension provided.")

        extension: str = parts[-1]
        file_contents = file.read().decode("utf-8")

        match extension.lower():
            case "kml":
                data = self.takeout_kml_to_dict(file_contents, user_profile)
            case "json":
                data = self.takeout_json_to_dict(file_contents, user_profile)
            case "csv":
                data = self.takeout_csv_to_dict(file_contents, user_profile)
            case _:
                raise ValueError("Unsupported file format. Supported formats are KML, JSON, and CSV.")

        # Parse every location in data
        pins: list[Pin] = []
        total = len(data)
        created_pins = 0
        exists = 0
        skipped = 0
        with tqdm(total=total, desc="Importing pins") as pbar:
            for pin_data in data:
                try:
                    pin, created = Pin.objects.get_nearby_or_create(
                        latitude=pin_data["latitude"],
                        longitude=pin_data["longitude"],
                        profile=user_profile,
                        defaults=pin_data,
                    )
                    if pin:
                        pins.append(pin)
                        if created:
                            created_pins += 1
                        else:
                            exists += 1
                    else:
                        skipped += 1
                finally:
                    pbar.update(1)
                    pbar.set_description(
                        f"Importing pins: {created_pins} created, {skipped} skipped, {exists} already existed. Last: {pin_data.get('name', '')}",
                    )

        return pins

    def _csv_row_iter(self, file_contents: str, user_profile: "Profile"):
        """Generator yielding one pin_data dict per CSV row, geocoding on demand. Yields None for rows that fail.

        Args:
            file_contents: Raw CSV text.
            user_profile: The profile to associate with each pin.

        Yields:
            dict with pin fields, or None when geocoding fails.
        """
        gateway = GoogleGeocodingGateway()
        reader = csv.DictReader(file_contents.splitlines())
        for row in reader:
            url = row.get("URL", "")
            if not url:
                logger.error("No url to extract coordinates from: row -> %s", row)
                yield None
                continue
            try:
                latitude, longitude = gateway.extract_coordinates_from_url(url)
                yield {
                    "latitude": latitude,
                    "longitude": longitude,
                    "profile": user_profile,
                    "name": row.get("Title", ""),
                    "description": (row.get("Note", "") + " " + row.get("Comment", "")).strip(),
                }
            except Exception as e:
                logger.warning("Failed to geocode URL %s: %s", url, e)
                yield None

    def import_pins_streaming(self, files: list[tuple[str, bytes]], user_profile: "Profile"):
        """Generator that yields SSE data strings while importing pins from a list of files.

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

        Yields:
            str: SSE-formatted data lines.
        """

        from urbanlens.dashboard.services.archive_extractor import validate_content_type

        def sse(data: dict) -> str:
            return f"data: {json.dumps(data)}\n\n"

        # First pass: validate and parse every file so we can report an accurate
        # grand total upfront.  CSV rows are counted by line (cheap); JSON and KML
        # are parsed fully and the results cached for the second pass.
        # Files that fail validation or parsing are skipped with a warning.
        parsed: list[tuple[str, Any, int]] = []  # (fmt, data_or_text, file_total)
        grand_total = 0

        for filename, raw_bytes in files:
            fmt = validate_content_type(filename, raw_bytes)
            if fmt is None:
                logger.info("Skipping unrecognised file during import: %s", filename)
                continue

            try:
                text = raw_bytes.decode("utf-8")
                if fmt == "json":
                    data_list = self.takeout_json_to_dict(text, user_profile)
                    parsed.append((fmt, data_list, len(data_list)))
                    grand_total += len(data_list)
                elif fmt == "kml":
                    data_list = self.takeout_kml_to_dict(text, user_profile)
                    parsed.append((fmt, data_list, len(data_list)))
                    grand_total += len(data_list)
                elif fmt == "csv":
                    file_total = max(0, len(text.splitlines()) - 1)
                    parsed.append((fmt, text, file_total))
                    grand_total += file_total
            except Exception as exc:
                logger.warning("Failed to parse '%s', skipping: %s", filename, exc)

        if not parsed:
            yield sse({"type": "error", "message": "No valid location files found in the upload."})
            return

        yield sse({"type": "start", "total": grand_total})

        created_count = 0
        exists_count = 0
        skipped_count = 0
        current = 0

        try:
            for fmt, file_data, _file_total in parsed:
                pin_iter = (
                    self._csv_row_iter(file_data, user_profile)
                    if fmt == "csv"
                    else iter(file_data)
                )

                for pin_data in pin_iter:
                    current += 1
                    pin_name = ""

                    if pin_data is None:
                        skipped_count += 1
                    else:
                        pin_name = pin_data.get("name", "")
                        try:
                            pin, created = Pin.objects.get_nearby_or_create(
                                latitude=pin_data["latitude"],
                                longitude=pin_data["longitude"],
                                profile=user_profile,
                                defaults=pin_data,
                            )
                            if pin:
                                if created:
                                    created_count += 1
                                else:
                                    exists_count += 1
                            else:
                                skipped_count += 1
                        except Exception as exc:
                            logger.warning("Failed to create pin '%s': %s", pin_name, exc)
                            skipped_count += 1

                    percent = min(100, int(current / grand_total * 100)) if grand_total > 0 else 100
                    yield sse({
                        "type": "progress",
                        "current": current,
                        "total": grand_total,
                        "percent": percent,
                        "created": created_count,
                        "exists": exists_count,
                        "skipped": skipped_count,
                        "name": pin_name,
                    })
        except Exception as exc:
            logger.exception("Unexpected error during streaming import: %s", exc)
            yield sse({"type": "error", "message": f"Import failed unexpectedly: {exc}"})
            return

        yield sse({
            "type": "complete",
            "total": grand_total,
            "created": created_count,
            "exists": exists_count,
            "skipped": skipped_count,
        })

    def takeout_kml_to_dict(self, file_contents: str, user_profile: Profile) -> list[dict[str, Any]]:
        try:
            k = kml.KML()
            k.from_string(file_contents)

            pins: list[dict[str, Any]] = []
            for feature in k.features():  # type: ignore[operator]
                for placemark in feature.features():
                    coords = placemark.geometry.coords[0]

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
        except Exception as e:
            logger.exception("Failed to import pins from KML: %s", str(e))
            raise

        return pins

    def takeout_json_to_dict(self, file_contents: str, user_profile: Profile) -> list[dict[str, Any]]:
        try:
            json_data = json.loads(file_contents)
            features = json_data.get("features", [])
            pins: list[dict[str, Any]] = []

            for feature in features:
                geometry = feature.get("geometry", {})
                properties = feature.get("properties", {})

                if geometry.get("type") != "Point":
                    continue

                coordinates = geometry.get("coordinates", [])
                if len(coordinates) != 2:
                    logger.warning("Skipping feature with unexpected coordinates: %s", coordinates)
                    continue

                # Coordinates are in [longitude, latitude] format
                longitude, latitude = coordinates
                pins.append(
                    {
                        "latitude": latitude,
                        "longitude": longitude,
                        "profile": user_profile,
                        "name": properties.get("name", "Unknown Location"),
                        "description": f"{properties.get('description', '')} {properties.get('address', '')}",
                    },
                )

            logger.info("Converted %s pins from JSON file to dicts.", len(pins))

        except Exception as e:
            logger.exception("Failed to import pins from JSON: %s", str(e))
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

            logger.info("Converted %s pins from CSV file to dicts.", len(pins))
        except Exception as e:
            logger.exception("Failed to import pins from CSV: %s", str(e))
            raise

        return pins
