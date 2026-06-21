"""Management command to diagnose Google Custom Search JSON API configuration."""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from urbanlens.dashboard.services.google.search import GoogleCustomSearchError, GoogleCustomSearchGateway, _mask_secret
from urbanlens.UrbanLens.settings.app import settings


def custom_search_fix_steps(error_message: str) -> list[str]:
    """Return the most relevant operator actions for a Custom Search failure."""
    normalized = error_message.lower()
    if "does not have the access to custom search json api" in normalized:
        return [
            "The API key is valid, but the Google Cloud project that owns it cannot use Custom Search JSON API.",
            "In that exact project, open APIs & Services > Library and enable Custom Search API / Custom Search JSON API.",
            "If the API is already enabled elsewhere, create/copy the key from that enabled project instead; API access is project-specific.",
            "After enabling it, wait a few minutes, restart the app/container, and rerun this command.",
        ]
    if "referer <empty>" in normalized or "referer" in normalized:
        return [
            "This key is restricted by HTTP referrer, but UrbanLens calls Custom Search from the server where the Referer header is empty.",
            "Create a separate server-side API key for UL_GOOGLE_SEARCH_API_KEY, or change this key's Application restrictions from HTTP referrers to None/IP addresses that match the server.",
            "Keep API restrictions enabled, but include Custom Search API / Custom Search JSON API for the search key.",
            "Do not reuse a browser-restricted Maps key for UL_GOOGLE_SEARCH_API_KEY.",
        ]
    if "apikey" in normalized or "api key" in normalized or "key" in normalized:
        return [
            "Verify UL_GOOGLE_SEARCH_API_KEY is copied from the same Google Cloud project where Custom Search JSON API is enabled.",
            "Ensure API restrictions on the key include Custom Search API / Custom Search JSON API.",
            "If the key has Application restrictions, make sure they allow requests from this server/container.",
        ]
    return [
        "Enable the Custom Search API / Custom Search JSON API for this Google Cloud project.",
        "Confirm billing/quota is available for the project and the daily Custom Search quota is not exhausted.",
        "Use an API key whose Application restrictions allow this server/container, or temporarily set Application restrictions to None for server-side diagnosis.",
        "Ensure API restrictions on the key include Custom Search API; do not reuse a browser-restricted Maps key.",
        "Verify the Programmable Search Engine id (cx) is copied into UL_GOOGLE_SEARCH_TENANT or UL_GOOGLE_SEARCH_CX.",
    ]


class Command(BaseCommand):
    """Run a single Google Custom Search JSON API request and print likely fixes."""

    help = "Diagnose Google Custom Search JSON API issues (403, bad key, wrong CSE id)"

    def add_arguments(self, parser):
        parser.add_argument("--query", default="UrbanLens", help="Query to test with Google Custom Search")
        parser.add_argument("--api-key", default=None, help="Override UL_GOOGLE_SEARCH_API_KEY")
        parser.add_argument("--cx", default=None, help="Override UL_GOOGLE_SEARCH_TENANT/UL_GOOGLE_SEARCH_CX")

    def handle(self, *args, **options):
        api_key = options["api_key"] or settings.google_search_api_key
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
            self.stdout.write("\nRecommended fix:")
            for index, step in enumerate(custom_search_fix_steps(str(exc)), start=1):
                self.stdout.write(f"  {index}. {step}")
            raise CommandError("Google Custom Search diagnostic failed") from exc

        self.stdout.write(self.style.SUCCESS(f"PASS: request succeeded; returned {len(results)} result(s)."))
