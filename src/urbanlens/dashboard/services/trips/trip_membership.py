"""Who is on a trip: inviting, joining, leaving, RSVPs, and organizer status.

Shared by the internal members panel and the external REST API. The
cross-cutting obligations a caller must never skip are enforced here:

- ``Profile.are_blocked`` is checked before an invitation can be forced on someone,
- ``SiteSettings.max_trip_members`` caps the roster,
- joining records share provenance (``record_trip_shares_for_member``),
- leaving or being removed revokes calendar sync (``disconnect_member_calendar_sync``),
- member identities are resolved through ``resolve_visible_identities``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid as uuid_module

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.trips.calendar_sync import disconnect_member_calendar_sync
from urbanlens.dashboard.services.trips.trip_access import can_perform, require_perform
from urbanlens.dashboard.services.trips.trip_errors import (
    TripMemberNotFoundError,
    TripNotFoundError,
    TripPermissionError,
    TripQuotaError,
    TripValidationError,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

ADD_MEMBER_DENIED = "You don't have permission to add members to this trip."
MEMBER_NOT_FOUND = "No such member on this trip."
ORGANIZER_DENIED = "Only the trip creator can manage organizers."
REMOVE_MEMBER_DENIED = "Only the trip creator can remove other members."
CREATOR_CANNOT_BE_REMOVED = "The trip creator cannot be removed."
CREATOR_ALWAYS_ORGANIZER = "The trip creator is always an organizer."
CREATOR_CANNOT_LEAVE = "The trip creator cannot leave - delete the trip instead."
NOT_A_MEMBER = "You are not a member of this trip."


def resolve_trip_member(trip: Trip, *, profile_id: int | str | None = None, slug: str | None = None) -> Profile:
    """Resolve a target member of *trip*, scoped to that trip's own roster.

    Previously the member-management views resolved the target with a global
    ``get_object_or_404(Profile, pk=profile_id)`` *before* narrowing to the
    trip, so any member of any trip could probe arbitrary profile ids and
    learn from the status code which ones existed. Lookups here never leave
    the trip: a real profile that simply isn't on this trip is indistinguishable
    from one that doesn't exist.

    Args:
        trip: The trip whose roster bounds the lookup.
        profile_id: The target's primary key, when addressing by id.
        slug: The target's profile slug *or* uuid, when addressing by the
            public identifier. Uuid is accepted because a member whose
            identity is masked from the caller is served ``slug: null`` - their
            uuid is the only handle such a caller has, and it discloses nothing.

    Returns:
        The matching profile - a current member, or the trip's creator.

    Raises:
        TripNotFoundError: Nobody on this trip matches.
    """
    from django.db.models import Q

    scope = Q(trip_memberships__trip=trip)
    if trip.creator_id is not None:
        scope |= Q(pk=trip.creator_id)
    candidates = Profile.objects.filter(scope).select_related("user").distinct()

    if profile_id is not None:
        target = candidates.filter(pk=profile_id).first() if str(profile_id).isdigit() else None
        if target is None:
            raise TripNotFoundError(MEMBER_NOT_FOUND)
        return target

    ref = (slug or "").strip()
    if not ref:
        raise TripNotFoundError(MEMBER_NOT_FOUND)
    target = candidates.filter(slug=ref).first()
    if target is None:
        try:
            target = candidates.filter(uuid=uuid_module.UUID(ref)).first()
        except ValueError:
            target = None
    if target is None:
        raise TripNotFoundError(MEMBER_NOT_FOUND)
    return target


def require_trip_creator(trip: Trip, actor: Profile, message: str = ORGANIZER_DENIED) -> None:
    """Raise unless *actor* is the trip's creator.

    Called before :func:`resolve_trip_member` on the organizer endpoints so a
    non-creator is refused identically whether or not the member they named
    exists - the permission answer must not depend on the target.

    Args:
        trip: The trip.
        actor: The profile attempting the action.
        message: The refusal message shown to the user.

    Raises:
        TripPermissionError: The actor is not the trip's creator.
    """
    if trip.creator_id != actor.id:
        raise TripPermissionError(message)


def list_members(trip: Trip, viewer: Profile) -> list[TripMembership]:
    """Return the trip's memberships with each member's identity resolved for *viewer*.

    A trip can include people who aren't friends with everyone else on it,
    whose privacy settings may not permit some viewers to see their name or
    avatar - RSVP status and trip activity involving them still show.

    Args:
        trip: The trip whose roster is wanted.
        viewer: The profile viewing the roster.

    Returns:
        Memberships ordered by username, each with ``membership.profile``
        carrying resolved ``display_name``/``display_avatar_url``/``is_masked``.
    """
    from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identities

    # "trip" is preloaded so a serializer marking the creator's row doesn't
    # re-query the trip once per member.
    members = list(trip.memberships.select_related("profile__user", "trip").order_by("profile__user__username"))
    resolve_visible_identities(viewer, [m.profile for m in members])
    return members


def notify_added_to_trip(inviter: Profile, invitee: Profile, trip: Trip) -> None:
    """Send an ADDED_TO_TRIP notification, respecting the invitee's delivery preference.

    Args:
        inviter: The profile who added *invitee*.
        invitee: The newly invited profile.
        trip: The trip they were invited to.
    """
    from django.urls import reverse

    from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identity

    try:
        pref = invitee.notification_preferences.added_to_trip
    except AttributeError:
        pref = DeliveryPreference.SITE
    if pref == DeliveryPreference.NONE:
        return
    # Resolved (and masked if needed) toward the specific recipient before
    # formatting - the message string is stored as plain text, so it must be
    # masked here, not at render time (see identity_visibility.py's docstring).
    inviter_name = resolve_visible_identity(invitee, inviter)["display_name"]
    NotificationLog.objects.notify(
        profile=invitee,
        source_profile=inviter,
        status=Status.UNREAD,
        importance=Importance.MEDIUM,
        notification_type=NotificationType.ADDED_TO_TRIP,
        title="Trip invitation",
        message=f'{inviter_name} invited you to join "{trip.name}".',
        url=reverse("trips.detail", kwargs={"trip_slug": trip.slug}),
    )


def suggest_connections_for_new_member(new_member: Profile, existing_members: Iterable[Profile]) -> None:
    """Soft-introduce a newly added trip member to existing members they aren't friends with.

    Both sides must allow friend recommendations (see
    ``services.social.connections.recommendable_strangers``) - never presumes on
    anyone's behalf, just makes an already-opted-in connection discoverable.

    Args:
        new_member: The profile that was just added to the trip.
        existing_members: The trip's other current members.
    """
    from urbanlens.dashboard.services.social.connections import recommendable_strangers, suggest_mutual_connection

    for other in recommendable_strangers(new_member, list(existing_members)):
        suggest_mutual_connection(new_member, other)


def addable_friends(trip: Trip, profile: Profile) -> list[Profile]:
    """The viewer's friends not already on this trip, for the add-member picker.

    Empty unless the viewer currently has permission to add members, so the
    picker reflects the trip's "Allow members to add people" setting instead
    of only ever working for the creator.

    Args:
        trip: The trip being added to.
        profile: The viewing profile.

    Returns:
        Friends eligible to be invited, or an empty list when the viewer may
        not invite anyone.
    """
    if not can_perform(profile, trip, trip.allow_add_members):
        return []
    from urbanlens.dashboard.services.social.connections import get_connections

    existing_ids: set[int] = set(trip.memberships.values_list("profile_id", flat=True))
    if trip.creator_id is not None:
        existing_ids.add(trip.creator_id)
    return [friend for friend in get_connections(profile) if friend.id not in existing_ids]


def add_member_by_username(trip: Trip, actor: Profile, username: str) -> tuple[TripMembership, bool]:
    """Invite a user to the trip by their username.

    Idempotent: re-inviting someone already on the roster returns their
    existing membership with ``created`` False and sends no second
    notification, so a client retrying an unacknowledged invite is harmless.

    Args:
        trip: The trip to invite to.
        actor: The inviting profile.
        username: The invitee's username, matched case-insensitively.

    Returns:
        The ``(membership, created)`` pair.

    Raises:
        TripPermissionError: The actor may not add members.
        TripValidationError: No username was supplied.
        TripQuotaError: The trip is already at ``max_trip_members``. Checked
            before the username is resolved, so trip capacity can never be
            used to infer whether an arbitrary username exists.
        TripMemberNotFoundError: No user has that username, or a block
            exists between the two profiles - both answer identically so a
            block can't be distinguished from a nonexistent account.
    """
    require_perform(actor, trip, trip.allow_add_members, ADD_MEMBER_DENIED)

    clean_username = (username or "").strip()
    if not clean_username:
        raise TripValidationError("Username is required.")

    # Checked before the username is even looked up: if this ran after
    # resolving the user, "trip full" would only ever fire for a real,
    # unblocked account, turning trip capacity into a free username-existence
    # oracle for anyone who can fill their own trip with known accounts once.
    max_members = SiteSettings.get_current().max_trip_members
    current_count = trip.profiles.count()
    if current_count >= max_members:
        raise TripQuotaError(f"This trip is full ({max_members} members maximum).")

    from django.contrib.auth.models import User

    try:
        user = User.objects.get(username__iexact=clean_username)
    except User.DoesNotExist as exc:
        # The raw username is carried on the exception rather than escaped into
        # the message - the HTML caller escapes it, the JSON caller must not.
        raise TripMemberNotFoundError(f'No user found with username "{clean_username}".', clean_username) from exc

    new_profile, _ = Profile.objects.get_or_create(user=user)
    # A block answers exactly like a nonexistent username (see
    # TripMemberNotFoundError above) instead of TripPermissionError: telling a
    # caller "this account exists and is blocking you" is itself the same
    # enumeration leak as confirming any other account's existence.
    if Profile.are_blocked(actor, new_profile):
        raise TripMemberNotFoundError(f'No user found with username "{clean_username}".', clean_username)

    membership, created = TripMembership.objects.get_or_create(trip=trip, profile=new_profile, defaults={"status": TripMembership.STATUS_INVITED})
    if created:
        notify_added_to_trip(actor, new_profile, trip)
        suggest_connections_for_new_member(new_profile, trip.profiles.exclude(pk=new_profile.pk))
    return membership, created


def remove_member(trip: Trip, actor: Profile, target: Profile) -> None:
    """Remove a member from a trip.

    Members may remove themselves; only the creator may remove anyone else.
    The creator can never be removed.

    Args:
        trip: The trip to remove from.
        actor: The profile performing the removal.
        target: The member being removed (resolve via :func:`resolve_trip_member`).

    Raises:
        TripValidationError: The target is the trip's creator.
        TripPermissionError: The actor is neither the target nor the creator.
    """
    if target.id == trip.creator_id:
        raise TripValidationError(CREATOR_CANNOT_BE_REMOVED)
    if actor.id not in {target.id, trip.creator_id}:
        raise TripPermissionError(REMOVE_MEMBER_DENIED)

    TripMembership.objects.for_trip_and_profile(trip, target).delete()
    # A live calendar export is a second, independent channel to the same data -
    # it has to be cut at the same moment membership is.
    disconnect_member_calendar_sync(trip, target)


def set_member_organizer(trip: Trip, actor: Profile, target: Profile, *, is_organizer: bool) -> TripMembership:
    """Set (not toggle) a member's organizer flag.

    An explicit target value rather than a toggle: a mobile client retrying a
    request it never saw acknowledged would otherwise flip the flag back. The
    internal panel keeps its toggle UX by passing
    ``is_organizer=not membership.is_organizer``.

    Args:
        trip: The trip.
        actor: The profile making the change - must be the creator.
        target: The member whose flag is being set.
        is_organizer: The value to store.

    Returns:
        The saved membership row.

    Raises:
        TripPermissionError: The actor is not the trip's creator.
        TripValidationError: The target is the creator, who is always an organizer.
        TripNotFoundError: The target has no membership row on this trip.
    """
    if trip.creator_id != actor.id:
        raise TripPermissionError(ORGANIZER_DENIED)
    if target.id == trip.creator_id:
        raise TripValidationError(CREATOR_ALWAYS_ORGANIZER)

    membership = TripMembership.objects.for_trip_and_profile(trip, target).first()
    if membership is None:
        raise TripNotFoundError(MEMBER_NOT_FOUND)
    membership.is_organizer = is_organizer
    membership.save(update_fields=["is_organizer", "updated"])
    return membership


def join_trip(trip: Trip, profile: Profile) -> None:
    """Accept a trip invitation, unlocking contribution rights.

    Separate from RSVP, which only says whether the member expects to show up.
    Joining reveals every place already on the itinerary, so each one is
    recorded in its sharer's reshare chain like any other pin share.

    Args:
        trip: The trip being joined.
        profile: The joining profile. The creator is already joined; the call
            is a harmless no-op for them.
    """
    if trip.creator_id == profile.id:
        return
    TripMembership.objects.for_trip_and_profile(trip, profile).update(status=TripMembership.STATUS_JOINED)
    from urbanlens.dashboard.services.trips.trip_share_tracking import record_trip_shares_for_member

    record_trip_shares_for_member(trip, profile)


def leave_trip(trip: Trip, profile: Profile) -> None:
    """Leave a trip, or decline an invitation that was never accepted.

    Args:
        trip: The trip being left.
        profile: The departing profile.

    Raises:
        TripValidationError: The creator cannot leave their own trip.
    """
    if trip.creator_id == profile.id:
        raise TripValidationError(CREATOR_CANNOT_LEAVE)

    TripMembership.objects.for_trip_and_profile(trip, profile).delete()
    disconnect_member_calendar_sync(trip, profile)


def set_trip_rsvp(trip: Trip, profile: Profile, rsvp: str | None) -> TripMembership:
    """Set or clear the profile's trip-wide RSVP.

    Args:
        trip: The trip being answered.
        profile: The responding member.
        rsvp: ``yes``/``no``/``maybe``, or None/blank to clear.

    Returns:
        The saved membership row.

    Raises:
        TripValidationError: The value is not a valid RSVP choice.
        TripNotFoundError: The profile has no membership row on this trip.
    """
    value = (rsvp or "").strip()
    valid = {choice[0] for choice in TripMembership.RSVP_CHOICES}
    if value and value not in valid:
        raise TripValidationError("Invalid RSVP value.")

    membership = TripMembership.objects.for_trip_and_profile(trip, profile).first()
    if membership is None:
        raise TripNotFoundError(NOT_A_MEMBER)
    membership.rsvp = value or None
    membership.save(update_fields=["rsvp", "updated"])
    return membership
