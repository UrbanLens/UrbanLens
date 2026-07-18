"""Research Tier 1 (ArcGIS/Socrata) property-record endpoints for unresearched counties.

The ``PropertyJurisdiction`` registry (``docs/property-records-plan.md``
section 1) grows over time - this command runs
``services.apis.property_records.discovery.discover_tier1_endpoint`` against
rows still at ``AdapterType.UNKNOWN`` and saves whatever it validates,
leaving the rest untouched for a human (or a future Tier 2/3 implementation)
to research some other way.
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction
from urbanlens.dashboard.services.apis.property_records.discovery import apply_discovery, discover_tier1_endpoint


class Command(BaseCommand):
    """Attempt Tier 1 endpoint discovery for unresearched jurisdictions."""

    help = "Search for and validate ArcGIS/Socrata parcel endpoints for PropertyJurisdiction rows still marked UNKNOWN."

    def add_arguments(self, parser):
        parser.add_argument("--fips", help="Only attempt this single 5-digit FIPS code.")
        parser.add_argument("--limit", type=int, default=25, help="Maximum jurisdictions to attempt in one run (default: 25).")
        parser.add_argument("--no-ai", action="store_true", help="Only use the deterministic search step; skip the AI-assisted fallback.")
        parser.add_argument("--sleep", type=float, default=1.0, help="Seconds to sleep between jurisdictions (default: 1.0).")
        parser.add_argument("--dry-run", action="store_true", help="Print what would be saved without saving.")

    def handle(self, *args, **options):
        queryset = PropertyJurisdiction.objects.unresearched()
        if options["fips"]:
            queryset = queryset.filter(fips=options["fips"])
        queryset = queryset.order_by("state", "county_name")[: options["limit"]]

        total = queryset.count()
        self.stdout.write(f"Attempting discovery for {total} unresearched jurisdiction(s).")

        found = 0
        for jurisdiction in queryset:
            result = discover_tier1_endpoint(jurisdiction, allow_ai=not options["no_ai"])
            if result is None:
                self.stdout.write(f"  [{jurisdiction.fips}] {jurisdiction.county_name}, {jurisdiction.state}: nothing found.")
            else:
                found += 1
                label = "AI-assisted" if result.via_ai else "deterministic"
                self.stdout.write(f"  [{jurisdiction.fips}] {jurisdiction.county_name}, {jurisdiction.state}: found {result.adapter_type} endpoint ({label}) -> {result.url}")
                if not options["dry_run"]:
                    apply_discovery(jurisdiction, result)

            if options["sleep"]:
                time.sleep(options["sleep"])

        self.stdout.write(f"Done. Found: {found}/{total}.")
