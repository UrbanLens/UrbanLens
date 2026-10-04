"""P236: a slug-addressed URL must cost the same for something the requester cannot see as for nothing at all.

Each test compares the SQL a request runs for a slug no row has ever used with the SQL it runs for a row the requester
may not reach. Any difference - a query more, a query fewer, a different statement - is a timing side channel that
tells an authenticated user the row exists.
"""

from __future__ import annotations

import re
import uuid

from django.contrib.auth.models import User
from django.db import connection, reset_queries
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.location.slug_history import LocationSlugHistory
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

CURRENT = "hudson-river-psychiatric-center"
FORMER = "hudson-river-state-hospital"
NEVER_USED = "sleepy-hollow-grain-elevator"

_SAVEPOINT = re.compile(r'"?s\d+_x\d+"?')
_EXTRA_ARGS = {"int": 1, "str": "x", "slug": "x", "uuid": "00000000-0000-4000-8000-000000000000"}
#: Values for the named groups of regex-routed prefixes, which carry no converter to read a type from.
_REGEX_ARGS = {"label_kind": "tags"}


def _shape(queries: list[dict], probe: str) -> list[str]:
    """The statements a request ran, with the probed slug (or uuid, which Postgres is sent undashed) and savepoint
    names made comparable."""
    shapes = []
    for query in queries:
        sql = query["sql"].replace(probe, "<probe>").replace(probe.replace("-", ""), "<probe>")
        shapes.append(_SAVEPOINT.sub("<savepoint>", sql))
    return shapes


def _location_slug_routes() -> list[tuple[str, dict[str, object]]]:
    """Every named web route addressed by ``location_slug``, with stand-in values for its other arguments."""
    found: dict[str, dict[str, object]] = {}

    def walk(patterns, prefix: str) -> None:
        for entry in patterns:
            route = prefix + str(entry.pattern)
            if isinstance(entry, URLResolver):
                if entry.namespace is None:
                    walk(entry.url_patterns, route)
            elif (
                isinstance(entry, URLPattern)
                and entry.name
                and "<slug:location_slug>" in route
                and "pin_slug" not in route
            ):
                extra = {
                    name: _EXTRA_ARGS[kind]
                    for kind, name in re.findall(r"<(\w+):(\w+)>", route)
                    if name != "location_slug"
                }
                extra |= {name: _REGEX_ARGS[name] for name in re.findall(r"\(\?P<(\w+)>", route)}
                found[entry.name] = extra

    walk(get_resolver().url_patterns, "")
    return sorted(found.items())


class _HiddenLocationFixture(TestCase):
    """A Location someone else pinned, moved off its first slug, and a requester who has pinned elsewhere."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        owner = baker.make(User)
        self.location = Location.objects.create(
            latitude=41.7,
            longitude=-73.9,
            official_name="Hudson River State Hospital",
            official_name_source="wikipedia",
        )
        self.assertEqual(self.location.slug, FORMER)
        self.location.slug = CURRENT
        self.location.save(update_fields=["slug", "updated"])
        self.assertTrue(LocationSlugHistory.objects.filter(location=self.location, slug=FORMER).exists())
        self.wiki = baker.make(Wiki, location=self.location, name="Hudson River State Hospital")
        baker.make(Pin, profile=owner.profile, location=self.location)

        self.user = baker.make(User)
        self.profile = self.user.profile
        own_place = Place.objects.create(kind=PlaceKind.PARCEL)
        own_location = Location.objects.create(latitude=40.1, longitude=-75.1, place=own_place)
        self.own_pin = baker.make(Pin, profile=self.profile, location=own_location, name="My Spot")
        self.own_pin.ensure_slug()
        self.client.force_login(self.user)

    def _probe(self, method: str, url_for, probe: str, **extra) -> tuple[int, list[str]]:
        """Run one request for *probe* and return its status and statement shapes."""
        url = url_for(probe)
        # The capture indexes into a log capped at 9000 entries, which this many requests overrun.
        reset_queries()
        with CaptureQueriesContext(connection) as queries:
            response = getattr(self.client, method)(url, **extra)
        return response.status_code, _shape(queries.captured_queries, probe)

    def _assert_indistinguishable(self, method: str, url_for, **extra) -> None:
        self._probe(method, url_for, "warm-up-slug", **extra)
        missing = self._probe(method, url_for, NEVER_USED, **extra)
        for label, probe in (("current slug", CURRENT), ("former slug", FORMER)):
            with self.subTest(probe=label):
                self.assertEqual(self._probe(method, url_for, probe, **extra), missing)
        hidden_uuid = self._probe(method, url_for, str(self.location.uuid), **extra)
        with self.subTest(probe="uuid"):
            self.assertEqual(hidden_uuid, self._probe(method, url_for, str(uuid.uuid4()), **extra))


class WikiRouteSideChannelTests(_HiddenLocationFixture):
    def test_the_route_list_is_not_empty(self) -> None:
        """Guards the sweep below against passing over nothing."""
        names = [name for name, _extra in _location_slug_routes()]

        self.assertIn("location.wiki", names)
        self.assertIn("location.detail", names)
        self.assertIn("location.wiki.detail_pins.json", names)
        self.assertGreater(len(names), 50)

    def test_every_wiki_route_costs_the_same_for_a_hidden_location_as_for_none(self) -> None:
        for name, extra in _location_slug_routes():
            for method in ("get", "post"):
                with self.subTest(route=name, method=method):
                    self._assert_indistinguishable(
                        method,
                        lambda probe, name=name, extra=extra: reverse(name, kwargs={"location_slug": probe, **extra}),
                    )

    def test_the_hidden_location_is_still_a_404(self) -> None:
        for probe in (CURRENT, FORMER, str(self.location.uuid)):
            with self.subTest(probe=probe):
                self.assertEqual(self.client.get(reverse("location.wiki", args=[probe])).status_code, 404)

    def test_relinking_a_pin_costs_the_same_for_a_hidden_location_as_for_none(self) -> None:
        self._assert_indistinguishable(
            "post", lambda probe: reverse("pin.link.to", kwargs={"pin_slug": self.own_pin.slug, "location_slug": probe})
        )


class ExternalApiWikiSideChannelTests(_HiddenLocationFixture):
    def test_the_external_wiki_routes_cost_the_same_for_a_hidden_location_as_for_none(self) -> None:
        _key, raw_key = generate_api_key(self.user, "Side channel client")
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.WIKI_READ.value, ApiKeyScope.WIKI_WRITE.value])
        self.client.logout()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}

        for path in ("", "history/", "aliases/"):
            with self.subTest(path=path):
                self._assert_indistinguishable(
                    "get", lambda probe, path=path: f"/dashboard/api/external/v1/wikis/{probe}/{path}", **auth
                )


class MarkupBodySlugSideChannelTests(_HiddenLocationFixture):
    def test_a_markup_map_s_location_slug_costs_the_same_for_a_hidden_location_as_for_none(self) -> None:
        from urbanlens.dashboard.controllers.markup import _resolve_title_context

        request = RequestFactory().post("/")
        request.user = self.user

        def shape(probe: str) -> tuple[object, list[str]]:
            with CaptureQueriesContext(connection) as queries:
                target = _resolve_title_context(request, {"location_slug": probe})
            return target, _shape(queries.captured_queries, probe)

        shape("warm-up-slug")
        missing = shape(NEVER_USED)
        self.assertIsNone(missing[0])
        self.assertEqual(shape(CURRENT), missing)
        self.assertEqual(shape(FORMER), missing)


TRIP_NAME = "Catskills Weekend"


class TripSideChannelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        creator = baker.make(User)
        self.trip = baker.make(Trip, creator=creator.profile, name=TRIP_NAME)
        self.trip.ensure_slug()
        baker.make(TripMembership, trip=self.trip, profile=creator.profile, status=TripMembership.STATUS_JOINED)
        self.user = baker.make(User)
        self.profile = self.user.profile
        mine = baker.make(Trip, creator=self.profile, name="My Own Trip")
        mine.ensure_slug()
        self.client.force_login(self.user)

    def _shape(self, call, probe: str) -> tuple[object, list[str]]:
        with CaptureQueriesContext(connection) as queries:
            outcome = call(probe)
        return outcome, _shape(queries.captured_queries, probe)

    def test_a_trip_page_costs_the_same_for_someone_else_s_trip_as_for_none(self) -> None:
        def status(probe: str) -> int:
            return self.client.get(reverse("trips.detail", args=[probe])).status_code

        self._shape(status, "warm-up-slug")
        missing = self._shape(status, "no-such-trip-anywhere")
        self.assertEqual(missing[0], 404)
        self.assertEqual(self._shape(status, self.trip.slug), missing)

    def test_the_trip_lookup_costs_the_same_for_someone_else_s_trip_as_for_none(self) -> None:
        from urbanlens.dashboard.services.trips.trip_access import get_trip_for_viewer
        from urbanlens.dashboard.services.trips.trip_errors import TripNotFoundError

        def outcome(probe: str) -> str:
            try:
                get_trip_for_viewer(probe, self.profile)
            except TripNotFoundError as error:
                return str(error)
            return "found"

        missing = self._shape(outcome, "no-such-trip-anywhere")
        self.assertEqual(self._shape(outcome, self.trip.slug), missing)

    def test_a_check_in_s_trip_costs_the_same_for_someone_else_s_trip_as_for_none(self) -> None:
        from django.http import Http404

        from urbanlens.dashboard.controllers.safety import _resolve_checkin_trip

        def outcome(probe: str) -> str:
            try:
                _resolve_checkin_trip(self.profile, probe)
            except Http404:
                return "404"
            return "found"

        missing = self._shape(outcome, "no-such-trip-anywhere")
        self.assertEqual(missing[0], "404")
        self.assertEqual(self._shape(outcome, self.trip.slug), missing)

    def test_a_member_still_reaches_the_trip(self) -> None:
        from urbanlens.dashboard.services.trips.trip_access import get_trip_for_viewer

        baker.make(TripMembership, trip=self.trip, profile=self.profile, status=TripMembership.STATUS_INVITED)

        self.assertEqual(get_trip_for_viewer(self.trip.slug, self.profile), self.trip)
        self.assertEqual(self.client.get(reverse("trips.detail", args=[self.trip.slug])).status_code, 200)
