"""Friendship state transitions, extracted from ``controllers.friendship``.
Every mutation a user can make to a friend relationship lives here as a plain ``(actor, target)`` function so the HTMX controller and the external API can share one implementation."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.urls import reverse

from urbanlens.dashboard.models.friendship import Friendship, FriendshipStatus
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.services.core.keyset_cursor import InvalidCursorError, decode_cursor, encode_cursor
from urbanlens.dashboard.services.core.text_limits import MAX_FRIEND_REQUEST_MESSAGE_LENGTH, text_length_error
from urbanlens.dashboard.services.notifications.notification_delivery import send_notification_email

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class FriendshipActionError(ValueError):
    """Raised when a friendship transition could not be applied."""


class FriendshipNotFoundError(FriendshipActionError):
    """No friendship row exists between the two profiles."""


class FriendLimitExceededError(FriendshipActionError):
    """Accepting would push one of the two profiles past ``max_friends_per_user``."""


class CommunityDisabledError(FriendshipActionError):
    """``Friendship.accept()`` refused because Community is off for one side."""


class MalformedCursorError(FriendshipActionError):
    """A :func:`list_friendships` pagination cursor didn't decode, or wasn't ours."""


class InviteValidationError(FriendshipActionError):
    """The invite payload itself was rejected - see the subclasses below."""


class MalformedEmailAddressError(InviteValidationError):
    """The submitted address failed Django's ``validate_email``."""


class SelfInviteError(InviteValidationError):
    """The (normalized) address is the inviter's own."""


class InviteMessageTooLongError(InviteValidationError):
    """The optional note exceeds ``MAX_FRIEND_REQUEST_MESSAGE_LENGTH``."""


class InviteRateLimitedError(FriendshipActionError):
    """The inviter has exhausted their outbound-email budget.
    Raised before the registered/unregistered branch is ever taken, so a capped caller cannot use the 429-vs-200 difference to probe membership."""


#: Default page size for :func:`list_friendships`.
DEFAULT_FRIEND_PAGE_SIZE = 50

#: Hard ceiling on a caller-supplied friend-list page size.
MAX_FRIEND_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class FriendshipPage:
    """One page of a profile's friend relationships plus its continuation token."""

    friendships: list[Friendship]
    next_cursor: str | None


def list_friendships(
    profile: Profile,
    *,
    status: str = FriendshipStatus.ACCEPTED,
    cursor: str | None = None,
    limit: int = DEFAULT_FRIEND_PAGE_SIZE,
) -> FriendshipPage:
    """Return one page of the relationships ``profile`` is part of.
    Only rows naming ``profile`` on one side or the other are ever considered.

    Args:
        profile: The profile whose relationships to list.
        status: The ``FriendshipStatus`` to filter to.
        cursor: Opaque continuation token from a previous page.
        limit: Page size, clamped to :data:`MAX_FRIEND_PAGE_SIZE`.

    Returns:
        The page of relationships, newest first, and the next page's cursor.

    Raises:
        MalformedCursorError: ``cursor`` is malformed or was never ours."""
    limit = min(max(int(limit or DEFAULT_FRIEND_PAGE_SIZE), 1), MAX_FRIEND_PAGE_SIZE)

    query = Friendship.objects.all().profile(profile).filter(status=status).select_related("from_profile__user", "to_profile__user")
    if cursor:
        try:
            stamp, pk = decode_cursor(cursor)
        except InvalidCursorError as exc:
            raise MalformedCursorError(f"cursor {cursor!r} for profile {profile.pk} failed to decode: {exc}") from exc
        query = query.filter(Q(created__lt=stamp) | Q(created=stamp, pk__lt=pk))

    rows = list(query.order_by("-created", "-pk")[: limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor = encode_cursor(rows[-1].created, rows[-1].pk) if has_more and rows else None
    return FriendshipPage(friendships=rows, next_cursor=next_cursor)


def notify_friend_request(from_profile: Profile, to_profile: Profile, message: str | None = None) -> None:
    """Create an in-app notification when a friend request is sent.

    Args:
        from_profile: Profile sending the request.
        to_profile: Profile receiving the request.
        message: Optional note the requester attached, appended to the notification.
    """
    try:
        pref = to_profile.notification_preferences.friend_request
    except AttributeError:
        pref = DeliveryPreference.SITE
    if pref == DeliveryPreference.NONE:
        return

    body = f"{from_profile.username} wants to be your friend."
    if message:
        body += f' "{message}"'
    url = reverse("profile.view_user", kwargs={"profile_slug": from_profile.slug or str(from_profile.uuid)})

    if pref in (DeliveryPreference.SITE, DeliveryPreference.BOTH):
        NotificationLog.objects.notify(
            profile=to_profile,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.FRIEND_REQUEST,
            title="New friend request",
            message=body,
            url=url,
            source_profile=from_profile,
        )
    if pref in (DeliveryPreference.EMAIL, DeliveryPreference.BOTH):
        send_notification_email(to_profile, title="New friend request", body_text=body, url=url)


def may_send_friend_request(actor: Profile, target: Profile) -> bool:
    """Whether ``actor`` may send ``target`` a friend request.

    One answer for every refusal (self, community off on either side, the target's ``friend_request_visibility``),
    so no caller can report which one applied: which audience someone accepts requests from is theirs to keep.

    Args:
        actor: The profile that would send the request.
        target: The profile that would receive it.

    Returns:
        True when the request may be sent.
    """
    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

    if actor.pk == target.pk or not actor.community_enabled or not target.community_enabled:
        return False
    return ProfileModel.visibility_permits(target.friend_request_visibility, target, actor)


def request_or_accept_friendship(from_profile: Profile, to_profile: Profile, message: str | None = None) -> Friendship | None:
    """Send a friend request, auto-accepting instead if one is already pending in reverse.

    Args:
        from_profile: Profile initiating this request.
        to_profile: Profile being requested.
        message: Optional note from the requester.

    Returns:
        The resulting Friendship (pending or newly accepted), or None if the request could not be created."""
    existing = Friendship.objects.all().between(from_profile, to_profile)
    if existing and existing.status == FriendshipStatus.REQUESTED and existing.from_profile_id == to_profile.pk:
        if not existing.accept():
            return None
        _notify_friend_accepted(to_profile, from_profile)
        # Mark from_profile's own pending friend_request notification (from to_profile) as read
        NotificationLog.objects.filter(
            profile=from_profile,
            notification_type=NotificationType.FRIEND_REQUEST,
            source_profile_id=to_profile.pk,
        ).update(status=Status.READ)
        return existing

    friendship = Friendship.request(from_profile=from_profile, to_profile=to_profile.pk, message=message)
    if friendship:
        notify_friend_request(from_profile, to_profile, message)
    return friendship


def _dismiss_friend_request_notifications(viewer_profile: Profile, source_profile_id: int) -> None:
    """Dismiss the viewer's friend-request notification(s) from a source.

    Args:
        viewer_profile: Profile who just acted on the request.
        source_profile_id: pk of the profile that sent the request."""
    NotificationLog.objects.filter(
        profile=viewer_profile,
        notification_type=NotificationType.FRIEND_REQUEST,
        source_profile_id=source_profile_id,
    ).mark_dismissed()


# Retained name for older imports; prefer ``_dismiss_friend_request_notifications``.
_mark_friend_request_notifications_read = _dismiss_friend_request_notifications


def _existing_friendship(actor: Profile, target: Profile) -> Friendship:
    """The friendship row between the two profiles, or raise.

    Args:
        actor: The profile performing the action.
        target: The other profile.

    Returns:
        The single Friendship row joining the pair, in either direction.

    Raises:
        FriendshipNotFoundError: No row joins the pair."""
    friendship = Friendship.objects.all().between(target, actor)
    if not friendship:
        raise FriendshipNotFoundError(f"no Friendship row joins profiles {actor.pk} and {target.pk}")
    return friendship


def _incoming_pending_request(actor: Profile, target: Profile) -> Friendship:
    """The pending request ``target`` sent ``actor``, or raise.

    Args:
        actor: The profile answering the request (the recipient).
        target: The profile that must have sent it.

    Returns:
        The pending Friendship directed from ``target`` to ``actor``.

    Raises:
        FriendshipNotFoundError: No row joins the pair, the row is not pending, or it is pending in the other direction."""
    friendship = _existing_friendship(actor, target)
    if friendship.status != FriendshipStatus.REQUESTED or friendship.from_profile_id != target.pk:
        raise FriendshipNotFoundError(
            f"friendship {friendship.pk} between {actor.pk} and {target.pk} is {friendship.status!r}, not a pending request from {target.pk}",
        )
    return friendship


def accept_friend_request(actor: Profile, target: Profile) -> Friendship:
    """Accept ``target``'s pending friend request to ``actor``.

    Args:
        actor: The profile accepting the request.
        target: The profile that sent it.

    Returns:
        The now-accepted Friendship.

    Raises:
        FriendshipNotFoundError: ``target`` has no pending request to ``actor`` - see :func:`_incoming_pending_request`.
        FriendLimitExceededError: Either profile is already at the site's ``max_friends_per_user`` limit.
        CommunityDisabledError: Either profile has Community disabled."""
    friendship = _incoming_pending_request(actor, target)

    if not friendship.accept():
        # Friendship.accept() returns a bare False for both refusal reasons;
        # re-deriving which one applies is what lets the caller dispatch on
        # exception type instead of a generic failure.
        if Friendship.profile_at_max_friends(actor) or Friendship.profile_at_max_friends(friendship.from_profile):
            raise FriendLimitExceededError(f"actor {actor.pk} or requester {friendship.from_profile_id} is at max_friends_per_user")
        raise CommunityDisabledError(f"actor {actor.pk} or requester {friendship.from_profile_id} has community_enabled=False")

    requester = friendship.from_profile if friendship.to_profile == actor else friendship.to_profile
    _notify_friend_accepted(requester, actor)
    _dismiss_friend_request_notifications(actor, target.pk)
    return friendship


def _notify_friend_accepted(requester: Profile, actor: Profile) -> None:
    """Raise the FRIEND_ACCEPTED notification for *requester*, honoring their delivery preference.
    Split out so the acceptance flow's post-notification steps (dismissing the request notification, returning the friendship) run whether or not the recipient has silenced this type.

    Args:
        requester: The profile being notified - the one who sent the original request.
        actor: The profile that just accepted it."""
    try:
        pref = requester.notification_preferences.friend_accepted
    except AttributeError:
        pref = DeliveryPreference.SITE
    if pref == DeliveryPreference.NONE:
        return

    title = "Friend request accepted"
    body = f"{actor.username} accepted your friend request."
    url = reverse("profile.view_user", kwargs={"profile_slug": actor.slug or str(actor.uuid)})

    if pref in (DeliveryPreference.SITE, DeliveryPreference.BOTH):
        NotificationLog.objects.notify(
            profile=requester,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.FRIEND_ACCEPTED,
            title=title,
            message=body,
            url=url,
            # The actor is who accepted - the same profile this notification's message and url
            # already point at.
            # Without it the external API's NotificationSerializer reports a null actor, so a mobile
            # client renders the notification with no one to link back to.
            source_profile=actor,
        )
    if pref in (DeliveryPreference.EMAIL, DeliveryPreference.BOTH):
        send_notification_email(requester, title=title, body_text=body, url=url)


def reject_friend_request(actor: Profile, target: Profile) -> Friendship:
    """Decline ``target``'s friend request, leaving them free to re-send later.

    Args:
        actor: The profile declining the request.
        target: The profile that sent it.

    Returns:
        The declined Friendship.

    Raises:
        FriendshipNotFoundError: ``target`` has no pending request to ``actor`` - see :func:`_incoming_pending_request`."""
    friendship = _incoming_pending_request(actor, target)
    friendship.decline()
    _dismiss_friend_request_notifications(actor, target.pk)
    return friendship


def ignore_friend_request(actor: Profile, target: Profile) -> Friendship:
    """Ignore ``target``'s friend request - silently, and permanently.
    Distinct from :func:`reject_friend_request` in both directions: no notification is sent, and ``FriendshipStatus.can_request`` excludes ``Ignored``, so the requester can never re-send.

    Args:
        actor: The profile ignoring the request.
        target: The profile that sent it.

    Returns:
        The ignored Friendship.

    Raises:
        FriendshipNotFoundError: ``target`` has no pending request to ``actor`` - see :func:`_incoming_pending_request`."""
    friendship = _incoming_pending_request(actor, target)
    friendship.ignore()
    _dismiss_friend_request_notifications(actor, target.pk)
    return friendship


def _placed_the_block(actor: Profile, friendship: Friendship) -> bool:
    """Whether ``actor`` is the profile that placed this block.
    ``Friendship`` carries no "blocked_by" column, so the row's *direction* is the only record of who blocked whom - which is exactly why :func:`block_profile` normalizes it (see that function).

    Args:
        actor: The profile attempting to act on the block.
        friendship: The relationship row, expected to be ``BLOCKED``.

    Returns:
        True when ``actor`` owns the block and may therefore lift it."""
    return friendship.from_profile_id == actor.pk


def remove_friend(actor: Profile, target: Profile) -> Friendship:
    """End an existing friendship.
    The row is retained at ``Removed`` rather than deleted, which is what lets ``FriendshipStatus.can_request`` allow a later re-request and what ``QuerySet.ever_friends`` reads.

    Args:
        actor: The profile removing the friend.
        target: The profile being removed.

    Returns:
        The removed Friendship.

    Raises:
        FriendshipNotFoundError: No friendship exists between the pair, or the pair is blocked and ``actor`` is not the one who blocked."""
    friendship = _existing_friendship(actor, target)
    if friendship.status == FriendshipStatus.BLOCKED and not _placed_the_block(actor, friendship):
        raise FriendshipNotFoundError(f"friendship {friendship.pk} is BLOCKED and actor {actor.pk} did not place the block")
    friendship.remove()
    return friendship


def block_profile(actor: Profile, target: Profile) -> Friendship:
    """Block ``target``, creating the relationship row if none exists yet.
    Unlike every other transition here, blocking must work against a complete stranger - that is the case it exists for - so a missing row is created rather than raising.

    Args:
        actor: The profile doing the blocking.
        target: The profile being blocked.

    Returns:
        The blocked Friendship, with ``actor`` as ``from_profile``."""
    _revoke_safety_partner_access(actor, target)
    _withdraw_pending_pin_shares(actor, target)
    _revoke_map_shares(actor, target)

    friendship = Friendship.objects.all().between(target, actor)
    if friendship:
        # The mute columns are named for the row's two *ends*, so swapping the ends without swapping
        # them hands each person the other's preference - A's mute of B silently becomes B's mute of
        # A, and neither of them did it.
        # Read before the swap, written with it, in one statement.
        muted_by_actor = friendship.is_muted_by(actor)
        muted_by_target = friendship.is_muted_by(target)
        friendship.from_profile = actor
        friendship.to_profile = target
        friendship.status = FriendshipStatus.BLOCKED
        friendship.muted_by_from_profile = muted_by_actor
        friendship.muted_by_to_profile = muted_by_target
        friendship.save(update_fields=["from_profile", "to_profile", "status", "muted_by_from_profile", "muted_by_to_profile", "updated"])
        return friendship
    try:
        # A savepoint, for the same reason `Friendship.request` has one: since
        # `friendship_one_row_per_pair`, a row inserted for the reverse direction between the
        # `between()` above and this line makes the insert fail - and a failed insert makes the
        # whole transaction unusable, so without this the re-read below could not run either.
        with transaction.atomic():
            return Friendship.objects.create(
                from_profile=actor,
                to_profile=target,
                status=FriendshipStatus.BLOCKED,
            )
    except IntegrityError:
        logger.info("Block by %s of %s raced another write; driving the existing row to BLOCKED", actor.pk, target.pk)
        friendship = Friendship.objects.all().between(actor, target)
        if friendship is None:
            raise
        # Same normalisation as the found-row branch above: `from_profile` is
        # the only record of who blocked whom, so the actor has to end up there.
        muted_by_actor = friendship.is_muted_by(actor)
        muted_by_target = friendship.is_muted_by(target)
        friendship.from_profile = actor
        friendship.to_profile = target
        friendship.status = FriendshipStatus.BLOCKED
        friendship.muted_by_from_profile = muted_by_actor
        friendship.muted_by_to_profile = muted_by_target
        friendship.save(update_fields=["from_profile", "to_profile", "status", "muted_by_from_profile", "muted_by_to_profile", "updated"])
        return friendship


def _revoke_safety_partner_access(actor: Profile, target: Profile) -> None:
    """End any safety-partner relationship between two profiles being blocked apart.
    An accepted partner watches the owner's live location, check-in chat and escalation status, so a block that left those rows in place would be the weakest thing in the app rather than the strongest.

    Args:
        actor: The profile doing the blocking.
        target: The profile being blocked."""
    from urbanlens.dashboard.models.safety.model import SafetyCheckinPartner
    from urbanlens.dashboard.services.visits.safety import remove_checkin_partner

    partners = SafetyCheckinPartner.objects.filter(
        Q(checkin__profile=actor, profile=target) | Q(checkin__profile=target, profile=actor),
    ).select_related("checkin")
    for partner in partners:
        remove_checkin_partner(partner)


def _revoke_map_shares(actor: Profile, target: Profile) -> None:
    """Delete any standalone map share between two profiles being blocked apart.
    A ``MarkupMapShare`` is live access, not a copy: it has no accept/reject step, and ``controllers.markup._map_visible_to`` honours it every time the recipient opens the map, so they keep seeing the owner's *current* map and can still clone it.

    Args:
        actor: The profile doing the blocking.
        target: The profile being blocked."""
    from urbanlens.dashboard.models.markup.share import MarkupMapShare

    MarkupMapShare.objects.filter(
        Q(from_profile=actor, to_profile=target) | Q(from_profile=target, to_profile=actor),
    ).delete()


def _withdraw_pending_pin_shares(actor: Profile, target: Profile) -> None:
    """Reject any still-pending pin share between two profiles being blocked apart.
    A pending share is a standing offer, and the accept path does not re-check blocking - so without this a blocked profile could accept afterwards and end up owning a copy of a place the blocker had just withdrawn from them.

    Args:
        actor: The profile doing the blocking.
        target: The profile being blocked."""
    from urbanlens.dashboard.models.pin_share.meta import PinShareStatus
    from urbanlens.dashboard.models.pin_share.model import PinShare

    PinShare.objects.filter(
        Q(from_profile=actor, to_profile=target) | Q(from_profile=target, to_profile=actor),
        status=PinShareStatus.PENDING,
    ).update(status=PinShareStatus.REJECTED)


def unblock_profile(actor: Profile, target: Profile) -> Friendship:
    """Lift a block ``actor`` placed on ``target``.
    The inverse :func:`block_profile` never had.

    Args:
        actor: The profile lifting its own block.
        target: The profile being unblocked.

    Returns:
        The now-``Removed`` Friendship.

    Raises:
        FriendshipNotFoundError: No row joins the pair, the row is not blocked, or the block belongs to ``target`` rather than ``actor``."""
    friendship = Friendship.objects.all().between(target, actor)
    if friendship is None or friendship.status != FriendshipStatus.BLOCKED or not _placed_the_block(actor, friendship):
        raise FriendshipNotFoundError(f"no block placed by {actor.pk} on {target.pk} to lift")
    friendship.remove()
    return friendship


def mute_profile(actor: Profile, target: Profile) -> Friendship:
    """Mute an existing relationship with ``target``, without altering it.

    Args:
        actor: The profile doing the muting.
        target: The profile being muted.

    Returns:
        The muted Friendship.

    Raises:
        FriendshipNotFoundError: No relationship exists between the pair."""
    friendship = _existing_friendship(actor, target)
    friendship.mute(actor)
    return friendship


def unmute_profile(actor: Profile, target: Profile) -> Friendship:
    """Un-mute an existing relationship with ``target``.
    Mute is a boolean flag rather than part of the relationship's status enum, so unmuting is a single boolean write and the relationship underneath is untouched throughout.

    Args:
        actor: The profile doing the unmuting.
        target: The profile being unmuted.

    Returns:
        The unmuted Friendship.

    Raises:
        FriendshipNotFoundError: No relationship exists between the pair - deliberately the same failure as muting a stranger, so the two halves of the toggle answer identically."""
    friendship = _existing_friendship(actor, target)
    friendship.unmute(actor)
    return friendship


def notifications_muted(recipient: Profile | int | None, source: Profile | int | None) -> bool:
    """Whether ``recipient`` has muted the relationship notifications from ``source`` travel on.

    Args:
        recipient: The profile the notification is addressed to, or its pk.
        source: The profile the notification is *about* - a sharer, a commenter, a requester - or its pk.

    Returns:
        True when a relationship joins the two and the recipient muted their own side of it.

    Note:
        Asks the same predicate as :func:`profiles_muting` rather than reading the row through ``between()``."""
    if recipient is None or source is None:
        return False
    recipient_id = recipient if isinstance(recipient, int) else recipient.pk
    source_id = source if isinstance(source, int) else source.pk
    if recipient_id is None or source_id is None or recipient_id == source_id:
        return False

    return Friendship.objects.filter(
        Q(from_profile_id=source_id, to_profile_id=recipient_id, muted_by_to_profile=True) | Q(to_profile_id=source_id, from_profile_id=recipient_id, muted_by_from_profile=True),
    ).exists()


@dataclass(frozen=True, slots=True)
class MutedRecipients:
    """A batched mute answer, carrying the source it was computed for.

    Attributes:
        source_id: The profile the mutes were resolved against.
        profile_ids: The recipients who muted them."""

    source_id: int | None
    profile_ids: frozenset[int]


def profiles_muting(source: Profile | int, recipient_ids: Iterable[int]) -> MutedRecipients:
    """Which of ``recipient_ids`` have muted notifications from ``source``, in one query.

    Args:
        source: The profile the notifications are about, or its pk.
        recipient_ids: The pks of the profiles being notified.

    Returns:
        The subset of ``recipient_ids`` that muted their side of a relationship with ``source``, tagged with that source."""
    source_id = source if isinstance(source, int) else source.pk
    ids = {pk for pk in recipient_ids if pk is not None and pk != source_id}
    if source_id is None or not ids:
        return MutedRecipients(source_id=source_id, profile_ids=frozenset())

    rows = Friendship.objects.filter(
        Q(from_profile_id=source_id, to_profile_id__in=ids, muted_by_to_profile=True) | Q(to_profile_id=source_id, from_profile_id__in=ids, muted_by_from_profile=True),
    ).values_list("from_profile_id", "to_profile_id")
    return MutedRecipients(source_id=source_id, profile_ids=frozenset(from_id if from_id != source_id else to_id for from_id, to_id in rows))


def invite_by_email(
    inviter: Profile,
    email: str,
    message: str | None = None,
    *,
    url_builder: Callable[[str], str],
    subscription_role: Any = None,
    subscription_duration: str = "",
) -> FriendInvitation:
    """Invite someone to connect by email address, revealing nothing about them.

    The request does the same work for every address: it charges the email budget on first contact and records
    a ``FriendInvitation``, which stays the inviter's pending entry until the invitee accepts. A task then asks
    the account proven to own the address, or emails the address (see ``services.social.friend_invitations``).

    Args:
        inviter: The profile sending the invitation.
        email: The raw address submitted by the caller.
        message: Optional note to include, bounded by ``MAX_FRIEND_REQUEST_MESSAGE_LENGTH``.
        url_builder: Builds an absolute URL from a site-relative path.
        subscription_role: Optional ``SubscriptionRole`` to grant whoever accepts or signs up.
        subscription_duration: Raw duration string paired with ``subscription_role``; ignored without one.

    Returns:
        The inviter's open invitation for the address.

    Raises:
        MalformedEmailAddressError: The address failed validation.
        SelfInviteError: The address is the inviter's own.
        InviteMessageTooLongError: The optional message exceeds ``MAX_FRIEND_REQUEST_MESSAGE_LENGTH``.
        InviteRateLimitedError: The inviter is over their email budget."""
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    from urbanlens.dashboard.models.email_log import EmailType
    from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
    from urbanlens.dashboard.services.auth.email_normalization import normalize_email, own_addresses
    from urbanlens.dashboard.services.security.email_safety import (
        email_rate_limit_error,
        has_charged_join_email,
        has_sent_join_email,
        is_reserved_address,
        record_email_sent,
        release_email_reservation,
    )
    from urbanlens.dashboard.services.social.friend_invitations import invitation_path, open_invitations

    email = (email or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError as exc:
        raise MalformedEmailAddressError("Submitted address failed Django's validate_email().") from exc

    if normalize_email(email) in own_addresses(inviter.user):
        raise SelfInviteError("Normalized invite address matches one of the inviter's own addresses.")

    message = (message or "").strip()
    length_error = text_length_error(message, MAX_FRIEND_REQUEST_MESSAGE_LENGTH, "Message")
    if length_error:
        raise InviteMessageTooLongError(length_error)

    # An address under a reserved domain can hold no mailbox, so it is not charged; that depends only on
    # what was typed.
    charged = not is_reserved_address(email)
    rate_limit_error = email_rate_limit_error(inviter) if charged else None
    if rate_limit_error:
        raise InviteRateLimitedError(f"inviter {inviter.pk} is over their outbound-email budget: {rate_limit_error}")
    if charged:
        # Charged on first contact whether or not the address has an account, so how the budget recovers
        # cannot tell the two apart. Marked delivered only once a join email actually goes out.
        if not has_charged_join_email(inviter, email):
            record_email_sent(inviter, email, EmailType.JOIN_INVITE, delivered=False)
        release_email_reservation(inviter)

    normalized = normalize_email(email)
    # Re-inviting keeps the invitation an invitee may already be looking at, and only retries its delivery.
    invitation = FriendInvitation.objects.filter(inviter=inviter, email_normalized=normalized).filter(open_invitations()).first()
    if invitation is None:
        FriendInvitation.objects.filter(inviter=inviter, email_normalized=normalized, accepted_at__isnull=True).delete()
        invitation = FriendInvitation.objects.create(inviter=inviter, email=email, message=message or None)
    if subscription_role is not None and not invitation.pending_subscription_grants.exists():
        from urbanlens.dashboard.models.subscriptions import PendingSubscriptionGrant

        PendingSubscriptionGrant.objects.create(
            invitation=invitation,
            role=subscription_role,
            granted_by=inviter.user,
            duration_months="" if subscription_duration == "indefinite" else subscription_duration,
        )

    url = url_builder(invitation_path(invitation))
    send_join_email = charged and not has_sent_join_email(inviter, email)
    transaction.on_commit(lambda: _queue_friend_invitation(invitation.pk, url, send_join_email=send_join_email))
    return invitation


def _queue_friend_invitation(invitation_id: int, url: str, *, send_join_email: bool) -> None:
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import deliver_friend_invitation

    safely_enqueue_task(deliver_friend_invitation, invitation_id, url, send_join_email)
