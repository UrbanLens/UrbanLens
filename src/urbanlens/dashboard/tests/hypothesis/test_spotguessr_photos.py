"""Tests for services.spotguessr.photos - Photos-mode candidate photo selection.

The wiki-attachment gate here is a privacy invariant, not a quality filter -
see photos.py's module docstring. It's tested first and most thoroughly for
that reason.
"""

from __future__ import annotations

from itertools import count

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, ImageSource, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin
from urbanlens.dashboard.models.spotguessr.model import GamePhotoFeedbackKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.spotguessr.photos import candidate_image_for_location

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class CandidateImageForLocationPrivacyTests(TestCase):
    """A photo must never be shown in the game unless it was explicitly shared to the location's wiki."""

    def test_a_photo_with_no_wiki_attachment_is_never_eligible(self) -> None:
        location = _make_location()
        profile = _make_profile()
        pin = baker.make(Pin, profile=profile, location=location)
        baker.make(Image, location=location, pin=pin, profile=profile, media_type=MediaKind.PHOTO, wiki=None)
        self.assertIsNone(candidate_image_for_location(location))

    def test_a_photo_attached_to_a_safety_checkin_is_never_eligible_even_if_location_matches(self) -> None:
        """Defense in depth: even a photo with an (unusual) safety_checkin
        link is caught by the same wiki==null gate as any other private
        photo - it was never explicitly shared to the wiki either."""
        location = _make_location()
        profile = _make_profile()
        checkin = baker.make(SafetyCheckin, profile=profile)
        baker.make(Image, location=location, safety_checkin=checkin, profile=profile, media_type=MediaKind.PHOTO, wiki=None)
        self.assertIsNone(candidate_image_for_location(location))

    def test_a_photo_shared_to_the_locations_wiki_is_eligible(self) -> None:
        location = _make_location()
        profile = _make_profile()
        wiki = baker.make(Wiki, location=location)
        pin = baker.make(Pin, profile=profile, location=location)
        image = baker.make(Image, location=location, pin=pin, wiki=wiki, profile=profile, media_type=MediaKind.PHOTO)
        self.assertEqual(candidate_image_for_location(location), image)

    def test_a_wiki_attached_photo_at_a_different_location_is_not_eligible(self) -> None:
        location = _make_location()
        other_location = _make_location()
        other_wiki = baker.make(Wiki, location=other_location)
        baker.make(Image, location=other_location, wiki=other_wiki, media_type=MediaKind.PHOTO)
        self.assertIsNone(candidate_image_for_location(location))


class CandidateImageForLocationExternalMediaOnlyTests(TestCase):
    def setUp(self) -> None:
        self.location = _make_location()
        self.wiki = baker.make(Wiki, location=self.location)

    def test_external_media_only_excludes_wiki_attached_uploads(self) -> None:
        baker.make(Image, location=self.location, wiki=self.wiki, media_type=MediaKind.PHOTO, source=ImageSource.UPLOAD)
        self.assertIsNone(candidate_image_for_location(self.location, external_media_only=True))

    def test_external_media_only_still_allows_externally_sourced_wiki_photos(self) -> None:
        image = baker.make(Image, location=self.location, wiki=self.wiki, media_type=MediaKind.PHOTO, source=ImageSource.WIKIMEDIA, media_source_key="wikimedia", media_item_key="a" * 40)
        self.assertEqual(candidate_image_for_location(self.location, external_media_only=True), image)

    def test_default_allows_both_uploads_and_external_media(self) -> None:
        upload = baker.make(Image, location=self.location, wiki=self.wiki, media_type=MediaKind.PHOTO, source=ImageSource.UPLOAD)
        self.assertEqual(candidate_image_for_location(self.location), upload)


class CandidateImageForLocationRelevanceTests(TestCase):
    """Only externally-sourced wiki photos are relevance-gated - personal uploads have no relevance identity at all."""

    def setUp(self) -> None:
        self.location = _make_location()
        self.wiki = baker.make(Wiki, location=self.location)

    def _external_image(self, item_key: str) -> Image:
        return baker.make(
            Image,
            location=self.location,
            wiki=self.wiki,
            media_type=MediaKind.PHOTO,
            source=ImageSource.WIKIMEDIA,
            media_source_key="wikimedia",
            media_item_key=item_key,
        )

    def test_a_reported_external_photo_is_excluded_by_default(self) -> None:
        from urbanlens.dashboard.models.spotguessr.model import GameRound, GameSession, SpotGuessrMode
        from urbanlens.dashboard.services.spotguessr.relevance import record_feedback

        image = self._external_image("b" * 40)
        session = baker.make(GameSession, mode=SpotGuessrMode.PHOTOS)
        round_ = baker.make(GameRound, session=session, location=self.location, image=image)
        record_feedback(round_, _make_profile(), GamePhotoFeedbackKind.REPORTED)

        self.assertIsNone(candidate_image_for_location(self.location))

    def test_a_reported_external_photo_is_included_when_arbitrary_is_allowed(self) -> None:
        from urbanlens.dashboard.models.spotguessr.model import GameRound, GameSession, SpotGuessrMode
        from urbanlens.dashboard.services.spotguessr.relevance import record_feedback

        image = self._external_image("c" * 40)
        session = baker.make(GameSession, mode=SpotGuessrMode.PHOTOS)
        round_ = baker.make(GameRound, session=session, location=self.location, image=image)
        record_feedback(round_, _make_profile(), GamePhotoFeedbackKind.REPORTED)

        self.assertEqual(candidate_image_for_location(self.location, allow_arbitrary_external_photos=True), image)

    def test_a_never_voted_external_photo_is_eligible_by_default(self) -> None:
        """Non-negative, not strictly positive - a brand new photo with zero
        votes must still be showable, or it could never accumulate the
        "shown, no reaction" signal that would eventually earn it a score."""
        image = self._external_image("d" * 40)
        self.assertEqual(candidate_image_for_location(self.location), image)
