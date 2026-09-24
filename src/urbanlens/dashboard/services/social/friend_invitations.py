"""Friend invitations sent to an email address: delivery, the invitee's answer, and binding at signup.

The inviter's pending entry is the ``FriendInvitation`` row until the invitee accepts, whether or not the address
has an account, so nothing the inviter sees - the entry, its cancel token, its expiry, the API - depends on it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
import uuid

from django.db.models import Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from urbanlens.dashboard.models.friendship import Friendship, FriendshipStatus
from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
from urbanlens.dashboard.models.profile.model import Profile

if TYPE_CHECKING:
    from django.contrib.auth.models import User

logger = logging.getLogger(__name__)


class FriendInvitationError(ValueError):
    """The invitation cannot be answered by this account, or no longer at all."""


def invitation_path(invitation: FriendInvitation) -> str:
    """The site-relative URL of an invitation's response page."""
    return reverse("friend.invitation", kwargs={"token": invitation.token})


def open_invitations() -> Q:
    """Matches invitations still waiting on the invitee."""
    return Q(accepted_at__isnull=True, declined_at__isnull=True, expires_at__gt=timezone.now())


def invitation_for_token(token: uuid.UUID | str) -> FriendInvitation | None:
    """The unexpired invitation a token addresses."""
    try:
        token = uuid.UUID(str(token))
    except ValueError:
        return None
    return FriendInvitation.objects.filter(token=token, expires_at__gt=timezone.now()).select_related("inviter__user", "invitee").first()


def addressed_to(invitation: FriendInvitation, profile: Profile) -> bool:
    """Whether ``profile`` may answer: the bound invitee, or the account that verified the address of an unbound one.

    Holding the link is not enough; it can be forwarded.
    """
    from urbanlens.dashboard.services.auth.email_normalization import has_verified_address

    if invitation.inviter_id == profile.pk:
        return False
    if invitation.invitee_id is not None:
        return invitation.invitee_id == profile.pk
    return has_verified_address(profile.user, invitation.email)


def can_be_asked(invitation: FriendInvitation, profile: Profile) -> bool:
    """Whether the friendship can be offered to ``profile`` at all: no block, Community on, not already friends,
    and ``profile``'s friend-request setting lets the inviter ask."""
    inviter = invitation.inviter
    if not inviter.community_enabled or not profile.community_enabled:
        return False
    existing = Friendship.objects.all().between(inviter, profile)
    if existing is not None and existing.status in (FriendshipStatus.ACCEPTED, FriendshipStatus.BLOCKED):
        return False
    return Profile.visibility_permits(profile.friend_request_visibility, profile, inviter)


def is_open_for(invitation: FriendInvitation, profile: Profile) -> bool:
    """Whether ``profile`` can still accept or decline this invitation."""
    return invitation.accepted_at is None and invitation.declined_at is None and not invitation.is_expired() and addressed_to(invitation, profile) and can_be_asked(invitation, profile)


def _bind(invitation: FriendInvitation, profile: Profile) -> bool:
    """Claim an unbound invitation for ``profile``; True when it is (now) theirs."""
    if invitation.invitee_id == profile.pk:
        return True
    if invitation.invitee_id is not None:
        return False
    if FriendInvitation.objects.filter(pk=invitation.pk, invitee__isnull=True).update(invitee=profile):
        invitation.invitee = profile
        return True
    invitation.refresh_from_db(fields=["invitee"])
    return invitation.invitee_id == profile.pk


def deliver(invitation_id: int, url: str, *, send_join_email: bool) -> None:
    """Deliver a new invitation after the request: ask the account proven to own the address, or email the address.

    When the owner's settings, a block or an existing friendship mean nothing can be asked, nothing is sent and
    the invitation simply waits out its expiry, as an unanswered email invitation would.

    Args:
        invitation_id: The invitation's pk.
        url: Absolute URL of its response page, for the email.
        send_join_email: Whether the address may be emailed.
    """
    from urbanlens.dashboard.services.auth.email_normalization import find_verified_user_by_email

    invitation = FriendInvitation.objects.filter(pk=invitation_id).filter(open_invitations()).select_related("inviter__user").first()
    if invitation is None:
        return
    account = find_verified_user_by_email(invitation.email)
    if account is None:
        if send_join_email:
            send_join_invitation_email(invitation, url)
        return
    profile = account.profile
    if profile.pk == invitation.inviter_id or invitation.invitee_id is not None or not can_be_asked(invitation, profile):
        return
    if _bind(invitation, profile):
        notify_invitee(invitation)


def notify_invitee(invitation: FriendInvitation) -> None:
    """Tell the bound invitee they have an invitation to answer, honouring their preference and any mute."""
    from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.services.notifications.notification_delivery import send_notification_email
    from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identity

    invitee = invitation.invitee
    if invitee is None:
        return
    try:
        pref = invitee.notification_preferences.friend_request
    except AttributeError:
        pref = DeliveryPreference.SITE
    if pref == DeliveryPreference.NONE:
        return
    inviter_name = resolve_visible_identity(invitee, invitation.inviter)["display_name"]
    title = "New friend request"
    body = f"{inviter_name} wants to be your friend."
    if invitation.message:
        body += f' "{invitation.message}"'
    url = invitation_path(invitation)
    if pref in (DeliveryPreference.SITE, DeliveryPreference.BOTH):
        NotificationLog.objects.notify(
            profile=invitee,
            source_profile=invitation.inviter,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.FRIEND_REQUEST,
            title=title,
            message=body,
            url=url,
        )
    if pref in (DeliveryPreference.EMAIL, DeliveryPreference.BOTH):
        send_notification_email(invitee, title=title, body_text=body, url=url)


def send_join_invitation_email(invitation: FriendInvitation, url: str) -> None:
    """Email an invitation to its address, once per inviter and address; a failure leaves it sendable later.

    Args:
        invitation: The open invitation.
        url: Absolute URL of its response page.
    """
    import smtplib

    # Looked up at call time so tests patching django.core.mail.EmailMultiAlternatives intercept it.
    from django.core.mail import EmailMultiAlternatives

    from urbanlens.dashboard.services.security.email_safety import has_sent_join_email, is_reserved_address, mark_join_email_delivered

    inviter = invitation.inviter
    if is_reserved_address(invitation.email) or has_sent_join_email(inviter, invitation.email):
        return
    message = invitation.message or None
    context = {"inviter": inviter, "signup_url": url, "message": message}
    subject = f"{inviter.username} invited you to join UrbanLens"
    text_body = f"Hi,\n\n{inviter.username} invited you to be friends on UrbanLens - a private mapping platform for urban explorers and photographers."
    if message:
        text_body += f'\n\n"{message}"'
    text_body += f"\n\nRespond to the invitation:\n{url}\n\nIf you'd rather not, you can simply ignore this email.\n\n- UrbanLens"
    html_body = render_to_string("dashboard/email/friend_invite.html", context)
    try:
        email = EmailMultiAlternatives(subject=subject, body=text_body, from_email=None, to=[invitation.email])
        email.attach_alternative(html_body, "text/html")
        email.send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send friend invitation %s", invitation.pk)
        return
    mark_join_email_delivered(inviter, invitation.email)


def accept(invitation: FriendInvitation, profile: Profile) -> Friendship:
    """Become friends with the inviter.

    Raises:
        FriendInvitationError: The invitation is not open to ``profile``, or the friendship could not be made.
    """
    from urbanlens.dashboard.services.social.friendship import FriendshipActionError, accept_friend_request

    if not is_open_for(invitation, profile) or not _bind(invitation, profile):
        raise FriendInvitationError("This invitation is no longer open.")
    # mark_accepted re-checks declined_at, which a concurrent decline may have set since is_open_for read it.
    if not invitation.mark_accepted():
        raise FriendInvitationError("This invitation is no longer open.")
    inviter = invitation.inviter
    try:
        Friendship.request(from_profile=inviter, to_profile=profile, message=invitation.message)
        friendship = accept_friend_request(profile, inviter)
    except FriendshipActionError as exc:
        FriendInvitation.objects.filter(pk=invitation.pk).update(accepted_at=None)
        raise FriendInvitationError("You couldn't be connected right now.") from exc
    _redeem_grants(invitation, profile)
    return friendship


def decline(invitation: FriendInvitation, profile: Profile) -> None:
    """Decline the invitation. The inviter is not told.

    Raises:
        FriendInvitationError: ``profile`` is not the one it was addressed to.
    """
    if not addressed_to(invitation, profile) or not _bind(invitation, profile):
        raise FriendInvitationError("This invitation is no longer open.")
    FriendInvitation.objects.filter(pk=invitation.pk, accepted_at__isnull=True, declined_at__isnull=True).update(declined_at=timezone.now())
    invitation.refresh_from_db(fields=["declined_at"])


def _redeem_grants(invitation: FriendInvitation, profile: Profile) -> None:
    from urbanlens.dashboard.models.subscriptions import PendingSubscriptionGrant, grant_subscription

    for pending_grant in PendingSubscriptionGrant.objects.for_invitation(invitation):
        grant_subscription(profile.user, pending_grant.role, pending_grant.granted_by, pending_grant.duration_as_int())
        pending_grant.delete()


def bind_to_new_account(user: User, *, email: str | None = None) -> int:
    """Show an account the invitations sent to an address it just verified. Answers none of them.

    A subscription grant attached to an invitation is the inviter's gift to whoever joins, so it is redeemed
    here, once.

    Args:
        user: The account.
        email: The address just verified; defaults to the account's primary.

    Returns:
        How many invitations were bound.
    """
    from urbanlens.dashboard.services.auth.email_normalization import normalize_email

    profile, _ = Profile.objects.get_or_create(user=user)
    address = email or user.email or ""
    normalized = normalize_email(address) if address else ""
    if not normalized:
        return 0
    bound = 0
    for invitation in FriendInvitation.objects.filter(email_normalized=normalized, invitee__isnull=True).filter(open_invitations()).select_related("inviter"):
        if invitation.inviter_id == profile.pk or not _bind(invitation, profile):
            continue
        bound += 1
        _redeem_grants(invitation, profile)
        if can_be_asked(invitation, profile):
            notify_invitee(invitation)
    return bound


def invitations_for(profile: Profile) -> list[FriendInvitation]:
    """The email invitations waiting on ``profile``'s answer, for its friends page."""
    invitations = FriendInvitation.objects.filter(invitee=profile).filter(open_invitations()).select_related("inviter__user").order_by("-created")
    return [invitation for invitation in invitations if can_be_asked(invitation, profile)]
