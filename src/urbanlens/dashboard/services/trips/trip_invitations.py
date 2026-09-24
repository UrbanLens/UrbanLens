"""Inviting people to a trip by email address.

The inviter sees the same outcome whether or not the address belongs to an account: a pending invitation
listed under the address they typed, and nothing else until the invitee answers. The invitee answers two
questions independently - join the trip, become friends with the inviter - and registering answers neither.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.urls import reverse

from urbanlens.dashboard.models.email_log import EmailType
from urbanlens.dashboard.models.friendship import Friendship, FriendshipStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.trips.invitation import TripInvitation, TripInvitationResponse
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.auth.email_normalization import find_verified_user_by_email, normalize_email
from urbanlens.dashboard.services.security.email_safety import email_rate_limit_error, hash_email, is_reserved_address, record_email_sent, release_email_reservation
from urbanlens.dashboard.services.trips.trip_access import can_perform, require_perform
from urbanlens.dashboard.services.trips.trip_errors import TripNotFoundError, TripQuotaError, TripRateLimitError, TripValidationError

if TYPE_CHECKING:
    from collections.abc import Callable
    import uuid

    from django.contrib.auth.models import User

logger = logging.getLogger(__name__)

ADD_MEMBER_DENIED = "You don't have permission to add members to this trip."
INVALID_ADDRESS = "Enter a valid email address."
OWN_ADDRESS = "That's your own email address."
INVITATION_NOT_FOUND = "This invitation doesn't exist or has expired."
INVITATION_WITHDRAWN = "Whoever invited you can no longer add people to this trip."
TRIP_FULL = "This trip is full ({max_members} members maximum)."

#: The most addresses one request may invite.
MAX_ADDRESSES_PER_REQUEST = 50


def parse_address_list(raw: object) -> list[str]:
    """Split a comma, semicolon or whitespace separated list of addresses, dropping blanks and repeats.

    Args:
        raw: A string, a list of strings, or nothing.

    Returns:
        Lowercased addresses in the order given, at most :data:`MAX_ADDRESSES_PER_REQUEST`.
    """
    import re

    parts = raw if isinstance(raw, list) else re.split(r"[\s,;]+", raw if isinstance(raw, str) else "")
    addresses = [str(part).strip().lower() for part in parts if str(part).strip()]
    return list(dict.fromkeys(addresses))[:MAX_ADDRESSES_PER_REQUEST]


def is_valid_address(address: str) -> bool:
    """Whether Django's validator accepts an address."""
    try:
        validate_email(address)
    except ValidationError:
        return False
    return True


def invitation_path(invitation: TripInvitation) -> str:
    """The site-relative URL of an invitation's response page."""
    return reverse("trips.invitation", kwargs={"token": invitation.token})


def invite_to_trip_by_email(trip: Trip, actor: Profile, email: str, *, invitation_url_builder: Callable[[str], str]) -> TripInvitation:
    """Invite an email address to a trip without revealing whether it belongs to an account.

    No refusal depends on whether the address belongs to someone else's account, and delivery - a
    notification for an account, an email otherwise - runs in a task after the request, so neither the
    response nor its latency tells the two apart. Inviting the same address to the same trip again does
    nothing.

    Args:
        trip: The trip to invite to.
        actor: The inviting profile.
        email: The address as typed.
        invitation_url_builder: Builds an absolute URL from a site-relative path, for the email.

    Returns:
        The invitation, new or existing.

    Raises:
        TripPermissionError: The actor may not add members.
        TripValidationError: The address is malformed or the actor's own.
        TripQuotaError: Members plus open invitations already fill the trip.
        TripRateLimitError: The actor is over their outbound-email budget.
    """
    require_perform(actor, trip, trip.allow_add_members, ADD_MEMBER_DENIED)

    address = (email or "").strip().lower()
    try:
        validate_email(address)
    except ValidationError as exc:
        raise TripValidationError(INVALID_ADDRESS) from exc
    if normalize_email(address) in _own_addresses(actor):
        raise TripValidationError(OWN_ADDRESS)

    email_hash = hash_email(address)
    existing = TripInvitation.objects.filter(trip=trip, inviter=actor, email_hash=email_hash).first()
    if existing is not None:
        if existing.trip_response != TripInvitationResponse.PENDING or not existing.is_expired():
            return existing
        existing.delete()

    max_members = SiteSettings.get_current().max_trip_members
    # An invitee who joined through another member's invitation is already counted as a member.
    open_addresses = TripInvitation.objects.filter(trip=trip).open().exclude(invitee__in=trip.profiles.all()).values("email_hash").distinct().count()
    if trip.profiles.count() + open_addresses >= max_members:
        raise TripQuotaError(TRIP_FULL.format(max_members=max_members))

    # The budget protects mailboxes, so an address that can have none is not charged. That depends only on
    # what was typed, never on whether it belongs to an account.
    charged = not is_reserved_address(address)
    rate_limit_error = email_rate_limit_error(actor) if charged else None
    if rate_limit_error:
        raise TripRateLimitError(rate_limit_error)

    try:
        with transaction.atomic():
            invitation = TripInvitation.objects.create(trip=trip, inviter=actor, email=address, email_hash=email_hash)
    except IntegrityError:
        return TripInvitation.objects.get(trip=trip, inviter=actor, email_hash=email_hash)

    if charged:
        # Charged whether or not the address has an account, so budget recovery cannot tell them apart.
        record_email_sent(actor, address, EmailType.TRIP_INVITE)
        release_email_reservation(actor)
    url = invitation_url_builder(invitation_path(invitation))
    transaction.on_commit(lambda: _queue_delivery(invitation.pk, url))
    return invitation


def _own_addresses(profile: Profile) -> set[str]:
    """Every address the profile has, normalized - read from its own rows, never looked up across accounts."""
    from urbanlens.dashboard.models.profile.email import ProfileEmail

    own = set(ProfileEmail.objects.filter(profile=profile).values_list("normalized_email", flat=True))
    own.add(normalize_email(profile.email or ""))
    return own


def _queue_delivery(invitation_id: int, url: str) -> None:
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import deliver_trip_invitation

    safely_enqueue_task(deliver_trip_invitation, invitation_id, url)


def deliver_invitation(invitation: TripInvitation, url: str) -> None:
    """Deliver a new invitation: a notification to the account proven to own its address, or an email to the address.

    An account whose primary address is unverified is not trusted with the invitation; the mailbox gets it.

    Args:
        invitation: The invitation.
        url: Absolute URL of its response page, for the email.
    """
    account = find_verified_user_by_email(invitation.email) if invitation.email else None
    if account is not None:
        if invitation.invitee_id is None and account.pk != invitation.inviter.user_id:
            TripInvitation.objects.filter(pk=invitation.pk, invitee__isnull=True).update(invitee=account.profile)
            invitation.invitee = account.profile
            _notify_invitee(invitation)
        return
    send_invitation_email(invitation, url)


def send_invitation_email(invitation: TripInvitation, url: str) -> bool:
    """Email an invitation to its unregistered address.

    Args:
        invitation: The invitation to send.
        url: Absolute URL of its response page.

    Returns:
        True when the email went out.
    """
    import smtplib

    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string

    address = invitation.email
    if not address or not invitation.is_open():
        return False
    inviter = invitation.inviter
    trip = invitation.trip
    subject = f'{inviter.username} invited you to "{trip.name}" on UrbanLens'
    text_body = (
        f'Hi,\n\n{inviter.username} invited you to join their trip "{trip.name}" on UrbanLens - a private mapping platform for urban '
        f"explorers and photographers.\n\nJoining the trip, becoming friends with {inviter.username} and creating an account are separate "
        f"choices. If you'd rather not, you can simply ignore this email.\n\nRespond to the invitation:\n{url}\n\n- UrbanLens"
    )
    html_body = render_to_string("dashboard/email/trip_invite.html", {"inviter": inviter, "trip": trip, "invitation_url": url})

    if is_reserved_address(address):
        return False
    try:
        message = EmailMultiAlternatives(subject=subject, body=text_body, from_email=None, to=[address])
        message.attach_alternative(html_body, "text/html")
        message.send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send trip invitation %s", invitation.pk)
        return False
    return True


def _notify_invitee(invitation: TripInvitation) -> None:
    """Tell a registered invitee about the invitation, unless a block or their preferences say otherwise."""
    from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.services.notifications.notification_delivery import send_notification_email
    from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identity

    invitee = invitation.invitee
    if invitee is None or not invitation.is_open():
        return
    inviter = invitation.inviter
    trip = invitation.trip
    if Profile.are_blocked(inviter, invitee) or TripMembership.objects.filter(trip=trip, profile=invitee).exists():
        return

    try:
        pref = invitee.notification_preferences.added_to_trip
    except AttributeError:
        pref = DeliveryPreference.SITE
    if pref == DeliveryPreference.NONE:
        return
    inviter_name = resolve_visible_identity(invitee, inviter)["display_name"]
    title = "Trip invitation"
    body = f'{inviter_name} invited you to join "{trip.name}".'
    url = invitation_path(invitation)
    if pref in (DeliveryPreference.SITE, DeliveryPreference.BOTH):
        NotificationLog.objects.notify(
            profile=invitee,
            source_profile=inviter,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.ADDED_TO_TRIP,
            title=title,
            message=body,
            url=url,
        )
    if pref in (DeliveryPreference.EMAIL, DeliveryPreference.BOTH):
        send_notification_email(invitee, title=title, body_text=body, url=url)


def invitations_visible_to(trip: Trip, viewer: Profile) -> list[TripInvitation]:
    """The email invitations a viewer may see and withdraw: their own, or every one when they created the trip.

    Listed while either question is unanswered, so an inviter can still withdraw a friendship offer after the
    trip question is settled.

    Args:
        trip: The trip.
        viewer: The viewing profile.

    Returns:
        Withdrawable invitations, oldest first, each carrying only what its inviter typed.
    """
    invitations = TripInvitation.objects.filter(trip=trip).withdrawable().select_related("inviter__user")
    if trip.creator_id != viewer.pk:
        invitations = invitations.filter(inviter=viewer)
    return list(invitations.order_by("created", "pk"))


def cancel_invitation(trip: Trip, actor: Profile, invitation_uuid: uuid.UUID | str) -> None:
    """Withdraw an invitation: the actor's own, or any on a trip the actor created.

    Raises:
        TripNotFoundError: No such withdrawable invitation is the actor's to withdraw.
    """
    invitations = TripInvitation.objects.filter(trip=trip, uuid=invitation_uuid).withdrawable()
    if trip.creator_id != actor.pk:
        invitations = invitations.filter(inviter=actor)
    deleted, _ = invitations.delete()
    if not deleted:
        raise TripNotFoundError(INVITATION_NOT_FOUND)


def invitation_for_token(token: uuid.UUID | str) -> TripInvitation:
    """The invitation a token addresses, while any of its questions is open.

    Raises:
        TripNotFoundError: No such invitation, or it has expired.
    """
    invitation = TripInvitation.objects.filter(token=token).select_related("trip", "inviter__user", "invitee").first()
    if invitation is None or invitation.is_expired():
        raise TripNotFoundError(INVITATION_NOT_FOUND)
    return invitation


def _claim(invitation: TripInvitation, profile: Profile) -> None:
    """Bind an unbound invitation to the account answering it.

    Raises:
        TripNotFoundError: The invitation belongs to another account, or a block separates the two.
    """
    if not invitation.addressed_to(profile) or Profile.are_blocked(invitation.inviter, profile):
        raise TripNotFoundError(INVITATION_NOT_FOUND)
    if invitation.invitee_id is None:
        claimed = TripInvitation.objects.filter(pk=invitation.pk, invitee__isnull=True).update(invitee=profile)
        if not claimed:
            invitation.refresh_from_db(fields=["invitee"])
            if invitation.invitee_id != profile.pk:
                raise TripNotFoundError(INVITATION_NOT_FOUND)
        invitation.invitee = profile


def respond_to_trip(invitation: TripInvitation, profile: Profile, *, accept: bool) -> None:
    """Join the trip, or decline it. Says nothing about friendship.

    Args:
        invitation: The invitation being answered.
        profile: The answering account.
        accept: True to join.

    Raises:
        TripNotFoundError: The invitation is not this account's to answer.
        TripValidationError: The trip question was already answered.
        TripQuotaError: The trip filled up in the meantime.
    """
    _claim(invitation, profile)
    if invitation.trip_response != TripInvitationResponse.PENDING:
        raise TripValidationError("You've already answered this invitation.")

    trip = invitation.trip
    if accept:
        if not can_perform(invitation.inviter, trip, trip.allow_add_members):
            raise TripValidationError(INVITATION_WITHDRAWN)
        already_member = TripMembership.objects.filter(trip=trip, profile=profile).exists()
        max_members = SiteSettings.get_current().max_trip_members
        if not already_member and trip.creator_id != profile.pk and trip.profiles.count() >= max_members:
            raise TripQuotaError(TRIP_FULL.format(max_members=max_members))

    response = TripInvitationResponse.ACCEPTED if accept else TripInvitationResponse.DECLINED
    answered = TripInvitation.objects.filter(pk=invitation.pk, trip_response=TripInvitationResponse.PENDING).update(trip_response=response)
    if not answered:
        raise TripValidationError("You've already answered this invitation.")
    invitation.trip_response = response
    if not accept:
        return

    from urbanlens.dashboard.services.trips.trip_membership import join_trip, suggest_connections_for_new_member

    _membership, created = TripMembership.objects.get_or_create(trip=trip, profile=profile, defaults={"status": TripMembership.STATUS_INVITED})
    join_trip(trip, profile)
    if created:
        suggest_connections_for_new_member(profile, trip.profiles.exclude(pk=profile.pk))


def friendship_offer_open(invitation: TripInvitation, profile: Profile) -> bool:
    """Whether the invitation's friendship question can be put to ``profile``.

    Closed once answered, when the two are already friends or blocked, when either has Community off, and
    when ``profile``'s friend-request setting would not let the inviter ask.
    """
    if invitation.friend_response != TripInvitationResponse.PENDING or not invitation.addressed_to(profile):
        return False
    inviter = invitation.inviter
    if inviter.pk == profile.pk or not inviter.community_enabled or not profile.community_enabled:
        return False
    existing = Friendship.objects.all().between(inviter, profile)
    if existing is not None and existing.status in (FriendshipStatus.ACCEPTED, FriendshipStatus.BLOCKED):
        return False
    return Profile.visibility_permits(profile.friend_request_visibility, profile, inviter)


def respond_to_friendship(invitation: TripInvitation, profile: Profile, *, accept: bool) -> None:
    """Become friends with the inviter, or decline. Says nothing about the trip.

    Args:
        invitation: The invitation being answered.
        profile: The answering account.
        accept: True to become friends.

    Raises:
        TripNotFoundError: The invitation is not this account's to answer.
        TripValidationError: The friendship question is closed, or the friendship could not be made.
    """
    _claim(invitation, profile)
    if not friendship_offer_open(invitation, profile):
        raise TripValidationError("That friend request is no longer open.")

    response = TripInvitationResponse.ACCEPTED if accept else TripInvitationResponse.DECLINED
    # Claimed before the friendship is made, so an answer that committed since friendship_offer_open read it wins.
    if not TripInvitation.objects.filter(pk=invitation.pk, friend_response=TripInvitationResponse.PENDING).update(friend_response=response):
        raise TripValidationError("That friend request is no longer open.")
    invitation.friend_response = response
    if not accept:
        return

    from urbanlens.dashboard.services.social.friendship import FriendshipActionError, accept_friend_request

    inviter = invitation.inviter
    try:
        Friendship.request(from_profile=inviter, to_profile=profile)
        accept_friend_request(profile, inviter)
    except FriendshipActionError as exc:
        TripInvitation.objects.filter(pk=invitation.pk).update(friend_response=TripInvitationResponse.PENDING)
        invitation.friend_response = TripInvitationResponse.PENDING
        raise TripValidationError("You couldn't be connected right now.") from exc


def bind_invitations_to_account(user: User, *, email: str | None = None) -> int:
    """Tie open invitations to an account whose address was just verified, and notify it.

    Nothing is joined or accepted: the account answers each invitation itself.

    Args:
        user: The account.
        email: The address just verified; defaults to the account's primary email.

    Returns:
        How many invitations were bound.
    """
    profile, _ = Profile.objects.get_or_create(user=user)
    address = (email or user.email or "").strip()
    if not address:
        return 0
    query = TripInvitation.objects.for_address(hash_email(address))
    bound = 0
    for invitation in query.unbound().open().select_related("trip", "inviter"):
        if invitation.inviter_id == profile.pk:
            continue
        if TripInvitation.objects.filter(pk=invitation.pk, invitee__isnull=True).update(invitee=profile):
            invitation.invitee = profile
            bound += 1
            _notify_invitee(invitation)
    return bound
