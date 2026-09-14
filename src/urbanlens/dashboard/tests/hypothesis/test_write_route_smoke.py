"""No write route may answer a well-formed-but-minimal request with a server error."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import transaction
from django.urls import NoReverseMatch, get_resolver, reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
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

#: Methods swept. GET is included despite `test_cross_user_route_access.py`
#: already requesting every route, because that sweep asserts a *different*
#: property: it flags only `200` to a stranger, so a GET that crashes answers
#: 500 and passes it silently - the identical blind spot that justified building
#: this file for writes.
_WRITE_METHODS = ("get", "post", "delete")

#: Routes known to crash, with the reason. **Empty**, and the way it emptied is the point: it held exactly one
#: entry, ``pin.link`` (detach a pin from its shared Location), excused as an open product decision.
#: Keep that property if anything is ever added here.
_KNOWN_CRASHES: set[str] = set()

#: Routes skipped because exercising them would sabotage the sweep itself rather
#: than test anything - not because they are excused. Deliberately tiny: every
#: other side effect (creating rows, deleting objects, starting jobs) is fine in
#: a test database and is exactly what we want executed.
_SKIP_ROUTES = {
    # Ends the session; every subsequent request in the sweep would then be
    # measuring the login redirect instead of the route.
    "logout",
    # Answers 503 when UL_STRIPE_WEBHOOK_SECRET is unset, which it is in tests.
    # That is the endpoint working: it fails closed rather than processing an
    # unverifiable payload. A fact about the environment, like the network guard
    # below, not a route that crashes.
    "billing.stripe_webhook",
}


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
        checkin = baker.make(SafetyCheckin, profile=profile, title="Smoke Checkin")

        self.identifiers = {
            "pin_slug": pin.slug,
            "location_slug": location.slug,
            "trip_slug": trip.slug,
            "list_slug": baker.make(PinList, profile=profile, name="Smoke List").slug,
            "checkin_slug": checkin.slug,
            "map_uuid": str(baker.make(MarkupMap, profile=profile).uuid),
            "filter_uuid": str(baker.make(SavedFilter, profile=profile).uuid),
            "label_id": baker.make(Label, profile=profile, kind="tag").pk,
            # Cheap to supply and each unlocks a whole family: label_kind is an
            # enum string, profile_slug/profile_id are the requester's own, and
            # checkin_uuid/group_uuid are one object each. Measured first -
            # these five parameter names alone gate 59 of the 133 routes that
            # were out of reach with a single unknown parameter.
            "label_kind": KIND_TAG,
            "profile_slug": profile.slug,
            "profile_id": profile.pk,
            "checkin_uuid": str(baker.make(SafetyCheckin, profile=profile, title="Smoke Checkin 2").uuid),
            "group_uuid": str(self._group_chat(profile).uuid),
            # `session_id` names a *different model* in each game, so one value cannot satisfy all 36 routes
            # that take it.
            "album_slug": baker.make(
                "dashboard.Album", profile=profile, parent_profile=profile, name="Smoke Album"
            ).slug,
            "activity_id": baker.make("dashboard.TripActivity", trip=trip).pk,
            "alias_id": baker.make("dashboard.PinAlias", pin=pin, name="smoke-alias").pk,
            "comment_id": baker.make("dashboard.Comment", profile=profile, pin=pin, text="smoke").pk,
            # With a real file attached, deliberately.
            "image_id": self._image_with_file(profile, pin, location).pk,
            "token": str(
                baker.make(
                    "dashboard.SafetyCheckinContact", checkin=checkin, email="smoke@example.com", contact_profile=None
                ).token
            ),
            "session_id": [
                baker.make("dashboard.GameSession", host_profile=profile).pk,
                baker.make("dashboard.TriviaSession", host_profile=profile).pk,
                baker.make("dashboard.ConsensusSession", host_profile=profile).pk,
            ],
        }

    @staticmethod
    def _image_with_file(profile, pin, location):
        """An Image row whose file actually exists, as every real upload path leaves it."""
        from django.core.files.base import ContentFile

        image = baker.make("dashboard.Image", profile=profile, pin=pin, location=location)
        image.image.save("smoke.jpg", ContentFile(b"not-a-real-jpeg"), save=True)
        return image

    @staticmethod
    def _group_chat(profile):
        """A group chat the requester belongs to, so group routes reach their logic."""
        from urbanlens.dashboard.models.group_chats.model import GroupChat, GroupChatMembership

        group = baker.make(GroupChat, creator=profile, name="Smoke Group")
        GroupChatMembership.objects.create(group=group, profile=profile)
        return group

    def _write_routes(self) -> list[tuple[str, str]]:
        """``(route name, url)`` for every route this sweep can build a URL for.

        The zero-parameter ones are the larger group and were the easier to overlook precisely because they need
        no fixture - there is nothing to build, so nothing prompts you to build it."""
        urls: list[tuple[str, str]] = []
        for name, entries in get_resolver().reverse_dict.lists():
            if not isinstance(name, str) or name in _SKIP_ROUTES:
                continue
            params = entries[0][0][0][1]
            if params and not all(param in self.identifiers for param in params):
                continue
            # A parameter may offer several candidate values (see `session_id`).
            # Single-parameter routes try each; multi-parameter routes take the
            # first of each, so the URL count stays linear rather than
            # combinatorial for no extra coverage.
            if len(params) == 1:
                candidates = [{params[0]: value} for value in self._candidates(params[0])]
            else:
                candidates = [{param: self._candidates(param)[0] for param in params}]
            for kwargs in candidates:
                try:
                    urls.append((name, reverse(name, kwargs=kwargs)))
                except NoReverseMatch:
                    continue
        return sorted(urls)

    def _candidates(self, param: str) -> list:
        """Every value worth trying for *param*, always as a list."""
        value = self.identifiers[param]
        return value if isinstance(value, list) else [value]

    def _crashing_routes(self) -> dict[str, str]:
        """Route name → how it crashed, for every swept write route that did."""
        crashes: dict[str, str] = {}
        for name, url in self._write_routes():
            for method in _WRITE_METHODS:
                try:
                    # Each request in its own savepoint.
                    with transaction.atomic():
                        self.client.force_login(self.user)
                        response = getattr(self.client, method)(url, data={})
                except Exception as exc:
                    if _NETWORK_GUARD_MARKER in str(exc):
                        continue
                    crashes[name] = f"[{method.upper()}] raised {type(exc).__name__}: {str(exc)[:160]}"
                    continue
                if response.status_code >= 500:
                    crashes[name] = f"[{method.upper()}] returned {response.status_code}"
        return crashes

    def test_no_write_route_answers_a_minimal_request_with_a_server_error(self) -> None:
        unexpected = {name: how for name, how in self._crashing_routes().items() if name not in _KNOWN_CRASHES}

        self.assertEqual(
            unexpected,
            {},
            "write routes crashed on a minimal request:\n" + "\n".join(f"{n} {h}" for n, h in unexpected.items()),
        )

    def test_the_known_crash_is_still_crashing(self) -> None:
        """Stops an exemption outliving the bug it describes.

        With ``_KNOWN_CRASHES`` empty this asserts the absence of excuses, which is the state to defend: every
        write route is swept for real."""
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
            except Exception:
                continue
        self.assertGreater(answered, 0, "no route answered at all - the sweep is not issuing real requests")
