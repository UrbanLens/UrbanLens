"""Relabel photos that connected-account imports filed as manual uploads.

achievements - is decided by ``Image.is_own_contribution``, which does not read ``source`` at all,
so a mislabelled row was never a privacy or scoring problem.
The Immich half is one query per connected account, because an Immich URL is the user's own server
and there is no single prefix - which is also why an account that has since been disconnected cannot
be matched at all.

- concealment, who may withdraw a photo from a wiki, reputation, the upload
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand

if TYPE_CHECKING:
    from argparse import ArgumentParser


class Command(BaseCommand):
    """Relabel ``UPLOAD`` rows that were really connected-account imports."""

    help = "Relabel Image rows written by the Immich/Google Photos importers before they set source=."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Register the command's flags.

        Args:
            parser: The argument parser.
        """
        parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")

    def handle(self, *args: Any, **options: Any) -> None:
        """Count, then relabel unless asked not to.

        Args:
            *args: Unused. **options: Parsed command options.
        """
        from urbanlens.dashboard.models.images.model import Image, ImageSource
        from urbanlens.dashboard.models.immich.model import ImmichAccount
        from urbanlens.dashboard.services.apis.photos.google import GOOGLE_PHOTOS_URL_PREFIX

        dry_run = bool(options["dry_run"])
        mislabelled = Image.objects.filter(source=ImageSource.UPLOAD)

        google = mislabelled.filter(source_url__startswith=GOOGLE_PHOTOS_URL_PREFIX)
        relabelled = self._relabel(google, ImageSource.GOOGLE_PHOTOS, "Google Photos", dry_run=dry_run)

        for account in ImmichAccount.objects.all():
            scope = mislabelled.filter(source_url__startswith=account.asset_url_prefix())
            relabelled += self._relabel(scope, ImageSource.IMMICH, f"Immich ({account.server_url})", dry_run=dry_run)

        if not relabelled:
            self.stdout.write("Nothing to relabel.")
        elif dry_run:
            self.stdout.write(self.style.WARNING(f"Would relabel {relabelled} row(s). Re-run without --dry-run to apply."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Relabelled {relabelled} row(s)."))

    def _relabel(self, scope, source: str, label: str, *, dry_run: bool) -> int:
        """Relabel one matched set, or report it.

        Args:
            scope: The rows to relabel.
            source: The ``ImageSource`` value to write.
            label: Human-readable name of the provider, for the output line.
            dry_run: When True, count without writing.

        Returns:
            How many rows matched.
        """
        # Materialised before the update so the count is of what was matched,
        # not of what a re-evaluated queryset finds afterwards.
        ids = list(scope.values_list("pk", flat=True))
        if not ids:
            return 0
        self.stdout.write(f"{label}: {len(ids)} row(s)")
        if not dry_run:
            from urbanlens.dashboard.models.images.model import Image

            Image.objects.filter(pk__in=ids).update(source=source)
        return len(ids)
