"""Request and response schemas for the trip settings and calendar endpoints.

Two additions that ``serializers.py`` could not host, for different reasons.

**A writable permissions serializer.** ``TripPermissionsSerializer`` over there
is the *response* shape embedded in ``TripDetailSerializer``; all four of its
fields are ``read_only`` and must stay that way, because making a response
schema writable is how a field nobody meant to expose becomes settable. The
write side is a genuinely different contract - every field optional, at least
one required overall - so it is a separate class rather than a flag on that
one.

**Calendar payloads.** The export/unexport pair answers with a status block
plus one summary number, and refuses with a machine-readable ``error_code``
alongside a URL the client is expected to *open in a system browser*. Those
shapes are declared here so the published schema describes them rather than
leaving a native client to discover them from a sample response.

Nothing in this module holds business logic. The permission vocabulary comes
from ``Trip.PERMISSION_CHOICES`` and the status block from
``services.calendar_sync.trip_calendar_status``, so neither can drift into a
second definition that only the API believes in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rest_framework import serializers

from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.services.trip_crud import TRIP_PERMISSION_FIELDS

if TYPE_CHECKING:
    from typing import Any


class TripPermissionsUpdateSerializer(serializers.Serializer):
    """A presence-keyed partial update of a trip's four permission levels.

    Every field is optional and **none carries a default**, which is the whole
    point: a field the caller omitted must be absent from ``validated_data`` so
    that :func:`~urbanlens.dashboard.services.trip_crud.set_trip_permissions`
    can leave it alone. A ``default=`` on any of these would silently
    reintroduce the very defect the service was fixed for - a client toggling
    one switch rewriting three other permissions on a trip it shares with other
    people - and would do so invisibly, because the request would still look
    like a partial update from the outside.

    Submitting nothing at all is refused rather than treated as a no-op. An
    empty body is far more likely to be a client that failed to serialize its
    form than a deliberate request to change nothing, and answering 200 to it
    would report success for a write that never happened.
    """

    allow_add_members = serializers.ChoiceField(choices=Trip.PERMISSION_CHOICES, required=False)
    allow_add_activities = serializers.ChoiceField(choices=Trip.PERMISSION_CHOICES, required=False)
    allow_edit_activities = serializers.ChoiceField(choices=Trip.PERMISSION_CHOICES, required=False)
    allow_comments = serializers.ChoiceField(choices=Trip.PERMISSION_CHOICES, required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Require at least one permission field to be present.

        Args:
            attrs: The fields actually submitted, already choice-validated.

        Returns:
            The same mapping, unchanged.

        Raises:
            serializers.ValidationError: The body named none of the four
                permission fields.
        """
        if not any(field in attrs for field in TRIP_PERMISSION_FIELDS):
            raise serializers.ValidationError(f"Submit at least one of: {', '.join(TRIP_PERMISSION_FIELDS)}.")
        return attrs


class TripCalendarStatusSerializer(serializers.Serializer):
    """Whether a trip is mirrored to the caller's Google Calendar (schema-only).

    ``connected`` and ``linked`` are separate on purpose: a connected account
    with no link means the calendar is reachable but this trip has never been
    exported to it, which is a different next action for the client than having
    no account at all.

    ``account_email`` answers a question the older status block could not: a
    user with more than one Google account can otherwise see that a shared
    trip's itinerary is being written to *some* calendar without being able to
    tell which. It is read from the same account row ``connected`` is derived
    from, so it costs no extra query.
    """

    #: A GoogleCalendarAccount exists for the caller.
    connected = serializers.BooleanField(read_only=True)
    #: A trip-level export link exists for this trip and caller.
    linked = serializers.BooleanField(read_only=True)
    #: Whether later edits keep pushing to the linked event.
    auto_sync = serializers.BooleanField(read_only=True)
    last_synced = serializers.DateTimeField(read_only=True, allow_null=True)
    #: Address of the connected Google account, or null when none is connected.
    account_email = serializers.CharField(read_only=True, allow_null=True)


class TripCalendarExportRequestSerializer(serializers.Serializer):
    """Options for a trip export.

    ``auto_sync`` is presence-keyed like everything else on this surface:
    omitting it leaves whatever the existing link already had, so re-exporting
    a trip to refresh its dates cannot quietly switch off a user's ongoing
    sync.
    """

    auto_sync = serializers.BooleanField(required=False)


class TripCalendarExportResponseSerializer(serializers.Serializer):
    """The result of an export: the refreshed status plus what was written."""

    calendar = TripCalendarStatusSerializer(read_only=True)
    #: How many scheduled activities were mirrored as their own timed events.
    #: Zero is normal - a trip with no scheduled stops still exports its own
    #: all-day event.
    activities_exported = serializers.IntegerField(read_only=True)


class TripCalendarRemovalResponseSerializer(serializers.Serializer):
    """The result of an unexport: the refreshed status plus whether anything went."""

    calendar = TripCalendarStatusSerializer(read_only=True)
    #: False when the trip was not on this caller's calendar to begin with.
    #: Deliberately not an error: unexporting something already absent is the
    #: state the caller asked for, and an offline client retrying a delete it
    #: never saw acknowledged must not be told it failed.
    removed = serializers.BooleanField(read_only=True)


class TripCalendarBlockedSerializer(serializers.Serializer):
    """A 409 the client resolves by sending the user through a browser.

    Both cases this covers - no connected calendar, and a grant Google has
    since rejected - are fixed by the same one-time consent flow, which cannot
    happen over an API credential: it ends in a redirect back to a callback
    that needs a browser session to identify the user.

    ``error_code`` is machine-readable because the two cases warrant different
    prose ("connect your calendar" versus "your connection expired") but the
    same action, and a client should branch on the code rather than on a
    message that may be reworded.
    """

    error = serializers.CharField(read_only=True)
    #: ``calendar_not_connected`` or ``calendar_reauthorization_required``.
    error_code = serializers.CharField(read_only=True)
    #: The **site's own** connect route, to be opened in a system browser.
    #: Never a Google authorization URL: one minted by this API would return to
    #: the OAuth callback with no session attached, bounce to the login page,
    #: and lose the authorization code on the way.
    authorization_url = serializers.CharField(read_only=True)
