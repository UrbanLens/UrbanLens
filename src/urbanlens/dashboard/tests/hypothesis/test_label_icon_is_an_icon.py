"""A label's icon is an icon, never markup (P128).

The add-pin dialog rendered ``Label.icon`` as HTML, and nothing server-side limited the column to the icon
shapes the picker offers: the external API, the bulk edit and the archive import all stored any 50-character
string. ``clean_icon`` already defined those shapes for the form paths; these tests hold every writer to it.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.labels.customization import LabelCustomization
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

HOSTILE = '<img src=x onerror="alert(1)">'
KEPT = ("🏥", "local_hospital", "/media/label_icons/a.png")

_BASE = "/dashboard/api/external/v1/labels/"


class TheColumnHoldsOnlyIconsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = Profile.objects.get(user=baker.make(User))

    def test_a_saved_label_drops_markup(self) -> None:
        label = ensure_label(profile=self.profile, name="Hostile", kind=KIND_TAG)
        label.icon = HOSTILE
        label.save()
        label.refresh_from_db()
        self.assertIsNone(label.icon)

    def test_a_saved_label_keeps_real_icons(self) -> None:
        label = ensure_label(profile=self.profile, name="Fine", kind=KIND_TAG)
        for icon in KEPT:
            with self.subTest(icon=icon):
                label.icon = icon
                label.save()
                label.refresh_from_db()
                self.assertEqual(label.icon, icon)

    def test_bulk_writes_drop_markup(self) -> None:
        created = Label.objects.bulk_create([Label(profile=self.profile, name="Bulk", kind=KIND_TAG, icon=HOSTILE)])
        self.assertIsNone(Label.objects.get(pk=created[0].pk).icon)

        label = ensure_label(profile=self.profile, name="Edited", kind=KIND_TAG)
        label.icon = HOSTILE
        Label.objects.bulk_update([label], ["icon"])
        self.assertIsNone(Label.objects.get(pk=label.pk).icon)

    def test_a_customization_drops_markup(self) -> None:
        label = ensure_label(profile=None, name="Global", kind=KIND_TAG)
        customization = LabelCustomization.objects.create(profile=self.profile, label=label, icon=HOSTILE, name="x")
        customization.refresh_from_db()
        self.assertIsNone(customization.icon)

    def test_a_pin_drops_markup(self) -> None:
        pin = baker.make(Pin, profile=self.profile, icon=HOSTILE)
        pin.refresh_from_db()
        self.assertIsNone(pin.icon)

        pin.icon = HOSTILE
        pin.save(update_fields=["icon"])
        pin.refresh_from_db()
        self.assertIsNone(pin.icon)


class TheApiRefusesMarkupTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        user = baker.make(User)
        self.profile = Profile.objects.get(user=user)
        _key, raw_key = generate_api_key(user, "Labels client")
        ApiKey.objects.filter(user=user).update(scopes=[ApiKeyScope.LABELS_READ.value, ApiKeyScope.LABELS_WRITE.value])
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}

    def _post(self, path: str, payload: dict):
        return self.client.post(f"{_BASE}{path}", payload, content_type="application/json", **self.auth)

    def test_create_refuses_a_markup_icon(self) -> None:
        response = self._post("", {"name": "Hostile", "kind": KIND_TAG, "icon": HOSTILE})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("icon", response.json()["fields"])
        self.assertFalse(Label.objects.filter(name="Hostile").exists())

    def test_create_accepts_an_emoji(self) -> None:
        response = self._post("", {"name": "P128 emoji label", "kind": KIND_TAG, "icon": "🏥"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Label.objects.get(name="P128 emoji label").icon, "🏥")

    def test_bulk_edit_refuses_a_markup_icon(self) -> None:
        label = ensure_label(profile=self.profile, name="Mine", kind=KIND_TAG)
        response = self._post("bulk/edit/", {"uuids": [str(label.uuid)], "icon": HOSTILE})
        self.assertEqual(response.status_code, 400, response.content)
        label.refresh_from_db()
        self.assertNotEqual(label.icon, HOSTILE)

    def test_customization_refuses_a_markup_icon(self) -> None:
        label = ensure_label(profile=None, name="Global", kind=KIND_TAG)
        response = self.client.put(
            f"{_BASE}{label.uuid}/customization/", {"icon": HOSTILE}, content_type="application/json", **self.auth
        )
        self.assertEqual(response.status_code, 400, response.content)
