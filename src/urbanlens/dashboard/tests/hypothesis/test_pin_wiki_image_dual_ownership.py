"""A photo shared between a pin and a wiki must survive being deleted from either side.

``wiki_creation._seed_photos`` and ``PinGalleryBulkView``'s "send to wiki" action both
repoint an existing pin photo's ``wiki`` FK rather than copying the row - one ``Image``
can serve a pin and a wiki at once. Every place a user can delete a single photo
(``PinImageView.delete``, ``WikiImageView.delete``, ``PinGalleryBulkView``'s bulk
delete, and the mobile API's ``PhotoDetailView.delete``) used to call
``image.delete()`` unconditionally, destroying the other surface's copy with no
guard at all - worse than the pin-to-pin sharing case, which at least has
``delete_stored_file``'s reference-count check (see
``test_shared_image_file_deletion.py``). See docs/audits/GOALS_CODE_AUDIT.md
("Pin-to-wiki sharing").
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, QuotaExemption
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.media.images import detach_image_from_pin, detach_image_from_wiki
from urbanlens.dashboard.services.media.storage import get_storage_used_bytes


def _make_stored_image(**kwargs) -> Image:
    """An Image row with a real stored file, so file-survival can be asserted too."""
    image = Image.objects.create(**kwargs)
    image.image.save("dual-owned.jpg", ContentFile(b"jpeg-bytes"), save=True)
    return image


class _DualOwnershipTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = Location.objects.create(latitude=42.5, longitude=-73.5)
        self.pin = Pin.objects.create(profile=self.profile, location=self.location, name="Dual-owned spot")
        self.wiki = Wiki.objects.create(location=self.location)


class DetachImageFromPinTests(_DualOwnershipTestCase):
    """Unit-level: ``detach_image_from_pin``."""

    def test_dual_owned_image_is_unlinked_not_destroyed(self) -> None:
        image = _make_stored_image(pin=self.pin, wiki=self.wiki, location=self.location, profile=self.profile)
        stored_name = image.image.name

        detach_image_from_pin(image)

        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        image.refresh_from_db()
        self.assertIsNone(image.pin_id)
        self.assertEqual(image.wiki_id, self.wiki.pk)
        self.assertTrue(default_storage.exists(stored_name))

    def test_pin_only_image_is_still_fully_deleted(self) -> None:
        """No wiki side to protect - same outcome as before the fix."""
        image = _make_stored_image(pin=self.pin, location=self.location, profile=self.profile)
        stored_name = image.image.name

        detach_image_from_pin(image)

        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(default_storage.exists(stored_name))


class DetachImageFromWikiTests(_DualOwnershipTestCase):
    """Unit-level: ``detach_image_from_wiki`` - the mirror of the above."""

    def test_dual_owned_image_is_unlinked_not_destroyed(self) -> None:
        image = _make_stored_image(pin=self.pin, wiki=self.wiki, location=self.location, profile=self.profile)
        stored_name = image.image.name

        detach_image_from_wiki(image, withdrawn_by_contributor=True)

        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        image.refresh_from_db()
        self.assertIsNone(image.wiki_id)
        self.assertEqual(image.pin_id, self.pin.pk)
        self.assertTrue(default_storage.exists(stored_name))

    def test_wiki_only_image_is_still_fully_deleted(self) -> None:
        image = _make_stored_image(wiki=self.wiki, location=self.location, profile=self.profile)
        stored_name = image.image.name

        detach_image_from_wiki(image, withdrawn_by_contributor=True)

        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(default_storage.exists(stored_name))


class PinImageViewDeleteTests(_DualOwnershipTestCase):
    """Endpoint-level: DELETE .../gallery/<id>/ on a pin."""

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.user)

    def _delete(self, image: Image):
        return self.client.delete(
            reverse("pin.gallery.image", kwargs={"pin_slug": self.pin.slug, "image_id": image.pk})
        )

    def test_deleting_a_dual_owned_photo_leaves_the_wiki_copy(self) -> None:
        image = baker.make(Image, pin=self.pin, wiki=self.wiki, location=self.location, profile=self.profile)

        response = self._delete(image)

        self.assertEqual(response.status_code, 204)
        self.assertTrue(Image.objects.filter(pk=image.pk, wiki=self.wiki, pin__isnull=True).exists())

    def test_deleting_a_pin_only_photo_removes_the_row(self) -> None:
        image = baker.make(Image, pin=self.pin, location=self.location, profile=self.profile)

        response = self._delete(image)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())

    def test_another_users_photo_is_404_and_survives(self) -> None:
        other = baker.make(User)
        other_image = baker.make(Image, pin=self.pin, location=self.location, profile=other.profile)

        response = self._delete(other_image)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Image.objects.filter(pk=other_image.pk).exists())


class WikiImageViewDeleteTests(_DualOwnershipTestCase):
    """Endpoint-level: DELETE .../wiki/gallery/<id>/ on a wiki."""

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.user)

    def _delete(self, image: Image):
        return self.client.delete(
            reverse("location.wiki.gallery.image", kwargs={"location_slug": self.location.slug, "image_id": image.pk})
        )

    def test_deleting_a_dual_owned_photo_leaves_the_pin_copy(self) -> None:
        image = baker.make(Image, pin=self.pin, wiki=self.wiki, location=self.location, profile=self.profile)

        response = self._delete(image)

        self.assertEqual(response.status_code, 204)
        self.assertTrue(Image.objects.filter(pk=image.pk, pin=self.pin, wiki__isnull=True).exists())

    def test_deleting_a_wiki_only_photo_removes_the_row(self) -> None:
        image = baker.make(Image, wiki=self.wiki, location=self.location, profile=self.profile)

        response = self._delete(image)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())


class PinGalleryBulkDeleteTests(_DualOwnershipTestCase):
    """Endpoint-level: POST .../gallery/bulk/ {"action": "delete"} - a batch can mix
    dual-owned and solo photos, and each must be handled correctly on its own."""

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.user)

    def _bulk_delete(self, image_ids: list[int]):
        return self.client.post(
            reverse("pin.gallery.bulk", kwargs={"pin_slug": self.pin.slug}),
            data=json.dumps({"action": "delete", "image_ids": image_ids}),
            content_type="application/json",
        )

    def test_a_mixed_batch_unlinks_dual_owned_and_destroys_the_rest(self) -> None:
        dual = baker.make(Image, pin=self.pin, wiki=self.wiki, location=self.location, profile=self.profile)
        solo = baker.make(Image, pin=self.pin, location=self.location, profile=self.profile)

        response = self._bulk_delete([dual.pk, solo.pk])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"deleted": 1, "unlinked": 1})
        self.assertTrue(Image.objects.filter(pk=dual.pk, wiki=self.wiki, pin__isnull=True).exists())
        self.assertFalse(Image.objects.filter(pk=solo.pk).exists())

    def test_an_all_solo_batch_behaves_as_before(self) -> None:
        images = baker.make(Image, pin=self.pin, location=self.location, profile=self.profile, _quantity=3)

        response = self._bulk_delete([image.pk for image in images])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"deleted": 3, "unlinked": 0})
        self.assertEqual(Image.objects.filter(pk__in=[image.pk for image in images]).count(), 0)

    def test_an_all_dual_owned_batch_unlinks_every_row(self) -> None:
        duals = baker.make(
            Image, pin=self.pin, wiki=self.wiki, location=self.location, profile=self.profile, _quantity=2
        )

        response = self._bulk_delete([image.pk for image in duals])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"deleted": 0, "unlinked": 2})
        self.assertEqual(
            Image.objects.filter(pk__in=[image.pk for image in duals], wiki=self.wiki, pin__isnull=True).count(), 2
        )


class WithdrawingAContributedPhotoTests(_DualOwnershipTestCase):
    """The community quota bonus ends when its contributor withdraws the photo.

    The reward is one-way against *other people's* later actions - votes taken
    back, or the wiki deleted by an editor or by the low-engagement sweep -
    because someone comfortably inside their quota must not be pushed over it
    by a change they did not make. The contributor removing their own photo
    from the wiki is the case that rule was never meant to cover: the
    contribution the bonus paid for has stopped existing.

    The two cases are indistinguishable at the column - every one of them ends
    as ``wiki_id IS NULL`` with no record of who did it - so the intent has to
    be stated where the caller knows it, which is why these tests exercise the
    call sites rather than a signal.
    """

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.user)

    def _rewarded_photo(self, size: int = 100) -> Image:
        return Image.objects.create(
            image=SimpleUploadedFile("contributed.jpg", b"bytes", content_type="image/jpeg"),
            pin=self.pin,
            wiki=self.wiki,
            location=self.location,
            profile=self.profile,
            file_size=size,
            quota_exempt_reason=QuotaExemption.COMMUNITY_CONTRIBUTION,
        )

    def _delete(self, image: Image):
        return self.client.delete(
            reverse("location.wiki.gallery.image", kwargs={"location_slug": self.location.slug, "image_id": image.pk})
        )

    def test_withdrawing_a_rewarded_photo_ends_the_bonus(self) -> None:
        """Contribute, collect the votes, withdraw - and keep the free storage."""
        image = self._rewarded_photo()
        self.assertEqual(get_storage_used_bytes(self.profile), 0)

        self.assertEqual(self._delete(image).status_code, 204)

        image.refresh_from_db()
        self.assertIsNone(image.wiki_id, "the photo is private again")
        self.assertEqual(image.quota_exempt_reason, "", "it kept the bonus the withdrawn contribution earned")
        self.assertEqual(get_storage_used_bytes(self.profile), 100)

    def test_another_users_photo_is_404_and_keeps_its_bonus(self) -> None:
        stranger = baker.make(User).profile
        image = self._rewarded_photo()
        Image.objects.filter(pk=image.pk).update(profile=stranger)

        self.assertEqual(self._delete(image).status_code, 404)

        image.refresh_from_db()
        self.assertEqual(image.quota_exempt_reason, QuotaExemption.COMMUNITY_CONTRIBUTION)

    def test_the_bonus_survives_an_unlink_the_contributor_did_not_ask_for(self) -> None:
        """The one-way rule, and what a moderator-removal path would rely on."""
        image = self._rewarded_photo()

        detach_image_from_wiki(image, withdrawn_by_contributor=False)

        image.refresh_from_db()
        self.assertIsNone(image.wiki_id)
        self.assertEqual(image.quota_exempt_reason, QuotaExemption.COMMUNITY_CONTRIBUTION)
        self.assertEqual(get_storage_used_bytes(self.profile), 0)

    def test_deleting_the_wiki_leaves_the_bonus_intact(self) -> None:
        """`Image.wiki` is SET_NULL, and nobody's storage should move because of it.

        This passes on the code before the revoke existed, and is meant to: it
        is not evidence for the revoke but a guard against the two shapes that
        would have been easier to write - a `post_save` receiver, or a rule in
        the storage aggregate - both of which take the bonus back here, where
        the action was somebody else's.
        """
        image = self._rewarded_photo()

        Wiki.objects.filter(pk=self.wiki.pk).delete()

        image.refresh_from_db()
        self.assertIsNone(image.wiki_id)
        self.assertEqual(image.quota_exempt_reason, QuotaExemption.COMMUNITY_CONTRIBUTION)
        self.assertEqual(get_storage_used_bytes(self.profile), 0)

    def test_only_the_community_bonus_is_taken_back(self) -> None:
        """The column carries five different exemptions; four are nothing to do with this."""
        for reason in (
            QuotaExemption.EXTERNAL_MEDIA,
            QuotaExemption.SHARED_COPY,
            QuotaExemption.DEDUPLICATED,
            QuotaExemption.WIKI_COPY,
        ):
            with self.subTest(reason=reason):
                image = self._rewarded_photo()
                Image.objects.filter(pk=image.pk).update(quota_exempt_reason=reason)
                image.refresh_from_db()

                self.assertEqual(self._delete(image).status_code, 204)

                image.refresh_from_db()
                self.assertEqual(image.quota_exempt_reason, reason)
