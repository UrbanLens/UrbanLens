"""Custom queryset/manager for TripCalendarLink."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.calendar_sync.model import TripCalendarLink
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import Trip


class TripCalendarLinkQuerySet(abstract.DashboardQuerySet["TripCalendarLink"]):
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

    def forget_written_events(self, profile: Profile) -> int:
        """Stop trusting that this profile's calendar holds what UrbanLens last wrote to it.

        Called when the profile (re)connects a Google account, which may not be the one the events went to: the next
        export rewrites every event instead of skipping it as current.

        Args:
            profile: The profile that connected.

        Returns:
            How many links were reset.
        """
        return self.filter(profile=profile).exclude(event_fingerprint="").update(event_fingerprint="")

    def set_auto_sync(self, link_pk: int, auto_sync: bool) -> None:
        """Update just the auto_sync flag for one link, by pk.

        Args:
            link_pk: Primary key of the link to update.
            auto_sync: New auto_sync value.
        """
        self.filter(pk=link_pk).update(auto_sync=auto_sync)


_TripCalendarLinkManagerBase = abstract.DashboardManager.from_queryset(TripCalendarLinkQuerySet)


class TripCalendarLinkManager(_TripCalendarLinkManagerBase):
    """Custom query manager for TripCalendarLink models."""
