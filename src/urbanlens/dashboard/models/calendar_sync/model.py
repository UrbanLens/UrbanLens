"""Per-user Google Calendar integration models.

Each user connects *their own* Google account via OAuth; the tokens stored
here grant access to that user's personal calendar only. There is no
site-wide calendar. ``GoogleCalendarAccount`` holds the OAuth tokens for one
profile, and ``TripCalendarLink`` records which trips have been mirrored to
(or created from) which calendar events so imports and exports stay
idempotent.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from cryptography.fernet import InvalidToken
from django.db.models import (
    CASCADE,
    BooleanField,
    CharField,
    DateTimeField,
    ForeignKey,
    Index,
    OneToOneField,
    Q,
    TextChoices,
    TextField,
    UniqueConstraint,
)
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.fields import EncryptedTextField

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class CalendarSyncDirection(TextChoices):
    """Origin of a trip↔event link.

    Attributes:
        IMPORTED: The trip was created in UrbanLens from a calendar event.
        EXPORTED: The calendar event was created from an UrbanLens trip.
    """

    IMPORTED = "imported", "Imported from Google Calendar"
    EXPORTED = "exported", "Exported to Google Calendar"


class GoogleCalendarAccount(abstract.DashboardModel):
    """OAuth credentials for one user's own Google Calendar.

    One row per profile. Created by the "Connect Google Calendar" flow and
    deleted (after best-effort token revocation) when the user disconnects.
    """

    profile = OneToOneField(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="google_calendar_account",
    )
    google_email = CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Email of the connected Google account, for display only.",
    )
    access_token = EncryptedTextField()
    refresh_token = EncryptedTextField(null=True, blank=True)
    token_expiry = DateTimeField(null=True, blank=True)
    calendar_id = CharField(
        max_length=255,
        default="primary",
        help_text="Target calendar for imports/exports. 'primary' is the user's main calendar.",
    )
    scopes = TextField(default="", blank=True)

    if TYPE_CHECKING:
        profile_id: int

    @property
    def is_token_expired(self) -> bool:
        """Whether the access token is expired or about to expire.

        A 60-second safety margin is applied so a token that expires mid-call
        is treated as already expired.

        Returns:
            True when the token must be refreshed before use.
        """
        if self.token_expiry is None:
            return True
        return self.token_expiry <= timezone.now() + datetime.timedelta(seconds=60)

    def __str__(self) -> str:
        return f"Google Calendar for {self.profile} ({self.google_email or 'unknown email'})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_google_calendar_accounts"


def get_calendar_account(profile: Profile) -> GoogleCalendarAccount | None:
    """Fetch the profile's Google Calendar connection, healing it if undecryptable.

    A row whose ``access_token``/``refresh_token`` can no longer be decrypted
    (e.g. ``UL_FIELD_ENCRYPTION_KEY`` rotated without migrating old rows) is
    permanently unusable - there is no way to recover the plaintext tokens.
    Rather than raising and crashing every page that touches the user's
    calendar connection, treat it as absent and delete the stale row so the
    "Connect Google Calendar" flow is offered again.

    Args:
        profile: The profile whose calendar connection to look up.

    Returns:
        The connected account, or None if there isn't one (or it was just
        removed for being undecryptable).
    """
    try:
        return GoogleCalendarAccount.objects.filter(profile=profile).first()
    except InvalidToken:
        logger.exception(
            "GoogleCalendarAccount for profile %s has undecryptable tokens (field_encryption_key "
            "changed?) - removing it so the user can reconnect.",
            profile.id,
        )
        GoogleCalendarAccount.objects.filter(profile=profile).delete()
        return None


class TripCalendarLink(abstract.DashboardModel):
    """Association between a trip (or one of its activities) and one user's Google Calendar event.

    Each member exports a trip to their *own* calendar, so a trip can have
    one link per profile. Exports mirror the trip itself as an all-day event
    (``activity`` is null) plus one timed event per scheduled activity
    (``activity`` set). The link also dedupes imports: an event that already
    has a link for this profile is never imported twice.
    """

    trip = ForeignKey(
        "dashboard.Trip",
        on_delete=CASCADE,
        related_name="calendar_links",
    )
    # Set when this link mirrors a single scheduled activity rather than the
    # whole trip. Deleting the activity cascades away its event link (the
    # orphaned Google event is cleaned up on the next export/removal sync).
    activity = ForeignKey(
        "dashboard.TripActivity",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="calendar_links",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="trip_calendar_links",
    )
    google_calendar_id = CharField(max_length=255, default="primary")
    # Google event IDs are 5-1024 chars (base32hex).
    google_event_id = CharField(max_length=1024)
    direction = CharField(max_length=10, choices=CalendarSyncDirection.choices)
    last_synced = DateTimeField(null=True, blank=True)
    auto_sync = BooleanField(
        default=False,
        help_text="Push future changes to this trip and its activities to the linked calendar event automatically. One-way only - edits made on Google Calendar are never pulled back.",
    )

    if TYPE_CHECKING:
        trip_id: int
        activity_id: int | None
        profile_id: int

    def __str__(self) -> str:
        subject = self.activity if self.activity_id else self.trip
        return f"{subject} ↔ event {self.google_event_id} ({self.direction}, {self.profile})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trip_calendar_links"
        constraints = [
            UniqueConstraint(
                fields=("trip", "profile"),
                condition=Q(activity__isnull=True),
                name="db_trip_calendar_link_unique",
            ),
            UniqueConstraint(
                fields=("trip", "profile", "activity"),
                name="db_trip_calendar_link_activity_unique",
            ),
        ]
        indexes = [
            Index(fields=["profile", "google_event_id"], name="idxdb_tcl_profile_event"),
        ]
