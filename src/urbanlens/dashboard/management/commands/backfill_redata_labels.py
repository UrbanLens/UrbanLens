"""Backfill: push every profile's existing tag/category taxonomy and pin assignments to REData.

Ongoing changes are kept in sync automatically once this integration is
live (see ``models.labels.signals`` and the ``Pin.labels`` m2m_changed
receiver in ``models.pin.signals``), but signals only fire on *future*
writes. Data created before this integration shipped - and REData's own
state if it is ever reset - needs this command to prime it. Safe to run
repeatedly: REData's own upsert/resend semantics mean this never duplicates
or corrupts existing state.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.labels import redata_suggestions


class Command(BaseCommand):
    """Sync every profile's tag/category labels and pin assignments to REData."""

    help = "Backfill every profile's existing tag/category label taxonomy and pin assignments to REData."

    def handle(self, *args, **options):
        if not redata_suggestions.redata_labels_configured():
            self.stderr.write("REData is not configured (UL_REDATA_API_URL/UL_REDATA_API_KEY) - nothing to do.")
            return

        profiles = list(Profile.objects.all())
        self.stdout.write(f"Backfilling REData labels for {len(profiles)} profile(s)...")

        for profile in profiles:
            labels_synced, pins_synced = redata_suggestions.backfill_profile(profile)
            self.stdout.write(f"  [{profile.pk}] synced {labels_synced} label(s), {pins_synced} pin(s)")

        self.stdout.write("Done.")
