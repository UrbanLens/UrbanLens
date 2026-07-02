"""Management command to diagnose Google Custom Search JSON API configuration."""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from urbanlens.dashboard.services.apis.search.google import GoogleCustomSearchError, GoogleCustomSearchGateway, _mask_secret
from urbanlens.UrbanLens.settings.app import settings


class Command(BaseCommand):
    """Run a single Google Custom Search JSON API request and print likely fixes."""

    help = "Diagnose Google Custom Search JSON API issues (403, bad key, wrong CSE id)"

    def add_arguments(self, parser):
        parser.add_argument("--query", default="UrbanLens", help="Query to test with Google Custom Search")
        parser.add_argument("--api-key", default=None, help="Override UL_GOOGLE_DOMAIN_RESTRICTED_API_KEY")
        parser.add_argument("--cx", default=None, help="Override UL_GOOGLE_SEARCH_TENANT/UL_GOOGLE_SEARCH_CX")

    def handle(self, *args, **options):
        api_key = options["api_key"] or settings.google_domain_restricted_api_key
        cx = options["cx"] or settings.google_search_tenant
        query = options["query"]

        self.stdout.write("Google Custom Search JSON API diagnostic")
        self.stdout.write(f"  key: {_mask_secret(api_key)}")
        self.stdout.write(f"  cx:  {_mask_secret(cx)}")
        self.stdout.write(f"  query: {query}")

        gateway = GoogleCustomSearchGateway(api_key=api_key, cx=cx)
        try:
            results = gateway.search(query, max_results=1)
        except GoogleCustomSearchError as exc:
            self.stdout.write(self.style.ERROR(f"FAIL: {exc}"))
            self.stdout.write("\nLikely fixes for HTTP 403:")
            self.stdout.write("  1. Enable the Custom Search API / Custom Search JSON API for this Google Cloud project.")
            self.stdout.write("  2. Confirm billing/quota is available for the project and the daily Custom Search quota is not exhausted.")
            self.stdout.write("  3. Use an API key whose Application restrictions allow this server/container, or temporarily set Application restrictions to None for server-side diagnosis.")
            self.stdout.write("  4. Ensure API restrictions on the key include Custom Search API; do not reuse a browser-restricted Maps key.")
            self.stdout.write("  5. Verify the Programmable Search Engine id (cx) is copied into UL_GOOGLE_SEARCH_TENANT or UL_GOOGLE_SEARCH_CX.")
            raise CommandError("Google Custom Search diagnostic failed") from exc

        self.stdout.write(self.style.SUCCESS(f"PASS: request succeeded; returned {len(results)} result(s)."))
