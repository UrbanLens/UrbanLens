"""Tests for the site-admin tag mapping page (controllers.site_admin_external_tags)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.place.external_tag import ExternalTagSource
from urbanlens.dashboard.models.place.external_tag_group import ExternalTagGroup, ExternalTagVocabularyEntry
from urbanlens.dashboard.services.admin.site_admin import add_user_to_site_admin_group


class SiteAdminExternalTagsAccessTests(TestCase):
    """The mapping page and its actions require the view_site_admin permission."""

    def test_unauthenticated_user_is_redirected(self):
        client = Client()
        response = client.get(reverse("site_admin_external_tags"))
        self.assertEqual(response.status_code, 302)

    def test_regular_user_gets_403(self):
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        user = baker.make(User)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("site_admin_external_tags"))
        self.assertEqual(response.status_code, 403)

    def test_site_admin_gets_200(self):
        user = baker.make(User)
        add_user_to_site_admin_group(user)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("site_admin_external_tags"))
        self.assertEqual(response.status_code, 200)


class _AdminClientMixin:
    """Sets self.client to a logged-in site admin."""

    def setUp(self):
        super().setUp()
        self.user = baker.make(User)
        add_user_to_site_admin_group(self.user)
        self.client = Client()
        self.client.force_login(self.user)


class SiteAdminExternalTagsGroupViewTests(_AdminClientMixin, TestCase):
    def test_grouping_two_entries_succeeds(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        b = ExternalTagVocabularyEntry.objects.create(
            source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant"
        )

        response = self.client.post(reverse("site_admin_external_tags_group"), {"entry_id": [a.pk, b.pk]})

        self.assertEqual(response.status_code, 200)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertIsNotNone(a.group_id)
        self.assertEqual(a.group_id, b.group_id)

    def test_no_entries_returns_400_without_a_500(self):
        response = self.client.post(reverse("site_admin_external_tags_group"), {})

        self.assertEqual(response.status_code, 400)

    def test_a_malformed_entry_id_does_not_500(self):
        response = self.client.post(reverse("site_admin_external_tags_group"), {"entry_id": ["not-a-number"]})

        self.assertEqual(response.status_code, 400)


class SiteAdminExternalTagsMoveViewTests(_AdminClientMixin, TestCase):
    def test_ungrouping_an_entry_returns_json_ok(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        b = ExternalTagVocabularyEntry.objects.create(
            source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant"
        )
        from urbanlens.dashboard.services.locations.external_tag_groups import create_group

        create_group([a.pk, b.pk])

        response = self.client.post(reverse("site_admin_external_tags_move"), {"entry_id": a.pk, "target_group_id": ""})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        a.refresh_from_db()
        self.assertIsNone(a.group_id)

    def test_moving_the_last_member_out_reports_the_emptied_group(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        from urbanlens.dashboard.services.locations.external_tag_groups import create_group

        group = create_group([a.pk])

        response = self.client.post(reverse("site_admin_external_tags_move"), {"entry_id": a.pk, "target_group_id": ""})

        self.assertEqual(response.json()["emptied_group_id"], group.pk)
        self.assertFalse(ExternalTagGroup.objects.filter(pk=group.pk).exists())

    def test_missing_entry_id_returns_400_json(self):
        response = self.client.post(reverse("site_admin_external_tags_move"), {})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])

    def test_unknown_entry_id_returns_400_json_not_500(self):
        response = self.client.post(
            reverse("site_admin_external_tags_move"), {"entry_id": 999_999, "target_group_id": ""}
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])


class SiteAdminExternalTagsPreferredViewTests(_AdminClientMixin, TestCase):
    def test_setting_preferred_updates_the_flag(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        b = ExternalTagVocabularyEntry.objects.create(
            source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant"
        )
        from urbanlens.dashboard.services.locations.external_tag_groups import create_group

        group = create_group([a.pk, b.pk])

        response = self.client.post(
            reverse("site_admin_external_tags_preferred"), {"entry_id": b.pk, "group_id": group.pk}
        )

        self.assertEqual(response.status_code, 200)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_preferred)
        self.assertTrue(b.is_preferred)

    def test_missing_ids_returns_400_without_a_500(self):
        response = self.client.post(reverse("site_admin_external_tags_preferred"), {})

        self.assertEqual(response.status_code, 400)


class SiteAdminExternalTagsSearchViewTests(_AdminClientMixin, TestCase):
    def test_search_filters_ungrouped_pool(self):
        ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="shop", value="bakery")

        response = self.client.get(reverse("site_admin_external_tags_search"), {"q": "restaurant"})

        self.assertEqual(response.status_code, 200)
        pool_values = [entry.value for entry in response.context["singleton_pool"]]
        self.assertIn("restaurant", pool_values)
        self.assertNotIn("bakery", pool_values)
