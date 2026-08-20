"""Export the site's public locations for import into a demo instance.

Run on the **real** site; read-only. Writes JSON that
``import_public_locations`` loads on the demo instance.

What "public" means here is not a judgement call this command makes. A location
is public only when its ``PublicPinCandidate`` reached ``PASSED`` - the outcome
of the community vote in ``services.pins.public_pins``. Nothing else qualifies,
and in particular **a location having a wiki does not**: wiki visibility is
*earned* per viewer (you must already hold a pin on that place or its place
domain), and ``resolve_visible_wiki`` deliberately 404s indistinguishably "so
guessing slugs can never reveal which locations other users have pinned".
Exporting wiki-backed locations would publish precisely the set that design
protects, for every place any user has ever pinned.

The same reasoning bounds what travels with each location. Coordinates and the
wiki's *cached* material - photos nobody authored, alternate names - describe
the place. Comments, articles, edit history and votes describe **people**, and
are never exported, so no profile, username or authored text can ride along.

An empty export is the correct and expected result on a site where no candidate
has passed yet. It is not a failure.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Write public locations, and their non-personal cached wiki data, to JSON."""

    help = "Export PASSED public locations (coordinates + cached wiki photos/aliases) for a demo instance."

    def add_arguments(self, parser) -> None:
        """Register CLI arguments."""
        parser.add_argument("--out", required=True, help="Path to write the JSON export to.")
        parser.add_argument("--indent", type=int, default=2, help="JSON indentation, for a readable diff.")

    def handle(self, *args, **options) -> None:
        """Collect and write the export.

        Raises:
            CommandError: The destination could not be written.
        """
        from urbanlens.dashboard.models.public_pins.model import PublicPinCandidate

        candidates = (
            PublicPinCandidate.objects.passed()
            .select_related("location", "location__wiki")
            .order_by("pk")
        )

        payload: list[dict[str, Any]] = []
        for candidate in candidates:
            location = candidate.location
            if location is None or location.latitude is None or location.longitude is None:
                continue
            payload.append(
                {
                    "latitude": str(location.latitude),
                    "longitude": str(location.longitude),
                    "official_name": location.official_name or "",
                    "wiki": self._wiki_payload(location),
                },
            )

        destination = Path(options["out"])
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps({"locations": payload}, indent=options["indent"]), encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"Could not write {destination}: {exc}") from exc

        self.stdout.write(f"Exported {len(payload)} public location(s) to {destination}")
        if not payload:
            self.stdout.write("No candidate has passed the public vote yet - an empty export is expected, not an error.")

    @staticmethod
    def _wiki_payload(location) -> dict[str, Any] | None:
        """The non-personal half of a public location's wiki.

        Args:
            location: The public Location.

        Returns:
            Name, aliases and cached photo URLs, or None when there is no wiki.
            Deliberately omits articles, comments, edits and every ``created_by``
            - those are authored by identifiable people.
        """
        wiki = getattr(location, "wiki", None)
        if wiki is None:
            return None

        # profile__isnull=True is the whole test for "nobody authored this":
        # images cached from a provider carry no owner, images a user uploaded
        # always do.
        photos = [
            image.image.name
            for image in wiki.images.filter(profile__isnull=True).order_by("pk")
            if getattr(image, "image", None) and image.image.name
        ]
        aliases = list(wiki.aliases.order_by("pk").values_list("name", flat=True))
        return {"name": wiki.name or "", "aliases": aliases, "photos": photos}
