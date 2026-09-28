"""Per-account row limits from SiteSettings are enforced on every write path through services.core.capacity."""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.album.model import Album
from urbanlens.dashboard.models.custom_fields.model import CustomField, CustomFieldEntity
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.push_device.model import PushDevice
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.core.capacity import (
    LABELS,
    SAVED_FILTERS,
    CapacityExceededError,
    reserve,
)
from urbanlens.dashboard.services.import_export.import_data import ImportContext, ImportResult, SavedFiltersImport
from urbanlens.dashboard.services.notifications.push import PushTransport, register_device
from urbanlens.dashboard.services.photos.albums import add_images_to_album
from urbanlens.dashboard.services.undo.handlers.saved_filter import SavedFilterUndoHandler
from urbanlens.dashboard.services.undo.service import UndoExpiredError

_API = "/dashboard/api/external/v1/"


def _limit(**values: int) -> None:
    SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(**values)


class CapacityTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="capped", password="pw")
        self.profile = self.user.profile

    def _labels_owned(self) -> int:
        return Label.objects.filter(profile=self.profile).count()


class ReserveTests(CapacityTestCase):
    def test_refuses_past_the_limit_and_names_it(self) -> None:
        seeded = SavedFilter.objects.filter(profile=self.profile).count()
        _limit(max_saved_filters_per_user=seeded + 1)
        with reserve(SAVED_FILTERS, self.profile.pk):
            SavedFilter.objects.create(profile=self.profile, name="One", criteria={})
        with self.assertRaises(CapacityExceededError) as caught, reserve(SAVED_FILTERS, self.profile.pk):
            SavedFilter.objects.create(profile=self.profile, name="Two", criteria={})
        self.assertIn(f"{seeded + 1} saved filters", caught.exception.user_message)
        self.assertEqual(SavedFilter.objects.filter(profile=self.profile).count(), seeded + 1)

    def test_zero_means_unlimited(self) -> None:
        _limit(max_saved_filters_per_user=0)
        for index in range(3):
            with reserve(SAVED_FILTERS, self.profile.pk):
                SavedFilter.objects.create(profile=self.profile, name=f"F{index}", criteria={})
        self.assertEqual(SAVED_FILTERS.ceiling(), SAVED_FILTERS.unlimited_ceiling)

    def test_counts_every_row_being_added(self) -> None:
        seeded = SavedFilter.objects.filter(profile=self.profile).count()
        _limit(max_saved_filters_per_user=seeded + 2)
        with self.assertRaises(CapacityExceededError) as caught, reserve(SAVED_FILTERS, self.profile.pk, adding=3):
            pass
        self.assertIn("room for 2 more", caught.exception.user_message)

    def test_holds_a_per_owner_lock_for_the_transaction(self) -> None:
        """Two concurrent adds serialise on this lock; without it both would count the same free slot."""
        _limit(max_saved_filters_per_user=5)
        with reserve(SAVED_FILTERS, self.profile.pk), connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
            (held,) = cursor.fetchone()
        self.assertGreaterEqual(held, 1)


class SavedFilterCapTests(CapacityTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.existing = SavedFilter.objects.filter(profile=self.profile).count()
        self.assertGreater(self.existing, 0, "a limit of 0 would mean unlimited")
        _limit(max_saved_filters_per_user=self.existing)

    def test_map_create_is_refused_at_the_cap(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post(reverse("saved_filters.create"), {"filter_name": "More", "min_rating": "3"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(SavedFilter.objects.filter(profile=self.profile).count(), self.existing)

    def test_api_create_is_refused_at_the_cap(self) -> None:
        _key, raw = generate_api_key(self.user, "client")
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.LISTS_WRITE.value])
        response = self.client.post(
            f"{_API}saved-filters/",
            {"name": "More", "criteria": {"min_rating": 3}},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw}",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SavedFilter.objects.filter(profile=self.profile).count(), self.existing)

    def test_import_skips_the_overflow_with_one_warning(self) -> None:
        _limit(max_saved_filters_per_user=self.existing + 1)
        result = ImportResult()
        ctx = ImportContext(profile=self.profile, data_dir="", result=result, pin_uuid_map={}, label_uuid_map={})
        SavedFiltersImport().run([{"name": f"Imported {index}", "criteria": {}} for index in range(3)], ctx)
        self.assertEqual(SavedFilter.objects.filter(profile=self.profile).count(), self.existing + 1)
        self.assertEqual(len([warning for warning in result.warnings if "saved filters" in warning]), 1)

    def test_undo_refuses_to_restore_past_the_cap(self) -> None:
        payload = [{"profile_id": self.profile.pk, "fields": {"name": "Restored", "criteria": {}}}]
        with self.assertRaises(UndoExpiredError):
            SavedFilterUndoHandler.restore(payload)


class PinListCapTests(CapacityTestCase):
    def test_create_is_refused_at_the_cap(self) -> None:
        PinList.objects.create(profile=self.profile, name="Only")
        _limit(max_pin_lists_per_user=1)
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("lists.create"), json.dumps({"name": "More"}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 409)
        self.assertFalse(PinList.objects.filter(profile=self.profile, name="More").exists())


class LabelCapTests(CapacityTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertGreater(self._labels_owned(), 0, "a limit of 0 would mean unlimited")
        _limit(max_labels_per_user=self._labels_owned())

    def test_a_new_name_is_refused_but_an_existing_one_still_resolves(self) -> None:
        existing = Label.objects.filter(profile=self.profile).first()
        self.assertIsNotNone(existing)
        with self.assertRaises(CapacityExceededError):
            Label.objects.resolve_or_create(self.profile, "Brand new", KIND_TAG)
        found, created = Label.objects.resolve_or_create(self.profile, existing.name, existing.kind)
        self.assertEqual((found.pk, created), (existing.pk, False))

    def test_create_unique_is_refused(self) -> None:
        with self.assertRaises(CapacityExceededError):
            Label.objects.create_unique(profile=self.profile, name="Brand new", kind=KIND_TAG)

    def test_global_labels_do_not_count(self) -> None:
        baker.make(Label, profile=None, name="Site wide", kind=KIND_TAG)
        self.assertEqual(LABELS.in_use(self.profile.pk).count(), self._labels_owned())

    def test_the_organize_create_view_answers_409(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post(reverse("label.create", kwargs={"label_kind": "tags"}), {"name": "Brand new"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(Label.objects.filter(profile=self.profile, name="Brand new").exists())


class CustomFieldCapTests(CapacityTestCase):
    def test_settings_create_is_refused_at_the_cap(self) -> None:
        _limit(max_custom_fields_per_user=1)
        CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PIN, name="First")
        self.client.force_login(self.user)
        self.client.post(
            reverse("custom_fields.settings"),
            {"entity_type": CustomFieldEntity.PIN, "name": "Second", "field_type": "text"},
        )
        self.assertFalse(CustomField.objects.filter(profile=self.profile, name="Second").exists())


@mock.patch("urbanlens.dashboard.services.notifications.push._validate_unifiedpush_endpoint")
class PushDeviceCapTests(CapacityTestCase):
    def _register(self, address: str) -> PushDevice:
        return register_device(self.profile, transport=PushTransport.UNIFIEDPUSH, address=address)

    def test_a_new_address_past_the_cap_is_refused(self, _validate) -> None:
        _limit(max_push_devices_per_user=2)
        self._register("https://push.example/a")
        self._register("https://push.example/b")
        with self.assertRaises(CapacityExceededError):
            self._register("https://push.example/c")

    def test_re_registering_an_active_address_takes_no_room(self, _validate) -> None:
        _limit(max_push_devices_per_user=1)
        first = self._register("https://push.example/a")
        self.assertEqual(self._register("https://push.example/a").pk, first.pk)

    def test_revoked_devices_do_not_count_until_revived(self, _validate) -> None:
        _limit(max_push_devices_per_user=1)
        old = self._register("https://push.example/old")
        PushDevice.objects.filter(pk=old.pk).update(revoked_at="2026-01-01T00:00:00Z")
        self._register("https://push.example/new")
        with self.assertRaises(CapacityExceededError):
            self._register("https://push.example/old")


class AlbumCapTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make_recipe("dashboard.pin")
        self.images = [baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile) for _ in range(4)]
        self.album = Album.objects.create(name="Interior", profile=self.pin.profile, parent_pin=self.pin)
        _limit(max_photos_per_album=3)

    def test_a_batch_that_would_overflow_is_refused_whole(self) -> None:
        add_images_to_album(self.album, self.images[:2], self.pin.profile)
        with self.assertRaises(CapacityExceededError):
            add_images_to_album(self.album, self.images[2:], self.pin.profile)
        self.assertEqual(self.album.items.count(), 2)

    def test_photos_already_in_the_album_take_no_room(self) -> None:
        add_images_to_album(self.album, self.images[:3], self.pin.profile)
        self.assertEqual(add_images_to_album(self.album, self.images[:3], self.pin.profile), 0)

    def test_reorder_naming_more_items_than_an_album_holds_is_refused(self) -> None:
        self.client.force_login(self.pin.profile.user)
        response = self.client.post(
            reverse("pin.albums.reorder", kwargs={"pin_slug": self.pin.slug, "album_slug": self.album.slug}),
            json.dumps({"items": list(range(1, 10))}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
