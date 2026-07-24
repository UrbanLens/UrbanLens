"""Tests for services.spotguessr.photo_coordinates - the SpotGuessr-side recording hook."""

from __future__ import annotations

from itertools import count

from django.contrib.gis.geos import Point
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.spotguessr.model import GameRound, GameSession, PhotoCoordinateGuess, SpotGuessrMode
from urbanlens.dashboard.services.spotguessr.photo_coordinates import record_guess

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_photo_round(location: Location, *, target_is_point: bool, image: Image | None = None) -> GameRound:
    session = baker.make(GameSession, mode=SpotGuessrMode.PHOTOS)
    return baker.make(GameRound, session=session, location=location, image=image, target_is_point=target_is_point)


class PhotoCoordinateGuessModelTests(TestCase):
    def test_the_model_has_no_way_to_identify_who_guessed(self) -> None:
        """Structural guarantee, not just a code-review convention: there is
        no profile/user/session/round field at all on this model."""
        field_names = {f.name for f in PhotoCoordinateGuess._meta.get_fields()}
        self.assertNotIn("profile", field_names)
        self.assertNotIn("round", field_names)
        self.assertNotIn("session", field_names)


class RecordGuessTests(TestCase):
    def test_a_boundary_target_round_records_a_guess(self) -> None:
        location = _make_location()
        image = baker.make(Image, location=location, media_type=MediaKind.PHOTO, latitude=None, longitude=None)
        round_ = _make_photo_round(location, target_is_point=False, image=image)

        record_guess(round_, Point(float(location.longitude), float(location.latitude), srid=4326), distance=0.0)

        self.assertEqual(PhotoCoordinateGuess.objects.filter(image=image).count(), 1)
        self.assertTrue(PhotoCoordinateGuess.objects.get(image=image).is_correct)

    def test_a_positive_distance_is_recorded_as_incorrect(self) -> None:
        location = _make_location()
        image = baker.make(Image, location=location, media_type=MediaKind.PHOTO, latitude=None, longitude=None)
        round_ = _make_photo_round(location, target_is_point=False, image=image)

        record_guess(round_, Point(0.0, 0.0, srid=4326), distance=5000.0)

        guess = PhotoCoordinateGuess.objects.get(image=image)
        self.assertFalse(guess.is_correct)

    def test_a_point_target_round_still_records_a_guess(self) -> None:
        """The photo already had its own coordinates when this round was
        generated - the estimate mechanism has no use for this guess, but
        it's saved anyway (no current use, but plausibly useful later)."""
        location = _make_location()
        image = baker.make(Image, location=location, media_type=MediaKind.PHOTO, latitude="42.0", longitude="-73.0")
        round_ = _make_photo_round(location, target_is_point=True, image=image)

        record_guess(round_, Point(-73.0, 42.0, srid=4326), distance=0.0)

        self.assertEqual(PhotoCoordinateGuess.objects.filter(image=image).count(), 1)
        self.assertTrue(PhotoCoordinateGuess.objects.get(image=image).is_correct)

    def test_a_point_target_round_never_triggers_an_estimate_recompute(self) -> None:
        """Recomputing an estimate for an already-placed photo would be pure
        waste - Image.effective_latitude/longitude never reads it once the
        real coordinates are set."""
        location = _make_location()
        image = baker.make(Image, location=location, media_type=MediaKind.PHOTO, latitude="42.0", longitude="-73.0")
        round_ = _make_photo_round(location, target_is_point=True, image=image)

        for _ in range(5):
            record_guess(round_, Point(-73.0, 42.0, srid=4326), distance=0.0)

        image.refresh_from_db()
        self.assertIsNone(image.estimated_latitude)
        self.assertIsNone(image.estimated_longitude)

    def test_a_round_with_no_photo_is_a_no_op(self) -> None:
        location = _make_location()
        round_ = _make_photo_round(location, target_is_point=False, image=None)

        record_guess(round_, Point(float(location.longitude), float(location.latitude), srid=4326), distance=0.0)

        self.assertFalse(PhotoCoordinateGuess.objects.exists())

    def test_five_correct_guesses_trigger_an_estimate(self) -> None:
        location = _make_location()
        image = baker.make(Image, location=location, media_type=MediaKind.PHOTO, latitude=None, longitude=None)
        round_ = _make_photo_round(location, target_is_point=False, image=image)

        for _ in range(5):
            record_guess(round_, Point(float(location.longitude), float(location.latitude), srid=4326), distance=0.0)

        image.refresh_from_db()
        self.assertIsNotNone(image.estimated_latitude)
        self.assertIsNotNone(image.estimated_longitude)
