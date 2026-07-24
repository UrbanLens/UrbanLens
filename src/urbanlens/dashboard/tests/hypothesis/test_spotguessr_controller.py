"""HTTP-level tests for controllers.spotguessr - auth boundaries, request validation, and answer-leak safety."""

from __future__ import annotations

from itertools import count
import json

from django.core.files.base import ContentFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import GameSession, SpotGuessrPreference

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class SpotGuessrStartViewTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()
        self.location = _make_location()
        baker.make(Pin, profile=self.profile, location=self.location)
        baker.make(
            Image,
            location=self.location,
            media_type=MediaKind.PHOTO,
            latitude=None,
            longitude=None,
            image=ContentFile(b"fake image bytes", name="test.jpg"),
        )
        self.client.force_login(self.profile.user)
        self.start_url = reverse("spotguessr.start")

    def test_requires_login(self) -> None:
        self.client.logout()
        response = self.client.post(self.start_url, {})
        self.assertEqual(response.status_code, 302)

    def test_starting_a_session_returns_a_round_without_leaking_the_answer(self) -> None:
        response = self.client.post(self.start_url, {"total_rounds": "3"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["finished"])
        self.assertIn("round_id", data["round"])
        self.assertIn("image_url", data["round"])
        self.assertNotIn("latitude", json.dumps(data))
        self.assertEqual(GameSession.objects.get(pk=data["session_id"]).total_rounds, 3)

    def test_invalid_difficulty_is_rejected(self) -> None:
        response = self.client.post(self.start_url, {"difficulty": "not-a-number"})
        self.assertEqual(response.status_code, 400)

    def test_invalid_geo_bounds_json_is_rejected(self) -> None:
        response = self.client.post(self.start_url, {"geo_bounds": "{not valid json"})
        self.assertEqual(response.status_code, 400)

    def test_geo_bounds_that_isnt_a_geometry_is_rejected(self) -> None:
        response = self.client.post(self.start_url, {"geo_bounds": json.dumps({"foo": "bar"})})
        self.assertEqual(response.status_code, 400)

    def test_no_eligible_locations_returns_a_finished_summary_instead_of_erroring(self) -> None:
        other_profile = _make_profile()
        self.client.force_login(other_profile.user)
        response = self.client.post(self.start_url, {})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["finished"])


class SpotGuessrGuessFlowTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()
        self.location = _make_location()
        baker.make(Pin, profile=self.profile, location=self.location)
        baker.make(Image, location=self.location, media_type=MediaKind.PHOTO, latitude=None, longitude=None)
        self.client.force_login(self.profile.user)
        start = self.client.post(reverse("spotguessr.start"), {"total_rounds": "1"}).json()
        self.session_id = start["session_id"]
        self.round_id = start["round"]["round_id"]
        self.guess_url = reverse("spotguessr.guess", args=[self.session_id, self.round_id])
        self.summary_url = reverse("spotguessr.summary", args=[self.session_id])

    def test_guessing_reveals_the_location_and_score(self) -> None:
        response = self.client.post(
            self.guess_url,
            {"latitude": str(self.location.latitude), "longitude": str(self.location.longitude)},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["points"], 5000)
        self.assertAlmostEqual(data["actual_latitude"], float(self.location.latitude), places=4)

    def test_a_second_guess_on_the_same_round_is_rejected(self) -> None:
        payload = {"latitude": str(self.location.latitude), "longitude": str(self.location.longitude)}
        self.client.post(self.guess_url, payload)
        response = self.client.post(self.guess_url, payload)
        self.assertEqual(response.status_code, 400)

    def test_missing_coordinates_are_rejected(self) -> None:
        response = self.client.post(self.guess_url, {})
        self.assertEqual(response.status_code, 400)

    def test_a_non_participant_cannot_guess_on_someone_elses_session(self) -> None:
        outsider = _make_profile()
        self.client.force_login(outsider.user)
        response = self.client.post(self.guess_url, {"latitude": "0", "longitude": "0"})
        self.assertEqual(response.status_code, 404)

    def test_summary_reports_the_final_score(self) -> None:
        self.client.post(
            self.guess_url,
            {"latitude": str(self.location.latitude), "longitude": str(self.location.longitude)},
        )
        response = self.client.get(self.summary_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["participants"][0]["total_points"], 5000)


class SpotGuessrPinsViewTests(TestCase):
    def test_only_returns_the_requesting_profiles_own_pins(self) -> None:
        mine = _make_profile()
        theirs = _make_profile()
        baker.make(Pin, profile=mine, location=_make_location())
        baker.make(Pin, profile=theirs, location=_make_location())

        self.client.force_login(mine.user)
        response = self.client.get(reverse("spotguessr.pins"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["pins"]), 1)


class SpotGuessrSettingsViewTests(TestCase):
    def test_toggling_show_ratings_to_friends(self) -> None:
        profile = _make_profile()
        self.client.force_login(profile.user)

        response = self.client.post(reverse("spotguessr.settings"), {"show_ratings_to_friends": "off"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SpotGuessrPreference.objects.get(profile=profile).show_ratings_to_friends)
