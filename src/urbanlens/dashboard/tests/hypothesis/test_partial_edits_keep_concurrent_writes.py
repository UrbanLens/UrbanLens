"""A partial edit writes only what it changed, so a concurrent edit to another field survives it (P5).

Each test lands a second writer's change between the handler loading the row and saving it, which is
what two overlapping autosaves or two editors of one annotation produce.
"""

from __future__ import annotations

import json
from unittest import mock

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.abstract.field_snapshot import FieldSnapshot
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import CustomLayer, PinMarkup
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings


def _concurrent_write_before_save(model, **other_writer):
    """Patch ``model.save`` so another writer's ``update`` lands just before the real save runs."""
    original = model.save

    def save(self, *args, **kwargs):
        model.objects.filter(pk=self.pk).update(**other_writer)
        return original(self, *args, **kwargs)

    return mock.patch.object(model, "save", autospec=True, side_effect=save)


class FieldSnapshotTests(TestCase):
    def test_nothing_changed_writes_nothing(self) -> None:
        layer = CustomLayer.objects.create(
            name="Tunnels", parent_pin=baker.make(Pin), profile=baker.make("auth.User").profile
        )
        with mock.patch.object(CustomLayer, "save") as save:
            self.assertEqual(FieldSnapshot(layer).save_changes(), [])
        save.assert_not_called()

    def test_an_in_place_change_to_a_json_value_counts_as_changed(self) -> None:
        item = PinMarkup(markup_type="polygon", geometry={"type": "Polygon", "coordinates": [[[0, 0]]]})
        snapshot = FieldSnapshot(item)
        item.geometry["coordinates"][0][0] = [1, 1]
        self.assertEqual(snapshot.changed(), ["geometry"])


class SiteSettingsAutosaveTests(TestCase):
    def test_saving_one_setting_keeps_another_admins_concurrent_change(self) -> None:
        settings = SiteSettings.get_current()
        admin = baker.make("auth.User", is_staff=True, is_superuser=True)
        self.client.force_login(admin)
        new_quota = settings.storage_quota_gb + 7
        other_members = settings.max_trip_members + 5

        with _concurrent_write_before_save(SiteSettings, max_trip_members=other_members):
            response = self.client.post(reverse("site_admin"), {"storage_quota_gb": new_quota})

        self.assertLess(response.status_code, 400)
        row = SiteSettings.objects.get(pk=settings.pk)
        self.assertEqual(row.storage_quota_gb, new_quota)
        self.assertEqual(row.max_trip_members, other_members, "the autosave reverted a setting it was never sent")


class _OwnPinCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location))


class MarkupEditTests(_OwnPinCase):
    def test_relabelling_keeps_a_concurrent_colour_change(self) -> None:
        created = self.client.post(
            reverse("pin.markup", kwargs={"pin_slug": self.pin.slug}),
            data=json.dumps(
                {"markup_type": "polygon", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 0]]]}}
            ),
            content_type="application/json",
        )
        self.assertLess(created.status_code, 400)
        item = PinMarkup.objects.get(profile=self.profile)

        with _concurrent_write_before_save(PinMarkup, color="#00ff00"):
            response = self.client.post(
                reverse("pin.markup.edit", kwargs={"pin_slug": self.pin.slug, "markup_uuid": item.uuid}),
                data=json.dumps({"label": "Loading dock"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.label, "Loading dock")
        self.assertEqual(item.color, "#00ff00", "the relabel reverted a colour it was never sent")


class CustomLayerEditTests(_OwnPinCase):
    def test_renaming_keeps_a_concurrent_visibility_change(self) -> None:
        layer = CustomLayer.objects.create(
            name="Tunnels", parent_pin=self.pin, profile=self.profile, default_visible=True
        )

        with _concurrent_write_before_save(CustomLayer, default_visible=False):
            response = self.client.post(
                reverse("pin.layers.edit", args=[self.pin.slug, layer.uuid]), {"name": "Drains"}
            )

        self.assertEqual(response.status_code, 200)
        layer.refresh_from_db()
        self.assertEqual(layer.name, "Drains")
        self.assertFalse(layer.default_visible, "the rename reverted a visibility it was never sent")
