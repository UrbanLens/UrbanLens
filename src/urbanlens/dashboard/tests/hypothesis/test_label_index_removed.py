"""The per-kind label pages duplicated the Organize page's tabs, and are gone (Jess, 2026-09-30)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import NoReverseMatch, reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


class LabelIndexRemovedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(baker.make(User))

    def test_no_kind_has_its_own_index_page(self) -> None:
        with self.assertRaises(NoReverseMatch):
            reverse("label.index", kwargs={"label_kind": "tags"})
        for kind in ("tags", "categories", "statuses", "people", "media"):
            with self.subTest(kind=kind):
                self.assertEqual(self.client.get(f"/dashboard/{kind}/").status_code, 404)

    def test_the_organize_tabs_still_serve_each_kind(self) -> None:
        for tab in ("tags", "categories", "status", "people", "media"):
            with self.subTest(tab=tab):
                response = self.client.get(reverse("organize.index"), {"tab": tab})
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "data-standalone-mode")

    def test_the_label_routes_under_each_kind_still_resolve(self) -> None:
        self.assertEqual(self.client.get(reverse("label.rows", kwargs={"label_kind": "tag"})).status_code, 200)
