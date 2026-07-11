"""Safety controls for user-triggered outbound email.

Two independent protections, both required before any feature makes the site
send email to an address a user typed in:

1. **Rate limits** - each user may trigger at most N emails per hour, day,
   and rolling 30 days. The site-wide defaults live on
   :class:`~urbanlens.dashboard.models.site_settings.model.SiteSettings` and
   subscription roles may raise them per tier (largest applicable limit wins,
   0 means unlimited - same resolution rule as storage quotas).
2. **Duplicate suppression** - a user who has already sent a "join the site"
   email to an address never sends that address another one.

Recipient addresses are stored only as one-way SHA-256 hashes of their
normalized form (see :class:`~urbanlens.dashboard.models.email_log.model.EmailSendLog`);
the recipient has not consented to having their address kept.
"""

from __future__ import annotations

import datetime
import hashlib
from typing import TYPE_CHECKING

from django.utils import timezone

from urbanlens.dashboard.models.email_log import EmailSendLog, EmailType
from urbanlens.dashboard.models.email_log.model import JOIN_EMAIL_TYPES
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.subscriptions.model import active_subscription_roles
from urbanlens.dashboard.services.email_normalization import normalize_email

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

# The rolling windows each limit applies to.
_HOUR = datetime.timedelta(hours=1)
_DAY = datetime.timedelta(days=1)
_MONTH = datetime.timedelta(days=30)


def hash_email(email: str) -> str:
    """One-way hash of an email address for storage and matching.

    Hashing operates on the normalized form (lowercased, Gmail dot/plus
    variants collapsed) so trivially distinct spellings of the same inbox
    hash identically.

    Args:
        email: Raw email address.

    Returns:
        Hex SHA-256 digest of the normalized address (64 chars).
    """
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


def get_email_limits(profile: Profile) -> tuple[int | None, int | None, int | None]:
    """Resolve the effective outbound-email limits for a profile.

    The site-wide defaults apply to everyone; active subscription roles with
    their own limits raise them (the largest applicable limit wins). A limit
    of 0 anywhere means unlimited.

    Args:
        profile: The profile whose limits to resolve.

    Returns:
        ``(per_hour, per_day, per_month)`` - each an int cap, or None when
        that window is unlimited for this user.
    """
    settings = SiteSettings.get_current()
    roles = active_subscription_roles(profile.user)

    def resolve(site_value: int, role_attr: str) -> int | None:
        values = [site_value]
        values.extend(role_value for role in roles if (role_value := getattr(role, role_attr)) is not None)
        if any(value == 0 for value in values):
            return None
        return max(values)

    return (
        resolve(settings.email_limit_per_hour, "email_limit_per_hour"),
        resolve(settings.email_limit_per_day, "email_limit_per_day"),
        resolve(settings.email_limit_per_month, "email_limit_per_month"),
    )


def email_rate_limit_error(profile: Profile) -> str | None:
    """Check whether the profile may trigger one more outbound email right now.

    Args:
        profile: The profile attempting to send.

    Returns:
        A user-facing error message when a window is exhausted, else None.
    """
    per_hour, per_day, per_month = get_email_limits(profile)
    now = timezone.now()
    logs = EmailSendLog.objects.filter(sender=profile)

    for limit, window, label in (
        (per_hour, _HOUR, "hour"),
        (per_day, _DAY, "day"),
        (per_month, _MONTH, "month"),
    ):
        if limit is None:
            continue
        if logs.filter(created__gte=now - window).count() >= limit:
            return f"You've reached your invitation email limit for this {label}. Please try again later."
    return None


def has_sent_join_email(profile: Profile, email: str) -> bool:
    """Whether this profile has ever sent a join-the-site email to this address.

    Args:
        profile: The prospective sender.
        email: Raw recipient address.

    Returns:
        True when any join-type email was already sent to the address by this
        user - a second one must not be sent.
    """
    return EmailSendLog.objects.filter(
        sender=profile,
        recipient_hash=hash_email(email),
        email_type__in=JOIN_EMAIL_TYPES,
    ).exists()


def record_email_sent(profile: Profile, email: str, email_type: EmailType | str) -> EmailSendLog:
    """Log one user-triggered outbound email (hashed recipient only).

    Args:
        profile: The profile whose action caused the send.
        email: Raw recipient address (hashed before storage, never kept).
        email_type: What kind of email was sent.

    Returns:
        The created log row.
    """
    return EmailSendLog.objects.create(
        sender=profile,
        recipient_hash=hash_email(email),
        email_type=email_type,
    )
