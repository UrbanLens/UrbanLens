"""No owner-scoped route may serve, or accept a write from, someone who doesn't own it.

A sweep rather than a per-view test: authorization here is enforced view by view
(``get_object_or_404(..., profile__user=request.user)``, ``_get_group``,
``resolve_visible_wiki``, ...), so the failure mode is a *new* route that forgets
to do it. A per-view test only ever covers the views someone remembered to write
one for; this covers every route that exists, including the next one added.

It also catches routes wired to a method that isn't there - ``as_view({"get":
"..."})`` resolves the handler name at request time, so a typo'd or removed
handler is a 500 that nothing else notices until a user hits it. That is exactly
how the dead ``google_images`` route was found.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import NoReverseMatch, get_resolver, reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.safety.model import SafetyCheckin
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.models.wiki.model import Wiki

#: Routes that legitimately answer 200 to a non-owner, with the reason. Anything
#: added here needs to be justified: the whole point of the sweep is that the
#: default is "a stranger gets nothing".
_ALLOWED_200 = {
    # Searches the *caller's own* trips; the slug in the URL is only used to
    # exclude the parent trip from the caller's results, so it reveals nothing
    # about that trip and returns the caller's data either way.
    "trips.child_trip_search",
}


class CrossUserRouteAccessTests(TestCase):
    """Every owner-scoped route must refuse a logged-in stranger, and an anonymous visitor."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        owner = baker.make(User)
        self.intruder = baker.make(User)
        profile = owner.profile

        # Names must differ between the two users: slugs on these models are
        # scoped per-profile, so identically-named objects collide and the
        # "intruder" would simply be reaching their *own* row through a matching
        # slug - which reads as a leak but is correct behaviour.
        location = baker.make(Location, official_name="Victim Location")
        pin = baker.make(Pin, profile=profile, location=location, name="Victim Pin")
        baker.make(Wiki, location=location)
        trip = baker.make(Trip, creator=profile, name="Victim Trip")
        trip.profiles.add(profile)

        self.identifiers = {
            "pin_slug": pin.slug,
            "location_slug": location.slug,
            "trip_slug": trip.slug,
            "list_slug": baker.make(PinList, profile=profile, name="Victim List").slug,
            "checkin_slug": baker.make(SafetyCheckin, profile=profile, title="Victim Checkin").slug,
            "map_uuid": str(baker.make(MarkupMap, profile=profile).uuid),
            "filter_uuid": str(baker.make(SavedFilter, profile=profile).uuid),
            "label_id": baker.make(Label, profile=profile, kind="tag").pk,
            "image_id": Image.objects.create(pin=pin, profile=profile, location=location).pk,
        }

        # For the nested-route test: children owned by the victim, parents owned
        # by the intruder, so only the child-scoping check can reject the request.
        self.victim_activity = baker.make(TripActivity, trip=trip)
        self.victim_visit = baker.make(PinVisit, pin=pin)
        self.victim_alias = baker.make(PinAlias, pin=pin, name="victim-alias")

        intruder_profile = self.intruder.profile
        self.intruder_location = baker.make(Location, official_name="Intruder Location")
        self.intruder_pin = baker.make(Pin, profile=intruder_profile, location=self.intruder_location, name="Intruder Pin")
        self.intruder_trip = baker.make(Trip, creator=intruder_profile, name="Intruder Trip")
        self.intruder_trip.profiles.add(intruder_profile)

    def _owner_scoped_routes(self) -> dict[str, str]:
        """Map route name → its single parameter, for routes keyed by an owned object."""
        found: dict[str, str] = {}
        for name, entries in get_resolver().reverse_dict.lists():
            if not isinstance(name, str) or name in _ALLOWED_200:
                continue
            params = entries[0][0][0][1]
            if len(params) == 1 and params[0] in self.identifiers:
                found[name] = params[0]
        return found

    def _urls(self) -> list[tuple[str, str]]:
        """Return ``(route_name, url)`` for every discovered owner-scoped route."""
        routes = self._owner_scoped_routes()
        # Guards the sweep itself: a refactor that breaks route discovery would
        # otherwise make every test here vacuously pass.
        self.assertGreater(len(routes), 100, "route discovery found suspiciously few routes")
        urls = []
        for name, param in sorted(routes.items()):
            try:
                urls.append((name, reverse(name, kwargs={param: self.identifiers[param]})))
            except NoReverseMatch:
                continue
        return urls

    def test_a_stranger_cannot_read_another_users_objects(self) -> None:
        self.client.force_login(self.intruder)

        leaks: list[str] = []
        for name, url in self._urls():
            try:
                status = self.client.get(url).status_code
            except Exception as exc:
                leaks.append(f"{name} GET raised {type(exc).__name__}: {exc}")
                continue
            if status == 200:
                leaks.append(f"{name} GET returned 200 to a non-owner")

        self.assertEqual(leaks, [], "\n".join(leaks))

    def test_a_stranger_cannot_mutate_another_users_objects(self) -> None:
        """POST/DELETE as a stranger must never report success.

        ``204 No Content`` with an empty body is this codebase's HTMX idiom for
        "nothing to swap", and several custom-field views use it as their
        *refusal* (see ``PhotoCustomFieldsView._resolve``: a non-owner resolves
        the object to None and the view returns 204 without writing). So an empty
        204 is accepted here; any other 2xx means the write went through.
        """
        self.client.force_login(self.intruder)

        leaks: list[str] = []
        for name, url in self._urls():
            for verb in ("post", "delete"):
                try:
                    response = getattr(self.client, verb)(url)
                except Exception as exc:
                    leaks.append(f"{name} {verb.upper()} raised {type(exc).__name__}: {exc}")
                    continue
                if 200 <= response.status_code < 300 and not (response.status_code == 204 and not response.content):
                    leaks.append(f"{name} {verb.upper()} returned {response.status_code} to a non-owner")

        self.assertEqual(leaks, [], "\n".join(leaks))

    def test_a_child_id_cannot_be_smuggled_through_the_callers_own_parent(self) -> None:
        """Nested routes must re-scope the child to the parent in the URL.

        The parent ownership check passes here on purpose - the caller owns the
        pin/trip/location in the URL. What must not pass is the *child*: an
        activity, visit or alias belonging to someone else. A view that resolves
        the child by id alone (``get_object_or_404(TripActivity, pk=...)``
        without ``trip=trip``) acts on another user's row while looking properly
        authorized.
        """
        parents = {
            "pin_slug": self.intruder_pin.slug,
            "trip_slug": self.intruder_trip.slug,
            "location_slug": self.intruder_location.slug,
        }
        children = {
            "activity_id": self.victim_activity.pk,
            "visit_id": self.victim_visit.pk,
            "alias_id": self.victim_alias.pk,
        }

        nested = []
        for name, entries in get_resolver().reverse_dict.lists():
            if not isinstance(name, str):
                continue
            params = list(entries[0][0][0][1])
            if len(params) == 2 and params[0] in parents and params[1] in children:
                nested.append((name, params))
        self.assertGreater(len(nested), 10, "nested-route discovery found suspiciously few routes")

        self.client.force_login(self.intruder)
        leaks: list[str] = []
        for name, params in sorted(nested):
            try:
                url = reverse(name, kwargs={params[0]: parents[params[0]], params[1]: children[params[1]]})
            except NoReverseMatch:
                continue
            for verb in ("get", "post", "delete"):
                try:
                    response = getattr(self.client, verb)(url)
                except Exception as exc:
                    leaks.append(f"{name} {verb.upper()} raised {type(exc).__name__}: {exc}")
                    continue
                if 200 <= response.status_code < 300 and not (response.status_code == 204 and not response.content):
                    leaks.append(f"{name} {verb.upper()} accepted another user's {params[1]} ({response.status_code})")

        self.assertEqual(leaks, [], "\n".join(leaks))

    def test_an_anonymous_visitor_cannot_read_owner_scoped_routes(self) -> None:
        """Catches a route that forgets ``LoginRequiredMixin`` entirely."""
        self.client.logout()

        leaks: list[str] = []
        for name, url in self._urls():
            try:
                status = self.client.get(url).status_code
            except Exception as exc:
                leaks.append(f"{name} GET raised {type(exc).__name__}: {exc}")
                continue
            if status == 200:
                leaks.append(f"{name} GET returned 200 to an anonymous visitor")

        self.assertEqual(leaks, [], "\n".join(leaks))
