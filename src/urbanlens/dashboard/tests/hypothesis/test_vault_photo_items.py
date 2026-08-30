"""Tests for the Vault Photos page's JSON items endpoint and sort options."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image

_ITEMS_URL = reverse("vault.photos.items")
_PAGE_URL = reverse("vault.photos")


class VaultPhotosPageTests(TestCase):
    """GET /vault/photos/ renders with the sort control and the first page embedded."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)

    def test_renders_with_no_photos(self) -> None:
        response = self.client.get(_PAGE_URL)
        self.assertEqual(response.status_code, 200)

    def test_renders_with_photos_and_sort_options(self) -> None:
        baker.make(Image, profile=self.profile, pin=None, wiki=None, _quantity=3)
        response = self.client.get(_PAGE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="vault-photos-sort"')
        self.assertContains(response, f'data-items-url="{_ITEMS_URL}"')


class PhotoItemsViewTests(TestCase):
    """GET /vault/photos/items/ - the windowed grid's JSON page endpoint."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.other_user: User = baker.make(User)
        self.other_profile = self.other_user.profile
        self.client = Client()
        self.client.force_login(self.user)

    def test_total_and_items_scoped_to_the_viewer(self) -> None:
        baker.make(Image, profile=self.profile, pin=None, wiki=None, _quantity=3)
        baker.make(Image, profile=self.other_profile, pin=None, wiki=None, _quantity=5)
        response = self.client.get(_ITEMS_URL)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(len(body["items"]), 3)
        self.assertEqual(body["offset"], 0)

    def test_offset_and_limit_page_through_results(self) -> None:
        baker.make(Image, profile=self.profile, pin=None, wiki=None, _quantity=5)
        response = self.client.get(_ITEMS_URL, {"offset": 2, "limit": 2})
        body = response.json()
        self.assertEqual(body["total"], 5)
        self.assertEqual(body["offset"], 2)
        self.assertEqual(body["limit"], 2)
        self.assertEqual(len(body["items"]), 2)

    def test_default_sort_is_recent_uploads_first(self) -> None:
        older = baker.make(Image, profile=self.profile, pin=None, wiki=None)
        Image.objects.filter(pk=older.pk).update(created=timezone.now() - timedelta(days=1))
        newer = baker.make(Image, profile=self.profile, pin=None, wiki=None)
        response = self.client.get(_ITEMS_URL)
        ids = [item["id"] for item in response.json()["items"]]
        self.assertEqual(ids, [newer.pk, older.pk])

    def test_oldest_sort_reverses_the_default_order(self) -> None:
        older = baker.make(Image, profile=self.profile, pin=None, wiki=None)
        Image.objects.filter(pk=older.pk).update(created=timezone.now() - timedelta(days=1))
        newer = baker.make(Image, profile=self.profile, pin=None, wiki=None)
        response = self.client.get(_ITEMS_URL, {"sort": "oldest"})
        ids = [item["id"] for item in response.json()["items"]]
        self.assertEqual(ids, [older.pk, newer.pk])

    def test_name_sort_orders_by_caption(self) -> None:
        zeta = baker.make(Image, profile=self.profile, pin=None, wiki=None, caption="zeta")
        alpha = baker.make(Image, profile=self.profile, pin=None, wiki=None, caption="alpha")
        response = self.client.get(_ITEMS_URL, {"sort": "name"})
        ids = [item["id"] for item in response.json()["items"]]
        self.assertEqual(ids, [alpha.pk, zeta.pk])

    def test_taken_sort_falls_back_to_created_when_taken_at_is_unset(self) -> None:
        older = baker.make(Image, profile=self.profile, pin=None, wiki=None, taken_at=None)
        Image.objects.filter(pk=older.pk).update(created=timezone.now() - timedelta(days=1))
        newer = baker.make(Image, profile=self.profile, pin=None, wiki=None, taken_at=None)
        response = self.client.get(_ITEMS_URL, {"sort": "taken"})
        ids = [item["id"] for item in response.json()["items"]]
        self.assertEqual(ids, [newer.pk, older.pk])

    def test_unknown_sort_falls_back_to_recent(self) -> None:
        older = baker.make(Image, profile=self.profile, pin=None, wiki=None)
        Image.objects.filter(pk=older.pk).update(created=timezone.now() - timedelta(days=1))
        newer = baker.make(Image, profile=self.profile, pin=None, wiki=None)
        response = self.client.get(_ITEMS_URL, {"sort": "not-a-real-sort"})
        ids = [item["id"] for item in response.json()["items"]]
        self.assertEqual(ids, [newer.pk, older.pk])

    def test_anonymous_request_is_redirected_to_login(self) -> None:
        self.client.logout()
        response = self.client.get(_ITEMS_URL)
        self.assertEqual(response.status_code, 302)
