"""Creating, editing, and deleting the trip record itself.

Shared by the internal HTMX controllers and the external REST API so the
upcoming-trip quota, the generated-name fallback, the description length
limit, and the Undo History stash all apply identically whichever surface a
trip was created or destroyed from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.core.text_limits import MAX_TRIP_DESCRIPTION_LENGTH, text_length_error
from urbanlens.dashboard.services.trips.trip_access import require_joined
from urbanlens.dashboard.services.trips.trip_errors import TripPermissionError, TripQuotaError, TripValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    import datetime
    from uuid import UUID

EDIT_TRIP_DENIED = "Join this trip to edit its details."
DELETE_TRIP_DENIED = "Only the trip creator can delete it."

#: Toast shown after a successful delete - the window matches the Undo
#: framework's retention, so it is quoted rather than reworded per surface.
TRIP_DELETED_MESSAGE = "Trip deleted. Undo within 7 days from Settings → Undo History."

#: The trip's four configurable permission fields, in the order the settings
#: form presents them. Exported so every writer - the HTMX settings form, the
#: external API's serializer, and the tests - agrees on the field set rather
#: than each repeating the four names and drifting when a fifth is added.
TRIP_PERMISSION_FIELDS: tuple[str, ...] = (
    "allow_add_members",
    "allow_add_activities",
    "allow_edit_activities",
    "allow_comments",
)


def create_trip(
    creator: Profile,
    *,
    name: str | None = None,
    description: str | None = None,
    start_date: datetime.date | str | None = None,
    end_date: datetime.date | str | None = None,
    invite_profile_ids: Sequence[Any] = (),
    client_uuid: UUID | None = None,
) -> tuple[Trip, bool]:
    """Create a trip, join its creator, and invite any chosen friends.

    Args:
        creator: The profile creating the trip, joined automatically with an
            RSVP of yes.
        name: Trip name. Blank or omitted gets a generated one, so a
            "just start planning" flow needn't invent a title up front.
        description: Optional free-text description.
        start_date: Optional start date.
        end_date: Optional end date.
        invite_profile_ids: Profile ids to invite. Filtered to the creator's
            accepted friends - arbitrary submitted ids are never trusted - and
            truncated to whatever room ``max_trip_members`` leaves.
        client_uuid: A caller-generated uuid making the create idempotent, in
            the same shape ``services.pins.pin_creation.create_pin_for_profile``
            uses: when a trip with this uuid already exists *and the caller is
            its creator*, that trip is returned with ``created`` False instead
            of a duplicate being made. Offline clients retry creates until
            acknowledged, so the same submission may legitimately arrive twice.

    Returns:
        The ``(trip, created)`` pair; ``created`` is False for an idempotent replay.

    Raises:
        TripValidationError: The description exceeds the shared text limit, or
            ``client_uuid`` already belongs to somebody else's trip.
        TripQuotaError: The creator is already at ``max_upcoming_trips_per_user``.
    """
    if client_uuid is not None:
        existing = Trip.objects.filter(uuid=client_uuid).first()
        if existing is not None:
            if existing.creator_id != creator.id:
                # uuids are caller-generated, so this is either a client bug or
                # a guess. Either way the trip is not theirs to replay.
                raise TripValidationError("This uuid is already in use.")
            return existing, False

    from urbanlens.dashboard.services.trips.trip_names import random_trip_name

    clean_name = (name or "").strip() or random_trip_name()

    clean_description = description or None
    length_error = text_length_error(clean_description, MAX_TRIP_DESCRIPTION_LENGTH, "Description")
    if length_error:
        raise TripValidationError(length_error)

    max_upcoming = SiteSettings.get_current().max_upcoming_trips_per_user
    if max_upcoming > 0 and Trip.objects.upcoming(creator).count() >= max_upcoming:
        raise TripQuotaError(f"You already have the maximum of {max_upcoming} upcoming trips.")

    create_kwargs: dict[str, Any] = {
        "name": clean_name,
        "description": clean_description,
        "start_date": start_date or None,
        "end_date": end_date or None,
        "creator": creator,
    }
    if client_uuid is not None:
        # uuid is editable=False on the abstract base, so it must be passed
        # explicitly at the ORM layer - serializers/forms never bind it.
        create_kwargs["uuid"] = client_uuid

    try:
        # Nested atomic so a lost idempotency race fails inside its own
        # savepoint rather than poisoning any enclosing transaction - the same
        # shape `services.messaging.direct_messages.create_direct_message` uses.
        with transaction.atomic():
            trip = Trip.objects.create(**create_kwargs)
    except IntegrityError:
        # Two offline retries carrying the same client uuid arrived close
        # enough together that both passed the existence check above. The uuid
        # is globally unique, so the loser lands here - and must replay the
        # winner's trip, exactly as a sequential retry would, rather than
        # surfacing the collision as a 500 for a request the client was
        # promised would be idempotent.
        if client_uuid is None:
            raise
        existing = Trip.objects.filter(uuid=client_uuid).first()
        if existing is None or existing.creator_id != creator.id:
            raise
        return existing, False

    TripMembership.objects.get_or_create(trip=trip, profile=creator, defaults={"rsvp": "yes", "status": TripMembership.STATUS_JOINED})

    invite_members(trip, creator, invite_profile_ids)
    return trip, True


def invite_members(trip: Trip, inviter: Profile, invite_profile_ids: Sequence[Any]) -> int:
    """Invite a set of the inviter's own friends to a freshly created trip.

    Args:
        trip: The trip to invite to.
        inviter: The inviting profile.
        invite_profile_ids: Candidate profile ids - anything that isn't one of
            the inviter's accepted friends is silently dropped rather than
            trusted.

    Returns:
        How many new invitations were actually created.
    """
    if not invite_profile_ids:
        return 0

    from urbanlens.dashboard.services.social.connections import get_connections
    from urbanlens.dashboard.services.trips.trip_membership import notify_added_to_trip

    friend_ids = {str(f.id) for f in get_connections(inviter)}
    selected_ids = {pid for pid in invite_profile_ids if str(pid) in friend_ids}
    if not selected_ids:
        return 0

    max_members = SiteSettings.get_current().max_trip_members
    remaining = max_members - trip.profiles.count()
    invited = 0
    for friend_profile in Profile.objects.filter(id__in=selected_ids)[:remaining]:
        _membership, created = TripMembership.objects.get_or_create(trip=trip, profile=friend_profile, defaults={"status": TripMembership.STATUS_INVITED})
        if created:
            notify_added_to_trip(inviter, friend_profile, trip)
            invited += 1
    return invited


def update_trip(trip: Trip, actor: Profile, *, changes: Mapping[str, Any]) -> Trip:
    """Apply a presence-keyed partial update to a trip's own metadata.

    Only keys present in *changes* are touched, so a client syncing one field
    never clobbers another changed elsewhere in the meantime.

    Name and description deliberately differ on blank input, preserving the
    existing behavior of the internal edit form: a blank ``name`` is ignored
    (a trip always has a name - clearing it would leave the header empty and
    break the slug's basis), whereas a blank ``description`` clears the field
    (there is a real difference between "no description" and one nobody has
    written yet).

    Args:
        trip: The trip to update.
        actor: The profile making the change.
        changes: Any of ``name``, ``description``, ``start_date``, ``end_date``.

    Returns:
        The saved trip.

    Raises:
        TripPermissionError: The actor has not joined the trip.
        TripValidationError: The description exceeds the shared text limit.
    """
    require_joined(actor, trip, EDIT_TRIP_DENIED)

    if "name" in changes:
        new_name = (changes["name"] or "").strip()
        if new_name:
            trip.name = new_name
    if "description" in changes:
        description = changes["description"] or None
        length_error = text_length_error(description, MAX_TRIP_DESCRIPTION_LENGTH, "Description")
        if length_error:
            raise TripValidationError(length_error)
        trip.description = description
    if "start_date" in changes:
        trip.start_date = changes["start_date"] or None
    if "end_date" in changes:
        trip.end_date = changes["end_date"] or None
    trip.save()
    return trip


def delete_trip(trip: Trip, actor: Profile) -> None:
    """Delete a trip, stashing it for Undo History first.

    The stash is not optional: a trip delete cascades to its activities,
    comments, memberships and calendar links, and Undo History is the only way
    any of that comes back.

    Args:
        trip: The trip to delete.
        actor: The profile deleting it - must be the creator.

    Raises:
        TripPermissionError: The actor is not the trip's creator.
    """
    from urbanlens.dashboard.services.undo.handlers.trip import MODEL_LABEL as TRIP_MODEL_LABEL
    from urbanlens.dashboard.services.undo.service import stash_for_undo

    if trip.creator_id != actor.id:
        raise TripPermissionError(DELETE_TRIP_DENIED)
    stash_for_undo(TRIP_MODEL_LABEL, [trip], actor)
    trip.delete()


def set_trip_permissions(trip: Trip, actor: Profile, *, changes: Mapping[str, Any]) -> Trip:
    """Apply a presence-keyed partial update to a trip's permission levels.

    Only keys present in *changes* are touched, matching :func:`update_trip`.
    That is not a stylistic choice, it is a correctness one: this function used
    to walk a hardcoded ``{field: default}`` table and assign *every* entry, so
    a field the caller had simply not mentioned was reset to that default. The
    only caller was the site's settings form, which submits all four radio
    groups on every save, so the behaviour was invisible - but a partial writer
    (the external API's ``PATCH /trips/{slug}/settings/``, an offline client
    syncing one toggle) would have silently rewritten three unrelated
    permissions on a trip other people share, with nothing in the response to
    show it had happened.

    An unrecognized level is refused rather than coerced, for the same reason:
    quietly substituting a default tells the caller their write succeeded while
    moving the permission somewhere they never asked for.

    Args:
        trip: The trip to configure.
        actor: The profile making the change.
        changes: Any subset of :data:`TRIP_PERMISSION_FIELDS`, each valued with
            one of ``Trip.PERM_NONE``/``PERM_ORGANIZERS``/``PERM_EVERYONE``.
            Unrelated keys are ignored, so a caller may hand this the whole
            submitted form (``request.POST.dict()``) unfiltered.

    Returns:
        The saved trip. Unchanged, and not written at all, when *changes*
        names none of the permission fields.

    Raises:
        TripPermissionError: The actor is neither the creator nor an organizer.
        TripValidationError: A submitted field carries a level that is not one
            of the three the model defines.
    """
    from urbanlens.dashboard.services.trips.trip_access import is_organizer

    if not is_organizer(actor, trip):
        raise TripPermissionError("Only the trip creator or an organizer can change settings.")

    valid_levels = {Trip.PERM_NONE, Trip.PERM_ORGANIZERS, Trip.PERM_EVERYONE}
    updated: list[str] = []
    for field in TRIP_PERMISSION_FIELDS:
        if field not in changes:
            continue
        value = str(changes[field] or "").strip()
        if value not in valid_levels:
            raise TripValidationError(f"{field.replace('_', ' ')} must be one of: {', '.join(sorted(valid_levels))}.")
        setattr(trip, field, value)
        updated.append(field)

    if updated:
        trip.save(update_fields=[*updated, "updated"])
    return trip
