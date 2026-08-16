"""No write route may answer a well-formed-but-minimal request with a server error.

The complement to ``test_cross_user_route_access.py``. That sweep asks whether a
*stranger* gets in; it flags only ``200``, so a route that crashes answers 500
and passes it silently. This one logs in as the **owner** - the case where a
route runs its real logic instead of short-circuiting on permission - posts a
minimal body, and asserts the response is not a 5xx.

The property is deliberately weak, because a generic sweep cannot know what any
given route is supposed to *do*. 400, 403, 404, 405, 409 all pass: refusing a
request with no payload is correct behaviour. Only "this request made the server
throw" fails. That is exactly the class of bug this exists for - the
detach-location 500 (PROBLEMS.md, 2026-08-13) survived because its route had no
test at all while its sibling did, and a single request would have caught it.

This is the strategy the ~187-untested-write-routes entry says is missing:
closing that gap one route at a time is not a strategy, but one *property*
asserted across all of them at once is.

**Routes that legitimately need the network** are excluded by exception type, not
by name: the suite's guard raises a ``RuntimeError`` naming external access, and
a route reaching for an API it is not allowed to call in tests is a fact about
the test environment rather than a defect. Anything else that raises is a
finding.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import NoReverseMatch, get_resolver, reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.safety.model import SafetyCheckin
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.models.trips.model import Trip

#: Marker text from ``core.testing_network``. A route that trips the guard is
#: reaching for a real integration; that is the environment, not a bug.
_NETWORK_GUARD_MARKER = "External network access is disabled during tests"

#: Methods worth sweeping. GET is already covered by the cross-user sweep.
_WRITE_METHODS = ("post", "delete")

#: Routes known to crash, with the reason. Exactly one, and it is the route that
#: motivated this whole sweep: ``pin.link`` (detach a pin from its shared
#: Location) raises ``IntegrityError`` every time, because it creates a
#: ``Location`` at coordinates a ``Location`` already occupies and that pair is
#: unique. That is an open **product decision**, not an oversight - see
#: PROBLEMS.md, "detach location on a pin fails with a 500", which lists three
#: defensible fixes and declines to pick one - and it already has a strict
#: ``xfail`` test of its own in ``test_pin_detach_location.py``.
#:
#: Kept honest by ``test_the_known_crash_is_still_crashing``: if the product
#: decision is made and the route fixed, that test fails and says to delete this
#: entry. An exemption nobody re-checks is how an allowlist rots into a blindfold.
_KNOWN_CRASHES = {"pin.link"}


class WriteRouteSmokeTests(TestCase):
    """Every owner-scoped write route refuses a minimal request rather than crashing."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        profile = self.user.profile

        location = baker.make(Location, official_name="Smoke Location")
        pin = baker.make(Pin, profile=profile, location=location, name="Smoke Pin")
        trip = baker.make(Trip, creator=profile, name="Smoke Trip")
        trip.profiles.add(profile)

        self.identifiers = {
            "pin_slug": pin.slug,
            "location_slug": location.slug,
            "trip_slug": trip.slug,
            "list_slug": baker.make(PinList, profile=profile, name="Smoke List").slug,
            "checkin_slug": baker.make(SafetyCheckin, profile=profile, title="Smoke Checkin").slug,
            "map_uuid": str(baker.make(MarkupMap, profile=profile).uuid),
            "filter_uuid": str(baker.make(SavedFilter, profile=profile).uuid),
            "label_id": baker.make(Label, profile=profile, kind="tag").pk,
        }

    def _write_routes(self) -> list[tuple[str, str]]:
        """``(route name, url)`` for every single-parameter owner-scoped route."""
        urls: list[tuple[str, str]] = []
        for name, entries in get_resolver().reverse_dict.lists():
            if not isinstance(name, str):
                continue
            params = entries[0][0][0][1]
            if len(params) != 1 or params[0] not in self.identifiers:
                continue
            try:
                urls.append((name, reverse(name, kwargs={params[0]: self.identifiers[params[0]]})))
            except NoReverseMatch:
                continue
        return sorted(urls)

    def _crashing_routes(self) -> dict[str, str]:
        """Route name → how it crashed, for every swept write route that did."""
        crashes: dict[str, str] = {}
        for name, url in self._write_routes():
            for method in _WRITE_METHODS:
                try:
                    response = getattr(self.client, method)(url, data={})
                except Exception as exc:  # noqa: BLE001 - classifying, then re-reporting
                    if _NETWORK_GUARD_MARKER in str(exc):
                        continue
                    crashes[name] = f"[{method.upper()}] raised {type(exc).__name__}: {str(exc)[:160]}"
                    continue
                if response.status_code >= 500:
                    crashes[name] = f"[{method.upper()}] returned {response.status_code}"
        return crashes

    def test_no_write_route_answers_a_minimal_request_with_a_server_error(self) -> None:
        unexpected = {name: how for name, how in self._crashing_routes().items() if name not in _KNOWN_CRASHES}

        self.assertEqual(unexpected, {}, "write routes crashed on a minimal request:\n" + "\n".join(f"{n} {h}" for n, h in unexpected.items()))

    def test_the_known_crash_is_still_crashing(self) -> None:
        """Stops the exemption above outliving the bug it describes.

        When the detach-location product decision is finally made, this fails and
        says so, rather than leaving a permanently-excused route in the sweep.
        """
        still_broken = set(self._crashing_routes()) & _KNOWN_CRASHES

        self.assertEqual(
            still_broken,
            _KNOWN_CRASHES,
            f"these routes no longer crash and should be removed from _KNOWN_CRASHES: {sorted(_KNOWN_CRASHES - still_broken)}",
        )

    # -- guard the guard ----------------------------------------------------

    def test_the_sweep_reaches_a_useful_number_of_routes(self) -> None:
        """A discovery refactor that matched nothing would make the sweep vacuous."""
        self.assertGreater(len(self._write_routes()), 100, "route discovery found suspiciously few routes")

    def test_the_sweep_actually_exercises_the_routes(self) -> None:
        """Proves requests are really issued: at least some must answer 2xx/3xx/4xx."""
        answered = 0
        for _name, url in self._write_routes()[:25]:
            try:
                if self.client.post(url, data={}).status_code < 500:
                    answered += 1
            except Exception:  # noqa: BLE001, S110 - counted by absence, not re-raised
                continue
        self.assertGreater(answered, 0, "no route answered at all - the sweep is not issuing real requests")
