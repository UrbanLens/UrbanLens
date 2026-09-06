"""Tests for storage-quota exemptions.

Two rules, both about not charging one user for storage the whole community
benefits from:

- Locally cached external media never counts against anyone's quota.
- A user's own wiki-shared photo stops counting once enough *other* people
  mark it relevant, and starts counting again only if that same user takes the
  photo back off the wiki.
"""

from __future__ import annotations

from io import StringIO

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, QuotaExemption
from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.undo import UndoAction
from urbanlens.dashboard.services.media.quota_rewards import (
    community_relevant_vote_count,
    is_cached_external_media,
    refresh_community_quota_bonus,
    revoke_community_quota_bonus,
)
from urbanlens.dashboard.services.media.storage import get_exempt_bytes, get_storage_totals, get_storage_used_bytes
from urbanlens.dashboard.services.undo.service import restore_undo_action


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


class RevokingTheCommunityBonusTests(TestCase):
    """Withdrawing the contribution ends the bonus it earned.

    The forward rule is one-way against everyone else - see
    ``test_the_bonus_is_never_revoked`` above, which must keep passing. This is
    the one case it was never meant to cover.
    """

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

    def _grant(self) -> None:
        for _ in range(2):
            _mark_relevant(self._voter(), self.location, self.image)
        self.assertTrue(refresh_community_quota_bonus(self.image))

    def test_revoking_puts_the_bytes_back_on_the_counted_total(self) -> None:
        self._grant()
        self.assertEqual((get_storage_used_bytes(self.profile), get_exempt_bytes(self.profile)), (0, 100))

        self.assertTrue(revoke_community_quota_bonus(self.image))

        self.image.refresh_from_db()
        self.assertEqual(self.image.quota_exempt_reason, "")
        self.assertEqual((get_storage_used_bytes(self.profile), get_exempt_bytes(self.profile)), (100, 0))

    def test_revoking_leaves_every_other_exemption_alone(self) -> None:
        """Four of the five reasons this column holds have nothing to do with a wiki."""
        for reason in (
            QuotaExemption.EXTERNAL_MEDIA,
            QuotaExemption.SHARED_COPY,
            QuotaExemption.DEDUPLICATED,
            QuotaExemption.WIKI_COPY,
        ):
            with self.subTest(reason=reason):
                image = _wiki_photo(self.profile, self.wiki, self.location, size=100)
                Image.objects.filter(pk=image.pk).update(quota_exempt_reason=reason)
                image.refresh_from_db()

                self.assertFalse(revoke_community_quota_bonus(image))

                image.refresh_from_db()
                self.assertEqual(image.quota_exempt_reason, reason)

    def test_revoking_an_unrewarded_photo_is_a_no_op(self) -> None:
        self.assertFalse(revoke_community_quota_bonus(self.image))
        self.image.refresh_from_db()
        self.assertEqual(self.image.quota_exempt_reason, "")

    def test_revoking_twice_is_harmless(self) -> None:
        self._grant()
        self.assertTrue(revoke_community_quota_bonus(self.image))
        self.assertFalse(revoke_community_quota_bonus(self.image))

    def test_the_votes_that_earned_it_survive_the_revoke(self) -> None:
        """Which is what makes re-contributing free rather than punitive."""
        self._grant()
        revoke_community_quota_bonus(self.image)
        self.assertEqual(community_relevant_vote_count(self.image), 2)

    def test_re_contributing_earns_it_back_with_no_new_votes(self) -> None:
        self._grant()
        revoke_community_quota_bonus(self.image)
        Image.objects.filter(pk=self.image.pk).update(wiki=None)
        self.image.refresh_from_db()

        Image.objects.filter(pk=self.image.pk).update(wiki=self.wiki)
        self.image.refresh_from_db()

        self.assertTrue(refresh_community_quota_bonus(self.image))
        self.assertEqual(get_storage_used_bytes(self.profile), 0)


class WikiDeleteQuotaBonusTests(TestCase):
    """Deleting a whole wiki ends the deleter's own bonuses and nobody else's.

    ``Image.wiki`` is ``SET_NULL``, so one delete detaches every contributor's
    photos at once. The one-way rule protects all of them except the deleter,
    whose own contribution stopped existing because they ended it - the same
    withdrawal ``revoke_community_quota_bonus`` covers one photo at a time.
    """

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make("dashboard.Location")
        self.parent_wiki = baker.make_recipe("dashboard.wiki", location=self.location)
        # resolve_visible_wiki reaches a wiki through a pin the viewer holds.
        baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)
        self.child_wiki = baker.make_recipe("dashboard.wiki", parent_wiki=self.parent_wiki)

    def _rewarded_photo(self, profile, wiki=None, size: int = 100) -> Image:
        """A photo already carrying the bonus, on ``wiki`` (the child by default)."""
        target = wiki or self.child_wiki
        image = _wiki_photo(profile, target, target.location, size=size)
        Image.objects.filter(pk=image.pk).update(quota_exempt_reason=QuotaExemption.COMMUNITY_CONTRIBUTION)
        image.refresh_from_db()
        return image

    def _delete_child_wiki(self):
        return self.client.delete(
            reverse("location.wiki.detail_pin.edit", args=[self.location.slug, self.child_wiki.uuid])
        )

    def test_deleting_the_wiki_ends_the_deleters_own_bonus(self) -> None:
        image = self._rewarded_photo(self.profile)
        self.assertEqual(get_storage_used_bytes(self.profile), 0)

        self.assertEqual(self._delete_child_wiki().status_code, 200)

        image.refresh_from_db()
        self.assertIsNone(image.wiki_id, "the photo is private again")
        self.assertEqual(image.quota_exempt_reason, "", "the contribution it paid for is gone")
        self.assertEqual(get_storage_used_bytes(self.profile), 100)

    def test_another_contributors_bonus_survives_the_same_delete(self) -> None:
        """Every contributor but the deleter is having this done to them."""
        other = baker.make(User).profile
        theirs = self._rewarded_photo(other)

        self.assertEqual(self._delete_child_wiki().status_code, 200)

        theirs.refresh_from_db()
        self.assertIsNone(theirs.wiki_id)
        self.assertEqual(theirs.quota_exempt_reason, QuotaExemption.COMMUNITY_CONTRIBUTION)
        self.assertEqual(get_storage_used_bytes(other), 0)

    def test_a_descendant_wikis_photos_are_covered_too(self) -> None:
        """The delete cascades down the subtree, so the revoke has to as well."""
        grandchild = baker.make_recipe("dashboard.wiki", parent_wiki=self.child_wiki)
        image = self._rewarded_photo(self.profile, wiki=grandchild)

        self.assertEqual(self._delete_child_wiki().status_code, 200)

        image.refresh_from_db()
        self.assertEqual(image.quota_exempt_reason, "")

    def test_the_delete_leaves_every_other_exemption_alone(self) -> None:
        """A photo exempt for an unrelated reason still stores no bytes."""
        image = _wiki_photo(self.profile, self.child_wiki, self.child_wiki.location, size=100)
        Image.objects.filter(pk=image.pk).update(quota_exempt_reason=QuotaExemption.EXTERNAL_MEDIA)

        self.assertEqual(self._delete_child_wiki().status_code, 200)

        image.refresh_from_db()
        self.assertEqual(image.quota_exempt_reason, QuotaExemption.EXTERNAL_MEDIA)

    def test_undo_gives_the_revoked_bonus_back(self) -> None:
        """The delete is undoable for seven days, so the revoke has to be too."""
        image = self._rewarded_photo(self.profile)
        self.assertEqual(self._delete_child_wiki().status_code, 200)
        image.refresh_from_db()
        self.assertEqual(image.quota_exempt_reason, "")

        restore_undo_action(UndoAction.objects.get(profile=self.profile, model_label="wiki"))

        image.refresh_from_db()
        self.assertIsNotNone(image.wiki_id, "the photo is back on the restored wiki")
        self.assertEqual(image.quota_exempt_reason, QuotaExemption.COMMUNITY_CONTRIBUTION)
        self.assertEqual(get_storage_used_bytes(self.profile), 0)

    def test_undo_does_not_invent_a_bonus_the_photo_never_had(self) -> None:
        """Restoring is not a grant: an unrewarded photo comes back unrewarded."""
        plain = _wiki_photo(self.profile, self.child_wiki, self.child_wiki.location, size=100)
        self.assertEqual(self._delete_child_wiki().status_code, 200)

        restore_undo_action(UndoAction.objects.get(profile=self.profile, model_label="wiki"))

        plain.refresh_from_db()
        self.assertEqual(plain.quota_exempt_reason, "")

    def test_the_low_engagement_sweep_takes_nothing_back(self) -> None:
        """Nobody performed that delete, so nobody's storage may move because of it."""
        image = self._rewarded_photo(self.profile)

        call_command("delete_low_engagement_wikis", "--yes", stdout=StringIO())

        image.refresh_from_db()
        self.assertIsNone(image.wiki_id)
        self.assertEqual(image.quota_exempt_reason, QuotaExemption.COMMUNITY_CONTRIBUTION)
        self.assertEqual(get_storage_used_bytes(self.profile), 0)
