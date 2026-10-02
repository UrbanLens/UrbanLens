"""A safety check-in's destination coordinates are refused unless finite and on the globe."""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinStatus

#: (latitude, longitude) pairs no route may accept. Each was parsed with a bare float(): NaN and infinity matched
#: no wiki or failed DecimalField.to_python on save, 1e400 overflowed float to inf, and 91/181 were stored.
INVALID_DESTINATIONS = (
    ("nan", "-74"),
    ("40", "NaN"),
    ("inf", "-74"),
    ("40", "-Infinity"),
    ("1e400", "-74"),
    ("91", "-74"),
    ("-90.000001", "-74"),
    ("40", "181"),
    ("40", "-180.5"),
    ("not-a-number", "-74"),
    ("40", ""),
    ("", "-74"),
)


class WikiOptionCoordinateTests(TestCase):
    """GET /safety/wiki-option/ answers bad coordinates with a 400."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.client.force_login(self.user)

    def test_invalid_coordinates_are_a_bad_request(self) -> None:
        for latitude, longitude in INVALID_DESTINATIONS:
            with self.subTest(latitude=latitude, longitude=longitude):
                response = self.client.get(
                    reverse("safety.checkin.wiki_option"),
                    {"destination_latitude": latitude, "destination_longitude": longitude},
                )
                self.assertEqual(response.status_code, 400)

    def test_no_destination_is_an_empty_toggle(self) -> None:
        for params in ({}, {"destination_latitude": "", "destination_longitude": ""}):
            with self.subTest(params=params):
                response = self.client.get(reverse("safety.checkin.wiki_option"), params)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "notify_community_wiki")

    def test_the_edges_of_the_globe_are_accepted(self) -> None:
        for latitude, longitude in (("90", "180"), ("-90", "-180")):
            with self.subTest(latitude=latitude, longitude=longitude):
                response = self.client.get(
                    reverse("safety.checkin.wiki_option"),
                    {"destination_latitude": latitude, "destination_longitude": longitude},
                )
                self.assertEqual(response.status_code, 200)


class CreateDestinationTests(TestCase):
    """POST /safety/new/ refuses a bad destination with the form's 400, creating nothing."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.client.force_login(self.user)

    def _post(self, latitude: str, longitude: str):
        return self.client.post(
            reverse("safety.checkin.create"),
            {
                "checkin_by": (timezone.now() + datetime.timedelta(hours=3)).isoformat(),
                "title": "Trip",
                "destination_latitude": latitude,
                "destination_longitude": longitude,
            },
        )

    def test_invalid_destinations_are_refused(self) -> None:
        for latitude, longitude in INVALID_DESTINATIONS:
            with self.subTest(latitude=latitude, longitude=longitude):
                response = self._post(latitude, longitude)
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, "Invalid destination", status_code=400)
                self.assertFalse(SafetyCheckin.objects.filter(profile=self.user.profile).exists())

    def test_a_valid_destination_is_stored(self) -> None:
        response = self._post("40.5", "-74.25")

        self.assertEqual(response.status_code, 302)
        checkin = SafetyCheckin.objects.get(profile=self.user.profile)
        self.assertEqual(
            (checkin.destination_latitude, checkin.destination_longitude), (Decimal("40.5"), Decimal("-74.25"))
        )

    def test_no_destination_is_allowed(self) -> None:
        response = self._post("", "")

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(SafetyCheckin.objects.get(profile=self.user.profile).destination_latitude)


class EditDestinationTests(TestCase):
    """POST /safety/<slug>/ (the autosave) refuses a bad destination and leaves the stored one alone."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.client.force_login(self.user)
        self.checkin = baker.make(
            "dashboard.SafetyCheckin",
            profile=self.user.profile,
            title="Hike",
            checkin_by=timezone.now() + datetime.timedelta(hours=3),
            grace_period=datetime.timedelta(hours=1),
            destination_latitude=Decimal("40.000000"),
            destination_longitude=Decimal("-74.000000"),
            status=SafetyCheckinStatus.SCHEDULED,
        )
        self.url = reverse("safety.checkin.detail", kwargs={"checkin_slug": self.checkin.slug})

    def _assert_destination_unchanged(self) -> None:
        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.destination_latitude, Decimal("40.000000"))
        self.assertEqual(self.checkin.destination_longitude, Decimal("-74.000000"))

    def test_an_autosave_with_an_invalid_destination_is_a_400(self) -> None:
        for latitude, longitude in INVALID_DESTINATIONS:
            with self.subTest(latitude=latitude, longitude=longitude):
                response = self.client.post(
                    self.url,
                    {"title": "Hike", "destination_latitude": latitude, "destination_longitude": longitude},
                    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["ok"])
                self._assert_destination_unchanged()

    def test_a_form_post_with_an_invalid_destination_changes_nothing(self) -> None:
        response = self.client.post(
            self.url, {"title": "Renamed", "destination_latitude": "nan", "destination_longitude": "-74"}
        )

        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self._assert_destination_unchanged()
        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.title, "Hike")

    def test_clearing_the_destination_still_works(self) -> None:
        response = self.client.post(
            self.url,
            {"title": "Hike", "destination_latitude": "", "destination_longitude": ""},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.checkin.refresh_from_db()
        self.assertIsNone(self.checkin.destination_latitude)
