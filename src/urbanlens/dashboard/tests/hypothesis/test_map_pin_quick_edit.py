"""Tests for MapController.map_pin_json and MapController.patch_pin (the map
popup's "Edit Pin" quick-edit flow).

Invariants verified:
  - map_pin_json's tags_data entries include each label's id, not just its
    name - the map popup's edit dialog matches labels by id to pre-fill the
    label picker, and names aren't guaranteed unique across label kinds/owners.
  - patch_pin round-trips label_ids taken straight from tags_data: saving a
    pin without changing its labels must not clear them (regression test for
    a bug where the edit dialog silently dropped a pin's labels on save
    because it could only match them by name).
  - patch_pin honors clear_custom_icon by removing an existing custom icon,
    but leaves it alone when the flag isn't sent.
  - map_pin_json separates "icon"/"color" (effective, possibly label-inherited
    display values) from "own_icon"/"own_custom_icon_url"/"own_color" (the
    pin's own overrides only). The edit dialog must pre-fill from the "own_*"
    fields - regression test for a bug where resaving a pin with no icon of
    its own silently baked in whichever label's icon it was currently
    inheriting for display, without the user ever touching the icon picker.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin


class MapPinJsonTagsDataTests(TestCase):
    """map_pin_json must expose enough per-label data to round-trip labels."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        location = baker.make(Location)
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None)

    def _tags_data(self) -> list[dict]:
        resp = self.client.get(f"/dashboard/map/pins/{self.pin.slug or self.pin.uuid}/")
        self.assertEqual(resp.status_code, 200)
        return resp.json()["pin"]["tags_data"]

    def test_tags_data_includes_label_id(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Urbex")
        self.pin.labels.add(label)
        tags_data = self._tags_data()
        self.assertEqual(len(tags_data), 1)
        self.assertEqual(tags_data[0]["id"], label.id)
        self.assertEqual(tags_data[0]["name"], "Urbex")

    def test_tags_data_disambiguates_same_named_labels_by_id(self) -> None:
        """Two distinct labels may legally share a name (no unique constraint) -
        matching by name alone would conflate them, so id must always be present."""
        tag = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Church")
        category = baker.make(Label, profile=self.profile, kind=KIND_CATEGORY, name="Church")
        self.pin.labels.add(tag, category)
        tags_data = self._tags_data()
        ids = {t["id"] for t in tags_data}
        self.assertEqual(ids, {tag.id, category.id})


class PatchPinLabelRoundTripTests(TestCase):
    """Saving the quick-edit form must not drop labels the pin already has."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        location = baker.make(Location)
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None)

    def _patch(self, **fields):
        return self.client.post(f"/dashboard/map/quick-edit/{self.pin.slug or self.pin.uuid}/", data=fields)

    def test_resubmitting_existing_label_ids_preserves_them(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Urbex")
        self.pin.labels.add(label)

        # Mirror what the edit dialog now does: read tags_data's ids straight
        # back out and resend them unchanged.
        tags_data = self.client.get(f"/dashboard/map/pins/{self.pin.slug or self.pin.uuid}/").json()["pin"]["tags_data"]
        label_ids = [str(t["id"]) for t in tags_data]

        resp = self._patch(name=self.pin.name or "Pin", label_ids=label_ids)
        self.assertEqual(resp.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(list(self.pin.labels.all()), [label])

    def test_empty_label_ids_clears_labels(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Urbex")
        self.pin.labels.add(label)

        resp = self._patch(name=self.pin.name or "Pin", label_ids=[""])
        self.assertEqual(resp.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(list(self.pin.labels.all()), [])


class PatchPinClearCustomIconTests(TestCase):
    """patch_pin must only clear an existing custom_icon when explicitly told to."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        location = baker.make(Location)
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None)
        self.pin.custom_icon = SimpleUploadedFile("icon.png", b"fake-png-bytes", content_type="image/png")
        self.pin.save()

    def _patch(self, **fields):
        return self.client.post(f"/dashboard/map/quick-edit/{self.pin.slug or self.pin.uuid}/", data=fields)

    def test_clear_custom_icon_flag_removes_existing_custom_icon(self) -> None:
        resp = self._patch(name=self.pin.name or "Pin", clear_custom_icon="1")
        self.assertEqual(resp.status_code, 200)
        self.pin.refresh_from_db()
        self.assertFalse(self.pin.custom_icon)

    def test_without_the_flag_existing_custom_icon_is_preserved(self) -> None:
        resp = self._patch(name=self.pin.name or "Pin")
        self.assertEqual(resp.status_code, 200)
        self.pin.refresh_from_db()
        self.assertTrue(self.pin.custom_icon)


class MapPinOwnIconColorFieldsTests(TestCase):
    """map_pin_json must expose the pin's own icon/color separately from the
    effective (possibly label-inherited) display values used for the marker."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        location = baker.make(Location)
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None, icon=None, color=None)

    def _pin_json(self) -> dict:
        resp = self.client.get(f"/dashboard/map/pins/{self.pin.slug or self.pin.uuid}/")
        self.assertEqual(resp.status_code, 200)
        return resp.json()["pin"]

    def test_effective_icon_falls_back_to_label_but_own_icon_does_not(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Factory", icon="factory")
        self.pin.labels.add(label)
        data = self._pin_json()
        self.assertEqual(data["icon"], "factory")  # effective: used for the map marker
        self.assertIsNone(data["own_icon"])  # the pin has no icon of its own
        self.assertIsNone(data["own_custom_icon_url"])

    def test_own_icon_reflects_the_pins_own_stored_icon(self) -> None:
        self.pin.icon = "castle"
        self.pin.save(update_fields=["icon"])
        data = self._pin_json()
        self.assertEqual(data["own_icon"], "castle")

    def test_own_custom_icon_url_set_when_pin_has_a_custom_icon(self) -> None:
        self.pin.custom_icon = SimpleUploadedFile("icon.png", b"fake-png-bytes", content_type="image/png")
        self.pin.save()
        data = self._pin_json()
        self.assertTrue(data["own_custom_icon_url"])

    def test_own_color_falls_back_to_label_but_effective_color_does_not_leak_into_it(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Factory", icon="factory", color="#ff0000")
        self.pin.labels.add(label)
        data = self._pin_json()
        self.assertEqual(data["color"], "#ff0000")  # effective: used for the map marker
        self.assertIsNone(data["own_color"])  # the pin has no color of its own

    def test_resaving_with_own_icon_does_not_bake_in_the_labels_icon(self) -> None:
        """Regression test: mirrors the fixed openEditPinDialog, which pre-fills
        the icon input from own_icon (empty here), not the label-inherited
        effective icon, then resubmits that unchanged value on an unrelated save."""
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Factory", icon="factory")
        self.pin.labels.add(label)

        own_icon = self._pin_json()["own_icon"]
        resp = self.client.post(
            f"/dashboard/map/quick-edit/{self.pin.slug or self.pin.uuid}/",
            data={"name": "Renamed", "icon": own_icon or ""},
        )
        self.assertEqual(resp.status_code, 200)
        self.pin.refresh_from_db()

        # The pin's own icon must still be unset, even after the label that was
        # supplying the display icon is removed.
        self.pin.labels.remove(label)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.icon)
