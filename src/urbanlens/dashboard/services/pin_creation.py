"""Shared pin-creation logic.

This is the single code path behind every way a Pin gets created on a user's
behalf: the map UI's "Add pin" flow (``controllers.maps.MapController.post_add_pin``)
and the external API's pin-creation endpoint (``external_api.views.PinsView``).
Keeping it in one place means a validation rule, sanitization step, or piece of
enrichment added for one caller automatically applies to the other - there is
no second, slightly-different pin-creation path for a third-party app to slip
untrusted data through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
import logging
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.db.models import DecimalField

from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.locations.geocoding import get_pin_by_address

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from django.core.files.uploadedfile import UploadedFile

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class PinCreationError(ValueError):
    """Raised when the given input can't be turned into a Pin.

    The message is safe to surface directly to the caller (map UI or external
    API) - it never includes anything beyond what the caller itself submitted.
    """


class PinCreationForbiddenError(PinCreationError):
    """The input was well-formed but a profile setting forbids acting on it.

    Distinct from plain :class:`PinCreationError` so HTTP-facing callers can
    map it to 403 rather than 400 without inspecting the message text.
    """


def quantize_coordinate(value: float | str | Decimal, field_name: str) -> Decimal:
    """Round a submitted coordinate to the precision ``Location`` actually stores.

    ``Location.latitude``/``longitude`` are fixed-precision decimals, so the
    database rounds on insert. Rounding here first means a lookup compares the
    same value the row will hold, rather than the caller's raw float.

    Args:
        value: The submitted coordinate.
        field_name: Which Location field it is - the precision comes from the
            field itself rather than a duplicated constant.

    Returns:
        The coordinate at the field's own decimal precision.

    Raises:
        TypeError: The named field isn't a fixed-precision decimal, so there is
            no precision to round to - the assumption this whole function rests
            on, worth failing loudly rather than silently mis-rounding.
    """
    field = Location._meta.get_field(field_name)  # noqa: SLF001 - _meta is public API despite the underscore
    if not isinstance(field, DecimalField) or field.decimal_places is None:
        raise TypeError(f"Location.{field_name} must be a fixed-precision DecimalField.")
    return Decimal(str(float(value))).quantize(Decimal(1).scaleb(-field.decimal_places), rounding=ROUND_HALF_UP)


def resolve_child_pin_location(
    profile: Profile,
    latitude: float | str | Decimal,
    longitude: float | str | Decimal,
    *,
    exclude_pin: Pin | None = None,
    defaults: dict | None = None,
) -> Location:
    """Resolve the Location a child (detail) pin sits at, refusing an exact overlap.

    A child pin records its own precise coordinates near its parent, so - unlike
    a top-level pin - this never applies the fuzzy proximity radius, which would
    collapse a door, a window, and a sign on one building onto the parent's own
    Location. Matching is on stored coordinate identity instead: ``Location`` is
    unique on (latitude, longitude) at the fields' own precision, so the
    submitted values are rounded to that precision before lookup. Comparing the
    raw floats by zero PostGIS distance instead would miss a row whose stored
    coordinates round to the same pair, then fail that unique constraint on
    insert.

    Two of one profile's pins may never occupy the exact same point: perfectly
    stacked markers can't be told apart or clicked through on a map, and the
    pair is nearly always one place the user meant to record once. Top-level
    pins get this from ``db_pin_unique_location_per_profile``; child pins are
    deliberately exempt from that constraint - they must be able to share a
    parcel with their parent and siblings - so the narrower exact-point rule is
    enforced here instead.

    Args:
        profile: Owner of the child pin being placed or moved.
        latitude: Submitted latitude.
        longitude: Submitted longitude.
        exclude_pin: A pin to ignore when looking for an overlap - the pin being
            moved, so re-submitting its current point stays a no-op instead of
            colliding with itself.
        defaults: Field defaults used only when creating a new Location.

    Returns:
        The Location at exactly these coordinates, created if it didn't exist.

    Raises:
        PinCreationError: This profile already has another pin at that point.
    """
    latitude_value = quantize_coordinate(latitude, "latitude")
    longitude_value = quantize_coordinate(longitude, "longitude")

    location = Location.objects.filter(latitude=latitude_value, longitude=longitude_value).first()
    if location is None:
        try:
            location = Location.objects.create(latitude=latitude_value, longitude=longitude_value, **(defaults or {}))
        except IntegrityError:
            # A concurrent request inserted this coordinate pair between the
            # lookup and the insert (the (latitude, longitude) unique
            # constraint) - use that row rather than surfacing a 500.
            location = Location.objects.filter(latitude=latitude_value, longitude=longitude_value).first()
            if location is None:
                raise

    overlapping = Pin.objects.filter(profile=profile, location=location)
    if exclude_pin is not None and exclude_pin.pk is not None:
        overlapping = overlapping.exclude(pk=exclude_pin.pk)
    if overlapping.exists():
        raise PinCreationError("You already have a pin at these exact coordinates. Place it slightly apart to keep both.")
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
) -> PinCreationResult:
    """Create a Pin for a profile from raw, untrusted-shaped input.

    Resolves (or creates) the pin's Location - geocoding ``address`` when no
    coordinates were given, gated by ``profile.external_apis_enabled`` exactly
    like a manual address entry on the map - creates the Pin, attaches any
    chosen labels scoped to what the profile can see, generates the pin's
    slug, links a Google Place when given, and enqueues the same background
    enrichment (external-data prefetch, web-search refresh, AI category
    suggestion) that runs after every pin creation.

    Args:
        profile: The owning profile - the pin is always created as this
            profile's own, regardless of who/what is calling.
        name: User-provided display name, if any.
        latitude: Marker latitude. Required unless ``address`` resolves to one.
        longitude: Marker longitude. Required unless ``address`` resolves to one.
        address: Free-text address to geocode when coordinates aren't given.
        icon: Icon key/emoji override.
        color: Hex color override.
        description: Personal notes to store on the pin, if any.
        pin_type: A ``PinType`` value; when given, the pin is marked
            user-classified (``pin_type_is_user_provided``) so automatic
            classification won't overwrite it - mirroring ``name``'s handling.
        custom_icon: An uploaded custom icon image.
        label_ids: Label ids to attach directly (takes precedence over tag_ids/category_ids).
        tag_ids: Tag-kind label ids to attach when ``label_ids`` wasn't given.
        category_ids: Category-kind label ids to attach when ``label_ids`` wasn't given.
        google_place_id: A Google Place id to link on both the pin and location.
        place_canonical_name: Canonical name to seed a newly-created Location with.
        client_uuid: A caller-generated uuid making the create idempotent: when a
            pin with this uuid already belongs to ``profile``, that pin is
            returned (``result.created`` False) instead of creating a duplicate.
            The external API's offline-outbox clients retry creates until
            acknowledged, so the same submission may legitimately arrive twice.
        parent_id: An existing pin of this profile's to create this one as a
            child (detail pin) of. When given, the Location is resolved by
            :func:`resolve_child_pin_location` - an exact-coordinate match
            instead of the default fuzzy dedup radius, since a detail pin's
            whole point is to sit at its own precise coordinates near its
            parent, which the fuzzy radius would otherwise collapse onto the
            parent's own Location. A child pin is exempt from the
            one-root-pin-per-location rule, but not from the narrower rule that
            no two of a profile's pins may share an exact point.

    Returns:
        The created (or, for an idempotent replay, existing) pin plus every
        Location match at this point.

    Raises:
        PinCreationError: Neither coordinates nor a usable address were given,
            the address couldn't be geocoded, ``client_uuid`` is already used
            by a pin that isn't this profile's, ``parent_id`` doesn't match
            one of this profile's own pins, the profile already has a
            top-level pin at this exact location, or (for a child pin) it
            already has any pin at this exact point.
        PinCreationForbiddenError: An address needed geocoding but external lookups
            are turned off for this profile.
    """
    if client_uuid is not None:
        existing = Pin.objects.filter(profile=profile, uuid=client_uuid).select_related("location").first()
        if existing is not None:
            return PinCreationResult(pin=existing, all_locations=[existing.location], created=False)

    new_parent: Pin | None = None
    if parent_id is not None:
        new_parent = Pin.objects.filter(uuid=parent_id, profile=profile).first()
        if new_parent is None:
            raise PinCreationError("No such pin to set as parent.")
    # An unset coordinate arrives as None or "" (e.g. the map's blank hidden
    # input) - normalize both to None so the checks below can use `is None`
    # without treating a valid 0/0.0 coordinate (equator, prime meridian) as missing.
    if isinstance(latitude, str) and latitude.strip() == "":
        latitude = None
    if isinstance(longitude, str) and longitude.strip() == "":
        longitude = None

    if latitude is None or longitude is None:
        if not address:
            raise PinCreationError("No address or lat/lon provided.")
        if not profile.external_apis_enabled:
            raise PinCreationForbiddenError("External lookups are turned off in your settings - drop a pin on the map instead.")
        latitude, longitude = get_pin_by_address(address)
        if latitude is None or longitude is None:
            raise PinCreationError("Unable to convert address to lat/lng.")

    lat_f = float(latitude)
    lon_f = float(longitude)

    if new_parent is not None:
        # A child pin keeps its own exact point (and may not stack on another of
        # this profile's pins) - see resolve_child_pin_location.
        location = resolve_child_pin_location(profile, lat_f, lon_f, defaults={"official_name": place_canonical_name})
    else:
        # Fuzzy 50m-radius dedup (not exact-coordinate get_or_create): GPS jitter between two
        # drops at the same real place must consolidate onto the same Location, matching every
        # other top-level Location-resolution path in the app (viewset.py, import_data.py).
        location, _ = Location.objects.get_nearby_or_create(lat_f, lon_f, threshold_meters=50, defaults={"official_name": place_canonical_name})

    # Locations whose bounding box also covers this point - when more than one
    # matches, the caller offers the user a choice (see below).
    all_locations = list(Location.objects.get_all_for_point(lat_f, lon_f))

    from urbanlens.dashboard.models.wiki.model import Wiki

    create_kwargs: dict = {
        "name": name,
        # Creation/import is not an explicit rename.  In particular, file and
        # offline-client imports commonly put a coordinate or another parser
        # fallback in ``name``.  Marking every non-empty value as user-provided
        # made that placeholder permanently outrank names discovered later.
        # The flag is set by the rename endpoints when the owner deliberately
        # changes the name.
        "name_is_user_provided": False,
        "location": location,
        # Link to the place's community wiki when one already exists; wikis
        # are only ever created explicitly from the pin page.
        "wiki": Wiki.objects.get_for_location(location),
        "icon": icon,
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
        # Two constraints can fire here; both have well-defined answers:
        # - uuid collision: a concurrent retry of the same client_uuid won the
        #   race (return its pin - the idempotent outcome), or the uuid belongs
        #   to another profile's pin (reject; uuids are caller-generated, so
        #   this is either a caller bug or a guess - either way not theirs).
        # - one-root-pin-per-location-per-profile: surfaced as a clean 4xx-able
        #   error instead of the 500 an unhandled IntegrityError becomes.
        if client_uuid is not None:
            existing = Pin.objects.filter(profile=profile, uuid=client_uuid).select_related("location").first()
            if existing is not None:
                return PinCreationResult(pin=existing, all_locations=all_locations, created=False)
            if Pin.objects.filter(uuid=client_uuid).exists():
                raise PinCreationError("This uuid is already in use.") from exc
        if Pin.objects.filter(profile=profile, location=location, parent_pin__isnull=True).exists():
            raise PinCreationError("You already have a pin at this location.") from exc
        raise

    # visible_to keeps the id__in lookups from resolving another user's
    # private labels - a guessed foreign label id would otherwise attach (and
    # render the name of) someone else's label.
    if label_ids:
        pin.labels.set(Label.objects.location_labels().visible_to(profile).filter(id__in=label_ids))
    else:
        if tag_ids:
            pin.labels.remove(*pin.labels.filter(kind="tag"))
            pin.labels.add(*Label.objects.tags().visible_to(profile).filter(id__in=tag_ids))
        if category_ids:
            pin.labels.remove(*pin.labels.filter(kind="category"))
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
    # web-search results cache, so the pin detail page doesn't need to hit the
    # APIs on first load.
    from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

    if location and profile.external_apis_enabled:
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import (
            prefetch_location_external_data,
            refresh_pin_web_search,
        )

        safely_enqueue_task(prefetch_location_external_data, location.pk, google_place_id=google_place_id, profile_id=profile.pk)

        if user_has_feature(profile.user, SiteFeature.SEARCH):
            safely_enqueue_task(refresh_pin_web_search, pin.pk)

    if user_has_feature(profile.user, SiteFeature.AI):
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import suggest_pin_category

        safely_enqueue_task(suggest_pin_category, pin.pk)

    return PinCreationResult(pin=pin, all_locations=all_locations)
