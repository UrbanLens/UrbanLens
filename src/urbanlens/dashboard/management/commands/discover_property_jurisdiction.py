"""Research property-record retrieval strategies for unresearched counties.

The ``PropertyJurisdiction`` registry (``docs/property-records-plan.md``
section 1) grows over time. Two independent modes:

- **Tier 1** (default): ``discovery.discover_tier1_endpoint`` against rows
  still at ``AdapterType.UNKNOWN``, saving whatever it validates.
- **Tier 3** (``--tier3``): ``discovery.discover_tier3_recipe`` against rows
  that already have an ``assessor_url`` set but no ``scrape_recipe`` yet -
  see that function's docstring for why its result is saved unverified
  (structurally validated against the live form, not confirmed against real
  data) and needs a human's follow-up check.

Tier 2 has no discovery step - it's populated by writing a
``vendor_templates.VendorTemplate`` (code) and setting a jurisdiction's
``vendor``/``gis_rest_url`` (data), not by searching.
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db.models import Q

from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction
from urbanlens.dashboard.services.apis.property_records.discovery import (
    apply_discovery,
    apply_tier3_discovery,
    discover_tier1_endpoint,
    discover_tier3_recipe,
)
from urbanlens.dashboard.services.apis.property_records.html_scrape import recipe_to_dict


class Command(BaseCommand):
    """Attempt Tier 1 endpoint (or, with --tier3, Tier 3 recipe) discovery for jurisdictions."""

    help = "Search for and validate property-record retrieval configuration for PropertyJurisdiction rows."

    def add_arguments(self, parser):
        parser.add_argument("--fips", help="Only attempt this single 5-digit FIPS code.")
        parser.add_argument("--limit", type=int, default=25, help="Maximum jurisdictions to attempt in one run (default: 25).")
        parser.add_argument("--no-ai", action="store_true", help="Tier 1 only: skip the AI-assisted fallback, deterministic search step only.")
        parser.add_argument("--sleep", type=float, default=1.0, help="Seconds to sleep between jurisdictions (default: 1.0).")
        parser.add_argument("--dry-run", action="store_true", help="Print what would be saved without saving.")
        parser.add_argument("--tier3", action="store_true", help="Discover Tier 3 search-form recipes instead of Tier 1 endpoints (requires assessor_url already set).")

    def handle(self, *args, **options):
        if options["tier3"]:
            self._handle_tier3(options)
        else:
            self._handle_tier1(options)

    def _handle_tier1(self, options: dict) -> None:
        queryset = PropertyJurisdiction.objects.unresearched()
        if options["fips"]:
            queryset = queryset.filter(fips=options["fips"])
        queryset = queryset.order_by("state", "county_name")[: options["limit"]]

        total = queryset.count()
        self.stdout.write(f"Attempting Tier 1 discovery for {total} unresearched jurisdiction(s).")

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

    def _handle_tier3(self, options: dict) -> None:
        queryset = PropertyJurisdiction.objects.filter(~Q(assessor_url="")).filter(Q(scrape_recipe={}) | Q(scrape_recipe__isnull=True))
        if options["fips"]:
            queryset = queryset.filter(fips=options["fips"])
        queryset = queryset.order_by("state", "county_name")[: options["limit"]]

        total = queryset.count()
        self.stdout.write(f"Attempting Tier 3 recipe discovery for {total} jurisdiction(s) with an assessor_url but no recipe yet.")

        found = 0
        for jurisdiction in queryset:
            recipe = discover_tier3_recipe(jurisdiction)
            if recipe is None:
                self.stdout.write(f"  [{jurisdiction.fips}] {jurisdiction.county_name}, {jurisdiction.state}: nothing found.")
            else:
                found += 1
                self.stdout.write(f"  [{jurisdiction.fips}] {jurisdiction.county_name}, {jurisdiction.state}: proposed recipe (UNVERIFIED against real data) -> {recipe_to_dict(recipe)}")
                if not options["dry_run"]:
                    apply_tier3_discovery(jurisdiction, recipe)

            if options["sleep"]:
                time.sleep(options["sleep"])

        self.stdout.write(f"Done. Found: {found}/{total}. Every proposed recipe still needs a human to confirm it against a real known property before trusting it.")
