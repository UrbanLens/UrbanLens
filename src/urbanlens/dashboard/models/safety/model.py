"""Safety check-in models: emergency contact defaults, check-ins, per-checkin contacts, and chat."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from django.core.validators import validate_email
from django.db.models import (
    CASCADE,
    SET_NULL,
    BooleanField,
    CheckConstraint,
    DecimalField,
    DurationField,
    EmailField,
    FloatField,
    ForeignKey,
    Index,
    IntegerField,
    Manager as DjangoManager,
    ManyToManyField,
    OneToOneField,
    PositiveIntegerField,
    Q,
    TextField,
    UUIDField,
)
from django.db.models.fields import CharField, DateTimeField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.fields import EncryptedTextField
from urbanlens.dashboard.models.safety.queryset import (
    EmergencyContactDefaultManager,
    SafetyCheckinContactManager,
    SafetyCheckinManager,
    SafetyCheckinPartnerManager,
    SafetyContactOptOutManager,
)

logger = logging.getLogger(__name__)

DEFAULT_GRACE_PERIOD = timedelta(hours=1)

# How long before a check-in escalates to emergency contacts that the owner gets one
# last "check in now" warning. Matches the polling cadence of the Celery beat tasks
# that drive this feature, so it can't realistically be tightened further without
# also tightening send_due_checkin_reminders/escalate_overdue_checkins.
FINAL_WARNING_LEAD_TIME = timedelta(minutes=5)

# How often the owner editing the trip plan, destination, or route markup after contacts have
# already been notified is allowed to trigger another "plan updated" notification - keeps rapid,
# incremental edits (e.g. drawing several map annotations in a row) from spamming contacts with
# one email per change.
PLAN_UPDATE_NOTIFICATION_COOLDOWN = timedelta(minutes=15)

DEFAULT_CONTACT_MESSAGE = (
    "Hi! I went on a trip and set up an automated safety check-in: if I don't confirm I'm safe by "
    "my expected return time, this message is sent to my emergency contacts. If you're reading this, "
    "I didn't come home and may need help - please try to reach me, and if you can't, use the trip plan "
    "included with this alert to help find me. I may not be able to use "
    "my phone to contact anyone else for help, so it's important you try to help me."
)


def humanize_hours_minutes(delta: timedelta) -> str:
    """Render a duration as a human-readable "X hours Y minutes" string.

    Args:
        delta: The duration to render.

    Returns:
        E.g. "30 minutes", "1 hour", "1 hour 30 minutes", or "0 minutes".
    """
    total_minutes = round(delta.total_seconds() / 60)
    hours, minutes = divmod(total_minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("" if hours == 1 else "s"))
    if minutes or not hours:
        parts.append(f"{minutes} minute" + ("" if minutes == 1 else "s"))
    return " ".join(parts)


class EmergencyContactDefault(abstract.DashboardModel):
    """A reusable emergency contact saved to a profile's safety defaults.

    Copied onto each new SafetyCheckin as a SafetyCheckinContact snapshot at
    creation time - editing a default here does not retroactively change any
    check-in that already copied it. Exactly one of contact_profile/email
    identifies the contact.
    """

    owner = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="safety_contact_defaults")
    contact_profile = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="+")
    # Encrypted: this is a reusable template copied onto SafetyCheckinContact at
    # check-in creation time (see services.visits.safety.save_contact_defaults) and
    # never itself matched by value - only the copies are (against SafetyContactOptOut).
    email = EncryptedTextField(null=True, blank=True, validators=[validate_email])
    label = CharField(max_length=150, blank=True, default="")
    order = IntegerField(default=0)

    if TYPE_CHECKING:
        owner_id: int
        contact_profile_id: int | None

    objects = EmergencyContactDefaultManager()

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

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_safety_contact_defaults"
        ordering = ["order", "created"]
        indexes = [Index(fields=["owner"], name="idxdb_ecd_owner")]
        constraints = [
            CheckConstraint(
                condition=Q(contact_profile__isnull=False) ^ Q(email__isnull=False),
                name="db_safety_contact_default_exactly_one_target",
            ),
        ]


class SafetyPreference(abstract.DashboardModel):
    """Per-profile defaults applied to new safety check-ins."""

    default_message = TextField(blank=True, default=DEFAULT_CONTACT_MESSAGE)
    default_grace_period = DurationField(default=DEFAULT_GRACE_PERIOD)
    # Days after a check-in resolves (checked in / found safe / cancelled) before it's
    # auto-deleted; null means "never" - see SafetyCheckinQuerySet.due_for_auto_delete().
    auto_delete_after_days = IntegerField(null=True, blank=True)
    profile = OneToOneField("dashboard.Profile", on_delete=CASCADE, related_name="safety_preference")

    if TYPE_CHECKING:
        profile_id: int

    @property
    def default_grace_period_hours(self) -> float:
        """``default_grace_period`` expressed in hours, for prefilling numeric form fields.

        Returns:
            The grace period as a plain float number of hours.
        """
        return self.default_grace_period.total_seconds() / 3600

    @property
    def default_grace_period_display(self) -> str:
        """``default_grace_period`` as a human-readable "X hours Y minutes" string.

        Returns:
            E.g. "30 minutes", "1 hour", or "1 hour 30 minutes".
        """
        return humanize_hours_minutes(self.default_grace_period)

    def __str__(self) -> str:
        """Return a human-readable description of this preference row.

        Returns:
            String like "Safety preferences for <profile id>".
        """
        return f"Safety preferences for {self.profile_id}"

    class Meta(abstract.DashboardModel.Meta):
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


class SafetyCheckin(abstract.PublicDashboardModel):
    """A planned trip with an expected check-in time and emergency contacts.

    If the profile doesn't check in by ``checkin_by`` + ``grace_period``, the
    linked ``SafetyCheckinContact`` rows are notified. Concluding the check-in
    (self check-in, or a contact marking the profile safe) raises a
    VisitSuggestion for the destination via ``services.visits.safety._conclude_checkin``,
    reusing the same confirm/reject flow as any other tentative visit.

    ``slug`` (from ``PublicDashboardModel``) is scoped per-profile - only the owner-facing
    detail/check-in pages use it for a human-readable URL; the contact portal
    keeps using its unguessable ``token``, since that's a security credential,
    not just an identifier.

    Attributes:
        profile: The profile who created and owns this check-in.
        trip: The Trip this check-in was started for, if any - a profile may
            have at most one active check-in per (profile, trip) scope (see
            ``services.visits.safety.get_active_checkin``), so a general (``trip``
            is ``None``) check-in and a trip-scoped one, or check-ins for two
            different trips, can be active at the same time.
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
        final_warning_sent_at: When the owner's last "check in now" warning was sent, if at all.
        escalated_at: When emergency contacts were notified, if at all.
        resolved_at: When the check-in concluded, if at all.
        plan_update_notified_at: When contacts were last re-notified of a trip plan/destination/
            route change made after escalation, if at all.
        markup_map: The standalone MarkupMap holding the route/plan drawing shown on the
            detail page and contact portal, if any.
        notify_community_wiki: Whether escalation should also post a comment to the destination's
            community wiki and notify users with pins there (see ``services.visits.safety.notify_community_wiki``).
        wiki_notified_at: When the community wiki comment was posted, if at all - also makes the
            escalation-time wiki notification idempotent.
        live_location_sharing_enabled: Whether the owner has opted into sharing their current
            position with accepted partners (see ``services.visits.safety.set_live_location_sharing``).
        live_latitude: The owner's most recently shared position, if sharing is/was enabled.
        live_longitude: The owner's most recently shared position, if sharing is/was enabled.
        live_location_accuracy: Accuracy (meters) reported alongside ``live_latitude``/``live_longitude``.
        live_location_updated_at: When the live position was last updated, if ever.
        archive_scheduled_at: When ``services.visits.safety.archive_checkin`` should encrypt and scrub
            this check-in's PII, if it has resolved - immediately on resolution if no one but the
            owner could ever see it, or after a 1-hour grace window otherwise (see
            ``services.visits.safety.schedule_checkin_archival``). ``None`` until resolved.
        resolved_by_label: Display label of whoever concluded this check-in ("you", a partner's
            username, or a contact's display name) - captured for the archive payload, then
            scrubbed at archival like every other PII field on this model.
    """

    title = CharField(max_length=200)
    plan_details = TextField(blank=True, default="")
    contact_message = TextField(blank=True, default="")
    checkin_by = DateTimeField()
    grace_period = DurationField(default=DEFAULT_GRACE_PERIOD)
    status = CharField(max_length=20, choices=SafetyCheckinStatus.choices, default=SafetyCheckinStatus.SCHEDULED)

    destination_latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    destination_longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    reminder_sent_at = DateTimeField(null=True, blank=True)
    final_warning_sent_at = DateTimeField(null=True, blank=True)
    escalated_at = DateTimeField(null=True, blank=True)
    resolved_at = DateTimeField(null=True, blank=True)
    plan_update_notified_at = DateTimeField(null=True, blank=True)

    notify_community_wiki = BooleanField(default=False)
    wiki_notified_at = DateTimeField(null=True, blank=True)

    # Opt-in, owner-controlled live position feed - visible only to accepted
    # partners (see consumers.SafetyCheckinChatConsumer), never to emergency
    # contacts. Default off: continuous GPS is a real battery/privacy cost the
    # owner must actively choose, not something a check-in should assume.
    # Current point only, overwritten on each update - no history/breadcrumb
    # trail (deliberately out of scope; see docs/designs' safety-partner plan).
    live_location_sharing_enabled = BooleanField(default=False)
    live_latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    live_longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    live_location_accuracy = FloatField(null=True, blank=True)
    live_location_updated_at = DateTimeField(null=True, blank=True)

    # Post-resolution encryption/archival - see SafetyCheckinArchive and
    # services.visits.safety.schedule_checkin_archival/archive_checkin.
    archive_scheduled_at = DateTimeField(null=True, blank=True)
    resolved_by_label = CharField(max_length=150, blank=True, default="")

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="safety_checkins")
    trip = ForeignKey(
        "dashboard.Trip",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="safety_checkins",
    )
    destination_location = ForeignKey(
        "dashboard.Location",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="safety_checkins",
    )
    # Standalone route/plan map (viewport + markup items). Created on the
    # check-in creation page (before the check-in exists) or lazily on the
    # detail page, then linked here.
    markup_map = ForeignKey(
        "dashboard.MarkupMap",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="safety_checkins",
    )
    # Additional, previously-drawn maps attached for reference alongside the primary
    # markup_map (the interactively-drawn route). Reference-only for the owner - unlike
    # markup_map, these are not surfaced to emergency contacts in emails or the contact
    # portal. See controllers.safety's SafetyCheckinMap*View.
    markup_maps = ManyToManyField("dashboard.MarkupMap", blank=True, related_name="attached_safety_checkins")

    if TYPE_CHECKING:
        profile_id: int
        trip_id: int | None
        destination_location_id: int | None
        markup_map_id: int | None
        contacts: DjangoManager[SafetyCheckinContact]
        messages: DjangoManager[SafetyCheckinMessage]
        partners: DjangoManager[SafetyCheckinPartner]
        archive: SafetyCheckinArchive | None

    objects = SafetyCheckinManager()

    @property
    def is_resolved(self) -> bool:
        """Whether this check-in has reached a terminal status.

        Returns:
            True when status is checked_in, found_safe, or cancelled.
        """
        return self.status in SafetyCheckinStatus.resolved_statuses()

    @property
    def contacts_locked(self) -> bool:
        """Whether title/message/contacts are frozen because contacts were already notified.

        Returns:
            True once ``escalated_at`` is set - the trip plan, destination, and route markup
            stay editable after that point (editing them re-notifies contacts), but the title,
            contact message, and contact list are locked so contacts already told to look for
            certain information aren't sent conflicting details later.
        """
        return self.escalated_at is not None

    @property
    def notifications_locked(self) -> bool:
        """Whether the message/contacts/wiki-notify fields are frozen against further edits.

        Returns:
            True once contacts have already been notified (``contacts_locked``) or the
            owner has already checked in - editing who/whether to notify no longer makes
            sense past either point. Unlike ``contacts_locked``, this does not cover the
            title field, which stays editable until contacts are actually notified.
        """
        return self.contacts_locked or self.status == SafetyCheckinStatus.CHECKED_IN

    def _slugify_base(self) -> str:
        """Return the raw text the URL slug is derived from.

        Returns:
            The check-in's title, or "checkin" if blank.
        """
        return self.title or "checkin"

    def _slugify_qs(self):
        """Return the queryset used to check slug uniqueness, scoped per-profile.

        Returns:
            This profile's other check-ins (excluding self, if saved).
        """
        qs = SafetyCheckin.objects.filter(profile_id=self.profile_id)
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        return qs

    def __str__(self) -> str:
        """Return a human-readable description of this check-in.

        Returns:
            String like "<title> (<profile id>)".
        """
        return f"{self.title} ({self.profile_id})"

    class Meta(abstract.PublicDashboardModel.Meta):
        db_table = "dashboard_safety_checkins"
        ordering = ["-checkin_by"]
        indexes = [
            Index(fields=["profile", "trip", "status"], name="idxdb_sc_profile_trip_status"),
            Index(fields=["status", "checkin_by"], name="idxdb_sc_status_by"),
            # Backs SafetyCheckinQuerySet.due_for_archival(), polled by the 5-minute
            # archival sweep beat task forever - without this it's a sequential scan
            # over every check-in on every tick.
            Index(fields=["archive_scheduled_at"], name="idxdb_sc_archive_scheduled_at"),
        ]


class SafetyCheckinContact(abstract.DashboardModel):
    """A single emergency contact attached to one specific check-in.

    A snapshot, not a live link back to ``EmergencyContactDefault`` - editing
    or deleting a default afterward does not affect check-ins already created
    from it. ``token`` is the magic-link credential for the public contact
    portal, since a contact identified only by email has no account to log
    into.
    """

    email = EmailField(null=True, blank=True)
    name = CharField(max_length=150, blank=True, default="")
    token = UUIDField(default=uuid4, unique=True, editable=False)
    notified_at = DateTimeField(null=True, blank=True)
    found_safe_at = DateTimeField(null=True, blank=True)

    checkin = ForeignKey(SafetyCheckin, on_delete=CASCADE, related_name="contacts")
    contact_profile = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="+")

    if TYPE_CHECKING:
        checkin_id: int
        contact_profile_id: int | None

    objects = SafetyCheckinContactManager()

    @property
    def display_name(self) -> str:
        """Return the best available display name for this contact.

        Returns:
            The name if set, else the linked profile's username, else the raw email.
        """
        if self.name:
            return self.name
        if self.contact_profile:
            return self.contact_profile.username
        return self.email or "Unknown contact"

    def __str__(self) -> str:
        """Return a human-readable description of this contact.

        Returns:
            String like "<name> for checkin <id>".
        """
        return f"{self.display_name} for checkin {self.checkin_id}"

    class Meta(abstract.DashboardModel.Meta):
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


class SafetyContactOptOutScope(abstract.TextChoices):
    """How broadly a ``SafetyContactOptOut`` suppresses notifications."""

    CHECKIN = "checkin", "This check-in"
    OWNER = "owner", "All future check-ins from this person"
    GLOBAL = "global", "All safety check-in notifications"


class SafetyContactOptOut(abstract.DashboardModel):
    """Records that a contact (by profile or email) no longer wants safety check-in notifications.

    Identity is resolved the same way as ``SafetyCheckinContact`` - exactly one of
    ``contact_profile``/``email``. Which of ``owner``/``checkin`` is set (if either) depends on
    ``scope``: a ``CHECKIN``-scoped row silences one specific check-in, an ``OWNER``-scoped row
    silences every future check-in created by that one owner, and a ``GLOBAL``-scoped row silences
    every safety check-in notification from the site, regardless of who created the check-in.
    """

    email = EmailField(null=True, blank=True)
    scope = CharField(max_length=10, choices=SafetyContactOptOutScope.choices)
    owner = ForeignKey("dashboard.Profile", on_delete=CASCADE, null=True, blank=True, related_name="+")
    checkin = ForeignKey(SafetyCheckin, on_delete=CASCADE, null=True, blank=True, related_name="contact_opt_outs")
    contact_profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, null=True, blank=True, related_name="+")

    if TYPE_CHECKING:
        contact_profile_id: int | None
        owner_id: int | None
        checkin_id: int | None

    objects = SafetyContactOptOutManager()

    def __str__(self) -> str:
        """Return a human-readable description of this opt-out.

        Returns:
            String like "<contact> opted out (<scope>)".
        """
        who = self.contact_profile.username if self.contact_profile else (self.email or "Unknown contact")
        return f"{who} opted out ({self.scope})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_safety_contact_opt_outs"
        indexes = [
            Index(fields=["contact_profile"], name="idxdb_scoo_profile"),
            Index(fields=["email"], name="idxdb_scoo_email"),
            Index(fields=["owner"], name="idxdb_scoo_owner"),
            Index(fields=["checkin"], name="idxdb_scoo_checkin"),
        ]
        constraints = [
            CheckConstraint(
                condition=Q(contact_profile__isnull=False) ^ Q(email__isnull=False),
                name="db_safety_contact_optout_exactly_one_target",
            ),
            CheckConstraint(
                condition=(
                    (Q(scope=SafetyContactOptOutScope.CHECKIN) & Q(checkin__isnull=False) & Q(owner__isnull=True))
                    | (Q(scope=SafetyContactOptOutScope.OWNER) & Q(owner__isnull=False) & Q(checkin__isnull=True))
                    | (Q(scope=SafetyContactOptOutScope.GLOBAL) & Q(owner__isnull=True) & Q(checkin__isnull=True))
                ),
                name="db_safety_contact_optout_scope_fields_match",
            ),
        ]


class SafetyCheckinPartnerStatus(abstract.TextChoices):
    """Lifecycle status of a SafetyCheckinPartner invite."""

    INVITED = "invited", "Invited"
    ACCEPTED = "accepted", "Accepted"


class SafetyCheckinPartner(abstract.DashboardModel):
    """A trusted account granted early, full visibility into one check-in, plus the
    ability to conclude it.

    Unlike a ``SafetyCheckinContact`` (notified without opting in, only once
    overdue/escalated), a partner sees the full check-in - plan, contacts, chat,
    and any shared live location - from the moment they accept, well before
    anything goes wrong. Access requires ``status == ACCEPTED``: this is a real
    safety responsibility, not a passive share, so an invite must be actively
    accepted before any view/act access is granted (see
    ``services.visits.safety.is_owner_or_accepted_partner``).

    Only the check-in's owner may invite a partner - unlike Trip's
    ``allow_add_members`` permission matrix, a check-in has exactly one
    accountable owner, so a delegated invite-permission matrix would only
    diffuse accountability for a safety-critical role.
    """

    status = CharField(max_length=10, choices=SafetyCheckinPartnerStatus.choices, default=SafetyCheckinPartnerStatus.INVITED)
    accepted_at = DateTimeField(null=True, blank=True)

    checkin = ForeignKey(SafetyCheckin, on_delete=CASCADE, related_name="partners")
    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="safety_checkin_partner_roles")
    invited_by = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="+")

    if TYPE_CHECKING:
        checkin_id: int
        profile_id: int
        invited_by_id: int

    objects = SafetyCheckinPartnerManager()

    def __str__(self) -> str:
        """Return a human-readable description of this partner assignment.

        Returns:
            String like "<username> partner on checkin <id> (<status>)".
        """
        return f"{self.profile.username} partner on checkin {self.checkin_id} ({self.status})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_safety_checkin_partners"
        unique_together = [("checkin", "profile")]
        indexes = [
            Index(fields=["checkin", "status"], name="idxdb_scp_checkin_status"),
            Index(fields=["profile", "status"], name="idxdb_scp_profile_status"),
        ]


class SafetyCheckinArchive(abstract.DashboardModel):
    """The encrypted, owner-only remnant of a concluded check-in.

    Created by ``services.visits.safety.archive_checkin`` once a resolved check-in's
    grace window elapses: the check-in's PII (plan, contacts, chat, resolution
    details) is serialized to JSON, encrypted with a fresh random key
    (``crypto_secretbox``), and that key is sealed (``crypto_box_seal``) to
    only the owner's ``MessagingKeyBundle.public_key`` - after which the
    plaintext columns on ``SafetyCheckin``/``SafetyCheckinContact``/
    ``SafetyCheckinMessage`` are scrubbed. Sealing needs only the owner's
    *public* key, so this can happen entirely server-side, with the owner
    offline, exactly like ``ConversationKey``/``GroupKeyEnvelope`` already do
    for direct messages - the server can never decrypt this row itself.

    ``hasattr(checkin, "archive")`` is the sole "has this check-in been
    archived" signal - no separate boolean flag exists on ``SafetyCheckin`` to
    risk drifting from it.
    """

    checkin = OneToOneField(SafetyCheckin, on_delete=CASCADE, related_name="archive")
    ciphertext = TextField()
    nonce = CharField(max_length=64)
    sealed_key = TextField()
    key_bundle_version = PositiveIntegerField()
    encrypted_at = DateTimeField(auto_now_add=True)

    if TYPE_CHECKING:
        checkin_id: int

    def __str__(self) -> str:
        """Return a human-readable description of this archive row.

        Returns:
            String like "Archive for checkin <id> (sealed v3)".
        """
        return f"Archive for checkin {self.checkin_id} (sealed v{self.key_bundle_version})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_safety_checkin_archives"


class SafetyCheckinMessage(abstract.DashboardModel):
    """A chat message on a check-in, from either the owner or an emergency contact.

    Exactly one of ``sender_profile``/``sender_contact`` is set at the
    application layer: the owner and any contact who is also a site user post
    as ``sender_profile``; a contact with no account posts as
    ``sender_contact`` so their display name still resolves without a login.
    """

    body = TextField()

    checkin = ForeignKey(SafetyCheckin, on_delete=CASCADE, related_name="messages")
    sender_profile = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="+")
    sender_contact = ForeignKey(SafetyCheckinContact, on_delete=SET_NULL, null=True, blank=True, related_name="+")

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

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_safety_checkin_messages"
        ordering = ["created"]
        indexes = [Index(fields=["checkin"], name="idxdb_scm_checkin")]
