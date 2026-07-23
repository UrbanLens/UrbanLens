"""Custom queryset/manager for GoogleCalendarAccount and TripCalendarLink."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cryptography.fernet import InvalidToken

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.calendar_sync.model import GoogleCalendarAccount, TripCalendarLink
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import Trip

logger = logging.getLogger(__name__)


class GoogleCalendarAccountManager(abstract.DashboardManager):
    """Adds a lookup that self-heals when a stored token can't be decrypted.

    Mirrors ``ImmichAccountManager.get_for_profile()`` - a field-encryption-key
    change (see ``models.fields.EncryptedTextField``) leaves any previously-saved
    ``access_token``/``refresh_token`` permanently unreadable, so every page or
    task that touches the account crashes with ``InvalidToken`` unless callers
    treat that the same as "never connected" and remove the now-useless row.
    """

    def get_for_profile(self, profile: Profile) -> GoogleCalendarAccount | None:
        """Return this profile's Google Calendar connection, or None if absent or undecryptable.

        Args:
            profile: The profile whose calendar connection to look up.

        Returns:
            The connected account, or None if there isn't one (or it was just
            removed for being undecryptable).
        """
        try:
            return self.filter(profile=profile).first()
        except InvalidToken:
            logger.exception(
                "GoogleCalendarAccount for profile %s has undecryptable tokens (field_encryption_key changed?) - removing it so the user can reconnect.",
                profile.id,
            )
            self.filter(profile=profile).delete()
            return None


class TripCalendarLinkQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for TripCalendarLink models."""

    def for_trip_and_profile(self, trip: Trip, profile: Profile) -> TripCalendarLinkQuerySet:
        """Every link (trip-level and activity-level) for one trip+profile pair.

        Args:
            trip: The trip.
            profile: The member whose links to look up.

        Returns:
            Matching links.
        """
        return self.filter(trip=trip, profile=profile)

    def trip_level(self) -> TripCalendarLinkQuerySet:
        """Links to the trip itself, rather than one of its scheduled activities."""
        return self.filter(activity__isnull=True)

    def activity_level(self) -> TripCalendarLinkQuerySet:
        """Links to a specific scheduled activity, rather than the trip as a whole."""
        return self.filter(activity__isnull=False)

    def trip_level_link(self, trip: Trip, profile: Profile):
        """The single trip-level link for a trip+profile pair, if any.

        Args:
            trip: The trip.
            profile: The member whose link to look up.

        Returns:
            The matching TripCalendarLink, or None.
        """
        return self.for_trip_and_profile(trip, profile).trip_level().first()

    def activity_links_by_activity_id(self, trip: Trip, profile: Profile) -> dict[int, TripCalendarLink]:
        """Activity-level links for a trip+profile, keyed by activity id.

        Args:
            trip: The trip.
            profile: The member whose links to look up.

        Returns:
            Mapping of activity id to its TripCalendarLink.
        """
        return {link.activity_id: link for link in self.for_trip_and_profile(trip, profile).activity_level()}

    def already_linked(self, profile: Profile, event_id: str) -> bool:
        """Whether a Google Calendar event is already linked for this profile.

        Args:
            profile: The profile whose links to check.
            event_id: The Google Calendar event id.

        Returns:
            True if a link already exists (import/export already ran for this event).
        """
        return self.filter(profile=profile, google_event_id=event_id).exists()

    def set_auto_sync(self, link_pk: int, auto_sync: bool) -> None:
        """Update just the auto_sync flag for one link, by pk.

        Args:
            link_pk: Primary key of the link to update.
            auto_sync: New auto_sync value.
        """
        self.filter(pk=link_pk).update(auto_sync=auto_sync)


class TripCalendarLinkManager(abstract.DashboardManager.from_queryset(TripCalendarLinkQuerySet)):
    """Custom query manager for TripCalendarLink models."""
