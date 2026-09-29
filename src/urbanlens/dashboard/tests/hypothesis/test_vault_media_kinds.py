"""The Vault's per-kind galleries (Photos, Documents): ownership, paging and upload, asserted once per kind."""

from __future__ import annotations

from http import HTTPStatus
from typing import ClassVar
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.images import JPEG_BYTES
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.issues import PhotoUploadFailure
from urbanlens.dashboard.models.images.model import Image, MediaKind


class _VaultKindCase(TestCase):
    __test__ = False

    kind: ClassVar[str]
    page_url_name: ClassVar[str]
    items_url_name: ClassVar[str]
    upload_url_name: ClassVar[str]
    upload_field: ClassVar[str]
    other_field: ClassVar[str]
    missing_file_error: ClassVar[str]

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first account is promoted to site admin, which grants every feature
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.stranger = baker.make(User).profile
        self.client.force_login(self.user)

    def _item(self, profile, caption: str = "") -> Image:
        return baker.make(Image, profile=profile, media_type=self.kind, pin=None, wiki=None, caption=caption)


class _OwnershipCases(_VaultKindCase):
    def test_the_page_does_not_render_a_strangers_item(self) -> None:
        self._item(self.stranger, caption="stranger-secret-caption")
        own = self._item(self.profile, caption="my-own-caption")

        response = self.client.get(reverse(self.page_url_name))

        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertContains(response, "my-own-caption")
        self.assertNotContains(response, "stranger-secret-caption")
        self.assertContains(response, f'data-id="{own.pk}"')

    def test_the_items_endpoint_does_not_list_a_strangers_item(self) -> None:
        stranger_item = self._item(self.stranger)

        body = self.client.get(reverse(self.items_url_name)).json()

        self.assertEqual(body["total"], 0)
        self.assertNotIn(stranger_item.pk, [item["id"] for item in body["items"]])

    def test_a_stranger_cannot_delete_the_item(self) -> None:
        stranger_item = self._item(self.stranger)

        response = self.client.post(reverse("vault.photos.action", args=[stranger_item.pk, "delete"]))

        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
        self.assertTrue(Image.objects.filter(pk=stranger_item.pk).exists())

    def test_the_owner_can_delete_the_item(self) -> None:
        own = self._item(self.profile)

        response = self.client.post(reverse("vault.photos.action", args=[own.pk, "delete"]))

        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertFalse(Image.objects.filter(pk=own.pk).exists())

    def test_every_endpoint_turns_away_an_anonymous_request(self) -> None:
        self.client.logout()
        for name in (self.page_url_name, self.items_url_name):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, HTTPStatus.FOUND)
        response = self.client.post(
            reverse(self.upload_url_name), {self.upload_field: SimpleUploadedFile("a.txt", b"x")}
        )
        self.assertEqual(response.status_code, HTTPStatus.FOUND)
        self.assertFalse(Image.objects.exists())


class _ItemsPagingCases(_VaultKindCase):
    def _page(self, **params) -> dict:
        return self.client.get(reverse(self.items_url_name), params).json()

    def test_limit_is_clamped_to_between_one_and_a_hundred(self) -> None:
        for _ in range(3):
            self._item(self.profile)
        self.assertEqual(self._page(limit=0)["limit"], 1)
        self.assertEqual(len(self._page(limit=0)["items"]), 1)
        self.assertEqual(self._page(limit=1000)["limit"], 100)

    def test_malformed_paging_params_fall_back_to_the_defaults(self) -> None:
        self._item(self.profile)
        body = self._page(offset="nope", limit="nope")
        self.assertEqual((body["offset"], body["limit"]), (0, 24))
        self.assertEqual(self._page(offset=-5)["offset"], 0)

    def test_only_this_kind_is_listed(self) -> None:
        own = self._item(self.profile)
        for other in MediaKind.values:
            if other != self.kind:
                baker.make(Image, profile=self.profile, media_type=other, pin=None, wiki=None)

        body = self._page()

        self.assertEqual([item["id"] for item in body["items"]], [own.pk])


class _UploadCases(_VaultKindCase):
    def test_a_missing_file_is_a_400_naming_the_kind(self) -> None:
        response = self.client.post(reverse(self.upload_url_name), {})
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)
        self.assertEqual(response.json()["error"], self.missing_file_error)

    def test_the_other_kinds_field_name_is_not_read(self) -> None:
        response = self.client.post(
            reverse(self.upload_url_name),
            {self.other_field: SimpleUploadedFile("photo.jpg", JPEG_BYTES, content_type="image/jpeg")},
        )
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)
        self.assertFalse(Image.objects.filter(profile=self.profile).exists())

    def test_a_refused_upload_is_recorded_for_retry_under_its_filename(self) -> None:
        with patch("urbanlens.dashboard.models.subscriptions.user_has_feature", return_value=False):
            response = self.client.post(
                reverse(self.upload_url_name),
                {self.upload_field: SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")},
            )

        self.assertGreaterEqual(response.status_code, HTTPStatus.BAD_REQUEST)
        self.assertEqual(response.json()["error"], PhotoUploadFailure.objects.get(profile=self.profile).error)
        self.assertEqual(PhotoUploadFailure.objects.get(profile=self.profile).filename, "notes.txt")


class _PhotoKind:
    kind = MediaKind.PHOTO
    page_url_name = "vault.photos"
    items_url_name = "vault.photos.items"
    upload_url_name = "vault.photos.upload"
    upload_field = "image"
    other_field = "document"
    missing_file_error = "No image provided."


class _DocumentKind:
    kind = MediaKind.DOCUMENT
    page_url_name = "vault.documents"
    items_url_name = "vault.documents.items"
    upload_url_name = "vault.documents.upload"
    upload_field = "document"
    other_field = "image"
    missing_file_error = "No document provided."


class PhotoOwnershipTests(_PhotoKind, _OwnershipCases):
    __test__ = True


class DocumentOwnershipTests(_DocumentKind, _OwnershipCases):
    __test__ = True


class PhotoPagingTests(_PhotoKind, _ItemsPagingCases):
    __test__ = True


class DocumentPagingTests(_DocumentKind, _ItemsPagingCases):
    __test__ = True


class PhotoUploadTests(_PhotoKind, _UploadCases):
    __test__ = True

    def test_an_upload_is_not_captioned_with_its_filename(self) -> None:
        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"):
            response = self.client.post(
                reverse(self.upload_url_name),
                {self.upload_field: SimpleUploadedFile("holiday.jpg", JPEG_BYTES, content_type="image/jpeg")},
            )
        self.assertEqual(response.status_code, HTTPStatus.CREATED, response.content)
        self.assertFalse(Image.objects.get(profile=self.profile).caption)


class DocumentUploadTests(_DocumentKind, _UploadCases):
    __test__ = True


class DocumentsIgnoreShowFromOthersTests(_DocumentKind, _VaultKindCase):
    """``?show=from_others`` narrows Photos only; Documents has no such view and lists everything."""

    __test__ = True

    def test_the_documents_items_endpoint_ignores_show(self) -> None:
        own = self._item(self.profile)
        body = self.client.get(reverse("vault.documents.items"), {"show": "from_others"}).json()
        self.assertEqual([item["id"] for item in body["items"]], [own.pk])
