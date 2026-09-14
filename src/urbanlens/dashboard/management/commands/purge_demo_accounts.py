"""Delete demo accounts whose day is up.

Run on a schedule on the demo instance.
"""

from __future__ import annotations

from datetime import timedelta
import logging

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from urbanlens.dashboard.services.demo import DEMO_USERNAME_PREFIX

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Purge expired demo accounts."""

    help = "Delete demo accounts older than the TTL. Dry-run unless --execute is given."

    def add_arguments(self, parser) -> None:
        """Register CLI arguments."""
        parser.add_argument("--ttl-hours", type=int, default=24, help="Age at which a demo account is purged.")
        parser.add_argument("--execute", action="store_true", help="Actually delete. Without this, only reports.")
        parser.add_argument(
            "--allow-non-demo",
            action="store_true",
            help="Run even though UL_DEMO_MODE is off. Only for a scratch database you are certain holds no real data.",
        )

    def handle(self, *args, **options) -> None:
        """Find and optionally delete expired demo accounts.

        Raises:
            CommandError: Not a demo instance and not explicitly overridden.
        """
        from urbanlens.dashboard.services.profile.account_deletion import hard_delete_profile
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        if not app_settings.demo_mode and not options["allow_non_demo"]:
            raise CommandError(
                "UL_DEMO_MODE is off. Refusing to delete accounts by username prefix on an instance that is not the demo. Pass --allow-non-demo only for a database you know holds no real data.",
            )

        cutoff = timezone.now() - timedelta(hours=options["ttl_hours"])
        expired = User.objects.filter(username__startswith=DEMO_USERNAME_PREFIX, date_joined__lt=cutoff).order_by("pk")

        count = expired.count()
        if not count:
            self.stdout.write("No demo account has expired.")
            return

        if not options["execute"]:
            self.stdout.write(f"{count} demo account(s) older than {options['ttl_hours']}h would be deleted. Re-run with --execute.")
            for user in expired[:20]:
                self.stdout.write(f"  {user.username} (joined {user.date_joined:%Y-%m-%d %H:%M})")
            return

        deleted = 0
        for user in expired:
            profile = getattr(user, "profile", None)
            if profile is None:
                # No profile to cascade from - delete the orphaned user directly
                # rather than skipping it, or it is selected again every run.
                user.delete()
                deleted += 1
                continue
            # Reused rather than reimplemented: this is the path that also clears the profile's stored files.
            hard_delete_profile(profile)
            deleted += 1

        logger.info("demo: purged %d expired demo account(s)", deleted)
        self.stdout.write(f"Deleted {deleted} demo account(s).")
