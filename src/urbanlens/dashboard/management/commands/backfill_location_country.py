"""Backfill Location.country for rows geocoded before country parsing existed.

Every Location's ``country`` used to be stuck on the model's old hardcoded
"United States" default (see migration 0025), since the reverse-geocoding
step never actually parsed a country out of the API response. This command
re-derives the real country for rows still missing one, using the Google
Geocoding API (through the same GeocodedLocation cache used elsewhere).
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db import DatabaseError

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway, parse_address_components
from urbanlens.UrbanLens.settings.app import settings as app_settings


class Command(BaseCommand):
    """Re-geocode Locations missing a country and save the parsed value."""

    help = "Backfill Location.country for rows left blank by the pre-fix reverse-geocoding step."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print what would change without saving.")
        parser.add_argument("--sleep", type=float, default=0.1, help="Seconds to sleep between API calls (default: 0.1).")

    def handle(self, *args, **options):
        if not app_settings.google_unrestricted_api_key:
            self.stderr.write("No Google API key configured (UL_GOOGLE_UNRESTRICTED_API_KEY) - aborting.")
            return

        dry_run = options["dry_run"]
        sleep_seconds = options["sleep"]
        gateway = GoogleGeocodingGateway()

        queryset = Location.objects.filter(country="").exclude(latitude__isnull=True).exclude(longitude__isnull=True)
        total = queryset.count()
        self.stdout.write(f"Found {total} Location(s) missing country.")

        updated = 0
        failed = 0
        for location in queryset.iterator():
            lat = float(location.latitude)
            lng = float(location.longitude)

            try:
                data = gateway.geocode_coordinates(lat, lng)
            except (OSError, ValueError) as exc:
                self.stderr.write(f"  [pk={location.pk}] geocoding failed: {exc}")
                failed += 1
                continue

            results = (data or {}).get("results", [])
            if not results:
                failed += 1
                continue

            country = parse_address_components(results[0].get("address_components", [])).get("country")
            if not country:
                failed += 1
                continue

            if dry_run:
                self.stdout.write(f"  [pk={location.pk}] would set country={country!r}")
            else:
                try:
                    Location.objects.filter(pk=location.pk).update(country=country)
                except DatabaseError:
                    self.stderr.write(f"  [pk={location.pk}] save failed")
                    failed += 1
                    continue
            updated += 1

            if sleep_seconds:
                time.sleep(sleep_seconds)

        self.stdout.write(f"Done. Updated: {updated}, failed/unresolved: {failed}.")
