"""Shared pin-creation logic."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.core.icons import clean_icon
from urbanlens.dashboard.services.locations.geocoding import get_pin_by_address

if TYPE_CHECKING:
    from collections.abc import Sequence
    from decimal import Decimal
    from uuid import UUID

    from django.core.files.uploadedfile import UploadedFile

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class PinCreationError(ValueError):
    """Raised when the given input can't be turned into a Pin."""


class DuplicateCoordinatesError(PinCreationError):
    """This profile already has a pin at these exact coordinates."""


class DuplicatePropertyError(PinCreationError):
    """This profile already has a root pin on this property/location."""


class PinParentNotFoundError(PinCreationError):
    """The named ``parent_id`` doesn't resolve to one of this profile's pins."""


class NoLocationProvidedError(PinCreationError):
    """Neither coordinates nor an address were given."""


class AddressResolutionError(PinCreationError):
    """The given address couldn't be geocoded to coordinates."""


class DuplicateUuidError(PinCreationError):
    """The caller-supplied uuid already belongs to a different pin."""


class PinCreationForbiddenError(PinCreationError):
    """The input was well-formed but a profile setting forbids acting on it.
    Distinct from plain :class:`PinCreationError` so HTTP-facing callers can map it to 403 rather than 400 without inspecting the message text."""


def resolve_child_pin_location(
    profile: Profile,
    latitude: float | str | Decimal,
    longitude: float | str | Decimal,
    *,
    exclude_pin: Pin | None = None,
    defaults: dict | None = None,
) -> Location:
    """Resolve the Location a child (detail) pin sits at, refusing an exact overlap.
    A child pin records its own precise coordinates near its parent, so - unlike a top-level pin - this never applies the fuzzy proximity radius, which would collapse a door, a window, and a sign on one building onto the parent's own Location.

    Args:
        profile: Owner of the child pin being placed or moved.
        latitude: Submitted latitude.
        longitude: Submitted longitude.
        exclude_pin: A pin to ignore when looking for an overlap - the pin being moved, so re-submitting its current point stays a no-op instead of colliding with itself.
        defaults: Field defaults used only when creating a new Location.

    Returns:
        The Location at exactly these coordinates, created if it didn't exist.

    Raises:
        PinCreationError: This profile already has another pin at that point."""
    location, _created = Location.objects.get_exact_or_create(latitude, longitude, defaults=defaults)

    overlapping = Pin.objects.filter(profile=profile, location=location)
    if exclude_pin is not None and exclude_pin.pk is not None:
        overlapping = overlapping.exclude(pk=exclude_pin.pk)
    if overlapping.exists():
        raise DuplicateCoordinatesError("Duplicate pin at these exact coordinates.")
    return location


@dataclass(slots=True)
class PinCreationResult:
    """The Pin created plus any other Locations whose bounding box also covers the point."""

    pin: Pin
    #: Every Location match at this point, including ``pin.location`` itself -
    #: callers only need to act on this when there's more than one, meaning the
    #: point is ambiguous between two or more distinct places.
    all_locations: list[Location] = field(default_factory=list)
    #: False when ``client_uuid`` matched an existing pin and the call was an
    #: idempotent replay - nothing was created and no enrichment was enqueued.
    created: bool = True


def create_pin_for_profile(
    profile: Profile,
    *,
    name: str | None = None,
    latitude: float | str | None = None,
    longitude: float | str | None = None,
    address: str | None = None,
    icon: str | None = None,
    color: str | None = None,
    description: str | None = None,
    pin_type: str | None = None,
    custom_icon: UploadedFile | None = None,
    label_ids: Sequence[str] = (),
    tag_ids: Sequence[str] = (),
    category_ids: Sequence[str] = (),
    google_place_id: str | None = None,
    place_canonical_name: str | None = None,
    client_uuid: UUID | None = None,
    parent_id: UUID | None = None,
    name_is_user_provided: bool = False,
) -> PinCreationResult:
    """Create a Pin for a profile from raw, untrusted-shaped input.

    Args:
        profile: The owning profile - the pin is always created as this profile's own, regardless of who/what is calling.
        name: User-provided display name, if any.
        latitude: Marker latitude.
        longitude: Marker longitude.
        address: Free-text address to geocode when coordinates aren't given.
        icon: Icon key/emoji override.
        color: Hex color override.
        description: Personal notes to store on the pin, if any.
        pin_type: A ``PinType`` value; when given, the pin is marked user-classified (``pin_type_is_user_provided``) so automatic classification won't overwrite it - mirroring ``name``'s handling.
        custom_icon: An uploaded custom icon image.
        label_ids: Label ids to attach directly (takes precedence over tag_ids/category_ids).
        tag_ids: Tag-kind label ids to attach when ``label_ids`` wasn't given.
        category_ids: Category-kind label ids to attach when ``label_ids`` wasn't given.
        google_place_id: A Google Place id to link on both the pin and location.
        place_canonical_name: Canonical name to seed a newly-created Location with.
        client_uuid: A caller-generated uuid making the create idempotent: when a pin with this uuid already belongs to ``profile``, that pin is returned (``result.created`` False) instead of creating a duplicate.
        parent_id: An existing pin of this profile's to create this one as a child (detail pin) of.
        name_is_user_provided: Whether ``name`` was deliberately typed by the owner, rather than produced by a parser/importer.

    Returns:
        The created (or, for an idempotent replay, existing) pin plus every Location match at this point.

    Raises:
        PinCreationError: Neither coordinates nor a usable address were given, the address couldn't be geocoded, ``client_uuid`` is already used by a pin that isn't this profile's, ``parent_id`` doesn't match one of this profile's own pins, the profile already has a...
        PinCreationForbiddenError: An address needed geocoding but external lookups are turned off for this profile."""
    if client_uuid is not None:
        existing = Pin.objects.filter(profile=profile, uuid=client_uuid).select_related("location").first()
        if existing is not None:
            return PinCreationResult(pin=existing, all_locations=[existing.location], created=False)

    new_parent: Pin | None = None
    if parent_id is not None:
        new_parent = Pin.objects.filter(uuid=parent_id, profile=profile).first()
        if new_parent is None:
            raise PinParentNotFoundError("parent_id does not resolve to one of this profile's pins.")
    # An unset coordinate arrives as None or "" (e.g. the map's blank hidden
    # input) - normalize both to None so the checks below can use `is None`
    # without treating a valid 0/0.0 coordinate (equator, prime meridian) as missing.
    if isinstance(latitude, str) and latitude.strip() == "":
        latitude = None
    if isinstance(longitude, str) and longitude.strip() == "":
        longitude = None

    if latitude is None or longitude is None:
        if not address:
            raise NoLocationProvidedError("Neither coordinates nor an address were given.")
        if not profile.external_apis_enabled:
            raise PinCreationForbiddenError("external_apis_enabled is False for this profile.")
        latitude, longitude = get_pin_by_address(address)
        if latitude is None or longitude is None:
            raise AddressResolutionError("Geocoding the given address returned no coordinates.")

    lat_f = float(latitude)
    lon_f = float(longitude)

    if new_parent is not None:
        # A child pin keeps its own exact point (and may not stack on another of
        # this profile's pins) - see resolve_child_pin_location.
        location = resolve_child_pin_location(profile, lat_f, lon_f, defaults={"official_name": place_canonical_name})
    else:
        # The user's exact coordinate is kept, always.
        # Consolidating two drops at one place is the *place's* job now: they resolve onto the same
        # parcel and share its wiki, its community, and its "places in common" entry without either
        # coordinate being thrown away.
        location, _ = Location.objects.get_exact_or_create(lat_f, lon_f, defaults={"official_name": place_canonical_name})

    # Resolve what this coordinate stands on, from geometry already known. No
    # provider is called here - provisioning stays in the background task, so
    # a drop never waits on a county GIS lookup.
    from urbanlens.dashboard.services.places.ambiguity import competing_places, representative_locations
    from urbanlens.dashboard.services.places.resolution import resolve_location_place

    if location.place_resolved_at is None:
        resolve_location_place(location)

    # One root pin per property, still - the guarantee the old 50 m snap was
    # really providing, now expressed against the property itself instead of
    # against whichever coordinate happened to be recorded first.
    if new_parent is None and location.place_id and Pin.objects.filter(profile=profile, parent_pin__isnull=True, location__place_id=location.place_id).exists():
        raise DuplicatePropertyError("Duplicate root pin on this property.")

    # The chosen location, plus any property this coordinate could plausibly mean instead - so a
    # caller can still treat "more than one" as "there is a real choice here".
    # Competitors are almost always absent: everything inside one property is the same answer, not a
    # rival one.
    rivals = competing_places(lat_f, lon_f, location.place if location.place_id else None)
    all_locations = [location, *[candidate for candidate in representative_locations(rivals) if candidate.pk != location.pk]]

    from urbanlens.dashboard.models.wiki.model import Wiki

    create_kwargs: dict = {
        "name": name,
        # Defaults to False because a create is not inherently a rename: file and offline-client
        # imports commonly put a coordinate or another parser fallback in ``name``, and marking
        # every non-empty value as user-provided made that placeholder permanently outrank names
        # discovered later.
        "name_is_user_provided": name_is_user_provided and bool((name or "").strip()),
        "location": location,
        # Link to the place's community wiki when one already exists; wikis
        # are only ever created explicitly from the pin page.
        "wiki": Wiki.objects.get_for_location(location),
        # Cleaned here rather than per-caller: the map's add-pin dialog, the
        # external API's pin create and the import paths all arrive through
        # this one function.
        "icon": clean_icon(icon),
        "custom_icon": custom_icon,
        "color": color,
        "profile": profile,
        "parent_pin": new_parent,
    }
    if description is not None and description.strip():
        create_kwargs["description"] = description
    if pin_type:
        create_kwargs["pin_type"] = pin_type
        create_kwargs["pin_type_is_user_provided"] = True
    if client_uuid is not None:
        # uuid is editable=False on the abstract base, so it must be passed
        # explicitly at the ORM layer - serializers/forms never bind it.
        create_kwargs["uuid"] = client_uuid

    try:
        with transaction.atomic():
            pin = Pin.objects.create(**create_kwargs)
    except IntegrityError as exc:
        # Two constraints can fire here; both have well-defined answers: - uuid collision: a
        # concurrent retry of the same client_uuid won the race (return its pin - the idempotent
        # outcome), or the uuid belongs to another profile's pin (reject; uuids are
        # caller-generated, so this is either a caller bug or a guess - either way not theirs). -
        if client_uuid is not None:
            existing = Pin.objects.filter(profile=profile, uuid=client_uuid).select_related("location").first()
            if existing is not None:
                return PinCreationResult(pin=existing, all_locations=all_locations, created=False)
            if Pin.objects.filter(uuid=client_uuid).exists():
                raise DuplicateUuidError("Client-supplied uuid already belongs to a different pin.") from exc
        if Pin.objects.filter(profile=profile, location=location, parent_pin__isnull=True).exists():
            raise DuplicatePropertyError("Duplicate root pin at this location (race with a concurrent create).") from exc
        raise

    # visible_to keeps the id__in lookups from resolving another user's
    # private labels - a guessed foreign label id would otherwise attach (and
    # render the name of) someone else's label.
    if label_ids:
        pin.labels.set(Label.objects.location_labels().visible_to(profile).filter(id__in=label_ids))
    else:
        if tag_ids:
            pin.labels.remove(*pin.labels.filter(kind=KIND_TAG))
            pin.labels.add(*Label.objects.tags().visible_to(profile).filter(id__in=tag_ids))
        if category_ids:
            pin.labels.remove(*pin.labels.filter(kind=KIND_CATEGORY))
            pin.labels.add(*Label.objects.categories().visible_to(profile).filter(id__in=category_ids))

    # Generate slug immediately so the "View Details" URL resolves without a
    # separate lookup - Pin.slug is nullable and is not auto-populated by create().
    pin.slug = pin.ensure_slug()

    # When adding from a Places layer marker, pre-populate the GooglePlace link
    # on both the pin and its location so subsequent views avoid an extra
    # Places Details API call.
    if google_place_id:
        try:
            from urbanlens.dashboard.services.apis.locations.google.place_info import (
                GooglePlaceService,
            )
            from urbanlens.dashboard.services.locations.naming import (
                update_location_name_from_external_sources,
            )

            gp_service = GooglePlaceService()
            gp_service.ensure_linked_by_place_id(pin.location, google_place_id)
            if location:
                gp_service.ensure_linked_by_place_id(location, google_place_id)
            update_location_name_from_external_sources(location, profile=profile)
        except Exception:
            logger.warning("Failed to link Google Place %s", google_place_id, exc_info=True)

    # Pre-warm LocationCache for Wikipedia, NPS, and Google Places, plus the
    # web-search results cache, so the Private Pin page doesn't need to hit the
    # APIs on first load.
    from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

    if location and profile.external_apis_enabled:
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import (
            prefetch_location_external_data,
            refresh_pin_web_search,
        )

        safely_enqueue_task(prefetch_location_external_data, location.pk, google_place_id=google_place_id, profile_id=profile.pk)

        if user_has_feature(profile.user, SiteFeature.SEARCH):
            safely_enqueue_task(refresh_pin_web_search, pin.pk)

    if user_has_feature(profile.user, SiteFeature.AI):
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import suggest_pin_category

        safely_enqueue_task(suggest_pin_category, pin.pk)

    # When another user already pinned this location, its building list is cached and this pin's
    # default structure can be built right away - no need to wait for a fetch that will never
    # re-run.
    # Queued, not inline: a campus can mean hundreds of child pins, which is not request-time work.
    if location is not None and pin.parent_pin_id is None:
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import auto_nest_building_pins

        safely_enqueue_task(auto_nest_building_pins, pin.pk)

    return PinCreationResult(pin=pin, all_locations=all_locations)
