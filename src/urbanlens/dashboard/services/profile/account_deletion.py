"""Business logic for self-service account deletion: request, cancel, remind, and hard-delete."""

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

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


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
        logger.exception("Failed to send account deletion email to %s", to)


def _absolute_url(path: str) -> str:
    """Build an absolute URL from a site-relative path."""
    return f"{settings.SITE_URL.rstrip('/')}{path}"


def request_deletion(profile: Profile) -> None:
    """Soft-delete a profile: start its 7-day grace period and notify the owner.

    Args:
        profile: The profile requesting deletion of its own account.
    """
    profile.deletion_requested_at = timezone.now()
    profile.deletion_reminder_sent_at = None
    profile.save(update_fields=["deletion_requested_at", "deletion_reminder_sent_at", "updated"])

    settings_path = reverse("settings.view")
    NotificationLog.objects.notify(
        profile=profile,
        status=Status.UNREAD,
        importance=Importance.HIGH,
        notification_type=NotificationType.ACCOUNT_DELETION_REQUESTED,
        title="Account deletion requested",
        message=(f"Your account will be permanently deleted on {profile.deletion_scheduled_for:%B %d, %Y}. Log in and undo this any time before then to keep your account."),
        url=settings_path,
    )
    if profile.user and profile.user.email:
        _send_email(
            to=profile.user.email,
            subject="Your UrbanLens account is scheduled for deletion",
            template="dashboard/email/account_deletion_requested.html",
            context={"profile": profile, "settings_url": _absolute_url(settings_path)},
        )


def cancel_deletion(profile: Profile) -> None:
    """Undo a pending account deletion.

    Args:
        profile: The profile whose deletion request is being cancelled.
    """
    profile.deletion_requested_at = None
    profile.deletion_reminder_sent_at = None
    profile.save(update_fields=["deletion_requested_at", "deletion_reminder_sent_at", "updated"])


def send_deletion_reminder(profile: Profile) -> None:
    """Send the "1 day left" notice for a profile about to be hard-deleted.

    Args:
        profile: The profile whose grace period is about to end."""
    settings_path = reverse("settings.view")
    NotificationLog.objects.notify(
        profile=profile,
        status=Status.UNREAD,
        importance=Importance.HIGH,
        notification_type=NotificationType.ACCOUNT_DELETION_REMINDER,
        title="Your account will be deleted tomorrow",
        message="This is your last chance to undo it before your account and all its data are permanently deleted.",
        url=settings_path,
    )
    if profile.user and profile.user.email:
        _send_email(
            to=profile.user.email,
            subject="Your UrbanLens account will be deleted tomorrow",
            template="dashboard/email/account_deletion_reminder.html",
            context={"profile": profile, "settings_url": _absolute_url(settings_path)},
        )
    profile.deletion_reminder_sent_at = timezone.now()
    profile.save(update_fields=["deletion_reminder_sent_at", "updated"])


def _delete_file_field(instance, field_name: str, *, label: str) -> None:
    """Best-effort delete of one FileField's underlying storage file.

    Args:
        instance: The model instance owning the file field.
        field_name: Name of the FileField/ImageField attribute.
        label: Short description for the failure log line.
    """
    field_file = getattr(instance, field_name)
    if not field_file:
        return
    try:
        field_file.delete(save=False)
    except OSError:
        logger.exception("Failed to delete %s file for %s %s", field_name, label, instance.pk)


def _delete_profile_files(profile: Profile) -> None:
    """Best-effort delete of storage files owned by this profile, before the DB rows go."""
    _delete_file_field(profile, "avatar", label="profile")

    for image in profile.uploaded_images.all():
        _delete_file_field(image, "image", label="image")

    for pin in profile.pins.all():
        _delete_file_field(pin, "custom_icon", label="pin")

    for comment in profile.comments.all():
        _delete_file_field(comment, "image", label="comment")

    for label in profile.custom_labels.all():
        _delete_file_field(label, "custom_icon", label="label")


def hard_delete_profile(profile: Profile) -> None:
    """Permanently delete a profile's account and all of its data.

    Args:
        profile: The profile whose grace period has fully elapsed (see ``ProfileQuerySet.due_for_hard_delete``)."""
    email = profile.user.email if profile.user else ""
    username = profile.username

    if email:
        _send_email(
            to=email,
            subject="Your UrbanLens account has been deleted",
            template="dashboard/email/account_deletion_completed.html",
            context={"username": username},
        )

    _delete_profile_files(profile)
    profile.user.delete()
