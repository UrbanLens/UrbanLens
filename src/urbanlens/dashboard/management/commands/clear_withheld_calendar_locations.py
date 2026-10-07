"""Rewrite the Google Calendar events UrbanLens made that may still hold a location or title now withheld.

An export without auto-sync is never pushed, so an event keeps what it was last given until its owner exports the
trip again. Exports made under 0.8.0 left a stop's address on the event, and its place's name in the title, after the
stop was hidden or its adder restricted who may see it (P335). This rewrites, once, each such event whose link does
not already vouch for the body it would now get: the location is cleared and the title masked as the activities panel
masks it. An event with nothing withheld is not touched, an event the user deleted is not recreated, and an event an
import linked from the user's own calendar is left alone. See UrbanLens#301 ("hidden location stays on an unreached calendar").

Dry-run by default; ``--apply`` writes. Every write goes through the calendar gateway and its rate limiter
(``google_calendar``, shared with members' own exports). When the budget runs out the command waits for the next
minute and goes on, or with ``--no-wait`` stops. Either way a second run skips each event already rewritten.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand, CommandError

if TYPE_CHECKING:
    from argparse import ArgumentParser

logger = logging.getLogger(__name__)

#: How long to wait when the budget runs out and the refusal names no wait: the limiter counts by the minute.
_BUDGET_WAIT_SECONDS = 60
#: Waits in a row with nothing written, after which the budget is taken to be spent for the day and the run stops.
_MAX_IDLE_WAITS = 15


class Command(BaseCommand):
    """Clear withheld locations and titles from the calendar events of exports nothing pushes to."""

    help = "Rewrite UrbanLens-made Google Calendar events that may hold a location or title now withheld. Dry-run unless --apply."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Register the command's flags.

        Args:
            parser: The argument parser.
        """
        parser.add_argument("--apply", action="store_true", help="Rewrite the events; without it, only count them.")
        parser.add_argument("--no-wait", action="store_true", help="Stop when the calendar budget runs out instead of waiting for it.")

    def handle(self, *args: Any, **options: Any) -> None:
        """Walk every trip on a connected calendar, oldest link first, and rewrite (or count) what may be withheld.

        Args:
            *args: Unused.
            **options: ``apply`` and ``no_wait``.

        Raises:
            CommandError: Google refused this site rather than a user, so no calendar can be written.
        """
        from urbanlens.dashboard.models.calendar_sync.model import GoogleCalendarAccount, TripCalendarLink
        from urbanlens.dashboard.services.apis.calendar.google import CalendarUnavailableError
        from urbanlens.dashboard.services.auth.google_oauth import GoogleAuthExpiredError
        from urbanlens.dashboard.services.core.gateway import GatewayRequestError
        from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError
        from urbanlens.dashboard.services.trips.calendar_sync import WithheldEventsReport, clear_withheld_events

        apply: bool = options["apply"]
        wait = not options["no_wait"]
        found = WithheldEventsReport()
        refused_grants = failed = 0
        idle_waits = 0
        accounts: dict[int, GoogleCalendarAccount | None] = {}
        links = TripCalendarLink.objects.filter(activity__isnull=True, profile__google_calendar_account__isnull=False).select_related("trip", "profile").order_by("pk")
        for link in links.iterator():
            if link.profile_id not in accounts:
                # None for a connection this process cannot decrypt.
                accounts[link.profile_id] = GoogleCalendarAccount.objects.get_for_profile(link.profile)
            account = accounts[link.profile_id]
            if account is None:
                continue
            while True:
                attempt = WithheldEventsReport()
                try:
                    clear_withheld_events(account, link.trip, attempt, apply=apply)
                except RateLimitExceededError as exc:
                    found.written += attempt.written
                    found.gone += attempt.gone
                    idle_waits = 0 if attempt.written else idle_waits + 1
                    if not wait or idle_waits > _MAX_IDLE_WAITS:
                        self.stdout.write(f"The calendar budget ran out after {found.written} rewritten events. Run the command again to continue.")
                        return
                    seconds = getattr(exc, "retry_after", None) or _BUDGET_WAIT_SECONDS
                    self.stdout.write(f"The calendar budget ran out; waiting {seconds}s.")
                    time.sleep(seconds)
                    continue
                except CalendarUnavailableError as exc:
                    raise CommandError("Google refused this site's Google project or OAuth client; nothing can be written until that is fixed.") from exc
                except GoogleAuthExpiredError:
                    refused_grants += 1
                except ValueError:
                    # No dates, so nothing of the trip was ever exported.
                    pass
                except GatewayRequestError:
                    logger.warning("Could not clear withheld fields of trip %s on profile %s's calendar.", link.trip_id, link.profile_id, exc_info=True)
                    failed += 1
                found.stale += attempt.stale
                found.written += attempt.written
                found.gone += attempt.gone
                if attempt.written:
                    idle_waits = 0
                break

        if apply:
            self.stdout.write(f"Rewrote {found.written} events; {found.gone} were gone from their calendars and were not recreated.")
        else:
            self.stdout.write(f"{found.stale} events may hold a location or title now withheld. Run with --apply to rewrite them.")
        if refused_grants or failed:
            self.stdout.write(f"Skipped {refused_grants} trips whose calendar grant Google refused and {failed} that failed; see the log.")
