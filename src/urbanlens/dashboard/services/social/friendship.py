"""Friendship state transitions, extracted from ``controllers.friendship``.

Every mutation a user can make to a friend relationship lives here as a plain
``(actor, target)`` function so the HTMX controller and the external API can
share one implementation. Before this split the transitions only existed as
``FriendController`` methods that read ``request.POST`` and returned rendered
HTML, which an API-key caller cannot use.

The functions raise :class:`FriendshipActionError` subclasses rather than
returning status codes, so each caller maps failures onto its own protocol
(``HttpResponse`` for the web controller, ``{"error": ...}`` + status for the
external API). The distinction between :class:`FriendshipNotFoundError`,
:class:`FriendLimitExceededError` and the plain base matters: they become 404,
403 and 400 respectively, and collapsing them would tell a caller "bad
request" when the real answer is "you are at the friend limit".

``invite_by_email`` is the security-sensitive one - see its docstring for the
anti-enumeration guarantee it must preserve.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from django.db.models import Q
from django.template.loader import render_to_string
from django.urls import reverse

from urbanlens.dashboard.models.friendship import Friendship, FriendshipStatus
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.core.keyset_cursor import InvalidCursorError, decode_cursor, encode_cursor
from urbanlens.dashboard.services.core.text_limits import MAX_FRIEND_REQUEST_MESSAGE_LENGTH, text_length_error

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class FriendshipActionError(ValueError):
    """A friendship transition could not be applied.

    ``safe_message`` is written for the end user and is safe to surface
    directly. Callers map this to HTTP 400 unless a more specific subclass
    applies.
    """

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


class FriendshipNotFoundError(FriendshipActionError):
    """No friendship row exists between the two profiles.

    Deliberately also raised when a row exists but the actor has no standing
    to act on it, so "not yours" and "not there" are indistinguishable.
    Callers map this to HTTP 404.
    """

    def __init__(self, message: str = "Friend request not found.") -> None:
        """Initialize with a caller-safe default message.

        Args:
            message: Human-readable detail to surface.
        """
        super().__init__(message)


class FriendLimitExceededError(FriendshipActionError):
    """Accepting would push one of the two profiles past ``max_friends_per_user``.

    Kept distinct from the base error so the caller can answer 403 (the
    request was understood and refused) rather than 400 (malformed).
    """

    def __init__(self, message: str = "This would exceed the maximum number of friends allowed.") -> None:
        """Initialize with a caller-safe default message.

        Args:
            message: Human-readable detail to surface.
        """
        super().__init__(message)


class InviteValidationError(FriendshipActionError):
    """The invite payload itself was rejected - bad address, own address, or over-long message.

    This is the *only* class of invite failure a caller may distinguish, since
    it depends solely on what the caller submitted and reveals nothing about
    who is registered. Callers map this to HTTP 400.
    """


class InviteRateLimitedError(FriendshipActionError):
    """The inviter has exhausted their outbound-email budget.

    Raised before the registered/unregistered branch is ever taken, so a
    capped caller cannot use the 429-vs-200 difference to probe membership.
    Callers map this to HTTP 429.
    """


#: The single message every :func:`unblock_profile` refusal carries. It is
#: deliberately the *profile lookup* wording rather than anything about blocks:
#: callers already answer 404 with this text for a uuid that names nobody, so
#: reusing it makes "there is no such person", "there is no block", and "the
#: block is not yours" one indistinguishable answer. A caller must not be able
#: to confirm a block exists by the shape of the failure to lift it.
UNBLOCK_NOT_FOUND_MESSAGE = "No such profile."

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
    Defaults to accepted friendships; pass ``FriendshipStatus.REQUESTED`` for
    the pending queue. Both sides' profiles (and their users) are selected
    eagerly, because the caller resolves display identity per row and would
    otherwise issue two queries per friend.

    Args:
        profile: The profile whose relationships to list.
        status: The ``FriendshipStatus`` to filter to.
        cursor: Opaque continuation token from a previous page.
        limit: Page size, clamped to :data:`MAX_FRIEND_PAGE_SIZE`.

    Returns:
        The page of relationships, newest first, and the next page's cursor.

    Raises:
        FriendshipActionError: ``cursor`` is malformed or was never ours.
    """
    limit = min(max(int(limit or DEFAULT_FRIEND_PAGE_SIZE), 1), MAX_FRIEND_PAGE_SIZE)

    query = Friendship.objects.all().profile(profile).filter(status=status).select_related("from_profile__user", "to_profile__user")
    if cursor:
        try:
            stamp, pk = decode_cursor(cursor)
        except InvalidCursorError as exc:
            raise FriendshipActionError("Invalid cursor.") from exc
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

    NotificationLog.objects.create(
        profile=to_profile,
        status=Status.UNREAD,
        importance=Importance.MEDIUM,
        notification_type=NotificationType.FRIEND_REQUEST,
        title="New friend request",
        message=body,
        url=reverse("profile.view_user", kwargs={"profile_slug": from_profile.slug or str(from_profile.uuid)}),
        source_profile=from_profile,
    )


def request_or_accept_friendship(from_profile: Profile, to_profile: Profile, message: str | None = None) -> Friendship | None:
    """Send a friend request, auto-accepting instead if one is already pending in reverse.

    If `to_profile` already sent `from_profile` a pending request, the two profiles
    clearly want to be friends - accept that request instead of creating a redundant
    crossed request and a duplicate "new friend request" notification.

    Args:
        from_profile: Profile initiating this request.
        to_profile: Profile being requested.
        message: Optional note from the requester. Ignored when this call
            resolves to an auto-accept (both profiles already wanted to be
            friends, so there's no pending request left to attach a note to).

    Returns:
        The resulting Friendship (pending or newly accepted), or None if the request
        could not be created.
    """
    existing = Friendship.objects.all().between(from_profile, to_profile)
    if existing and existing.status == FriendshipStatus.REQUESTED and existing.from_profile_id == to_profile.pk:
        if not existing.accept():
            return None
        try:
            accepted_pref = to_profile.notification_preferences.friend_accepted
        except AttributeError:
            accepted_pref = DeliveryPreference.SITE
        if accepted_pref != DeliveryPreference.NONE:
            NotificationLog.objects.create(
                profile=to_profile,
                status=Status.UNREAD,
                importance=Importance.MEDIUM,
                notification_type=NotificationType.FRIEND_ACCEPTED,
                title="Friend request accepted",
                message=f"{from_profile.username} accepted your friend request.",
                url=reverse("profile.view_user", kwargs={"profile_slug": from_profile.slug or str(from_profile.uuid)}),
                source_profile=from_profile,
            )
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


def _mark_friend_request_notifications_read(viewer_profile: Profile, source_profile_id: int) -> None:
    """Mark the viewer's pending "new friend request" notification(s) from a source as read.

    Accepting/declining/ignoring a request on the profile page (rather than via the
    notification dropdown's own accept/decline buttons) previously left the originating
    notification unread indefinitely, inflating the bell label count forever.

    Args:
        viewer_profile: Profile who just acted on the request.
        source_profile_id: pk of the profile that sent the request.
    """
    NotificationLog.objects.filter(
        profile=viewer_profile,
        notification_type=NotificationType.FRIEND_REQUEST,
        source_profile_id=source_profile_id,
    ).update(status=Status.READ)


def _existing_friendship(actor: Profile, target: Profile) -> Friendship:
    """The friendship row between the two profiles, or raise.

    Args:
        actor: The profile performing the action.
        target: The other profile.

    Returns:
        The single Friendship row joining the pair, in either direction.

    Raises:
        FriendshipNotFoundError: No row joins the pair.
    """
    friendship = Friendship.objects.all().between(target, actor)
    if not friendship:
        raise FriendshipNotFoundError
    return friendship


def _incoming_pending_request(actor: Profile, target: Profile) -> Friendship:
    """The pending request ``target`` sent ``actor``, or raise.

    Every answer to a friend request - accept, decline, ignore - is a response
    to *someone else's* pending offer, and this is the only function that
    establishes that premise. :func:`_existing_friendship` cannot: it resolves
    the pair's single row in either direction and reports nothing about its
    status, while ``Friendship.accept``/``decline``/``ignore`` overwrite
    ``status`` unconditionally. Answering a request through those two together
    therefore used to apply the transition to *whatever row happened to exist*,
    which is wrong in two directions at once:

    - **A block is not a request.** ``decline()`` or ``ignore()`` against a
      ``Blocked`` row rewrites it to ``Declined``/``Ignored``, so the *blocked*
      party could clear a block placed on them and resume contact operations
      that consult ``Profile.are_blocked``. That is the same one-call bypass of
      the site's only hard safety control that :func:`remove_friend` documents
      and guards against - reachable from the accept/reject/ignore endpoints
      instead of the remove one.
    - **A request is not consent.** ``accept()`` against the caller's *own*
      outgoing ``Requested`` row makes them both parties to the acceptance,
      creating a friendship the other person never agreed to. The same call
      against a ``Declined``, ``Ignored`` or ``Removed`` row resurrects a
      relationship its owner deliberately ended.

    Requiring ``REQUESTED`` **and** ``from_profile == target`` refuses all of
    the above with the one check the transitions themselves never make.

    Args:
        actor: The profile answering the request (the recipient).
        target: The profile that must have sent it.

    Returns:
        The pending Friendship directed from ``target`` to ``actor``.

    Raises:
        FriendshipNotFoundError: No row joins the pair, the row is not pending,
            or it is pending in the other direction. Deliberately one
            indistinguishable error - a caller must not be able to tell "you
            are blocked" from "there is nothing here", which is the whole point
            of that exception's docstring.
    """
    friendship = _existing_friendship(actor, target)
    if friendship.status != FriendshipStatus.REQUESTED or friendship.from_profile_id != target.pk:
        raise FriendshipNotFoundError
    return friendship


def accept_friend_request(actor: Profile, target: Profile) -> Friendship:
    """Accept ``target``'s pending friend request to ``actor``.

    Notifies the original requester and clears ``actor``'s own now-answered
    "new friend request" notification, matching what the profile-page button
    has always done.

    Args:
        actor: The profile accepting the request.
        target: The profile that sent it.

    Returns:
        The now-accepted Friendship.

    Raises:
        FriendshipNotFoundError: ``target`` has no pending request to ``actor``
            - see :func:`_incoming_pending_request`.
        FriendLimitExceededError: Either profile is already at the site's
            ``max_friends_per_user`` limit.
        FriendshipActionError: Either profile has Community disabled.
    """
    friendship = _incoming_pending_request(actor, target)

    if not friendship.accept():
        # Friendship.accept() returns a bare False for both refusal reasons;
        # re-deriving which one applies is what lets the caller answer 403 with
        # the actionable message rather than a generic failure.
        if Friendship.profile_at_max_friends(actor) or Friendship.profile_at_max_friends(friendship.from_profile):
            raise FriendLimitExceededError
        raise FriendshipActionError("Enable Community in Settings to accept friend requests.")

    requester = friendship.from_profile if friendship.to_profile == actor else friendship.to_profile
    try:
        accepted_pref = requester.notification_preferences.friend_accepted
    except AttributeError:
        accepted_pref = DeliveryPreference.SITE
    if accepted_pref != DeliveryPreference.NONE:
        _notify_friend_accepted(requester, actor)
    _mark_friend_request_notifications_read(actor, target.pk)
    return friendship


def _notify_friend_accepted(requester: Profile, actor: Profile) -> None:
    """Raise the FRIEND_ACCEPTED notification for *requester*.

    Split out so the acceptance flow's post-notification steps (marking the
    request notification read, returning the friendship) run whether or not
    the recipient has silenced this type.
    """
    NotificationLog.objects.create(
        profile=requester,
        status=Status.UNREAD,
        importance=Importance.MEDIUM,
        notification_type=NotificationType.FRIEND_ACCEPTED,
        title="Friend request accepted",
        message=f"{actor.username} accepted your friend request.",
        url=reverse("profile.view_user", kwargs={"profile_slug": actor.slug or str(actor.uuid)}),
        # The actor is who accepted - the same profile this notification's message
        # and url already point at. Without it the external API's
        # NotificationSerializer reports a null actor, so a mobile client renders
        # the notification with no one to link back to. The other two paths that
        # raise FRIEND_ACCEPTED both set it.
        source_profile=actor,
    )


def reject_friend_request(actor: Profile, target: Profile) -> Friendship:
    """Decline ``target``'s friend request, leaving them free to re-send later.

    Args:
        actor: The profile declining the request.
        target: The profile that sent it.

    Returns:
        The declined Friendship.

    Raises:
        FriendshipNotFoundError: ``target`` has no pending request to ``actor``
            - see :func:`_incoming_pending_request`.
    """
    friendship = _incoming_pending_request(actor, target)
    friendship.decline()
    _mark_friend_request_notifications_read(actor, target.pk)
    return friendship


def ignore_friend_request(actor: Profile, target: Profile) -> Friendship:
    """Ignore ``target``'s friend request - silently, and permanently.

    Distinct from :func:`reject_friend_request` in both directions: no
    notification is sent, and ``FriendshipStatus.can_request`` excludes
    ``Ignored``, so the requester can never re-send. Without this function the
    ``Ignored`` state would be unreachable for any API caller.

    Args:
        actor: The profile ignoring the request.
        target: The profile that sent it.

    Returns:
        The ignored Friendship.

    Raises:
        FriendshipNotFoundError: ``target`` has no pending request to ``actor``
            - see :func:`_incoming_pending_request`.
    """
    friendship = _incoming_pending_request(actor, target)
    friendship.ignore()
    _mark_friend_request_notifications_read(actor, target.pk)
    return friendship


def _placed_the_block(actor: Profile, friendship: Friendship) -> bool:
    """Whether ``actor`` is the profile that placed this block.

    ``Friendship`` carries no "blocked_by" column, so the row's *direction* is
    the only record of who blocked whom - which is exactly why
    :func:`block_profile` normalizes it (see that function). ``from_profile``
    is the blocker; ``to_profile`` is the person blocked.

    Args:
        actor: The profile attempting to act on the block.
        friendship: The relationship row, expected to be ``BLOCKED``.

    Returns:
        True when ``actor`` owns the block and may therefore lift it.
    """
    return friendship.from_profile_id == actor.pk


def remove_friend(actor: Profile, target: Profile) -> Friendship:
    """End an existing friendship.

    The row is retained at ``Removed`` rather than deleted, which is what lets
    ``FriendshipStatus.can_request`` allow a later re-request and what
    ``QuerySet.ever_friends`` reads.

    **A block is not a friendship and cannot be ended from the wrong side.**
    ``_existing_friendship`` resolves the row direction-agnostically, so before
    this guard existed the *blocked* party could call this function against the
    person who blocked them, set the row to ``Removed``, and immediately
    re-request contact - a one-request bypass of the site's only hard safety
    control, reachable from both the API's ``DELETE /friends/{uuid}/`` and the
    profile page's Remove button. A blocked caller now gets exactly the
    ``FriendshipNotFoundError`` a stranger would, rather than a permission
    error, so they cannot even confirm the block exists.

    Args:
        actor: The profile removing the friend.
        target: The profile being removed.

    Returns:
        The removed Friendship.

    Raises:
        FriendshipNotFoundError: No friendship exists between the pair, or the
            pair is blocked and ``actor`` is not the one who blocked.
    """
    friendship = _existing_friendship(actor, target)
    if friendship.status == FriendshipStatus.BLOCKED and not _placed_the_block(actor, friendship):
        raise FriendshipNotFoundError
    friendship.remove()
    return friendship


def block_profile(actor: Profile, target: Profile) -> Friendship:
    """Block ``target``, creating the relationship row if none exists yet.

    Unlike every other transition here, blocking must work against a complete
    stranger - that is the case it exists for - so a missing row is created
    rather than raising.

    **The row is re-pointed so ``from_profile`` is always the blocker.** There
    is one relationship row per pair and no column recording who blocked whom,
    so direction is the only available record - and reusing the existing row
    untouched got that record backwards half the time. A row created by an
    inbound friend request has ``from_profile`` = the requester, so blocking
    that requester used to leave the *blocked* party owning the row, at which
    point they (and not the blocker) satisfied :func:`_placed_the_block` and
    could lift their own block. Swapping the two foreign keys is safe because
    ``QuerySet.between`` already guarantees a single row per pair in either
    direction; nothing else reads a blocked row's direction, since every other
    consumer of ``BLOCKED`` (``Profile.are_blocked``, the direct-message
    temporary-access veto, the import path) deliberately matches both
    directions.

    Args:
        actor: The profile doing the blocking.
        target: The profile being blocked.

    Returns:
        The blocked Friendship, with ``actor`` as ``from_profile``.
    """
    _revoke_safety_partner_access(actor, target)
    _withdraw_pending_pin_shares(actor, target)
    _revoke_map_shares(actor, target)

    friendship = Friendship.objects.all().between(target, actor)
    if friendship:
        friendship.from_profile = actor
        friendship.to_profile = target
        friendship.status = FriendshipStatus.BLOCKED
        friendship.save()
        return friendship
    return Friendship.objects.create(
        from_profile=actor,
        to_profile=target,
        status=FriendshipStatus.BLOCKED,
    )


def _revoke_safety_partner_access(actor: Profile, target: Profile) -> None:
    """End any safety-partner relationship between two profiles being blocked apart.

    An accepted partner watches the owner's live location, check-in chat and
    escalation status, so a block that left those rows in place would be the
    weakest thing in the app rather than the strongest. ``remove_checkin_partner``
    is the mechanism: it deletes the row *and* closes any live WebSocket, whose
    permission was only ever checked at connect() time.

    Both directions go. Blocking is a mutual disengagement, and "still watching
    someone you blocked" is the same relationship seen from the other end.
    Outstanding invitations go too - an unaccepted invite is an offer of exactly
    the access being revoked.

    Args:
        actor: The profile doing the blocking.
        target: The profile being blocked.
    """
    from urbanlens.dashboard.models.safety.model import SafetyCheckinPartner
    from urbanlens.dashboard.services.visits.safety import remove_checkin_partner

    partners = SafetyCheckinPartner.objects.filter(
        Q(checkin__profile=actor, profile=target) | Q(checkin__profile=target, profile=actor),
    ).select_related("checkin")
    for partner in partners:
        remove_checkin_partner(partner)


def _revoke_map_shares(actor: Profile, target: Profile) -> None:
    """Delete any standalone map share between two profiles being blocked apart.

    A ``MarkupMapShare`` is live access, not a copy: it has no accept/reject
    step, and ``controllers.markup._map_visible_to`` honours it every time the
    recipient opens the map, so they keep seeing the owner's *current* map and
    can still clone it. Blocking has to end that, for the same reason it ends
    safety-partner access.

    Both directions, matching the rule the other revocations here follow -
    blocking is a mutual disengagement, and continuing to watch someone you have
    blocked is the same relationship seen from the other side.

    The map itself is untouched; only the grant is. The other two channels
    ``_map_visible_to`` accepts are deliberately left alone: a DM attachment,
    because a past conversation stays readable by design, and a ``PinShare``
    attachment, which follows the pin share's own fate above.

    Args:
        actor: The profile doing the blocking.
        target: The profile being blocked.
    """
    from urbanlens.dashboard.models.markup.share import MarkupMapShare

    MarkupMapShare.objects.filter(
        Q(from_profile=actor, to_profile=target) | Q(from_profile=target, to_profile=actor),
    ).delete()


def _withdraw_pending_pin_shares(actor: Profile, target: Profile) -> None:
    """Reject any still-pending pin share between two profiles being blocked apart.

    A pending share is a standing offer, and the accept path does not re-check
    blocking - so without this a blocked profile could accept afterwards and end
    up owning a copy of a place the blocker had just withdrawn from them.

    Accepted shares are deliberately left alone, following the line
    ``DirectMessageShare.revoke`` already draws: accepting runs
    ``create_pin_from_share``, so the recipient owns their own ``Pin`` and there
    is nothing a status change could take back.

    Args:
        actor: The profile doing the blocking.
        target: The profile being blocked.
    """
    from urbanlens.dashboard.models.pin_share.meta import PinShareStatus
    from urbanlens.dashboard.models.pin_share.model import PinShare

    PinShare.objects.filter(
        Q(from_profile=actor, to_profile=target) | Q(from_profile=target, to_profile=actor),
        status=PinShareStatus.PENDING,
    ).update(status=PinShareStatus.REJECTED)


def unblock_profile(actor: Profile, target: Profile) -> Friendship:
    """Lift a block ``actor`` placed on ``target``.

    The inverse :func:`block_profile` never had. Without it the only way out of
    a block was :func:`remove_friend`, which is the path the P0 above closed -
    so the profile page's "Unblock" button pointed at an action that (once
    hardened) refuses, and API clients had no unblock at all.

    Lands on ``REMOVED`` rather than deleting the row, matching every other
    ending transition here: ``FriendshipStatus.can_request`` accepts
    ``Removed``, so the two profiles can contact each other again, and
    ``QuerySet.ever_friends`` still sees any friendship that preceded the
    block.

    Every refusal is the same :class:`FriendshipNotFoundError` - unknown pair,
    no relationship row, a row in some other state, and a block placed by the
    *other* person all answer identically. Distinguishing them would let the
    blocked party confirm the block exists, which is the one fact a block is
    meant to keep ambiguous.

    Args:
        actor: The profile lifting its own block.
        target: The profile being unblocked.

    Returns:
        The now-``Removed`` Friendship.

    Raises:
        FriendshipNotFoundError: No row joins the pair, the row is not blocked,
            or the block belongs to ``target`` rather than ``actor``. Carries
            :data:`UNBLOCK_NOT_FOUND_MESSAGE` so the refusal reads identically
            to the one an unknown profile uuid produces.
    """
    friendship = Friendship.objects.all().between(target, actor)
    if friendship is None or friendship.status != FriendshipStatus.BLOCKED or not _placed_the_block(actor, friendship):
        raise FriendshipNotFoundError(UNBLOCK_NOT_FOUND_MESSAGE)
    friendship.remove()
    return friendship


def mute_profile(actor: Profile, target: Profile) -> Friendship:
    """Mute an existing relationship with ``target``, without altering it.

    Requires an existing row, matching the profile-page button: muting is a
    volume control on someone you already have a relationship with, whereas
    blocking (above) is a veto that must work on strangers.

    Sets ``Friendship.muted`` and leaves ``status`` exactly as it was. It used
    to write ``FriendshipStatus.MUTED`` over the status instead, which meant
    muting an accepted friend un-friended them for every visibility gate that
    reads ``Profile.are_friends``, and left no way back - the pre-mute status
    was gone, and ``FriendshipStatus.can_request`` refuses ``Muted``, so the
    site's own Unmute button answered 400. Callers that need to know whether a
    relationship is muted must read the flag; nothing writes the status value
    any more.

    Idempotent: re-muting an already-muted relationship is a no-op, which is
    what makes a retried mobile request safe.

    Args:
        actor: The profile doing the muting.
        target: The profile being muted.

    Returns:
        The muted Friendship.

    Raises:
        FriendshipNotFoundError: No relationship exists between the pair.
    """
    friendship = _existing_friendship(actor, target)
    friendship.mute()
    return friendship


def unmute_profile(actor: Profile, target: Profile) -> Friendship:
    """Un-mute an existing relationship with ``target``.

    The inverse of :func:`mute_profile`, and previously unreachable: with mute
    stored as a status there was no prior state to restore, so the profile
    page's Unmute button posted to the friend-*request* endpoint and was
    rejected (``can_request`` excludes ``Muted``). Now that mute is a flag,
    unmuting is a single boolean write and the relationship underneath is
    untouched throughout.

    Idempotent, for the same retry-safety reason as :func:`mute_profile`.

    Args:
        actor: The profile doing the unmuting.
        target: The profile being unmuted.

    Returns:
        The unmuted Friendship.

    Raises:
        FriendshipNotFoundError: No relationship exists between the pair -
            deliberately the same failure as muting a stranger, so the two
            halves of the toggle answer identically.
    """
    friendship = _existing_friendship(actor, target)
    friendship.unmute()
    return friendship


def invite_by_email(
    inviter: Profile,
    email: str,
    message: str | None = None,
    *,
    signup_url_builder: Callable[[str], str],
    subscription_role: Any = None,
    subscription_duration: str = "",
) -> None:
    """Invite someone to connect by email address, revealing nothing about them.

    If the address belongs to an existing account (primary or verified
    secondary), that account gets a friend request. Otherwise a
    ``FriendInvitation`` is created and the address is emailed a join link;
    signing up through it auto-accepts the pending request.

    **Anti-enumeration guarantee.** This function returns ``None`` on every
    non-validation path - registered, unregistered, visibility-rejected, and
    send-failed alike - and raises only for failures that depend purely on
    what the caller submitted (:class:`InviteValidationError`) or on the
    caller's own quota (:class:`InviteRateLimitedError`). Neither depends on
    whether the address is registered. The rate-limit check therefore runs
    *before* the registered/unregistered branch: doing it only on the path
    that actually sends mail would let a capped caller distinguish members
    from non-members by which error came back. Callers must preserve this by
    emitting one identical success response for the ``None`` return.

    Args:
        inviter: The profile sending the invitation.
        email: The raw address submitted by the caller.
        message: Optional note to include, bounded by
            ``MAX_FRIEND_REQUEST_MESSAGE_LENGTH``.
        signup_url_builder: Builds the absolute signup URL from an invitation
            token. Injected because the service has no request to call
            ``build_absolute_uri`` on.
        subscription_role: Optional ``SubscriptionRole`` to grant on
            acceptance. Site-admin-only; callers on untrusted surfaces must
            not accept this from user input.
        subscription_duration: Raw duration string paired with
            ``subscription_role``; ignored without one.

    Raises:
        InviteValidationError: The address is malformed, is the inviter's own,
            or the message is over-long.
        InviteRateLimitedError: The inviter is over their email budget.
    """
    # Imported inside the function, exactly as the controller version did.
    # Not a style quirk: the mail classes must be looked up on their own module
    # at call time so that ``mock.patch("django.core.mail.EmailMultiAlternatives")``
    # - which several existing tests rely on - actually intercepts the send. A
    # module-level ``from django.core.mail import ...`` binds the original class
    # at import time and silently defeats those patches.
    import smtplib

    from django.core.exceptions import ValidationError
    from django.core.mail import EmailMultiAlternatives
    from django.core.validators import validate_email

    from urbanlens.dashboard.models.email_log import EmailType
    from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
    from urbanlens.dashboard.services.auth.email_normalization import find_user_by_email, normalize_email
    from urbanlens.dashboard.services.security.email_safety import email_rate_limit_error, has_sent_join_email, record_email_sent

    email = (email or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError as exc:
        raise InviteValidationError("Please enter a valid email address.") from exc

    if normalize_email(email) == normalize_email(inviter.email):
        raise InviteValidationError("That's your own email address.")

    message = (message or "").strip()
    length_error = text_length_error(message, MAX_FRIEND_REQUEST_MESSAGE_LENGTH, "Message")
    if length_error:
        raise InviteValidationError(length_error)

    # Must precede the registered/unregistered branch - see the anti-enumeration
    # note in this function's docstring.
    rate_limit_error = email_rate_limit_error(inviter)
    if rate_limit_error:
        raise InviteRateLimitedError(rate_limit_error)

    existing_user = find_user_by_email(email)
    if existing_user:
        to_profile = existing_user.profile
        # Respect visibility settings silently - no error, no distinguishable response.
        # Same evaluator request_friend uses (Profile.visibility_permits already
        # rejects NO_ONE) - a bare "!= NO_ONE" check here previously let any
        # stranger who knew the email bypass a restricted FRIENDS/COMMON_PIN/
        # COMMON_FRIEND/COMMON_TRIP/ANYTHING_IN_COMMON visibility setting entirely.
        if to_profile != inviter and Profile.visibility_permits(to_profile.friend_request_visibility, to_profile, inviter):
            friendship = request_or_accept_friendship(inviter, to_profile, message or None)
            if friendship and subscription_role is not None:
                from urbanlens.dashboard.controllers.site_admin import _parse_duration_months
                from urbanlens.dashboard.models.subscriptions import grant_subscription

                grant_subscription(existing_user, subscription_role, inviter.user, _parse_duration_months(subscription_duration))
        return

    # No registered account - create an invitation token and send email.
    # Avoid duplicate pending invitations from the same inviter.
    FriendInvitation.objects.filter(
        inviter=inviter,
        email=email,
        accepted_at__isnull=True,
    ).delete()

    invitation = FriendInvitation(inviter=inviter, email=email, message=message or None)
    invitation.save()
    if subscription_role is not None:
        from urbanlens.dashboard.models.subscriptions import PendingSubscriptionGrant

        PendingSubscriptionGrant.objects.create(
            invitation=invitation,
            role=subscription_role,
            granted_by=inviter.user,
            duration_months="" if subscription_duration == "indefinite" else subscription_duration,
        )

    # A given user only ever sends one join-the-site email to a given address -
    # the invitation row above still enables auto-friending on sign-up, but the
    # mailbox is not contacted again.
    if not has_sent_join_email(inviter, email):
        signup_url = signup_url_builder(str(invitation.token))
        context = {
            "inviter": inviter,
            "signup_url": signup_url,
            "message": message or None,
        }
        subject = f"{inviter.username} invited you to join UrbanLens"
        text_body = f"Hi,\n\n{inviter.username} invited you to join UrbanLens - a private mapping platform for urban explorers and photographers."
        if message:
            text_body += f'\n\n"{message}"'
        text_body += f"\n\nAccept the invitation:\n{signup_url}\n\n- UrbanLens"
        html_body = render_to_string("dashboard/email/friend_invite.html", context)

        try:
            msg = EmailMultiAlternatives(subject=subject, body=text_body, from_email=None, to=[email])
            msg.attach_alternative(html_body, "text/html")
            msg.send()
        except (smtplib.SMTPException, OSError):
            # Swallowed on purpose: a delivery failure must look exactly like a
            # success to the caller, or the difference becomes the oracle this
            # whole function is built to deny.
            logger.exception("Failed to send friend invitation to %s", email)
        else:
            record_email_sent(inviter, email, EmailType.JOIN_INVITE)
