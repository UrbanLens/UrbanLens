"""Server-side page context resolution (plan §9, batch 3).

The client sends only its own URL path (``location.pathname``, query string
stripped server-side) - never DOM text, never an object id beyond what the
URL itself already names. :func:`resolve_page_context` resolves that path
the same way Django would for the request that rendered it
(``django.urls.resolve()``), then re-runs the *same* access check the page's
own view would before returning an object id - a spoofed or unresolvable
path resolves to nothing (never an error), and a URL naming an object the
requesting profile can't see resolves to nothing too, never that object.
Both failure modes look identical to a caller: ``None``.

Only a handful of pages are wired up so far - the ones with an existing,
already-tested access-check entry point this module can call straight
through (:func:`Pin.objects.by_profile`, :func:`get_trip_for_viewer`). Pages
whose access check needs researching first (wikis - the plan names
``visible_wiki_location_ids_cached``, which lives on a queryset this module
hasn't audited yet) are deliberately not registered here; ``resolve_page_context``
already returns ``None`` for anything unregistered, so adding one later is
purely additive.

Nothing here is wired to any tool yet: no shipped tool declares
``needs_page=True`` on its :class:`~services.ai.tools.registry.ToolSpec`
(that starts in batch 4, per the plan). This module - and getting its result
onto :attr:`~services.ai.tools.registry.ToolContext.page` - exists so a page
tool, when one ships, only has to ask "what kind of object is context.page"
rather than waiting on this resolution machinery to be built too.
"""

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
        url_name: The resolved Django URL name (e.g. ``"pin.details"``).
        page_help_key: Key into ``services.ai.page_help.PAGE_HELP`` (batch 4)
            - carried here now so that module's registration doesn't also
            need to touch this one.
        object: The page's own object, re-loaded under the requesting
            profile's access rules, or ``None`` for a page with none (the
            map) or where the URL's own object doesn't exist / isn't visible
            to this profile.
    """

    url_name: str
    page_help_key: str
    object: PageObject | None = None


class _ObjectLoader(Protocol):
    def __call__(self, profile: Profile, kwargs: dict[str, str]) -> PageObject | None: ...


@dataclass(frozen=True, slots=True)
class _PageResolver:
    page_help_key: str
    #: None for a page with no single object of its own (e.g. the map).
    load_object: _ObjectLoader | None = None
    #: The "kind" load_object's own PageObject carries, when load_object is
    #: set. Must have a matching _EXISTENCE_CHECKS entry - a mismatch means
    #: verify_page_object always refuses this page's object, silently
    #: dropping it on every turn (see test_every_resolver_with_an_object_kind_has_a_verification_check).
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
    "map.view": _PageResolver(page_help_key="map"),
    "pin.details": _PageResolver(page_help_key="pin_detail", load_object=_load_pin, object_kind="pin"),
    "trips.detail": _PageResolver(page_help_key="trip_detail", load_object=_load_trip, object_kind="trip"),
}


def resolve_page_context(path: str, profile: Profile) -> PageContext | None:
    """Resolve a client-sent ``location.pathname`` into a :class:`PageContext`.

    Args:
        path: The client's current path. Any query string is stripped before
            resolution - this module only ever trusts the path itself.
        profile: The requesting profile - every object load is scoped to it,
            exactly as the real page's own view would scope it.

    Returns:
        The resolved context, or ``None`` when the path doesn't resolve to a
        known Django URL, resolves to a page this module has no entry for, or
        names an object that either doesn't exist or isn't visible to
        ``profile``. All of these read identically to a caller - the point is
        that a spoofed path can't be told apart from a merely unsupported one.
    """
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
    return PageContext(url_name=url_name, page_help_key=resolver.page_help_key, object=page_object)


def _pin_still_visible(profile: Profile, obj_id: int) -> bool:
    from urbanlens.dashboard.models.pin.model import Pin

    return Pin.objects.by_profile(profile).filter(pk=obj_id).exists()


def _trip_still_visible(profile: Profile, obj_id: int) -> bool:
    # Mirrors get_trip_for_viewer's own access check exactly (creator OR
    # membership) - Trip.objects.for_list_page(profile) looked equivalent
    # but isn't: it missed a trip the profile created but never joined as a
    # member, which get_trip_for_viewer (and so _load_trip above) allows.
    from urbanlens.dashboard.models.trips.model import Trip, TripMembership

    trip = Trip.objects.filter(pk=obj_id).first()
    if trip is None:
        return False
    return trip.creator_id == profile.id or TripMembership.objects.for_trip_and_profile(trip, profile).exists()


#: kind -> "does this profile still have access to this id" check, used by
#: :func:`verify_page_object`. Each entry re-applies the same profile-scoping
#: filter its loader above uses, but by id rather than by URL kwargs, since
#: verification only ever has the id round-tripped through a task queue.
_EXISTENCE_CHECKS: dict[str, Callable[[Profile, int], bool]] = {
    "pin": _pin_still_visible,
    "trip": _trip_still_visible,
}


def verify_page_object(profile: Profile, page_object: PageObject) -> bool:
    """Re-confirm that ``profile`` may still see ``page_object``.

    The web view resolves a turn's page once, before enqueueing, and only
    ``{kind, id}`` round-trips through the task queue to ``ai-worker`` (see
    ``services.ai.tasks.run_assistant_turn_task``) - never the raw URL path,
    and never anything the loader itself derived beyond that id. This is the
    task's own check on that id, scoped by the *current* task's profile, so a
    queue payload can't smuggle access to an object that profile can't (or
    can no longer) see - not by trusting the web view's earlier resolution,
    and not by skipping verification because "the web view already checked".

    Args:
        profile: The task's own resolved profile.
        page_object: The ``{kind, id}`` pair carried by the queue payload.

    Returns:
        True if ``profile`` may still see this object. False for an unknown
        ``kind`` (a payload this version of the code doesn't recognize) as
        well as a real access failure - both mean "don't use this".
    """
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
