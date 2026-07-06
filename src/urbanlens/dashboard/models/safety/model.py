"""Safety check-in models: emergency contact defaults, check-ins, per-checkin contacts, and chat."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from django.db.models import (
    CASCADE,
    SET_NULL,
    CheckConstraint,
    DecimalField,
    DurationField,
    EmailField,
    ForeignKey,
    Index,
    IntegerField,
    Manager as DjangoManager,
    OneToOneField,
    Q,
    TextField,
    UUIDField,
)
from django.db.models.fields import CharField, DateTimeField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.safety.queryset import SafetyCheckinManager

logger = logging.getLogger(__name__)

DEFAULT_GRACE_PERIOD = timedelta(hours=1)


class EmergencyContactDefault(abstract.Model):
    """A reusable emergency contact saved to a profile's safety defaults.

    Copied onto each new SafetyCheckin as a SafetyCheckinContact snapshot at
    creation time - editing a default here does not retroactively change any
    check-in that already copied it. Exactly one of contact_profile/email
    identifies the contact.
    """

    owner = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="safety_contact_defaults")
    contact_profile = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="+")
    email = EmailField(null=True, blank=True)
    label = CharField(max_length=150, blank=True, default="")
    order = IntegerField(default=0)

    if TYPE_CHECKING:
        owner_id: int
        contact_profile_id: int | None

    @property
    def display_name(self) -> str:
        """Return the best available display name for this contact.

        Returns:
            The label if set, else the linked profile's username, else the raw email.
        """
        if self.label:
            return self.label
        if self.contact_profile:
            return self.contact_profile.username
        return self.email or "Unknown contact"

    def __str__(self) -> str:
        """Return a human-readable description of this default contact.

        Returns:
            String like "<name> (default for <owner id>)".
        """
        return f"{self.display_name} (default for {self.owner_id})"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_safety_contact_defaults"
        ordering = ["order", "created"]
        indexes = [Index(fields=["owner"], name="idxdb_ecd_owner")]
        constraints = [
            CheckConstraint(
                condition=Q(contact_profile__isnull=False) ^ Q(email__isnull=False),
                name="db_safety_contact_default_exactly_one_target",
            ),
        ]


class SafetyPreference(abstract.Model):
    """Per-profile defaults applied to new safety check-ins."""

    profile = OneToOneField("dashboard.Profile", on_delete=CASCADE, related_name="safety_preference")
    default_message = TextField(blank=True, default="")
    default_grace_period = DurationField(default=DEFAULT_GRACE_PERIOD)

    if TYPE_CHECKING:
        profile_id: int

    @property
    def default_grace_period_hours(self) -> float:
        """``default_grace_period`` expressed in hours, for prefilling numeric form fields.

        Returns:
            The grace period as a plain float number of hours.
        """
        return self.default_grace_period.total_seconds() / 3600

    def __str__(self) -> str:
        """Return a human-readable description of this preference row.

        Returns:
            String like "Safety preferences for <profile id>".
        """
        return f"Safety preferences for {self.profile_id}"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_safety_preferences"


class SafetyCheckinStatus(abstract.TextChoices):
    """Lifecycle status of a SafetyCheckin."""

    SCHEDULED = "scheduled", "Scheduled"
    AWAITING_CHECKIN = "awaiting_checkin", "Awaiting Check-in"
    CHECKED_IN = "checked_in", "Checked In"
    OVERDUE = "overdue", "Overdue"
    FOUND_SAFE = "found_safe", "Found Safe"
    CANCELLED = "cancelled", "Cancelled"

    @classmethod
    def resolved_statuses(cls) -> tuple[str, ...]:
        """Return the statuses that mean the check-in has concluded.

        Returns:
            Tuple of terminal status values.
        """
        return (cls.CHECKED_IN, cls.FOUND_SAFE, cls.CANCELLED)


class SafetyCheckin(abstract.Model):
    """A planned trip with an expected check-in time and emergency contacts.

    If the profile doesn't check in by ``checkin_by`` + ``grace_period``, the
    linked ``SafetyCheckinContact`` rows are notified. Concluding the check-in
    (self check-in, or a contact marking the profile safe) raises a
    VisitSuggestion for the destination via ``services.safety._conclude_checkin``,
    reusing the same confirm/reject flow as any other tentative visit.

    Attributes:
        profile: The profile who created and owns this check-in.
        title: Short display label (e.g. "Weekend hike - Eagle Ridge").
        plan_details: Free-form trip plan description.
        contact_message: Custom message shown to emergency contacts.
        checkin_by: When the profile is expected to check in.
        grace_period: How long after ``checkin_by`` before contacts are notified.
        status: Current lifecycle status.
        destination_location: Shared Location for the planned destination, if known.
        destination_latitude: Destination latitude, used for the concluding VisitSuggestion.
        destination_longitude: Destination longitude, used for the concluding VisitSuggestion.
        reminder_sent_at: When the check-in-due reminder was sent, if at all.
        escalated_at: When emergency contacts were notified, if at all.
        resolved_at: When the check-in concluded, if at all.
    """

    uuid = UUIDField(default=uuid4, unique=True, editable=False)
    title = CharField(max_length=200)
    plan_details = TextField(blank=True, default="")
    contact_message = TextField(blank=True, default="")
    checkin_by = DateTimeField()
    grace_period = DurationField(default=DEFAULT_GRACE_PERIOD)
    status = CharField(max_length=20, choices=SafetyCheckinStatus.choices, default=SafetyCheckinStatus.SCHEDULED)

    destination_latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    destination_longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    reminder_sent_at = DateTimeField(null=True, blank=True)
    escalated_at = DateTimeField(null=True, blank=True)
    resolved_at = DateTimeField(null=True, blank=True)

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="safety_checkins")
    destination_location = ForeignKey(
        "dashboard.Location",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="safety_checkins",
    )

    objects = SafetyCheckinManager()

    if TYPE_CHECKING:
        profile_id: int
        destination_location_id: int | None
        contacts: DjangoManager[SafetyCheckinContact]
        messages: DjangoManager[SafetyCheckinMessage]

    @property
    def is_resolved(self) -> bool:
        """Whether this check-in has reached a terminal status.

        Returns:
            True when status is checked_in, found_safe, or cancelled.
        """
        return self.status in SafetyCheckinStatus.resolved_statuses()

    def __str__(self) -> str:
        """Return a human-readable description of this check-in.

        Returns:
            String like "<title> (<profile id>)".
        """
        return f"{self.title} ({self.profile_id})"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_safety_checkins"
        ordering = ["-checkin_by"]
        indexes = [
            Index(fields=["uuid"], name="idxdb_sc_uuid"),
            Index(fields=["profile", "status"], name="idxdb_sc_profile_status"),
            Index(fields=["status", "checkin_by"], name="idxdb_sc_status_by"),
        ]


class SafetyCheckinContact(abstract.Model):
    """A single emergency contact attached to one specific check-in.

    A snapshot, not a live link back to ``EmergencyContactDefault`` - editing
    or deleting a default afterward does not affect check-ins already created
    from it. ``token`` is the magic-link credential for the public contact
    portal, since a contact identified only by email has no account to log
    into.
    """

    checkin = ForeignKey(SafetyCheckin, on_delete=CASCADE, related_name="contacts")
    contact_profile = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="+")
    email = EmailField(null=True, blank=True)
    name = CharField(max_length=150, blank=True, default="")
    token = UUIDField(default=uuid4, unique=True, editable=False)
    notified_at = DateTimeField(null=True, blank=True)
    found_safe_at = DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        checkin_id: int
        contact_profile_id: int | None

    @property
    def display_name(self) -> str:
        """Return the best available display name for this contact.

        Returns:
            The name if set, else the linked profile's username, else the raw email.
        """
        if self.name:
            return self.name
        if self.contact_profile_id:
            return self.contact_profile.username
        return self.email or "Unknown contact"

    def __str__(self) -> str:
        """Return a human-readable description of this contact.

        Returns:
            String like "<name> for checkin <id>".
        """
        return f"{self.display_name} for checkin {self.checkin_id}"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_safety_checkin_contacts"
        indexes = [
            Index(fields=["checkin"], name="idxdb_scc_checkin"),
            Index(fields=["token"], name="idxdb_scc_token"),
        ]
        constraints = [
            CheckConstraint(
                condition=Q(contact_profile__isnull=False) ^ Q(email__isnull=False),
                name="db_safety_checkin_contact_exactly_one_target",
            ),
        ]


class SafetyCheckinMessage(abstract.Model):
    """A chat message on a check-in, from either the owner or an emergency contact.

    Exactly one of ``sender_profile``/``sender_contact`` is set at the
    application layer: the owner and any contact who is also a site user post
    as ``sender_profile``; a contact with no account posts as
    ``sender_contact`` so their display name still resolves without a login.
    """

    checkin = ForeignKey(SafetyCheckin, on_delete=CASCADE, related_name="messages")
    sender_profile = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="+")
    sender_contact = ForeignKey(SafetyCheckinContact, on_delete=SET_NULL, null=True, blank=True, related_name="+")
    body = TextField()

    if TYPE_CHECKING:
        checkin_id: int
        sender_profile_id: int | None
        sender_contact_id: int | None

    @property
    def sender_name(self) -> str:
        """Return the display name of whoever sent this message.

        Returns:
            The contact's display name, the sending profile's username, or "Unknown".
        """
        if self.sender_contact:
            return self.sender_contact.display_name
        if self.sender_profile:
            return self.sender_profile.username
        return "Unknown"

    def __str__(self) -> str:
        """Return a human-readable description of this message.

        Returns:
            String like "[<sender>] <body prefix>".
        """
        return f"[{self.sender_name}] {self.body[:60]}"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_safety_checkin_messages"
        ordering = ["created"]
        indexes = [Index(fields=["checkin"], name="idxdb_scm_checkin")]
