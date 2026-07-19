"""Shared pin-creation logic.

This is the single code path behind every way a Pin gets created on a user's
behalf: the map UI's "Add pin" flow (``controllers.maps.MapController.post_add_pin``)
and the external API's pin-creation endpoint (``external_api.views.PinCreateView``).
Keeping it in one place means a validation rule, sanitization step, or piece of
enrichment added for one caller automatically applies to the other - there is
no second, slightly-different pin-creation path for a third-party app to slip
untrusted data through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.locations.geocoding import get_pin_by_address

if TYPE_CHECKING:
    from collections.abc import Sequence

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


@dataclass(slots=True)
class PinCreationResult:
    """The Pin created plus any other Locations whose bounding box also covers the point."""

    pin: Pin
    #: Every Location match at this point, including ``pin.location`` itself -
    #: callers only need to act on this when there's more than one, meaning the
    #: point is ambiguous between two or more distinct places.
    all_locations: list[Location] = field(default_factory=list)


def create_pin_for_profile(
    profile: Profile,
    *,
    name: str | None = None,
    latitude: float | str | None = None,
    longitude: float | str | None = None,
    address: str | None = None,
    icon: str | None = None,
    color: str | None = None,
    custom_icon: UploadedFile | None = None,
    label_ids: Sequence[str] = (),
    tag_ids: Sequence[str] = (),
    category_ids: Sequence[str] = (),
    google_place_id: str | None = None,
    place_canonical_name: str | None = None,
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
        custom_icon: An uploaded custom icon image.
        label_ids: Label ids to attach directly (takes precedence over tag_ids/category_ids).
        tag_ids: Tag-kind label ids to attach when ``label_ids`` wasn't given.
        category_ids: Category-kind label ids to attach when ``label_ids`` wasn't given.
        google_place_id: A Google Place id to link on both the pin and location.
        place_canonical_name: Canonical name to seed a newly-created Location with.

    Returns:
        The created pin plus every Location match at this point.

    Raises:
        PinCreationError: Neither coordinates nor a usable address were given,
            or the address couldn't be geocoded.
        PinCreationForbiddenError: An address needed geocoding but external lookups
            are turned off for this profile.
    """
    if not latitude or not longitude:
        if not address:
            raise PinCreationError("No address or lat/lon provided.")
        if not profile.external_apis_enabled:
            raise PinCreationForbiddenError("External lookups are turned off in your settings - drop a pin on the map instead.")
        latitude, longitude = get_pin_by_address(address)
        if not latitude or not longitude:
            raise PinCreationError("Unable to convert address to lat/lng.")

    lat_f = float(latitude)
    lon_f = float(longitude)

    location, _ = Location.objects.get_or_create(latitude=lat_f, longitude=lon_f, defaults={"official_name": place_canonical_name})

    # Locations whose bounding box also covers this point - when more than one
    # matches, the caller offers the user a choice (see below).
    all_locations = list(Location.objects.get_all_for_point(lat_f, lon_f))

    from urbanlens.dashboard.models.wiki.model import Wiki

    pin = Pin.objects.create(
        name=name,
        name_is_user_provided=bool((name or "").strip()),
        location=location,
        # Link to the place's community wiki when one already exists; wikis
        # are only ever created explicitly from the pin page.
        wiki=Wiki.objects.get_for_location(location),
        icon=icon,
        custom_icon=custom_icon,
        color=color,
        profile=profile,
    )

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
