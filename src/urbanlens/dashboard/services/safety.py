"""Business logic for safety check-ins: contact resolution, lifecycle transitions, and notifications."""

from __future__ import annotations

from datetime import timedelta
import logging
import smtplib
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import (
    PLAN_UPDATE_NOTIFICATION_COOLDOWN,
    EmergencyContactDefault,
    SafetyCheckin,
    SafetyCheckinArchive,
    SafetyCheckinContact,
    SafetyCheckinMessage,
    SafetyCheckinPartner,
    SafetyCheckinPartnerStatus,
    SafetyCheckinStatus,
    SafetyContactOptOut,
    SafetyContactOptOutScope,
    SafetyPreference,
)
from urbanlens.dashboard.services.email_normalization import normalize_email
from urbanlens.dashboard.services.notification_delivery import send_sms, send_whatsapp
from urbanlens.dashboard.services.visits import create_visit_suggestion

if TYPE_CHECKING:
    from collections.abc import Iterable
    import datetime
    from decimal import Decimal

    from django.contrib.auth.models import AnonymousUser, User

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.safety.queryset import SafetyCheckinQuerySet
    from urbanlens.dashboard.models.trips.model import Trip
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

# (contact_profile, email, name) - contact_profile wins when both are given.
ContactInput = tuple["Profile | None", "str | None", str]

# Chat messages are TextField (unbounded at the DB layer) - this caps abuse/accidental
# pastes at the application layer. The client mirrors this via maxlength on the input,
# but that's trivially bypassed, so it's re-checked here.
MAX_CHAT_MESSAGE_LENGTH = 4000

# How long a resolved check-in's page stays readable, once resolved, for anyone besides
# the owner who was able to see it (accepted partners, contacts of either kind) - long
# enough to read/post a final comment, short enough that the PII isn't sitting around
# unencrypted for long. See schedule_checkin_archival.
ARCHIVE_VIEWER_GRACE_PERIOD = timedelta(hours=1)


def safety_checkin_group_name(checkin_pk: int) -> str:
    """Return the channel-layer group name for a check-in's shared chat.

    Every real-time surface elsewhere in this codebase (``notification_group_name``,
    ``direct_message_group_name``, ``session_group_name``) centralizes its group-name
    format behind one function so a rename can never silently break delivery in only
    some of the places that construct it - this is the safety check-in equivalent.

    Args:
        checkin_pk: Primary key of the ``SafetyCheckin``.

    Returns:
        The group name joined by the check-in's chat consumer and used for chat/status broadcasts.
    """
    return f"safety_checkin_{checkin_pk}"


def safety_checkin_location_group_name(checkin_pk: int) -> str:
    """Return the channel-layer group name for a check-in's live-location broadcasts.

    Args:
        checkin_pk: Primary key of the ``SafetyCheckin``.

    Returns:
        The group name joined only by the session route (owner/accepted partner) -
        contacts never join this group, matching the live-location visibility rule.
    """
    return f"safety_checkin_location_{checkin_pk}"


def _find_profile_by_email(email: str) -> Profile | None:
    """Return the Profile for an existing active user with this email, if any.

    Uses the site-wide normalized email matching (``find_user_by_email``) - the same
    lookup used by registration, friend invites, and profile contact settings - so
    Gmail dot/plus variants and a verified secondary email all resolve to the same
    account, not just an exact match on ``User.email``.

    Args:
        email: Email address to look up.

    Returns:
        The matching Profile, or None.
    """
    from urbanlens.dashboard.services.email_normalization import find_user_by_email

    user = find_user_by_email(email)
    return user.profile if user else None


def _resolve_contact(contact_profile: Profile | None, email: str | None) -> tuple[Profile | None, str | None]:
    """Resolve a raw (contact_profile, email) pair, matching email to an existing user when possible.

    Args:
        contact_profile: An explicitly chosen connection, if any.
        email: A raw email address, if any.

    Returns:
        (contact_profile, email) with exactly one populated - contact_profile
        when the email belongs to an existing user, matching the exactly-one
        CheckConstraint on EmergencyContactDefault/SafetyCheckinContact.
    """
    if contact_profile is not None:
        return contact_profile, None
    if email:
        resolved = _find_profile_by_email(email)
        if resolved:
            return resolved, None
        return None, email
    return None, None


def _send_email(*, to: str, subject: str, template: str, context: dict) -> None:
    """Send an HTML email, logging (not raising) on delivery failure.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        template: Template path for the HTML body.
        context: Template context.
    """
    if not to:
        return
    try:
        html_body = render_to_string(template, context)
        msg = EmailMultiAlternatives(subject=subject, body=subject, from_email=None, to=[to])
        msg.attach_alternative(html_body, "text/html")
        msg.send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send safety check-in email to %s", to)
    except Exception:
        # A template-context bug (missing var, bad filter) must be logged like every other
        # delivery failure here, not raised uncaught - especially since escalate_checkin()
        # would otherwise abort mid-contact-loop on a template bug, which (before the
        # notified_at idempotency fix above) used to mean re-notifying already-notified
        # contacts on the next retry.
        logger.exception("Failed to render/send safety check-in email to %s", to)


def _absolute_url(path: str) -> str:
    """Build an absolute URL from a site-relative path.

    Args:
        path: Site-relative path, e.g. from ``reverse()`` - already includes
            whatever prefix the urlconf mounts the dashboard app under.

    Returns:
        Absolute URL using the configured SITE_URL.
    """
    return f"{settings.SITE_URL.rstrip('/')}{path}"


def _checkin_url_slug(checkin: SafetyCheckin) -> str:
    """Return the identifier to reverse an owner-facing check-in URL with.

    Args:
        checkin: The check-in being linked to.

    Returns:
        The check-in's slug, falling back to its UUID for the rare case a
        slug hasn't been generated yet.
    """
    return checkin.slug or str(checkin.uuid)


def get_or_create_preference(profile: Profile) -> SafetyPreference:
    """Return a profile's safety preferences, creating defaults if none exist yet.

    Args:
        profile: Profile whose preferences should be fetched.

    Returns:
        The profile's SafetyPreference row.
    """
    preference, _ = SafetyPreference.objects.get_or_create(profile=profile)
    return preference


def save_contact_defaults(profile: Profile, contacts: Iterable[ContactInput]) -> None:
    """Replace a profile's saved default emergency contacts.

    Args:
        profile: Profile whose defaults are being replaced.
        contacts: Iterable of (contact_profile, email, label) tuples.
    """
    EmergencyContactDefault.objects.for_owner(profile).delete()
    for order, (contact_profile, email, label) in enumerate(contacts):
        resolved_profile, resolved_email = _resolve_contact(contact_profile, email)
        EmergencyContactDefault.objects.create(
            owner=profile,
            contact_profile=resolved_profile,
            email=resolved_email,
            label=label,
            order=order,
        )


def default_contacts_as_input(profile: Profile) -> list[ContactInput]:
    """Return a profile's saved default contacts in the shape ``create_checkin`` expects.

    Args:
        profile: Profile whose defaults should be listed.

    Returns:
        List of (contact_profile, email, label) tuples.
    """
    return [(default.contact_profile, default.email, default.label) for default in EmergencyContactDefault.objects.for_owner(profile)]


def get_active_checkin(profile: Profile, trip: Trip | None = None) -> SafetyCheckin | None:
    """Return the profile's current active (unresolved) check-in for one scope, if any.

    A profile may have at most one active check-in per (profile, trip) scope -
    ``create_checkin`` enforces this - so the earliest-due active check-in in
    that scope is also the only one, in practice. ``trip=None`` (the default)
    means the general, non-trip scope; pass a ``Trip`` to look up that trip's
    own active check-in instead. A profile can have a general check-in and one
    or more trip-scoped check-ins active simultaneously - see
    ``get_active_checkins`` to fetch all of them regardless of scope.

    Args:
        profile: Profile to look up.
        trip: Scope to check - ``None`` for the general check-in, or a
            specific ``Trip`` for that trip's check-in.

    Returns:
        The active SafetyCheckin for that scope, or None if there isn't one.
    """
    return SafetyCheckin.objects.active().filter(profile=profile, trip=trip).order_by("checkin_by").first()


def get_active_checkins(profile: Profile) -> SafetyCheckinQuerySet:
    """Return every active (unresolved) check-in for a profile, across all scopes.

    Unlike ``get_active_checkin``, this isn't scoped to a single trip/general
    slot - a profile can have several active check-ins at once (general plus
    one per trip). Used by the nav banner, which must never hide an active
    check-in just because a different scope's slot is occupied.

    Args:
        profile: Profile to look up.

    Returns:
        Queryset of the profile's active check-ins, soonest-due first.
    """
    return SafetyCheckin.objects.active().filter(profile=profile).select_related("trip").order_by("checkin_by")


def set_checkin_contacts(checkin: SafetyCheckin, contacts: Iterable[ContactInput]) -> None:
    """Reconcile a check-in's contact list with a newly submitted one.

    Matches submitted contacts to existing rows by (contact_profile, email)
    identity and updates in place, rather than deleting and recreating
    everything - a plain edit on the detail page must not invalidate the
    magic-link ``token`` already emailed to a contact, nor wipe
    ``notified_at``/``found_safe_at`` once a check-in has escalated.

    Args:
        checkin: The check-in whose contacts are being (re)set.
        contacts: Iterable of (contact_profile, email, name) tuples.
    """
    existing_by_key = {(contact.contact_profile_id, contact.email): contact for contact in checkin.contacts.all()}
    keep_ids: set[int] = set()

    for contact_profile, email, name in contacts:
        resolved_profile, resolved_email = _resolve_contact(contact_profile, email)
        existing = existing_by_key.get((resolved_profile.pk if resolved_profile else None, resolved_email))
        if existing is not None:
            if existing.name != name:
                existing.name = name
                existing.save(update_fields=["name", "updated"])
            keep_ids.add(existing.pk)
        else:
            created = SafetyCheckinContact.objects.create(
                checkin=checkin,
                contact_profile=resolved_profile,
                email=resolved_email,
                name=name,
            )
            keep_ids.add(created.pk)

    checkin.contacts.exclude(pk__in=keep_ids).delete()


def is_contact_opted_out(
    contact_profile: Profile | None,
    email: str | None,
    *,
    owner: Profile,
    checkin: SafetyCheckin | None = None,
) -> bool:
    """Whether a contact identity has opted out of notifications relevant to this owner/check-in.

    Args:
        contact_profile: The contact's linked profile, already resolved via ``_resolve_contact``.
        email: The contact's raw email, already resolved via ``_resolve_contact``.
        owner: The profile who would be sending/notifying on this contact - matches an
            ``OWNER``-scoped opt-out against this specific owner.
        checkin: The specific check-in being notified about, if any - omitted when validating
            contacts not yet attached to any check-in (e.g. saved as defaults), in which case
            only ``GLOBAL``/``OWNER``-scoped opt-outs apply.

    Returns:
        True if a matching ``SafetyContactOptOut`` row blocks notifying this identity.
    """
    return SafetyContactOptOut.objects.blocks_notification(contact_profile, email, owner=owner, checkin=checkin)


def validate_notifiable_contacts(
    owner: Profile,
    contacts: Iterable[ContactInput],
    *,
    checkin: SafetyCheckin | None = None,
) -> tuple[list[ContactInput], list[str]]:
    """Split submitted contacts into those safe to save and those that must be rejected.

    Rejects, each with a ready-to-display message:
      - the owner's own account/email - notifying yourself as your own emergency contact
        makes no sense and is almost certainly a mistake;
      - a contact identity that duplicates another one already in this same submission
        (e.g. a friend picked by avatar *and* separately typed in by their own email -
        two different-looking chips that ``_resolve_contact`` resolves to the same account);
      - a contact who has opted out and would never actually be notified (see
        ``is_contact_opted_out``);
      - a contact submitted past the site's ``max_safety_checkin_contacts`` limit.

    Args:
        owner: The profile these contacts are being added for.
        contacts: Iterable of (contact_profile, email, name) tuples, as submitted.
        checkin: The specific check-in being edited, if any - see ``is_contact_opted_out``.

    Returns:
        (allowed, rejected_messages).
    """
    from urbanlens.dashboard.models.site_settings.model import SiteSettings

    max_contacts = SiteSettings.get_current().max_safety_checkin_contacts

    allowed: list[ContactInput] = []
    rejected: list[str] = []
    seen: set[tuple[int | None, str | None]] = set()

    for contact_profile, email, name in contacts:
        resolved_profile, resolved_email = _resolve_contact(contact_profile, email)
        label = name or (resolved_profile.username if resolved_profile else resolved_email) or "That contact"
        key = (resolved_profile.pk if resolved_profile else None, normalize_email(resolved_email) if resolved_email else None)

        if resolved_profile is not None and resolved_profile.pk == owner.pk:
            rejected.append(f"{label} is your own account and can't be an emergency contact.")
        elif key in seen:
            rejected.append(f"{label} was already added.")
        elif is_contact_opted_out(resolved_profile, resolved_email, owner=owner, checkin=checkin):
            rejected.append(f"{label} has opted out of notifications and wasn't added.")
        elif max_contacts > 0 and len(allowed) >= max_contacts:
            rejected.append(f"{label} wasn't added - a check-in can have at most {max_contacts} contacts.")
        else:
            seen.add(key)
            allowed.append((contact_profile, email, name))
    return allowed, rejected


def record_contact_opt_out(contact: SafetyCheckinContact, scope: SafetyContactOptOutScope) -> None:
    """Record that a contact no longer wants certain safety check-in notifications.

    Idempotent - repeat clicks (or an email client's link-scanner prefetching the GET confirm
    page behind an opt-out link) don't create duplicate rows.

    Args:
        contact: The contact opting out, identified via its own profile/email.
        scope: How broadly to suppress future notifications.
    """
    SafetyContactOptOut.objects.get_or_create(
        contact_profile=contact.contact_profile,
        email=contact.email,
        scope=scope,
        owner=contact.checkin.profile if scope == SafetyContactOptOutScope.OWNER else None,
        checkin=contact.checkin if scope == SafetyContactOptOutScope.CHECKIN else None,
    )


def _optout_urls(contact: SafetyCheckinContact) -> dict[str, str]:
    """Build the three opt-out confirmation URLs for a contact-facing email footer.

    Args:
        contact: The contact these links are being generated for.

    Returns:
        Dict of ``optout_checkin_url``/``optout_owner_url``/``optout_global_url`` absolute URLs.
    """
    return {f"optout_{scope.value}_url": _absolute_url(reverse("safety.contact.optout", kwargs={"token": contact.token, "scope": scope.value})) for scope in SafetyContactOptOutScope}


def is_owner_or_accepted_partner(checkin: SafetyCheckin, profile: Profile) -> bool:
    """Whether ``profile`` may view the full check-in and act as its owner would.

    Args:
        checkin: The check-in being accessed.
        profile: The requesting profile.

    Returns:
        True if ``profile`` owns the check-in, or is an ACCEPTED SafetyCheckinPartner on it.
    """
    if checkin.profile_id == profile.pk:
        return True
    return checkin.partners.filter(profile=profile, status=SafetyCheckinPartnerStatus.ACCEPTED).exists()


def invite_checkin_partner(checkin: SafetyCheckin, *, inviter: Profile, username: str) -> SafetyCheckinPartner:
    """Invite an existing account as a safety check-in partner.

    Mirrors ``TripMembersView.post``'s add-by-username flow: no friendship is
    required, but a block still vetoes it (see ``Profile.are_blocked``) since
    a forced partner invite is an unsolicited-contact vector like any other.

    Args:
        checkin: The check-in gaining a partner.
        inviter: The profile sending the invite - must be the check-in's owner.
        username: The invitee's username, matched case-insensitively.

    Returns:
        The newly created (INVITED) SafetyCheckinPartner row.

    Raises:
        ValueError: Unknown username, inviting yourself, inviting a blocked
            profile, an existing invite/acceptance for that profile, or the
            site's ``max_safety_checkin_partners`` cap already reached.
    """
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.site_settings.model import SiteSettings

    try:
        user = User.objects.get(username__iexact=username)
    except User.DoesNotExist:
        raise ValueError(f'No user found with username "{username}".') from None
    invitee, _ = Profile.objects.get_or_create(user=user)

    if invitee.pk == checkin.profile_id:
        raise ValueError("You can't add yourself as a partner on your own check-in.")
    if Profile.are_blocked(inviter, invitee):
        raise ValueError("This user isn't accepting invitations from you.")
    if checkin.partners.filter(profile=invitee).exists():
        raise ValueError(f"{invitee.username} has already been invited.")

    max_partners = SiteSettings.get_current().max_safety_checkin_partners
    if max_partners > 0 and checkin.partners.count() >= max_partners:
        raise ValueError(f"A check-in can have at most {max_partners} partners.")

    try:
        partner = SafetyCheckinPartner.objects.create(checkin=checkin, profile=invitee, invited_by=inviter)
    except IntegrityError:
        # The .exists() check above isn't atomic against the unique_together
        # constraint - a double-submitted invite can race past it and only get
        # caught here. Same outcome as losing the .exists() check normally.
        raise ValueError(f"{invitee.username} has already been invited.") from None
    _notify_checkin_partner_invite(partner)
    return partner


def accept_checkin_partner_invite(partner: SafetyCheckinPartner) -> None:
    """Accept a pending partner invite, granting full check-in visibility and act rights.

    Idempotent - a repeat accept on an already-accepted row is a no-op.

    Args:
        partner: The invite being accepted.
    """
    accepted_at = timezone.now()
    # Conditional UPDATE, not read-then-write: only flips a row that is still INVITED at
    # write time. Guards against a repeat accept (row already ACCEPTED) *and* against the
    # owner removing this same invite concurrently (row no longer exists) - either way,
    # `.update()` on a non-matching/absent row affects 0 rows and this is a clean no-op,
    # instead of a save() into a phantom row silently succeeding and still sending a
    # "partner accepted" notification for a partnership that no longer exists.
    updated = SafetyCheckinPartner.objects.filter(pk=partner.pk, status=SafetyCheckinPartnerStatus.INVITED).update(status=SafetyCheckinPartnerStatus.ACCEPTED, accepted_at=accepted_at, updated=accepted_at)
    if not updated:
        return
    partner.status = SafetyCheckinPartnerStatus.ACCEPTED
    partner.accepted_at = accepted_at

    checkin = partner.checkin
    system_message = SafetyCheckinMessage.objects.create(
        checkin=checkin,
        sender_profile=partner.profile,
        body=f"{partner.profile.username} is now a partner on this check-in.",
    )
    broadcast_chat_message(checkin, system_message)
    _notify_checkin_partner_accepted(partner)


def decline_checkin_partner_invite(partner: SafetyCheckinPartner) -> None:
    """Decline a pending partner invite.

    Args:
        partner: The invite being declined.
    """
    partner.delete()


def remove_checkin_partner(partner: SafetyCheckinPartner) -> None:
    """Remove a partner (invited or already accepted) from a check-in - owner-initiated.

    Args:
        partner: The partner row to remove.
    """
    checkin, profile_id = partner.checkin, partner.profile_id
    partner.delete()
    # An accepted partner may already have a live WebSocket connection whose
    # permission was only checked once, at connect() time (see
    # SafetyCheckinChatConsumer._resolve) - without this, they'd keep receiving
    # chat/location/status events (and could keep sending messages) until they
    # happen to reload or disconnect on their own.
    _broadcast_partner_access_revoked(checkin, profile_id)


def _notify_checkin_partner_invite(partner: SafetyCheckinPartner) -> None:
    """Notify the invitee that they've been asked to be a safety check-in partner.

    Args:
        partner: The freshly created (INVITED) partner row.
    """
    from urbanlens.dashboard.services.identity_visibility import resolve_visible_identity

    try:
        pref = partner.profile.notification_preferences.safety_checkin_partner_invite
    except AttributeError:
        pref = DeliveryPreference.BOTH
    if pref == DeliveryPreference.NONE:
        return

    checkin = partner.checkin
    # Resolved (and masked if needed) toward the specific recipient before formatting -
    # the message string is stored as plain text, so it must be masked here, not at
    # render time (see identity_visibility.py's docstring).
    inviter_name = resolve_visible_identity(partner.profile, partner.invited_by)["display_name"]
    # Not yet ACCEPTED, so the detail page itself would 404 for this invitee (see
    # _get_checkin_as_partner) - point at the safety overview page instead, where the
    # "Pending partner invites" section already renders the accept/decline actions.
    checkin_path = reverse("safety.home")

    if pref in (DeliveryPreference.SITE, DeliveryPreference.BOTH):
        NotificationLog.objects.create(
            profile=partner.profile,
            source_profile=partner.invited_by,
            status=Status.UNREAD,
            importance=Importance.HIGH,
            notification_type=NotificationType.SAFETY_CHECKIN_PARTNER_INVITE,
            title="Safety check-in partner request",
            message=f'{inviter_name} wants you to be a safety partner for "{checkin.title}".',
            url=checkin_path,
        )

    invitee_email = partner.profile.user.email if partner.profile.user else None
    if pref in (DeliveryPreference.EMAIL, DeliveryPreference.BOTH) and invitee_email:
        _send_email(
            to=invitee_email,
            subject=f"{inviter_name} wants you as a safety check-in partner",
            template="dashboard/email/safety_checkin_partner_invite.html",
            context={"checkin": checkin, "partner": partner, "inviter_name": inviter_name, "checkin_url": _absolute_url(checkin_path)},
        )

    message_text = f'{inviter_name} wants you to be a safety partner for "{checkin.title}": {_absolute_url(checkin_path)}'
    try:
        prefs = partner.profile.notification_preferences
    except AttributeError:
        prefs = None
    if prefs and prefs.safety_checkin_partner_invite_whatsapp:
        send_whatsapp(partner.profile, message_text)
    if prefs and prefs.safety_checkin_partner_invite_sms:
        send_sms(partner.profile, message_text)


def _notify_checkin_partner_accepted(partner: SafetyCheckinPartner) -> None:
    """Notify the check-in owner that an invited partner accepted.

    Not gated by a delivery preference, matching the rest of this module's
    owner-facing resolution/administrative notifications (e.g. the
    SAFETY_CHECKIN_RESOLVED notice created in ``_resolve_as_found_safe``) -
    these are about the owner's own check-in, not a third party's activity
    they might want to mute.

    Args:
        partner: The now-ACCEPTED partner row.
    """
    checkin = partner.checkin
    checkin_path = reverse("safety.checkin.detail", kwargs={"checkin_slug": _checkin_url_slug(checkin)})
    NotificationLog.objects.create(
        profile=checkin.profile,
        source_profile=partner.profile,
        status=Status.UNREAD,
        importance=Importance.MEDIUM,
        notification_type=NotificationType.SAFETY_CHECKIN_PARTNER_ACCEPTED,
        title="Safety check-in partner accepted",
        message=f'{partner.profile.username} accepted your invite to be a safety partner for "{checkin.title}".',
        url=checkin_path,
    )
    if checkin.profile.user and checkin.profile.user.email:
        _send_email(
            to=checkin.profile.user.email,
            subject=f"{partner.profile.username} accepted your safety check-in partner invite",
            template="dashboard/email/safety_checkin_partner_accepted.html",
            context={"checkin": checkin, "partner": partner, "checkin_url": _absolute_url(checkin_path)},
        )


def set_live_location_sharing(checkin: SafetyCheckin, *, enabled: bool) -> None:
    """Turn live location sharing on or off for a check-in.

    Disabling immediately clears the last-known position rather than leaving
    a stale marker visible to partners after the owner turns sharing off.

    Args:
        checkin: The check-in whose sharing state is changing.
        enabled: The new sharing state.
    """
    checkin.live_location_sharing_enabled = enabled
    update_fields = ["live_location_sharing_enabled", "updated"]
    if not enabled:
        checkin.live_latitude = None
        checkin.live_longitude = None
        checkin.live_location_accuracy = None
        checkin.live_location_updated_at = None
        update_fields += ["live_latitude", "live_longitude", "live_location_accuracy", "live_location_updated_at"]
    checkin.save(update_fields=update_fields)
    _broadcast_live_location(checkin)


def update_live_location(checkin: SafetyCheckin, *, latitude: float, longitude: float, accuracy: float | None) -> None:
    """Record the owner's current position and broadcast it to connected partners.

    Args:
        checkin: The check-in being updated.
        latitude: Current latitude.
        longitude: Current longitude.
        accuracy: Reported accuracy in meters, if the browser/device provided one.

    Raises:
        ValueError: If sharing isn't enabled, or the check-in has already resolved -
            there's no one left to broadcast a live position to either way.
    """
    updated_at = timezone.now()
    # Conditional UPDATE (not read-then-write): re-checks sharing-enabled/unresolved
    # against the DB at write time, not the possibly-stale in-memory `checkin` the
    # caller passed in - closes a race where a toggle-off lands between the caller's
    # own checks and this save, which would otherwise persist a real position onto a
    # row now flagged "sharing disabled" and broadcast a stale sharing_enabled: true.
    updated = (
        SafetyCheckin.objects.filter(pk=checkin.pk, live_location_sharing_enabled=True)
        .exclude(status__in=SafetyCheckinStatus.resolved_statuses())
        .update(live_latitude=latitude, live_longitude=longitude, live_location_accuracy=accuracy, live_location_updated_at=updated_at, updated=updated_at)
    )
    if not updated:
        raise ValueError("Live location sharing is not enabled for this check-in, or it has already concluded.")

    checkin.live_latitude = latitude
    checkin.live_longitude = longitude
    checkin.live_location_accuracy = accuracy
    checkin.live_location_updated_at = updated_at
    _broadcast_live_location(checkin)


def _broadcast_live_location(checkin: SafetyCheckin) -> None:
    """Push the check-in's current live-location state to its connected location group.

    Best-effort, same as ``broadcast_chat_message``/``_broadcast_status_update`` -
    the position is already durably saved regardless of whether anyone is
    connected right now.

    Args:
        checkin: The check-in whose location group should receive the update.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            safety_checkin_location_group_name(checkin.pk),
            {
                "type": "location.update",
                "payload": {
                    "type": "location_update",
                    "sharing_enabled": checkin.live_location_sharing_enabled,
                    "latitude": float(checkin.live_latitude) if checkin.live_latitude is not None else None,
                    "longitude": float(checkin.live_longitude) if checkin.live_longitude is not None else None,
                    "accuracy": checkin.live_location_accuracy,
                    "updated_at": checkin.live_location_updated_at.isoformat() if checkin.live_location_updated_at else None,
                },
            },
        )
    except Exception:
        logger.exception("Failed to broadcast live location for checkin %s", checkin.pk)


def schedule_checkin_archival(checkin: SafetyCheckin) -> None:
    """Schedule post-resolution encryption/archival for a just-resolved check-in.

    Called at the end of every resolution path (``check_in``, ``mark_found_safe``,
    ``mark_found_safe_by_partner``, ``cancel_checkin`` - cancellation is included,
    since a cancelled check-in still contains a plan and destination, exactly the
    PII this feature protects). If no one but the owner could ever have seen this
    check-in (no accepted partners, no contacts of either kind), there's no one who
    needs a final look before archival, so it happens immediately; otherwise a
    1-hour grace window is left open for final updates/comments.

    Args:
        checkin: The just-resolved check-in. ``checkin.resolved_at`` must already be set.

    Raises:
        ValueError: If ``checkin.resolved_at`` is unset - every caller sets it
            immediately before calling this, so reaching here without it set is
            a bug in the caller, not a normal "not resolved yet" state to handle quietly.
    """
    resolved_at = checkin.resolved_at
    if resolved_at is None:
        raise ValueError(f"Cannot schedule archival for checkin {checkin.pk}: resolved_at is not set.")

    other_viewers_exist = checkin.partners.filter(status=SafetyCheckinPartnerStatus.ACCEPTED).exists() or checkin.contacts.exists()
    archive_at = resolved_at + ARCHIVE_VIEWER_GRACE_PERIOD if other_viewers_exist else resolved_at
    checkin.archive_scheduled_at = archive_at
    checkin.save(update_fields=["archive_scheduled_at", "updated"])
    _broadcast_archive_scheduled(checkin)

    from urbanlens.dashboard.services.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import archive_safety_checkin

    countdown = max(0, int((archive_at - timezone.now()).total_seconds()))
    safely_enqueue_task(archive_safety_checkin, checkin.pk, countdown=countdown)


def archive_checkin(checkin: SafetyCheckin) -> None:
    """Encrypt a resolved check-in's PII, sealed to only the owner's E2EE key, and scrub the plaintext.

    Idempotent - a no-op if ``checkin`` already has an archive. If the owner has
    no ``MessagingKeyBundle`` yet (rare - enrollment is automatic at every login/
    authenticated page load, see ``docs/e2ee.md``), logs a warning and returns
    without archiving or scrubbing anything; the periodic sweep
    (``tasks.sweep_due_safety_checkin_archival``) retries every 5 minutes until
    a bundle appears.

    Args:
        checkin: The check-in due for archival.
    """
    if hasattr(checkin, "archive"):
        return

    from urbanlens.dashboard.models.e2ee.key_bundle import MessagingKeyBundle

    bundle = MessagingKeyBundle.objects.for_profile(checkin.profile).first()
    if bundle is None:
        logger.warning("Safety checkin %s is due for archival, but owner %s has no E2EE key bundle yet - will retry", checkin.uuid, checkin.profile_id)
        return

    ciphertext, nonce, sealed_key = _seal_archive_payload(_build_archive_payload(checkin), bundle.public_key)
    try:
        # Atomic so a crash/restart between the two can never leave an archive row
        # committed with its plaintext still unscrubbed (or vice versa) - the
        # idempotency check above only means anything if the two always happen together.
        with transaction.atomic():
            SafetyCheckinArchive.objects.create(
                checkin=checkin,
                ciphertext=ciphertext,
                nonce=nonce,
                sealed_key=sealed_key,
                key_bundle_version=bundle.version,
            )
            _scrub_checkin_pii(checkin)
    except IntegrityError:
        # The countdown-scheduled task and the periodic sweep can both reach here for
        # the same checkin at nearly the same time; the loser's unique-constraint
        # violation just means the winner already did (or is doing) this work.
        logger.info("Safety checkin %s was already archived by a concurrent task", checkin.pk)
        return
    _broadcast_checkin_archived(checkin)


def _build_archive_payload(checkin: SafetyCheckin) -> dict:
    """Collect a resolved check-in's PII into the JSON structure that gets encrypted.

    Args:
        checkin: The check-in being archived.

    Returns:
        A JSON-serializable dict of everything scrubbed by ``_scrub_checkin_pii``,
        plus enough context (contacts, partners, chat) to be a meaningful record.
    """
    location = checkin.destination_location
    trip = checkin.trip
    primary_map = checkin.markup_map
    attached_maps = [*([primary_map] if primary_map is not None else []), *checkin.markup_maps.all()]
    return {
        "title": checkin.title,
        "plan_details": checkin.plan_details,
        "contact_message": checkin.contact_message,
        "destination_latitude": float(checkin.destination_latitude) if checkin.destination_latitude is not None else None,
        "destination_longitude": float(checkin.destination_longitude) if checkin.destination_longitude is not None else None,
        "destination_location": {"name": location.official_name, "address": location.address} if location is not None else None,
        "trip": {"name": trip.name} if trip is not None else None,
        # Snapshots (viewport + drawn shapes) of every map linked to this check-in - the
        # primary route map (`markup_map`) and any secondary reference maps
        # (`markup_maps`) alike. The MarkupMap rows themselves are left alone (they're
        # independently owned/reusable resources, see MarkupMap's own docstring), but the
        # check-in's own record must not keep a plaintext FK a DB-level reader could join
        # through, so the content is captured here before those links are severed below.
        "maps": [{"title": markup_map.title, **markup_map.to_snapshot()} for markup_map in attached_maps],
        "resolved_by_label": checkin.resolved_by_label,
        "resolved_at": checkin.resolved_at.isoformat() if checkin.resolved_at else None,
        "contacts": [{"display_name": contact.display_name, "email": contact.email} for contact in checkin.contacts.select_related("contact_profile").all()],
        "partners": [partner.profile.username for partner in checkin.partners.select_related("profile").all()],
        "messages": [
            {"sender_name": message.sender_name, "body": message.body, "created": message.created.isoformat()}
            for message in checkin.messages.select_related("sender_profile", "sender_contact").all()
        ],
    }


def _seal_archive_payload(payload: dict, owner_public_key_b64: str) -> tuple[str, str, str]:
    """Encrypt a payload under a fresh random key, then seal that key to a public key.

    Mirrors the exact primitives ``ConversationKey``/direct messages already use
    (see ``docs/e2ee.md`` and ``tests/hypothesis/test_e2ee_interop.py``), just run
    server-side instead of in the browser - sealing only ever needs the
    recipient's *public* key, so the owner doesn't need to be online for this.

    Args:
        payload: JSON-serializable dict to encrypt.
        owner_public_key_b64: The owner's ``MessagingKeyBundle.public_key`` (base64 X25519 key).

    Returns:
        (ciphertext_b64, nonce_b64, sealed_key_b64).
    """
    import base64
    import json

    import nacl.public
    import nacl.secret
    import nacl.utils

    symmetric_key = nacl.utils.random(nacl.secret.SecretBox.KEY_SIZE)
    nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)
    ciphertext = nacl.secret.SecretBox(symmetric_key).encrypt(json.dumps(payload).encode(), nonce).ciphertext

    public_key = nacl.public.PublicKey(base64.b64decode(owner_public_key_b64))
    sealed_key = nacl.public.SealedBox(public_key).encrypt(symmetric_key)

    return (
        base64.b64encode(ciphertext).decode(),
        base64.b64encode(nonce).decode(),
        base64.b64encode(sealed_key).decode(),
    )


def _scrub_checkin_pii(checkin: SafetyCheckin) -> None:
    """Null/blank every PII field an archive now covers, leaving structure intact.

    Status enums, timestamps, and ``uuid``/``token`` all stay - the undo
    framework and ``SafetyContactOptOut`` FK resolution both depend on the rows
    (and, for contacts, the ``contact_profile`` FK) continuing to exist.
    ``destination_location``/``trip``/``markup_map``/``markup_maps`` are
    severed (all nullable) rather than left pointing at plaintext a DB-level
    reader could join through - their content was already captured into the
    encrypted archive by ``_build_archive_payload``, and the referenced
    Location/Trip/MarkupMap rows themselves are untouched, shared/reusable
    data with their own independent lifecycle (see those models' docstrings).

    Args:
        checkin: The check-in whose plaintext PII should be scrubbed.
    """
    checkin.title = ""
    checkin.plan_details = ""
    checkin.contact_message = ""
    checkin.destination_latitude = None
    checkin.destination_longitude = None
    checkin.destination_location = None
    checkin.trip = None
    checkin.markup_map = None
    checkin.live_latitude = None
    checkin.live_longitude = None
    checkin.resolved_by_label = ""
    checkin.save(
        update_fields=[
            "title",
            "plan_details",
            "contact_message",
            "destination_latitude",
            "destination_longitude",
            "destination_location",
            "trip",
            "markup_map",
            "live_latitude",
            "live_longitude",
            "resolved_by_label",
            "updated",
        ],
    )
    checkin.markup_maps.clear()
    # A contact_profile-linked contact already has email=None (the model's own
    # exactly-one-of CheckConstraint enforces that at creation) - blanking name is
    # all that's needed. An email-only contact (no account) can't have its email
    # nulled without violating that same constraint (both sides would be null), so
    # it's replaced with a non-PII sentinel instead of cleared outright.
    checkin.contacts.filter(contact_profile__isnull=False).update(name="")
    checkin.contacts.filter(contact_profile__isnull=True).update(email="scrubbed@archived.invalid", name="")
    checkin.messages.update(body="")


def _broadcast_archive_scheduled(checkin: SafetyCheckin) -> None:
    """Push a check-in's archival countdown to its connected group.

    Lets every open tab's countdown display agree without polling - see
    ``_archive_countdown.html``. Best-effort, same as the other broadcast
    helpers in this module: the schedule is already durably saved regardless
    of whether anyone is connected right now.

    Args:
        checkin: The check-in whose group should receive the countdown.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            safety_checkin_group_name(checkin.pk),
            {
                "type": "archive.scheduled",
                "payload": {
                    "type": "archive_scheduled",
                    "archive_at": checkin.archive_scheduled_at.isoformat() if checkin.archive_scheduled_at else None,
                },
            },
        )
    except Exception:
        logger.exception("Failed to broadcast archive schedule for checkin %s", checkin.pk)


def _broadcast_partner_access_revoked(checkin: SafetyCheckin, profile_id: int) -> None:
    """Tell a just-removed partner's connection(s) to close, if any are open.

    Delivered on the main ``safety_checkin_{pk}`` group - every connection for this
    check-in receives it, but ``SafetyCheckinChatConsumer.partner_access_revoked``
    only acts on it when the payload's ``profile_id`` matches its own bound profile,
    so the owner and every other partner/contact are unaffected.

    Args:
        checkin: The check-in the partner was removed from.
        profile_id: PK of the removed partner's profile.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            safety_checkin_group_name(checkin.pk),
            {"type": "partner.access_revoked", "payload": {"profile_id": profile_id}},
        )
    except Exception:
        logger.exception("Failed to broadcast partner access revocation for checkin %s", checkin.pk)


def _broadcast_checkin_archived(checkin: SafetyCheckin) -> None:
    """Push the "this check-in is now archived" event to its connected group.

    Drives the dissolve animation on any page that has this check-in open
    (see ``_dissolve_animation.html``) - purely a live UX cue, since a plain
    page refresh already renders the archived state directly from
    ``hasattr(checkin, "archive")`` regardless of whether this broadcast
    is ever received.

    Args:
        checkin: The just-archived check-in.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            safety_checkin_group_name(checkin.pk),
            {"type": "checkin.archived", "payload": {"type": "checkin_archived"}},
        )
    except Exception:
        logger.exception("Failed to broadcast archival for checkin %s", checkin.pk)


def notify_contacts_of_update(checkin: SafetyCheckin, summary: str) -> None:
    """Re-notify already-notified contacts that the owner changed something after escalation.

    No-ops unless the check-in has actually escalated and is still unresolved - editing before
    contacts have been told to watch a check-in, or after it's already concluded, leaves no one
    to notify. Also no-ops within ``PLAN_UPDATE_NOTIFICATION_COOLDOWN`` of the last such resend,
    so a burst of edits (e.g. drawing several map annotations in a row) sends one notification,
    not one per edit.

    Args:
        checkin: The check-in that was just edited.
        summary: Short description of what changed, e.g. "updated their trip plan".
    """
    if checkin.escalated_at is None or checkin.is_resolved:
        return
    now = timezone.now()
    if checkin.plan_update_notified_at and now - checkin.plan_update_notified_at < PLAN_UPDATE_NOTIFICATION_COOLDOWN:
        return

    for contact in checkin.contacts.filter(notified_at__isnull=False, found_safe_at__isnull=True):
        if is_contact_opted_out(contact.contact_profile, contact.email, owner=checkin.profile, checkin=checkin):
            continue
        portal_path = reverse("safety.contact.portal", kwargs={"token": contact.token})
        if contact.contact_profile_id:
            NotificationLog.objects.create(
                profile=contact.contact_profile,
                source_profile=checkin.profile,
                status=Status.UNREAD,
                importance=Importance.MEDIUM,
                notification_type=NotificationType.SAFETY_CHECKIN_PLAN_UPDATED,
                title=f"{checkin.profile.username} {summary}",
                message=f'"{checkin.title}" was just updated - take another look.',
                url=portal_path,
            )
        contact_email = contact.contact_profile.user.email if contact.contact_profile and contact.contact_profile.user else contact.email
        _send_email(
            to=contact_email or "",
            subject=f"{checkin.profile.username} updated their check-in",
            template="dashboard/email/safety_checkin_plan_updated.html",
            context={
                "checkin": checkin,
                "contact": contact,
                "summary": summary,
                "portal_url": _absolute_url(portal_path),
                **_optout_urls(contact),
            },
        )

    checkin.plan_update_notified_at = now
    checkin.save(update_fields=["plan_update_notified_at", "updated"])


def find_community_wiki(latitude: float | Decimal | None, longitude: float | Decimal | None) -> Wiki | None:
    """Return the community Wiki covering a destination point, if one already exists.

    Only *existing* wikis count - wikis are created explicitly by users (see
    ``WikiCreationService``), and a check-in escalation must not conjure a
    community page for a place nobody has written about. Matching mirrors
    ``LocationManager.get_for_point``: location-default generated boundary
    polygons first, then a 50 m proximity fallback.

    Args:
        latitude: WGS-84 latitude of the destination, or None if no destination is set.
        longitude: WGS-84 longitude of the destination, or None if no destination is set.

    Returns:
        The matching Wiki, or None when there is no destination or no wiki covers it.
    """
    if latitude is None or longitude is None:
        return None
    from urbanlens.dashboard.models.location.model import Location

    location = Location.objects.filter(wiki__isnull=False).within_bounding_box(float(latitude), float(longitude)).first()
    return location.wiki if location else None


def wiki_notify_stats(wiki: Wiki) -> tuple[datetime.datetime | None, int]:
    """Return the wiki-notify toggle's stats: last edit time and editor count.

    The editor count is deliberately drawn from edit *history* (who has actually
    contributed), not from any access/permissions list - showing how many people
    could see the wiki would leak private visibility information the check-in
    owner never agreed to disclose.

    Args:
        wiki: The destination's community wiki.

    Returns:
        Tuple of (last edit timestamp or None if never edited, distinct editor count).
    """
    from urbanlens.dashboard.models.wiki_edit.model import WikiEdit

    edits = WikiEdit.objects.for_wiki(wiki).active()
    last_edit = edits.first()
    editor_count = edits.exclude(editor__isnull=True).values_list("editor", flat=True).distinct().count()
    return (last_edit.created if last_edit else None, editor_count)


def post_checkin_to_community_wiki(checkin: SafetyCheckin) -> None:
    """Post an escalated check-in to its destination's community wiki and notify pin owners there.

    Runs at escalation time, alongside the emergency-contact notifications, and only
    when the owner opted in (``checkin.notify_community_wiki``). Posts a comment on
    the wiki (authored by the check-in's owner) linking to the check-in page, then
    notifies every profile with a pin at the wiki's location - per their
    ``wiki_safety_checkin`` notification preference - that a safety check-in was
    posted. Idempotent via ``wiki_notified_at``.

    Args:
        checkin: The escalating check-in.
    """
    if checkin.wiki_notified_at is not None:
        return
    wiki = find_community_wiki(checkin.destination_latitude, checkin.destination_longitude)
    if wiki is None:
        logger.info("Safety check-in %s opted into community wiki notification, but no wiki covers its destination", checkin.uuid)
        return

    from urbanlens.dashboard.models.comments.model import Comment

    # The community-facing check-in page is linked by UUID, not slug - slugs are
    # only unique per-profile, so a slug URL isn't safe to resolve across owners.
    checkin_path = reverse("safety.checkin.detail", kwargs={"checkin_slug": str(checkin.uuid)})
    checkin_url = _absolute_url(checkin_path)
    wiki_path = reverse("location.wiki", kwargs={"location_slug": wiki.location.slug or str(wiki.location.uuid)})

    Comment.objects.create(
        wiki=wiki,
        profile=checkin.profile,
        text=(f"Safety alert: {checkin.profile.username} planned a trip here (\"{checkin.title}\") and hasn't checked in as expected. If you've seen them or can help, see the check-in page: {checkin_url}"),
    )
    checkin.wiki_notified_at = timezone.now()
    checkin.save(update_fields=["wiki_notified_at", "updated"])

    recipients = Profile.objects.filter(pins__location=wiki.location, pins__parent_pin__isnull=True).exclude(pk=checkin.profile_id).distinct()
    for recipient in recipients:
        try:
            pref = recipient.notification_preferences.wiki_safety_checkin
        except AttributeError:
            pref = DeliveryPreference.BOTH
        if pref == DeliveryPreference.NONE:
            continue
        if pref in (DeliveryPreference.SITE, DeliveryPreference.BOTH):
            NotificationLog.objects.create(
                profile=recipient,
                source_profile=checkin.profile,
                status=Status.UNREAD,
                importance=Importance.HIGH,
                notification_type=NotificationType.WIKI_SAFETY_CHECKIN,
                title=f"Safety check-in posted to {wiki.name}",
                message=f"{checkin.profile.username} hasn't checked in from a trip to {wiki.name} - a place you have pinned.",
                url=wiki_path,
            )
        recipient_email = recipient.user.email if recipient.user else None
        if pref in (DeliveryPreference.EMAIL, DeliveryPreference.BOTH) and recipient_email:
            _send_email(
                to=recipient_email,
                subject=f"Safety check-in posted to {wiki.name}",
                template="dashboard/email/safety_checkin_wiki.html",
                context={
                    "checkin": checkin,
                    "wiki": wiki,
                    "checkin_url": checkin_url,
                    "wiki_url": _absolute_url(wiki_path),
                },
            )

        message_text = f"Safety check-in posted to {wiki.name}: {checkin.profile.username} hasn't checked in from a trip there. {checkin_url}"
        try:
            prefs = recipient.notification_preferences
        except AttributeError:
            prefs = None
        if prefs and prefs.wiki_safety_checkin_whatsapp:
            send_whatsapp(recipient, message_text)
        if prefs and prefs.wiki_safety_checkin_sms:
            send_sms(recipient, message_text)


def create_checkin(
    *,
    profile: Profile,
    title: str,
    checkin_by: datetime.datetime,
    grace_period: datetime.timedelta,
    plan_details: str = "",
    contact_message: str = "",
    trip: Trip | None = None,
    destination_location: Location | None = None,
    destination_latitude: float | Decimal | None = None,
    destination_longitude: float | Decimal | None = None,
    contacts: Iterable[ContactInput] = (),
    notify_community_wiki: bool = False,
) -> SafetyCheckin:
    """Create a new safety check-in with its emergency contacts.

    Args:
        profile: The profile the check-in belongs to.
        title: Short display label.
        checkin_by: When the profile is expected to check in.
        grace_period: How long after checkin_by before contacts are notified.
        plan_details: Free-form trip plan description.
        contact_message: Custom message shown to emergency contacts.
        trip: The Trip this check-in is for, if started from one - scopes the
            active-check-in exclusivity check (see ``get_active_checkin``) so
            a general check-in and one per trip can be active simultaneously.
        destination_location: Shared Location for the destination, if known.
        destination_latitude: Destination latitude, for the concluding VisitSuggestion.
        destination_longitude: Destination longitude, for the concluding VisitSuggestion.
        contacts: Iterable of (contact_profile, email, name) tuples.
        notify_community_wiki: Whether escalation should also post to the destination's
            community wiki (see ``post_checkin_to_community_wiki``).

    Returns:
        The newly created SafetyCheckin.

    Raises:
        ValueError: If the profile already has an active check-in in this
            scope - only one may be active per (profile, trip) at a time
            (see ``get_active_checkin``).
    """
    if get_active_checkin(profile, trip=trip) is not None:
        raise ValueError("You already have an active check-in. Check in or cancel it before starting a new one.")

    checkin = SafetyCheckin.objects.create(
        profile=profile,
        title=title,
        checkin_by=checkin_by,
        grace_period=grace_period,
        plan_details=plan_details,
        contact_message=contact_message,
        trip=trip,
        destination_location=destination_location,
        destination_latitude=destination_latitude,
        destination_longitude=destination_longitude,
        notify_community_wiki=notify_community_wiki,
    )
    checkin.ensure_slug()
    set_checkin_contacts(checkin, contacts)
    return checkin


def cancel_checkin(checkin: SafetyCheckin) -> None:
    """Cancel a check-in so it will never fire a reminder or escalation.

    Args:
        checkin: The check-in to cancel.
    """
    checkin.status = SafetyCheckinStatus.CANCELLED
    checkin.resolved_at = timezone.now()
    checkin.resolved_by_label = "cancelled by owner"
    checkin.save(update_fields=["status", "resolved_at", "resolved_by_label", "updated"])
    _broadcast_status_update(checkin)
    schedule_checkin_archival(checkin)


def send_checkin_reminder(checkin: SafetyCheckin) -> None:
    """Notify the owner that their check-in is due, and mark the reminder sent.

    Args:
        checkin: The check-in whose ``checkin_by`` time has arrived.
    """
    checkin_path = reverse("safety.checkin.checkin", kwargs={"checkin_slug": _checkin_url_slug(checkin)})
    NotificationLog.objects.create(
        profile=checkin.profile,
        status=Status.UNREAD,
        importance=Importance.HIGH,
        notification_type=NotificationType.SAFETY_CHECKIN_DUE,
        title="Time to check in",
        message=f'"{checkin.title}" is due for a check-in.',
        url=checkin_path,
    )
    if checkin.profile.user and checkin.profile.user.email:
        _send_email(
            to=checkin.profile.user.email,
            subject=f'Check in for "{checkin.title}"',
            template="dashboard/email/safety_checkin_reminder.html",
            context={"checkin": checkin, "checkin_url": _absolute_url(checkin_path)},
        )
    checkin.status = SafetyCheckinStatus.AWAITING_CHECKIN
    checkin.reminder_sent_at = timezone.now()
    checkin.save(update_fields=["status", "reminder_sent_at", "updated"])


def send_final_warning(checkin: SafetyCheckin) -> None:
    """Give the owner one last chance to check in before contacts are notified.

    Args:
        checkin: The check-in nearing the end of its grace period (see
            ``SafetyCheckin.objects.due_for_final_warning``).
    """
    checkin_path = reverse("safety.checkin.checkin", kwargs={"checkin_slug": _checkin_url_slug(checkin)})
    NotificationLog.objects.create(
        profile=checkin.profile,
        status=Status.UNREAD,
        importance=Importance.HIGH,
        notification_type=NotificationType.SAFETY_CHECKIN_FINAL_WARNING,
        title="Check in now",
        message=f'Your emergency contacts will be notified soon if you don\'t check in for "{checkin.title}".',
        url=checkin_path,
    )
    if checkin.profile.user and checkin.profile.user.email:
        _send_email(
            to=checkin.profile.user.email,
            subject=f'Final reminder: check in for "{checkin.title}"',
            template="dashboard/email/safety_checkin_final_warning.html",
            context={"checkin": checkin, "checkin_url": _absolute_url(checkin_path)},
        )
    checkin.final_warning_sent_at = timezone.now()
    checkin.save(update_fields=["final_warning_sent_at", "updated"])


def check_in(checkin: SafetyCheckin, profile: Profile) -> None:
    """Record that the profile checked in on time (or late, before escalation).

    Args:
        checkin: The check-in being resolved.
        profile: The profile checking in (must be checkin.profile).
    """
    checkin.status = SafetyCheckinStatus.CHECKED_IN
    checkin.resolved_at = timezone.now()
    checkin.resolved_by_label = "you"
    checkin.save(update_fields=["status", "resolved_at", "resolved_by_label", "updated"])
    _broadcast_status_update(checkin)
    _conclude_checkin(checkin)
    schedule_checkin_archival(checkin)


def escalate_checkin(checkin: SafetyCheckin) -> None:
    """Notify every emergency contact that the profile hasn't checked in.

    Does not resolve the check-in - contacts still need to respond by marking
    the profile safe. If the owner opted in, the destination's community wiki
    is notified at the same time (see ``post_checkin_to_community_wiki``).

    Args:
        checkin: The overdue check-in.
    """
    if checkin.notify_community_wiki:
        post_checkin_to_community_wiki(checkin)

    # Only contacts not yet notified THIS escalation cycle: escalate_overdue_checkins()
    # re-selects any checkin still in SCHEDULED/AWAITING_CHECKIN on its next 5-minute tick,
    # which includes this one if anything below raises before the status update at the end
    # of this function runs. Filtering on notified_at makes a retry idempotent - it only
    # reaches the contacts a prior, partially-failed attempt never got to - instead of
    # re-emailing every contact already notified, including real emergency contacts.
    for contact in checkin.contacts.filter(notified_at__isnull=True):
        if is_contact_opted_out(contact.contact_profile, contact.email, owner=checkin.profile, checkin=checkin):
            continue
        portal_path = reverse("safety.contact.portal", kwargs={"token": contact.token})
        if contact.contact_profile_id:
            NotificationLog.objects.create(
                profile=contact.contact_profile,
                source_profile=checkin.profile,
                status=Status.UNREAD,
                importance=Importance.HIGH,
                notification_type=NotificationType.SAFETY_CHECKIN_OVERDUE,
                title=f"{checkin.profile.username} hasn't checked in",
                message=f'"{checkin.title}" is overdue. Take a look and let them know if you find them.',
                url=portal_path,
            )
        contact_email = contact.contact_profile.user.email if contact.contact_profile and contact.contact_profile.user else contact.email
        _send_email(
            to=contact_email or "",
            subject=f"{checkin.profile.username} hasn't checked in",
            template="dashboard/email/safety_checkin_overdue.html",
            context={"checkin": checkin, "contact": contact, "portal_url": _absolute_url(portal_path), **_optout_urls(contact)},
        )
        contact.notified_at = timezone.now()
        contact.save(update_fields=["notified_at", "updated"])

    checkin.status = SafetyCheckinStatus.OVERDUE
    checkin.escalated_at = timezone.now()
    checkin.save(update_fields=["status", "escalated_at", "updated"])
    _broadcast_status_update(checkin)


def mark_found_safe(contact: SafetyCheckinContact) -> None:
    """Record that an emergency contact found the profile safe, and notify everyone else.

    Args:
        contact: The contact reporting the profile as safe.
    """
    contact.found_safe_at = timezone.now()
    contact.save(update_fields=["found_safe_at", "updated"])

    checkin = contact.checkin
    system_message = SafetyCheckinMessage.objects.create(
        checkin=checkin,
        sender_contact=contact,
        body=f"Marked {checkin.profile.username} as safe.",
    )
    broadcast_chat_message(checkin, system_message)
    _resolve_as_found_safe(checkin, resolved_by_label=contact.display_name, exclude_contact=contact)


def mark_found_safe_by_partner(checkin: SafetyCheckin, partner: Profile) -> None:
    """Record that an accepted partner found the profile safe, and notify everyone else.

    Mirrors ``mark_found_safe``, but a partner has no ``SafetyCheckinContact``
    row of their own to stamp ``found_safe_at`` on - the resolution and every
    owner/contact notification path is otherwise identical.

    Args:
        checkin: The check-in being resolved.
        partner: The accepted partner's profile reporting the owner as safe.
    """
    system_message = SafetyCheckinMessage.objects.create(
        checkin=checkin,
        sender_profile=partner,
        body=f"Marked {checkin.profile.username} as safe.",
    )
    broadcast_chat_message(checkin, system_message)
    _resolve_as_found_safe(checkin, resolved_by_label=partner.username)


def _resolve_as_found_safe(checkin: SafetyCheckin, *, resolved_by_label: str, exclude_contact: SafetyCheckinContact | None = None) -> None:
    """Shared resolution logic for ``mark_found_safe``/``mark_found_safe_by_partner``.

    No-ops if the check-in is already resolved - a contact and a partner (or
    two contacts) racing to mark the same check-in safe must not double-notify
    everyone or re-raise the concluding VisitSuggestion.

    Args:
        checkin: The check-in being resolved.
        resolved_by_label: Display name of whoever reported the profile safe,
            for the owner/other-contact notification text.
        exclude_contact: The contact who just reported this, if any - excluded
            from the "everyone else" notification pass so they don't get told
            about their own report.
    """
    resolved_at = timezone.now()
    # A conditional UPDATE, not a read-then-write - two concurrent reports (a contact and
    # a partner, or two contacts, racing to mark the same check-in safe) must not both
    # pass an in-memory `is_resolved` check and double-notify everyone/double-schedule
    # archival. Only the row matching the WHERE clause at UPDATE time - i.e. still
    # unresolved - gets touched; the loser's `updated` count is 0.
    updated = SafetyCheckin.objects.filter(pk=checkin.pk).exclude(status__in=SafetyCheckinStatus.resolved_statuses()).update(
        status=SafetyCheckinStatus.FOUND_SAFE,
        resolved_at=resolved_at,
        resolved_by_label=resolved_by_label,
        updated=resolved_at,
    )
    if not updated:
        return
    checkin.status = SafetyCheckinStatus.FOUND_SAFE
    checkin.resolved_at = resolved_at
    checkin.resolved_by_label = resolved_by_label
    _broadcast_status_update(checkin)

    checkin_path = reverse("safety.checkin.detail", kwargs={"checkin_slug": _checkin_url_slug(checkin)})
    NotificationLog.objects.create(
        profile=checkin.profile,
        status=Status.UNREAD,
        importance=Importance.HIGH,
        notification_type=NotificationType.SAFETY_CHECKIN_RESOLVED,
        title=f"{resolved_by_label} found you",
        message=f'{resolved_by_label} marked you safe for "{checkin.title}".',
        url=checkin_path,
    )
    if checkin.profile.user and checkin.profile.user.email:
        _send_email(
            to=checkin.profile.user.email,
            subject=f'You were marked safe for "{checkin.title}"',
            template="dashboard/email/safety_checkin_resolved.html",
            context={"checkin": checkin, "resolved_by_label": resolved_by_label, "checkin_url": _absolute_url(checkin_path)},
        )

    other_contacts = checkin.contacts.all()
    if exclude_contact is not None:
        other_contacts = other_contacts.exclude(pk=exclude_contact.pk)
    for other in other_contacts:
        if is_contact_opted_out(other.contact_profile, other.email, owner=checkin.profile, checkin=checkin):
            continue
        portal_path = reverse("safety.contact.portal", kwargs={"token": other.token})
        if other.contact_profile:
            NotificationLog.objects.create(
                profile=other.contact_profile,
                status=Status.UNREAD,
                importance=Importance.MEDIUM,
                notification_type=NotificationType.SAFETY_CHECKIN_RESOLVED,
                title=f"{checkin.profile.username} has been found",
                message=f"{resolved_by_label} marked {checkin.profile.username} safe.",
                url=portal_path,
            )
            other_email = other.contact_profile.user.email if other.contact_profile and other.contact_profile.user else other.email
        else:
            other_email = other.email

        _send_email(
            to=other_email or "",
            subject=f"{checkin.profile.username} has been found",
            template="dashboard/email/safety_checkin_resolved.html",
            context={"checkin": checkin, "resolved_by_label": resolved_by_label, "checkin_url": _absolute_url(portal_path), **_optout_urls(other)},
        )

    _conclude_checkin(checkin)
    schedule_checkin_archival(checkin)


def _conclude_checkin(checkin: SafetyCheckin) -> None:
    """Raise a pending VisitSuggestion for the destination, once, when a check-in concludes.

    Idempotent - skips if this check-in already raised a suggestion.

    Args:
        checkin: The concluded check-in.
    """
    if checkin.destination_latitude is None or checkin.destination_longitude is None:
        return
    if checkin.visit_suggestions.exists():
        return
    create_visit_suggestion(
        suggested_to=checkin.profile,
        suggested_by=None,
        visited_at=checkin.resolved_at or timezone.now(),
        location=checkin.destination_location,
        latitude=checkin.destination_latitude,
        longitude=checkin.destination_longitude,
        candidate_profiles=[],
        safety_checkin=checkin,
    )


def resolve_message_sender(user: User | AnonymousUser, contact: SafetyCheckinContact | None) -> tuple[Profile | None, SafetyCheckinContact | None]:
    """Resolve who is sending a chat message: the owner, a user-linked contact, or an anonymous/email-only contact.

    Shared between the HTTP chat endpoint and the WebSocket consumer so the
    two code paths can't drift.

    Args:
        user: The requesting Django user. Always authenticated on the owner
            route; may or may not be on the contact route, since a contact
            link works without an account.
        contact: The SafetyCheckinContact authorizing this request, or None
            on the owner route.

    Returns:
        (sender_profile, sender_contact) - exactly one is set. When contact
        is None, sender_profile is the owner's own profile. When a contact is
        set and the requesting user happens to be logged in as that same
        linked profile, sender_profile is used instead so the message is
        attributed to a real profile rather than the anonymous contact
        record.
    """
    if contact is None:
        profile, _ = Profile.objects.get_or_create(user=user)
        return profile, None
    if contact.contact_profile_id and user.is_authenticated:
        profile, _ = Profile.objects.get_or_create(user=user)
        if profile.pk == contact.contact_profile_id:
            return contact.contact_profile, None
    return None, contact


def create_chat_message(checkin: SafetyCheckin, *, user: User | AnonymousUser, contact: SafetyCheckinContact | None, body: str) -> SafetyCheckinMessage:
    """Create a new chat message on a check-in.

    Args:
        checkin: The check-in the message belongs to.
        user: The requesting Django user (see ``resolve_message_sender``).
        contact: The SafetyCheckinContact authorizing this request, or None on the owner route.
        body: Message text.

    Returns:
        The newly created SafetyCheckinMessage.

    Raises:
        ValueError: If ``body`` is blank or exceeds ``MAX_CHAT_MESSAGE_LENGTH``,
            or the check-in has already been archived. Both callers (the
            WebSocket consumer and the no-JS HTTP fallback) catch this and
            surface it to the sender - a safety check-in chat failing
            silently is worse than most other features failing silently.
    """
    if hasattr(checkin, "archive"):
        raise ValueError("This check-in has concluded and can no longer receive messages.")
    body = body.strip()
    if not body:
        raise ValueError("Message cannot be empty.")
    if len(body) > MAX_CHAT_MESSAGE_LENGTH:
        raise ValueError(f"Message is too long (max {MAX_CHAT_MESSAGE_LENGTH} characters).")

    sender_profile, sender_contact = resolve_message_sender(user, contact)
    message = SafetyCheckinMessage.objects.create(checkin=checkin, sender_profile=sender_profile, sender_contact=sender_contact, body=body)
    logger.info(
        "Safety check-in %s: chat message %s from %s",
        checkin.uuid,
        message.pk,
        sender_contact.display_name if sender_contact else (sender_profile.username if sender_profile else "unknown"),
    )
    return message


def broadcast_chat_message(checkin: SafetyCheckin, message: SafetyCheckinMessage) -> None:
    """Push a chat message to any live-connected chat clients for this check-in.

    Mirrors the payload shape ``SafetyCheckinChatConsumer._create_message`` builds,
    so the frontend's ``appendMessage()`` handles either source identically. Used
    for system-generated messages (e.g. mark-safe) that don't go through the
    consumer's own ``receive()`` broadcast path.

    Best-effort: the message is already durably saved regardless of whether
    anyone is connected right now, so a broadcast failure is logged, not raised.

    Args:
        checkin: The check-in whose chat group should receive the message.
        message: The already-saved message to broadcast.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            safety_checkin_group_name(checkin.pk),
            {
                "type": "chat.message",
                "message": {
                    "type": "message",
                    "id": message.pk,
                    "sender_name": message.sender_name,
                    "body": message.body,
                    "created": message.created.isoformat(),
                },
            },
        )
    except Exception:
        logger.exception("Failed to broadcast chat message for checkin %s", checkin.pk)


def _broadcast_status_update(checkin: SafetyCheckin) -> None:
    """Push a check-in status change to any live-connected chat clients for this check-in.

    The status label, the "I'm safe"/"I found them" action buttons, and the leave-page
    warning all depend on ``checkin.status``/``is_resolved`` - without this, anyone with
    the detail page or contact portal already open only saw the change via the system
    chat message, not in the label/button state, until they reloaded.

    Best-effort, same as ``broadcast_chat_message`` - the status is already durably
    saved regardless of whether anyone is connected right now.

    Args:
        checkin: The check-in whose chat group should receive the update.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            safety_checkin_group_name(checkin.pk),
            {
                "type": "status.update",
                "payload": {
                    "type": "status_update",
                    "status": checkin.status,
                    "status_display": checkin.get_status_display(),
                    "is_resolved": checkin.is_resolved,
                },
            },
        )
    except Exception:
        logger.exception("Failed to broadcast status update for checkin %s", checkin.pk)
