"""Diagnostic: run one live property-record lookup end-to-end and print what came back.

The companion to ``discover_property_jurisdiction`` (which only *researches*
endpoints): this exercises the full retrieval pipeline
(``services.apis.property_records.orchestrator.get_property_record``) for a
single coordinate or address and prints the resolved jurisdiction, its
registry configuration, and either the normalized ``PropertyRecord`` or the
machine-readable reason nothing was returned. Nothing is written to the
database or cache - it's a read-only probe of live county data, safe to run
repeatedly.

Examples::

    # By coordinate (the pipeline's primary key):
    manage.py test_property_record --lat 42.6526 --lng -73.7562

    # By address (geocoded to a coordinate first, and used as the Tier 2/3 hint):
    manage.py test_property_record --address "16 Sheridan Ave, Albany, NY"

    # Full raw JSON payload (what a LocationCache row would store):
    manage.py test_property_record --lat 42.6526 --lng -73.7562 --json
"""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from urbanlens.dashboard.services.apis.property_records.jurisdiction import resolve_jurisdiction, resolve_jurisdiction_from_address
from urbanlens.dashboard.services.apis.property_records.orchestrator import PERMANENT_REASONS, PropertyRecordsUnavailableError, get_property_record


class Command(BaseCommand):
    """Run a single live property-record lookup and report the outcome."""

    help = "Run one end-to-end property-record lookup for a coordinate or address (read-only; live county data)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--lat", type=float, help="WGS-84 latitude.")
        parser.add_argument("--lng", type=float, help="WGS-84 longitude.")
        parser.add_argument("--address", help="US street address. Used as the situs/search hint, and geocoded to a coordinate when --lat/--lng are omitted.")
        parser.add_argument("--apn", default="", help="Known parcel/APN, for jurisdictions whose Tier 2/3 recipe searches by parcel number.")
        parser.add_argument("--json", action="store_true", help="Print the full record payload as JSON (the LocationCache row shape) instead of a summary.")

    def handle(self, *args: Any, **options: Any) -> None:
        latitude, longitude, situs_hint = self._resolve_inputs(options)

        self.stdout.write("Property-record lookup diagnostic")
        self.stdout.write(f"  coordinate: {latitude:.5f}, {longitude:.5f}")
        if situs_hint:
            self.stdout.write(f"  situs hint: {situs_hint}")

        self._print_jurisdiction(latitude, longitude)

        try:
            record = get_property_record(latitude, longitude, situs_address=situs_hint, apn=options["apn"])
        except PropertyRecordsUnavailableError as exc:
            permanence = "permanent (needs registry/adapter work)" if exc.reason in PERMANENT_REASONS else "transient (worth retrying)"
            self.stdout.write(self.style.WARNING(f"\nNo record: [{exc.reason}] {permanence}"))
            self.stdout.write(f"  {exc}")
            if exc.links:
                self.stdout.write("  manual-lookup links:")
                for name, url in exc.links.items():
                    self.stdout.write(f"    {name}: {url}")
            return

        if options["json"]:
            self.stdout.write("\n" + json.dumps(record.to_dict(), indent=2, default=str))
        else:
            self._print_summary(record)
        self.stdout.write(self.style.SUCCESS("\nPASS: a record was retrieved."))

    def _resolve_inputs(self, options: dict[str, Any]) -> tuple[float, float, str]:
        """Turn the given options into (lat, lng, situs_hint), geocoding an address when needed."""
        lat, lng, address = options["lat"], options["lng"], options["address"]

        if lat is not None and lng is not None:
            return lat, lng, address or ""

        if address:
            resolved = resolve_jurisdiction_from_address(address)
            if resolved is None:
                raise CommandError(f"Could not geocode {address!r} to a US county coordinate. Pass --lat/--lng explicitly.")
            _jurisdiction, geo_lat, geo_lng = resolved
            self.stdout.write(f"  geocoded {address!r} -> {geo_lat:.5f}, {geo_lng:.5f}")
            return geo_lat, geo_lng, address

        raise CommandError("Provide either --lat and --lng, or --address.")

    def _print_jurisdiction(self, latitude: float, longitude: float) -> None:
        """Show which registry row the coordinate resolves to, and how it's configured."""
        jurisdiction = resolve_jurisdiction(latitude, longitude)
        if jurisdiction is None:
            self.stdout.write(self.style.WARNING("  jurisdiction: none (outside US county coverage)"))
            return
        self.stdout.write(f"  jurisdiction: {jurisdiction} [fips={jurisdiction.fips}]")
        self.stdout.write(f"    adapter: {jurisdiction.get_adapter_type_display()}  automatable={jurisdiction.is_automatable}  captcha={jurisdiction.requires_captcha}")
        if jurisdiction.gis_rest_url:
            self.stdout.write(f"    gis_rest_url: {jurisdiction.gis_rest_url}")
        if jurisdiction.vendor:
            self.stdout.write(f"    vendor: {jurisdiction.vendor}")
        if jurisdiction.scrape_recipe:
            self.stdout.write(f"    scrape_recipe: {json.dumps(jurisdiction.scrape_recipe)}")

    def _print_summary(self, record: Any) -> None:
        """Print a human-readable digest of the retrieved record."""
        source = record.source
        self.stdout.write(f"\n  source: Tier {source.tier} - {source.provider}  ({record.confidence:.0%} confidence)")
        if record.field_mismatches:
            self.stdout.write(self.style.WARNING(f"  sources disagree on: {', '.join(record.field_mismatches)}"))

        rows = [
            ("owner(s)", ", ".join(record.owner_name) or "-"),
            ("situs address", record.situs_address or "-"),
            ("APN", record.apn or "-"),
            ("land use", record.land_use_code or "-"),
            ("lot size (sqft)", record.lot_size_sqft),
            ("building (sqft)", record.building_sqft),
            ("year built", record.year_built),
            ("assessed total", record.assessed_value.total if record.assessed_value else None),
            ("market value", record.market_value),
            ("sales on record", len(record.sales_history)),
        ]
        for label, value in rows:
            if value not in (None, "-") or label in ("owner(s)", "situs address"):
                self.stdout.write(f"    {label:>16}: {value}")
