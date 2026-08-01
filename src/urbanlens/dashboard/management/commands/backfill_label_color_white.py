"""One-off backfill: labels with no color ("clear") now default to white for new labels.

TEMPORARY - delete this command once it has been run against production. It exists
only to bring existing rows in line with the new default (see LabelCreateView and
the external API's label-create endpoint, which now fall back to
DEFAULT_LABEL_COLOR instead of None when the caller doesn't pick a color).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import DatabaseError
from django.db.models import Q

from urbanlens.dashboard.models.labels.meta import DEFAULT_LABEL_COLOR
from urbanlens.dashboard.models.labels.model import Label


class Command(BaseCommand):
    """Set color=white on every Label currently left clear (null or blank)."""

    help = "Backfill Label.color to white for every label currently set to clear (null/blank)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print what would change without saving.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        queryset = Label.objects.filter(Q(color__isnull=True) | Q(color=""))
        total = queryset.count()
        self.stdout.write(f"Found {total} label(s) with a clear color.")

        if total == 0:
            return

        if dry_run:
            self.stdout.write(f"Would set color={DEFAULT_LABEL_COLOR!r} on {total} label(s).")
            return

        # Saved one at a time (not queryset.update()) so Label's post_save signal
        # fires and refreshes the map pin cache for every pin carrying each label -
        # a bulk update would leave those pins showing the old clear color until
        # something else happened to touch them.
        updated = 0
        for label in queryset.iterator():
            label.color = DEFAULT_LABEL_COLOR
            try:
                label.save(update_fields=["color"])
            except DatabaseError:
                self.stderr.write(f"  [pk={label.pk}] save failed")
                continue
            updated += 1

        self.stdout.write(f"Done. Updated {updated} label(s) to color={DEFAULT_LABEL_COLOR!r}.")
