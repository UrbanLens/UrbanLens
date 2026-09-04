"""Tests for storage-quota exemptions.

Two rules, both about not charging one user for storage the whole community
benefits from:

- Locally cached external media never counts against anyone's quota.
- A user's own wiki-shared photo stops counting once enough *other* people
  mark it relevant.
"""

from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, QuotaExemption
from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.media.quota_rewards import (
    community_relevant_vote_count,
    is_cached_external_media,
    refresh_community_quota_bonus,
)
from urbanlens.dashboard.services.media.storage import get_exempt_bytes, get_storage_totals, get_storage_used_bytes


def _set_bonus_threshold(votes: int) -> None:
    settings_obj = SiteSettings.get_current()
    settings_obj.community_photo_quota_bonus_votes = votes
    settings_obj.save(update_fields=["community_photo_quota_bonus_votes"])


def _wiki_photo(profile, wiki, location, size: int = 100) -> Image:
    """A user's own photo contributed to a wiki."""
    return Image.objects.create(
        image=SimpleUploadedFile("mine.jpg", b"bytes", content_type="image/jpeg"),
        wiki=wiki,
        location=location,
        profile=profile,
        file_size=size,
    )


def _mark_relevant(profile, location, image) -> None:
    MediaRelevance.objects.create(
        profile=profile,
        location=location,
        source="photos",
        item_key=media_item_key(image.image.url),
        is_relevant=True,
    )


class StorageAccountingTests(TestCase):
    """get_storage_used_bytes skips exempt rows; get_exempt_bytes reports them."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make_recipe("dashboard.pin").profile

    def test_ordinary_uploads_count(self) -> None:
        baker.make_recipe("dashboard.image", profile=self.profile, file_size=300)
        self.assertEqual(get_storage_used_bytes(self.profile), 300)
        self.assertEqual(get_exempt_bytes(self.profile), 0)

    def test_exempt_rows_do_not_count(self) -> None:
        baker.make_recipe("dashboard.image", profile=self.profile, file_size=300)
        baker.make_recipe(
            "dashboard.image",
            profile=self.profile,
            file_size=900,
            quota_exempt_reason=QuotaExemption.EXTERNAL_MEDIA,
        )
        self.assertEqual(get_storage_used_bytes(self.profile), 300)
        self.assertEqual(get_exempt_bytes(self.profile), 900)

    def test_community_exempt_rows_do_not_count_either(self) -> None:
        baker.make_recipe(
            "dashboard.image",
            profile=self.profile,
            file_size=500,
            quota_exempt_reason=QuotaExemption.COMMUNITY_CONTRIBUTION,
        )
        self.assertEqual(get_storage_used_bytes(self.profile), 0)
        self.assertEqual(get_exempt_bytes(self.profile), 500)

    def test_combined_totals_agree_with_the_separate_helpers(self) -> None:
        """get_storage_totals is an optimisation, not a second definition."""
        baker.make_recipe("dashboard.image", profile=self.profile, file_size=300)
        baker.make_recipe(
            "dashboard.image", profile=self.profile, file_size=900, quota_exempt_reason=QuotaExemption.EXTERNAL_MEDIA
        )
        baker.make_recipe(
            "dashboard.image",
            profile=self.profile,
            file_size=500,
            quota_exempt_reason=QuotaExemption.COMMUNITY_CONTRIBUTION,
        )

        with self.assertNumQueries(1):
            counted, exempt = get_storage_totals(self.profile)
        self.assertEqual(counted, get_storage_used_bytes(self.profile))
        self.assertEqual(exempt, get_exempt_bytes(self.profile))
        self.assertEqual((counted, exempt), (300, 1400))

    def test_combined_totals_are_zero_for_an_empty_profile(self) -> None:
        self.assertEqual(get_storage_totals(self.profile), (0, 0))


class CachedExternalMediaTests(TestCase):
    """Materialized external media is exempt at creation."""

    def test_is_cached_external_media_reads_the_gallery_identity(self) -> None:
        pin = baker.make_recipe("dashboard.pin")
        external = baker.make_recipe(
            "dashboard.image", profile=pin.profile, media_source_key="wikimedia", media_item_key="abc"
        )
        own = baker.make_recipe("dashboard.image", profile=pin.profile)
        self.assertTrue(is_cached_external_media(external))
        self.assertFalse(is_cached_external_media(own))

    def test_external_media_never_earns_the_community_bonus(self) -> None:
        """It's already exempt for a different reason - don't relabel it."""
        pin = baker.make_recipe("dashboard.pin")
        wiki = baker.make_recipe("dashboard.wiki", location=pin.location)
        external = baker.make_recipe(
            "dashboard.image",
            profile=pin.profile,
            wiki=wiki,
            location=pin.location,
            media_source_key="wikimedia",
            media_item_key="abc",
        )
        _set_bonus_threshold(1)
        self.assertFalse(refresh_community_quota_bonus(external))


class CommunityQuotaBonusTests(TestCase):
    """A wiki-shared photo stops counting once enough other people upvote it."""

    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make_recipe("dashboard.pin")
        self.location = self.pin.location
        self.profile = self.pin.profile
        self.wiki = baker.make_recipe("dashboard.wiki", location=self.location)
        self.image = _wiki_photo(self.profile, self.wiki, self.location, size=100)
        _set_bonus_threshold(2)

    def _voter(self):
        return baker.make_recipe("dashboard.pin").profile

    def test_below_the_threshold_earns_nothing(self) -> None:
        _mark_relevant(self._voter(), self.location, self.image)
        self.assertFalse(refresh_community_quota_bonus(self.image))
        self.image.refresh_from_db()
        self.assertEqual(self.image.quota_exempt_reason, "")

    def test_reaching_the_threshold_grants_the_bonus(self) -> None:
        for _ in range(2):
            _mark_relevant(self._voter(), self.location, self.image)
        self.assertTrue(refresh_community_quota_bonus(self.image))
        self.image.refresh_from_db()
        self.assertEqual(self.image.quota_exempt_reason, QuotaExemption.COMMUNITY_CONTRIBUTION)

    def test_the_bonus_equals_the_photos_own_size(self) -> None:
        """The reward is exactly 'this file stops counting', not a flat grant."""
        self.assertEqual(get_storage_used_bytes(self.profile), 100)
        for _ in range(2):
            _mark_relevant(self._voter(), self.location, self.image)
        refresh_community_quota_bonus(self.image)
        self.assertEqual(get_storage_used_bytes(self.profile), 0)
        self.assertEqual(get_exempt_bytes(self.profile), 100)

    def test_the_uploaders_own_vote_does_not_count(self) -> None:
        _mark_relevant(self.profile, self.location, self.image)
        _mark_relevant(self._voter(), self.location, self.image)
        self.assertEqual(community_relevant_vote_count(self.image), 1)
        self.assertFalse(refresh_community_quota_bonus(self.image))

    def test_not_relevant_votes_do_not_count(self) -> None:
        MediaRelevance.objects.create(
            profile=self._voter(),
            location=self.location,
            source="photos",
            item_key=media_item_key(self.image.image.url),
            is_relevant=False,
        )
        _mark_relevant(self._voter(), self.location, self.image)
        self.assertEqual(community_relevant_vote_count(self.image), 1)

    def test_a_photo_never_shared_to_a_wiki_earns_nothing(self) -> None:
        private = Image.objects.create(
            image=SimpleUploadedFile("p.jpg", b"bytes", content_type="image/jpeg"),
            pin=self.pin,
            location=self.location,
            profile=self.profile,
            file_size=100,
        )
        for _ in range(2):
            _mark_relevant(self._voter(), self.location, private)
        self.assertFalse(refresh_community_quota_bonus(private))

    def test_the_bonus_is_never_revoked(self) -> None:
        """One-way by design: a granted bonus can't be taken back by re-voting,
        so a user inside their quota can't be pushed over it retroactively."""
        for _ in range(2):
            _mark_relevant(self._voter(), self.location, self.image)
        refresh_community_quota_bonus(self.image)

        MediaRelevance.objects.filter(source="photos").delete()
        self.image.refresh_from_db()
        self.assertEqual(self.image.quota_exempt_reason, QuotaExemption.COMMUNITY_CONTRIBUTION)
        self.assertEqual(get_storage_used_bytes(self.profile), 0)

    def test_granting_is_idempotent(self) -> None:
        for _ in range(2):
            _mark_relevant(self._voter(), self.location, self.image)
        self.assertTrue(refresh_community_quota_bonus(self.image))
        self.assertFalse(refresh_community_quota_bonus(self.image))

    def test_a_zero_threshold_disables_the_reward(self) -> None:
        _set_bonus_threshold(0)
        for _ in range(3):
            _mark_relevant(self._voter(), self.location, self.image)
        self.assertFalse(refresh_community_quota_bonus(self.image))
