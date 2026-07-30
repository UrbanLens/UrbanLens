"""Tests for services.spotguessr.relevance - recording GamePhotoFeedback."""

from __future__ import annotations

from itertools import count

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import GamePhotoFeedback, GamePhotoFeedbackKind, GameRound, GameSession, SpotGuessrMode
from urbanlens.dashboard.services.spotguessr.relevance import backfill_no_reaction, record_feedback

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


def _make_photo_round(image: Image | None = None) -> GameRound:
    location = _make_location()
    if image is None:
        image = baker.make(Image, location=location, media_type=MediaKind.PHOTO)
    session = baker.make(GameSession, mode=SpotGuessrMode.PHOTOS)
    return baker.make(GameRound, session=session, location=location, image=image)


def _make_named_place_round() -> GameRound:
    location = _make_location()
    session = baker.make(GameSession, mode=SpotGuessrMode.NAMED_PLACE)
    return baker.make(GameRound, session=session, location=location, image=None)


def _make_street_view_round() -> GameRound:
    """Street View rounds have no Image row - imagery is fetched live, never stored."""
    location = _make_location()
    session = baker.make(GameSession, mode=SpotGuessrMode.STREET_VIEW)
    return baker.make(GameRound, session=session, location=location, image=None)


class RecordFeedbackTests(TestCase):
    def test_records_an_explicit_reaction(self) -> None:
        round_ = _make_photo_round()
        profile = _make_profile()
        feedback = record_feedback(round_, profile, GamePhotoFeedbackKind.THUMBS_UP)
        assert feedback is not None
        self.assertEqual(feedback.kind, GamePhotoFeedbackKind.THUMBS_UP)
        self.assertEqual(GamePhotoFeedback.objects.filter(round=round_, profile=profile).count(), 1)

    def test_changing_ones_mind_overwrites_the_prior_reaction(self) -> None:
        round_ = _make_photo_round()
        profile = _make_profile()
        record_feedback(round_, profile, GamePhotoFeedbackKind.THUMBS_UP)
        record_feedback(round_, profile, GamePhotoFeedbackKind.REPORTED)
        feedback = GamePhotoFeedback.objects.get(round=round_, profile=profile)
        self.assertEqual(feedback.kind, GamePhotoFeedbackKind.REPORTED)

    def test_a_named_place_round_is_a_no_op(self) -> None:
        round_ = _make_named_place_round()
        profile = _make_profile()
        self.assertIsNone(record_feedback(round_, profile, GamePhotoFeedbackKind.THUMBS_UP))
        self.assertFalse(GamePhotoFeedback.objects.filter(round=round_).exists())

    def test_a_street_view_round_with_no_image_row_still_records_feedback(self) -> None:
        """Regression guard: Street View shows real imagery (fetched live,
        never stored as an Image row) - gating on `round.image_id is None`
        wrongly treated it the same as Named Place's "no photo" case,
        producing a false "no photo to react to" 400 for a round that
        clearly did show a photo."""
        round_ = _make_street_view_round()
        profile = _make_profile()
        feedback = record_feedback(round_, profile, GamePhotoFeedbackKind.THUMBS_DOWN)
        assert feedback is not None
        self.assertEqual(feedback.kind, GamePhotoFeedbackKind.THUMBS_DOWN)


class BackfillNoReactionTests(TestCase):
    def test_backfills_no_reaction_for_every_given_profile(self) -> None:
        round_ = _make_photo_round()
        host, guest = _make_profile(), _make_profile()
        backfill_no_reaction(round_, [host, guest])
        kinds = set(GamePhotoFeedback.objects.filter(round=round_).values_list("kind", flat=True))
        self.assertEqual(kinds, {GamePhotoFeedbackKind.NO_REACTION})
        self.assertEqual(GamePhotoFeedback.objects.filter(round=round_).count(), 2)

    def test_never_overwrites_an_explicit_reaction_already_recorded(self) -> None:
        round_ = _make_photo_round()
        profile = _make_profile()
        record_feedback(round_, profile, GamePhotoFeedbackKind.THUMBS_UP)
        backfill_no_reaction(round_, [profile])
        feedback = GamePhotoFeedback.objects.get(round=round_, profile=profile)
        self.assertEqual(feedback.kind, GamePhotoFeedbackKind.THUMBS_UP)

    def test_a_named_place_round_is_a_no_op(self) -> None:
        round_ = _make_named_place_round()
        profile = _make_profile()
        backfill_no_reaction(round_, [profile])
        self.assertFalse(GamePhotoFeedback.objects.filter(round=round_).exists())
