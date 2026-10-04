"""Read-only list of Wayback snapshots taken of links that work as a password (P289).

Until the archive task refused them, a share link on a pin or wiki was handed to Save Page Now, which publishes its
capture. A removal from the Wayback Machine is requested from the Internet Archive, by snapshot URL.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from urbanlens.dashboard.models.links.model import PinLink, WikiLink
from urbanlens.dashboard.services.security.capability_urls import is_capability_url


class Command(BaseCommand):
    """Print every stored snapshot whose original URL grants access to whoever holds it."""

    help = "List Wayback snapshots stored for share links (Drive, Dropbox, token URLs). Never writes."

    def handle(self, *args, **options):
        found = 0
        for model in (PinLink, WikiLink):
            rows = model.objects.exclude(wayback_url="").order_by("pk").values_list("pk", "url", "wayback_url")
            for pk, url, snapshot in rows.iterator(chunk_size=2000):
                if is_capability_url(url):
                    found += 1
                    self.stdout.write(f"{model.__name__} #{pk}\t{snapshot}")
        self.stdout.write(f"{found} snapshot(s) of share links.")
