"""Business logic for safety check-ins: contact resolution, lifecycle transitions, and notifications."""

from __future__ import annotations

import logging
import smtplib
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import (
    EmergencyContactDefault,
    SafetyCheckin,
    SafetyCheckinContact,
    SafetyCheckinMessage,
    SafetyCheckinStatus,
    SafetyPreference,
)
from urbanlens.dashboard.services.visits import create_visit_suggestion

if TYPE_CHECKING:
    from collections.abc import Iterable
    import datetime
    from decimal import Decimal

    from django.contrib.auth.models import AnonymousUser, User

    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

# (contact_profile, email, name) - contact_profile wins when both are given.
ContactInput = tuple["Profile | None", "str | None", str]

# Chat messages are TextField (unbounded at the DB layer) - this caps abuse/accidental
# pastes at the application layer. The client mirrors this via maxlength on the input,
# but that's trivially bypassed, so it's re-checked here.
MAX_CHAT_MESSAGE_LENGTH = 4000


def _find_profile_by_email(email: str) -> Profile | None:
    """Return the Profile for an existing active user with this email, if any.

    Args:
        email: Email address to look up.

    Returns:
        The matching Profile, or None.
    """
    from django.contrib.auth.models import User

    user = User.objects.filter(email__iexact=email, is_active=True).select_related("profile").first()
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
    html_body = render_to_string(template, context)
    try:
        msg = EmailMultiAlternatives(subject=subject, body=subject, from_email=None, to=[to])
        msg.attach_alternative(html_body, "text/html")
        msg.send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send safety check-in email to %s", to)


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
    EmergencyContactDefault.objects.filter(owner=profile).delete()
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
    return [(default.contact_profile, default.email, default.label) for default in EmergencyContactDefault.objects.filter(owner=profile)]


def get_active_checkin(profile: Profile) -> SafetyCheckin | None:
    """Return the profile's current active (unresolved) check-in, if any.

    A profile may only have one active check-in at a time - ``create_checkin``
    enforces this - so the earliest-due active check-in is also the only one,
    in practice.

    Args:
        profile: Profile to look up.

    Returns:
        The active SafetyCheckin, or None if the profile has none.
    """
    return SafetyCheckin.objects.active().filter(profile=profile).order_by("checkin_by").first()


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


def create_checkin(
    *,
    profile: Profile,
    title: str,
    checkin_by: datetime.datetime,
    grace_period: datetime.timedelta,
    plan_details: str = "",
    contact_message: str = "",
    destination_location: Location | None = None,
    destination_latitude: float | Decimal | None = None,
    destination_longitude: float | Decimal | None = None,
    contacts: Iterable[ContactInput] = (),
) -> SafetyCheckin:
    """Create a new safety check-in with its emergency contacts.

    Args:
        profile: The profile the check-in belongs to.
        title: Short display label.
        checkin_by: When the profile is expected to check in.
        grace_period: How long after checkin_by before contacts are notified.
        plan_details: Free-form trip plan description.
        contact_message: Custom message shown to emergency contacts.
        destination_location: Shared Location for the destination, if known.
        destination_latitude: Destination latitude, for the concluding VisitSuggestion.
        destination_longitude: Destination longitude, for the concluding VisitSuggestion.
        contacts: Iterable of (contact_profile, email, name) tuples.

    Returns:
        The newly created SafetyCheckin.

    Raises:
        ValueError: If the profile already has an active check-in - only one
            may be active at a time (see ``get_active_checkin``).
    """
    if get_active_checkin(profile) is not None:
        raise ValueError("You already have an active check-in. Check in or cancel it before starting a new one.")

    checkin = SafetyCheckin.objects.create(
        profile=profile,
        title=title,
        checkin_by=checkin_by,
        grace_period=grace_period,
        plan_details=plan_details,
        contact_message=contact_message,
        destination_location=destination_location,
        destination_latitude=destination_latitude,
        destination_longitude=destination_longitude,
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
    checkin.save(update_fields=["status", "resolved_at", "updated"])


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


def check_in(checkin: SafetyCheckin, profile: Profile) -> None:
    """Record that the profile checked in on time (or late, before escalation).

    Args:
        checkin: The check-in being resolved.
        profile: The profile checking in (must be checkin.profile).
    """
    checkin.status = SafetyCheckinStatus.CHECKED_IN
    checkin.resolved_at = timezone.now()
    checkin.save(update_fields=["status", "resolved_at", "updated"])
    _conclude_checkin(checkin)


def escalate_checkin(checkin: SafetyCheckin) -> None:
    """Notify every emergency contact that the profile hasn't checked in.

    Does not resolve the check-in - contacts still need to respond by marking
    the profile safe.

    Args:
        checkin: The overdue check-in.
    """
    for contact in checkin.contacts.all():
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
            context={"checkin": checkin, "contact": contact, "portal_url": _absolute_url(portal_path)},
        )
        contact.notified_at = timezone.now()
        contact.save(update_fields=["notified_at", "updated"])

    checkin.status = SafetyCheckinStatus.OVERDUE
    checkin.escalated_at = timezone.now()
    checkin.save(update_fields=["status", "escalated_at", "updated"])


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
    _broadcast_chat_message(checkin, system_message)

    if checkin.is_resolved:
        return

    checkin.status = SafetyCheckinStatus.FOUND_SAFE
    checkin.resolved_at = timezone.now()
    checkin.save(update_fields=["status", "resolved_at", "updated"])

    checkin_path = reverse("safety.checkin.detail", kwargs={"checkin_slug": _checkin_url_slug(checkin)})
    NotificationLog.objects.create(
        profile=checkin.profile,
        status=Status.UNREAD,
        importance=Importance.HIGH,
        notification_type=NotificationType.SAFETY_CHECKIN_RESOLVED,
        title=f"{contact.display_name} found you",
        message=f'{contact.display_name} marked you safe for "{checkin.title}".',
        url=checkin_path,
    )
    if checkin.profile.user and checkin.profile.user.email:
        _send_email(
            to=checkin.profile.user.email,
            subject=f'You were marked safe for "{checkin.title}"',
            template="dashboard/email/safety_checkin_resolved.html",
            context={"checkin": checkin, "contact": contact, "checkin_url": _absolute_url(checkin_path)},
        )

    for other in checkin.contacts.exclude(pk=contact.pk):
        portal_path = reverse("safety.contact.portal", kwargs={"token": other.token})
        if other.contact_profile:
            NotificationLog.objects.create(
                profile=other.contact_profile,
                status=Status.UNREAD,
                importance=Importance.MEDIUM,
                notification_type=NotificationType.SAFETY_CHECKIN_RESOLVED,
                title=f"{checkin.profile.username} has been found",
                message=f"{contact.display_name} marked {checkin.profile.username} safe.",
                url=portal_path,
            )
            other_email = other.contact_profile.user.email if other.contact_profile and other.contact_profile.user else other.email
        else:
            other_email = other.email

        _send_email(
            to=other_email or "",
            subject=f"{checkin.profile.username} has been found",
            template="dashboard/email/safety_checkin_resolved.html",
            context={"checkin": checkin, "contact": contact, "checkin_url": _absolute_url(portal_path)},
        )

    _conclude_checkin(checkin)


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
        ValueError: If ``body`` is blank or exceeds ``MAX_CHAT_MESSAGE_LENGTH``.
            Both callers (the WebSocket consumer and the no-JS HTTP fallback)
            catch this and surface it to the sender - a safety check-in chat
            failing silently is worse than most other features failing silently.
    """
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


def _broadcast_chat_message(checkin: SafetyCheckin, message: SafetyCheckinMessage) -> None:
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
            f"safety_checkin_{checkin.pk}",
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
