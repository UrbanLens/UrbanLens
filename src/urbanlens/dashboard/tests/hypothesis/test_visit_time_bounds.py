"""A visit is a record of somewhere the user has been, so it cannot be ahead of now.

Found by the integration suite on 2026-08-24: `POST pins/{slug}/visits/`
accepted a `visited_at` a week in the future and answered 201. Nothing here
asserted the *absence* of that validation, because every existing visit test
supplies a sensible timestamp - the author writing a test is thinking about the
feature working, not about it being abused. See `docs/audits/TEST_COVERAGE_GAPS.md`,
which records that blind spot as the common thread through most of what the
integration suite caught.

The damage is not local. `create_manual_visit` calls `sync_last_visited`, which
sets `Pin.last_visited`, which is displayed and ordered by - so one mistyped
year makes a pin permanently the most recently visited thing its owner has, and
nothing about the pin looks wrong.

Both layers are tested here, deliberately. The serializer is the API's
first line and gives a client field-level detail; the service is the choke point
the *web form* also goes through, so it is what stops the same value arriving by
the other road.
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.external_api.serializers import PinVisitCreateSerializer
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.visits.visits import MAX_VISIT_CLOCK_SKEW, VisitInFutureError, create_manual_visit


def _bearer(raw_key: str) -> dict[str, str]:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class VisitTimeSerializerTests(TestCase):
    """The API's first line: field-level validation with a usable message."""

    def test_a_past_visit_is_accepted(self) -> None:
        """The check must not be satisfiable by rejecting everything."""
        serializer = PinVisitCreateSerializer(
            data={"visited_at": (timezone.now() - datetime.timedelta(days=3)).isoformat()}
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_a_visit_a_week_from_now_is_refused(self) -> None:
        serializer = PinVisitCreateSerializer(
            data={"visited_at": (timezone.now() + datetime.timedelta(days=7)).isoformat()}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("visited_at", serializer.errors)

    def test_a_clock_a_little_fast_is_tolerated(self) -> None:
        """A client's clock is its own; a phone a minute fast is not lying.

        The tolerance is what keeps this a check on the *date* rather than a
        trap for anybody whose device is not perfectly synchronised.
        """
        serializer = PinVisitCreateSerializer(
            data={"visited_at": (timezone.now() + MAX_VISIT_CLOCK_SKEW / 2).isoformat()}
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)


class VisitTimeServiceTests(TestCase):
    """The choke point the web form shares, which the serializer does not cover."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location), parent_pin=None)

    def test_the_service_refuses_a_future_visit(self) -> None:
        """The API serializer is not the only road to this function."""
        with self.assertRaises(VisitInFutureError):
            create_manual_visit(self.pin, visited_at=timezone.now() + datetime.timedelta(days=7))

        self.assertFalse(PinVisit.objects.filter(pin=self.pin).exists(), "a refused visit was still written")

    def test_the_service_accepts_a_past_visit(self) -> None:
        visit = create_manual_visit(self.pin, visited_at=timezone.now() - datetime.timedelta(hours=2))

        self.assertIsNotNone(visit.pk)

    def test_a_refused_visit_does_not_move_last_visited(self) -> None:
        """The reason this matters at all.

        `last_visited` is what the pin list sorts on and what the detail page
        shows. A future visit that is stored corrupts an ordering the user never
        thinks to question.
        """
        create_manual_visit(self.pin, visited_at=timezone.now() - datetime.timedelta(days=1))
        self.pin.refresh_from_db()
        before = self.pin.last_visited

        with self.assertRaises(VisitInFutureError):
            create_manual_visit(self.pin, visited_at=timezone.now() + datetime.timedelta(days=365))

        self.pin.refresh_from_db()
        self.assertEqual(self.pin.last_visited, before, "a refused future visit still moved last_visited")


class VisitTimeEndpointTests(TestCase):
    """End to end through the endpoint the integration suite caught this on."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        _key, self.raw_key = generate_api_key(self.user, "visit-bounds")
        api_key = self.user.api_keys.first()
        api_key.scopes = list(ApiKeyScope.values)
        api_key.save()
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location), parent_pin=None)

    def _post(self, when: datetime.datetime):
        return self.client.post(
            reverse("external_api:pins.visits", kwargs={"pin_slug": self.pin.slug}),
            data={"visited_at": when.isoformat()},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

    def test_a_future_visit_is_refused_with_the_standard_envelope(self) -> None:
        response = self._post(timezone.now() + datetime.timedelta(days=7))

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("error", response.json())

    def test_a_past_visit_still_succeeds(self) -> None:
        response = self._post(timezone.now() - datetime.timedelta(days=1))

        self.assertLess(response.status_code, 300, response.content)
