"""Server-side page context resolution (plan §9, batch 3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.urls import Resolver404, resolve

if TYPE_CHECKING:
    from collections.abc import Callable

    from urbanlens.dashboard.models.profile.model import Profile


@dataclass(frozen=True, slots=True)
class PageObject:
    """One object the resolved page is about - e.g. the pin on its detail page."""

    kind: str
    id: int


@dataclass(frozen=True, slots=True)
class PageContext:
    """What :func:`resolve_page_context` returns for a URL path.

    Attributes:
        url_name: The resolved Django URL name (e.g. ``"pin.details"``) - also the lookup key into ``services.ai.page_help.PAGE_HELP`` (batch 4's ``get_page_help`` tool), so no separate key is kept here.
        object: The page's own object, re-loaded under the requesting profile's access rules, or ``None`` for a page with none (the map) or where the URL's own object doesn't exist / isn't visible to this profile."""

    url_name: str
    object: PageObject | None = None


class _ObjectLoader(Protocol):
    def __call__(self, profile: Profile, kwargs: dict[str, str]) -> PageObject | None: ...


@dataclass(frozen=True, slots=True)
class _PageResolver:
    #: None for a page with no single object of its own (e.g. the map).
    load_object: _ObjectLoader | None = None
    #: The "kind" load_object's own PageObject carries, when load_object is set.
    #: Must have a matching _EXISTENCE_CHECKS entry - a mismatch means verify_page_object always
    #: refuses this page's object, silently dropping it on every turn (see
    #: test_every_resolver_with_an_object_kind_has_a_verification_check).
    object_kind: str | None = None


def _load_pin(profile: Profile, kwargs: dict[str, str]) -> PageObject | None:
    from urbanlens.dashboard.models.pin.model import Pin

    pin_slug = kwargs.get("pin_slug")
    if not pin_slug:
        return None
    pin = Pin.objects.by_profile(profile).filter(slug=pin_slug).first()
    if pin is None:
        # pin.details accepts a uuid fallback too (see controllers.pin.PinController.view) -
        # mirrored here so a page using that fallback still resolves.
        try:
            pin = Pin.objects.by_profile(profile).filter(uuid=pin_slug).first()
        except (ValueError, ValidationError):
            pin = None
    return None if pin is None else PageObject(kind="pin", id=pin.pk)


def _load_trip(profile: Profile, kwargs: dict[str, str]) -> PageObject | None:
    from urbanlens.dashboard.services.trips.trip_access import get_trip_for_viewer
    from urbanlens.dashboard.services.trips.trip_errors import TripNotFoundError

    trip_slug = kwargs.get("trip_slug")
    if not trip_slug:
        return None
    try:
        trip = get_trip_for_viewer(trip_slug, profile)
    except TripNotFoundError:
        return None
    return PageObject(kind="trip", id=trip.pk)


#: url_name -> resolver. See the module docstring for why this list is short.
_RESOLVERS: dict[str, _PageResolver] = {
    "map.view": _PageResolver(),
    "pin.details": _PageResolver(load_object=_load_pin, object_kind="pin"),
    "trips.detail": _PageResolver(load_object=_load_trip, object_kind="trip"),
}


def resolve_page_context(path: str, profile: Profile) -> PageContext | None:
    """Resolve a client-sent ``location.pathname`` into a :class:`PageContext`.

    Args:
        path: The client's current path.
        profile: The requesting profile - every object load is scoped to it, exactly as the real page's own view would scope it.

    Returns:
        The resolved context, or ``None`` when the path doesn't resolve to a known Django URL, resolves to a page this module has no entry for, or names an object that either doesn't exist or isn't visible to ``profile``."""
    clean_path = urlsplit(path).path or "/"
    try:
        match = resolve(clean_path)
    except Resolver404:
        return None
    url_name = match.url_name or ""
    resolver = _RESOLVERS.get(url_name)
    if resolver is None:
        return None
    page_object = resolver.load_object(profile, match.kwargs) if resolver.load_object else None
    return PageContext(url_name=url_name, object=page_object)


def _pin_still_visible(profile: Profile, obj_id: int) -> bool:
    from urbanlens.dashboard.models.pin.model import Pin

    return Pin.objects.by_profile(profile).filter(pk=obj_id).exists()


def _trip_still_visible(profile: Profile, obj_id: int) -> bool:
    # Mirrors get_trip_for_viewer's own access check exactly (creator OR membership) -
    # Trip.objects.for_list_page(profile) looked equivalent but isn't: it missed a trip the profile
    # created but never joined as a member, which get_trip_for_viewer (and so _load_trip above)
    # allows.
    from urbanlens.dashboard.models.trips.model import Trip, TripMembership

    trip = Trip.objects.filter(pk=obj_id).first()
    if trip is None:
        return False
    return trip.creator_id == profile.id or TripMembership.objects.for_trip_and_profile(trip, profile).exists()


#: kind -> "does this profile still have access to this id" check, used by
#: :func:`verify_page_object`.
#: Each entry re-applies the same profile-scoping filter its loader above uses, but by id rather than
#: by URL kwargs, since verification only ever has the id round-tripped through a task queue.
_EXISTENCE_CHECKS: dict[str, Callable[[Profile, int], bool]] = {
    "pin": _pin_still_visible,
    "trip": _trip_still_visible,
}


def verify_page_object(profile: Profile, page_object: PageObject) -> bool:
    """Re-confirm that ``profile`` may still see ``page_object``.

    Args:
        profile: The task's own resolved profile.
        page_object: The ``{kind, id}`` pair carried by the queue payload.

    Returns:
        True if ``profile`` may still see this object."""
    check = _EXISTENCE_CHECKS.get(page_object.kind)
    return False if check is None else check(profile, page_object.id)


def page_object_to_dict(page_object: PageObject | None) -> dict[str, Any] | None:
    """The JSON-safe shape of ``page_object`` for a Celery task's queue payload."""
    return None if page_object is None else {"kind": page_object.kind, "id": page_object.id}


def page_object_from_dict(data: dict[str, Any] | None) -> PageObject | None:
    """The inverse of :func:`page_object_to_dict` - ``None`` for anything malformed, never a raise."""
    if not isinstance(data, dict):
        return None
    try:
        return PageObject(kind=str(data["kind"]), id=int(data["id"]))
    except (KeyError, TypeError, ValueError):
        return None
