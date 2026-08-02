"""Tests for services.media.media_relevance - blended wiki + SpotGuessr photo relevance."""

from __future__ import annotations

from itertools import count

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import GamePhotoFeedback, GamePhotoFeedbackKind, GameRound, GameSession, SpotGuessrMode
from urbanlens.dashboard.services.media.media_relevance import effective_relevance, local_images_for_gallery_items

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


def _make_external_image(location: Location, item_key: str) -> Image:
    return baker.make(Image, location=location, media_type=MediaKind.PHOTO, media_source_key="wikimedia", media_item_key=item_key)


def _feedback(image: Image, profile: Profile, kind: str) -> GamePhotoFeedback:
    session = baker.make(GameSession, mode=SpotGuessrMode.PHOTOS)
    round_ = baker.make(GameRound, session=session, location=image.location, image=image)
    return GamePhotoFeedback.objects.create(round=round_, profile=profile, kind=kind)


class EffectiveRelevanceTests(TestCase):
    def test_an_image_with_no_media_identity_scores_zero(self) -> None:
        image = baker.make(Image, location=_make_location(), media_type=MediaKind.PHOTO, media_source_key=None, media_item_key=None)
        self.assertEqual(effective_relevance(image), 0.0)

    def test_wiki_votes_alone_sum_to_the_net_score(self) -> None:
        location = _make_location()
        image = _make_external_image(location, "a" * 40)
        MediaRelevance.objects.create(profile=_make_profile(), location=location, source="wikimedia", item_key="a" * 40, is_relevant=True)
        MediaRelevance.objects.create(profile=_make_profile(), location=location, source="wikimedia", item_key="a" * 40, is_relevant=True)
        MediaRelevance.objects.create(profile=_make_profile(), location=location, source="wikimedia", item_key="a" * 40, is_relevant=False)
        self.assertEqual(effective_relevance(image), 1.0)

    def test_game_thumbs_up_counts_at_half_weight(self) -> None:
        image = _make_external_image(_make_location(), "b" * 40)
        _feedback(image, _make_profile(), GamePhotoFeedbackKind.THUMBS_UP)
        self.assertEqual(effective_relevance(image), 0.5)

    def test_game_thumbs_down_counts_at_only_a_token_weight(self) -> None:
        """Deliberate: 'wrong photo for this game' must not meaningfully tank the wiki's
        own relevance signal - it's real, but tiny (only useful for ordering among
        already-relevant photos later, not for the eligibility filter)."""
        image = _make_external_image(_make_location(), "c" * 40)
        _feedback(image, _make_profile(), GamePhotoFeedbackKind.THUMBS_DOWN)
        self.assertAlmostEqual(effective_relevance(image), -0.001)

    def test_a_realistic_number_of_thumbs_down_cannot_zero_out_a_relevant_photo(self) -> None:
        location = _make_location()
        image = _make_external_image(location, "9" * 40)
        MediaRelevance.objects.create(profile=_make_profile(), location=location, source="wikimedia", item_key="9" * 40, is_relevant=True)
        for _ in range(10):
            _feedback(image, _make_profile(), GamePhotoFeedbackKind.THUMBS_DOWN)
        self.assertGreater(effective_relevance(image), 0.0)

    def test_game_report_counts_at_full_negative_weight(self) -> None:
        image = _make_external_image(_make_location(), "d" * 40)
        _feedback(image, _make_profile(), GamePhotoFeedbackKind.REPORTED)
        self.assertEqual(effective_relevance(image), -1.0)

    def test_no_reaction_counts_at_one_hundredth_weight(self) -> None:
        image = _make_external_image(_make_location(), "e" * 40)
        _feedback(image, _make_profile(), GamePhotoFeedbackKind.NO_REACTION)
        self.assertAlmostEqual(effective_relevance(image), 0.01)

    def test_wiki_and_game_signals_blend_together(self) -> None:
        location = _make_location()
        image = _make_external_image(location, "f" * 40)
        MediaRelevance.objects.create(profile=_make_profile(), location=location, source="wikimedia", item_key="f" * 40, is_relevant=True)
        _feedback(image, _make_profile(), GamePhotoFeedbackKind.THUMBS_UP)
        _feedback(image, _make_profile(), GamePhotoFeedbackKind.REPORTED)
        self.assertAlmostEqual(effective_relevance(image), 1.0 + 0.5 - 1.0)

    def test_repeated_no_reaction_impressions_accumulate(self) -> None:
        """Multiple separate rounds showing the same image to the same profile
        must each contribute - this is what lets the very weak per-impression
        weight add up to something meaningful over many plays."""
        image = _make_external_image(_make_location(), "0" * 40)
        profile = _make_profile()
        session = baker.make(GameSession, mode=SpotGuessrMode.PHOTOS)
        for sequence_index in range(3):
            round_ = baker.make(GameRound, session=session, location=image.location, image=image, sequence_index=sequence_index)
            GamePhotoFeedback.objects.create(round=round_, profile=profile, kind=GamePhotoFeedbackKind.NO_REACTION)
        self.assertAlmostEqual(effective_relevance(image), 0.03)


class LocalImagesForGalleryItemsTests(TestCase):
    def test_finds_a_materialized_match_by_location_source_and_url_hash(self) -> None:
        location = _make_location()
        url = "https://example.test/photo.jpg"
        image = baker.make(Image, location=location, media_source_key="wikimedia", media_item_key=media_item_key(url))
        result = local_images_for_gallery_items(location, "wikimedia", [url])
        self.assertEqual(result, {url: image})

    def test_a_url_with_no_local_copy_is_simply_absent(self) -> None:
        location = _make_location()
        result = local_images_for_gallery_items(location, "wikimedia", ["https://example.test/no-copy.jpg"])
        self.assertEqual(result, {})

    def test_does_not_match_a_different_locations_materialized_copy(self) -> None:
        url = "https://example.test/photo.jpg"
        baker.make(Image, location=_make_location(), media_source_key="wikimedia", media_item_key=media_item_key(url))
        result = local_images_for_gallery_items(_make_location(), "wikimedia", [url])
        self.assertEqual(result, {})

    def test_does_not_match_a_different_sources_materialized_copy(self) -> None:
        location = _make_location()
        url = "https://example.test/photo.jpg"
        baker.make(Image, location=location, media_source_key="smithsonian", media_item_key=media_item_key(url))
        result = local_images_for_gallery_items(location, "wikimedia", [url])
        self.assertEqual(result, {})
