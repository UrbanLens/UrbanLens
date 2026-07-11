"""Tests for custom fields: models, settings CRUD, value editing, map filtering, and export."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
import os
import tempfile

from django.urls import reverse
from hypothesis import given, settings as hypothesis_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.forms.search import SearchForm
from urbanlens.dashboard.models.custom_fields.model import CustomField, CustomFieldEntity, CustomFieldType, CustomFieldValue
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.export import _export_custom_fields


class CustomFieldTestsBase(TestCase):
    """Shared fixture: a logged-in user with a profile and a pin."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Old Mill", name_is_user_provided=True)
        self.client.force_login(self.user)

    def _field(self, entity_type: str = CustomFieldEntity.PIN, name: str = "Gate code", field_type: str = CustomFieldType.TEXT) -> CustomField:
        return CustomField.objects.create(profile=self.profile, entity_type=entity_type, name=name, field_type=field_type)


class CustomFieldValueParsingTests(TestCase):
    """set_value/display_value round-trips per field type (no DB writes)."""

    def _value(self, field_type: str) -> CustomFieldValue:
        return CustomFieldValue(field=CustomField(field_type=field_type))

    @given(st.decimals(allow_nan=False, allow_infinity=False, min_value=Decimal("-999999999"), max_value=Decimal("999999999"), places=6))
    @hypothesis_settings(max_examples=25, deadline=None)
    def test_number_round_trip(self, number: Decimal) -> None:
        value = self._value(CustomFieldType.NUMBER)
        value.set_value(str(number))
        self.assertEqual(value.value_number, number)
        self.assertEqual(Decimal(value.display_value), number)

    @given(st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31)))
    @hypothesis_settings(max_examples=25, deadline=None)
    def test_date_round_trip(self, day: date) -> None:
        value = self._value(CustomFieldType.DATE)
        value.set_value(day.isoformat())
        self.assertEqual(value.value_date, day)
        self.assertEqual(value.display_value, day.isoformat())

    @given(st.text(min_size=1, max_size=200).filter(lambda s: s.strip()))
    @hypothesis_settings(max_examples=25, deadline=None)
    def test_text_round_trip(self, text: str) -> None:
        value = self._value(CustomFieldType.TEXT)
        value.set_value(text)
        self.assertEqual(value.value_text, text.strip())

    def test_invalid_number_raises(self) -> None:
        value = self._value(CustomFieldType.NUMBER)
        with self.assertRaises(ValueError):
            value.set_value("not a number")

    def test_invalid_date_raises(self) -> None:
        value = self._value(CustomFieldType.DATE)
        with self.assertRaises(ValueError):
            value.set_value("2026-13-45")

    def test_empty_value_raises(self) -> None:
        value = self._value(CustomFieldType.TEXT)
        with self.assertRaises(ValueError):
            value.set_value("   ")

    def test_display_value_strips_trailing_zeros(self) -> None:
        value = self._value(CustomFieldType.NUMBER)
        value.set_value("3.500000")
        self.assertEqual(value.display_value, "3.5")


class CustomFieldSettingsViewTests(CustomFieldTestsBase):
    """Settings > Customize panel CRUD."""

    def test_panel_lists_all_entity_groups(self) -> None:
        response = self.client.get(reverse("custom_fields.settings"))
        self.assertEqual(response.status_code, 200)
        for label in ("Pins", "Photos", "People", "Maps"):
            self.assertContains(response, label)

    def test_create_field(self) -> None:
        response = self.client.post(
            reverse("custom_fields.settings"),
            {"entity_type": "pin", "name": "Gate code", "field_type": "text"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(CustomField.objects.filter(profile=self.profile, entity_type="pin", name="Gate code").exists())

    def test_create_duplicate_name_rejected(self) -> None:
        self._field(name="Gate code")
        self.client.post(
            reverse("custom_fields.settings"),
            {"entity_type": "pin", "name": "gate CODE", "field_type": "number"},
        )
        self.assertEqual(CustomField.objects.filter(profile=self.profile, entity_type="pin").count(), 1)

    def test_create_invalid_entity_rejected(self) -> None:
        self.client.post(
            reverse("custom_fields.settings"),
            {"entity_type": "starship", "name": "Warp factor", "field_type": "number"},
        )
        self.assertFalse(CustomField.objects.filter(profile=self.profile).exists())

    def test_rename_field(self) -> None:
        field = self._field(name="Gate code")
        response = self.client.post(
            reverse("custom_fields.update", args=[field.id]),
            {"name": "Door code", "field_type": "text"},
        )
        self.assertEqual(response.status_code, 200)
        field.refresh_from_db()
        self.assertEqual(field.name, "Door code")

    def test_type_change_blocked_when_values_exist(self) -> None:
        field = self._field(name="Gate code")
        CustomFieldValue.objects.create(field=field, pin=self.pin, value_text="1234")
        self.client.post(
            reverse("custom_fields.update", args=[field.id]),
            {"name": "Gate code", "field_type": "number"},
        )
        field.refresh_from_db()
        self.assertEqual(field.field_type, CustomFieldType.TEXT)

    def test_delete_field_cascades_values(self) -> None:
        field = self._field()
        CustomFieldValue.objects.create(field=field, pin=self.pin, value_text="1234")
        response = self.client.delete(reverse("custom_fields.delete", args=[field.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CustomField.objects.filter(pk=field.pk).exists())
        self.assertFalse(CustomFieldValue.objects.filter(field_id=field.pk).exists())

    def test_cannot_touch_another_users_field(self) -> None:
        other_profile = baker.make("auth.User").profile
        field = CustomField.objects.create(profile=other_profile, entity_type="pin", name="Theirs")
        response = self.client.post(
            reverse("custom_fields.update", args=[field.id]),
            {"name": "Mine now", "field_type": "text"},
        )
        self.assertEqual(response.status_code, 404)
        response = self.client.delete(reverse("custom_fields.delete", args=[field.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(CustomField.objects.filter(pk=field.pk).exists())


class PinCustomFieldPanelTests(CustomFieldTestsBase):
    """The pin detail Custom Fields card and its value endpoint."""

    def test_panel_renders_add_form_when_no_fields(self) -> None:
        response = self.client.get(reverse("pin.custom_fields", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cf-add-form")

    def test_panel_requires_pin_ownership(self) -> None:
        other_pin = baker.make(Pin, profile=baker.make("auth.User").profile, name="Not Yours")
        response = self.client.get(reverse("pin.custom_fields", args=[other_pin.slug]))
        self.assertEqual(response.status_code, 404)

    def test_create_field_inline(self) -> None:
        response = self.client.post(
            reverse("pin.custom_fields", args=[self.pin.slug]),
            {"name": "Floors", "field_type": "number"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(CustomField.objects.filter(profile=self.profile, entity_type="pin", name="Floors").exists())

    def test_set_update_and_clear_value(self) -> None:
        field = self._field(name="Floors", field_type=CustomFieldType.NUMBER)
        url = reverse("pin.custom_fields.value", args=[self.pin.slug, field.id])

        self.client.post(url, {"value": "12"})
        value = CustomFieldValue.objects.get(field=field, pin=self.pin)
        self.assertEqual(value.value_number, Decimal("12"))

        self.client.post(url, {"value": "14.5"})
        value.refresh_from_db()
        self.assertEqual(value.value_number, Decimal("14.5"))

        self.client.post(url, {"value": ""})
        self.assertFalse(CustomFieldValue.objects.filter(field=field, pin=self.pin).exists())

    def test_invalid_value_not_stored(self) -> None:
        field = self._field(name="Floors", field_type=CustomFieldType.NUMBER)
        url = reverse("pin.custom_fields.value", args=[self.pin.slug, field.id])
        self.client.post(url, {"value": "twelve"})
        self.assertFalse(CustomFieldValue.objects.filter(field=field, pin=self.pin).exists())

    def test_value_rejected_for_field_of_wrong_entity(self) -> None:
        field = self._field(name="Camera", entity_type=CustomFieldEntity.PHOTO)
        url = reverse("pin.custom_fields.value", args=[self.pin.slug, field.id])
        response = self.client.post(url, {"value": "X100"})
        self.assertEqual(response.status_code, 404)


class ProfileCustomFieldValueTests(CustomFieldTestsBase):
    """People fields on another user's profile page."""

    def setUp(self) -> None:
        super().setUp()
        self.subject = Profile.objects.get(user=baker.make("auth.User"))
        self.subject.ensure_slug()

    def test_set_value_on_other_profile(self) -> None:
        field = self._field(name="Met at", entity_type=CustomFieldEntity.PROFILE)
        response = self.client.post(
            reverse("profile.custom_field_value", args=[self.subject.slug, field.id]),
            {"value": "Kingston meetup"},
        )
        self.assertEqual(response.status_code, 200)
        value = CustomFieldValue.objects.get(field=field, target_profile=self.subject)
        self.assertEqual(value.value_text, "Kingston meetup")

    def test_cannot_annotate_own_profile(self) -> None:
        field = self._field(name="Met at", entity_type=CustomFieldEntity.PROFILE)
        self.profile.ensure_slug()
        response = self.client.post(
            reverse("profile.custom_field_value", args=[self.profile.slug, field.id]),
            {"value": "myself"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CustomFieldValue.objects.filter(field=field).exists())


class LightboxStripTests(CustomFieldTestsBase):
    """Photo and markup-map custom-field strips."""

    def test_photo_strip_204_without_fields(self) -> None:
        image = baker.make(Image, profile=self.profile)
        response = self.client.get(reverse("custom_fields.photo", args=[image.pk]))
        self.assertEqual(response.status_code, 204)

    def test_photo_strip_renders_and_saves(self) -> None:
        image = baker.make(Image, profile=self.profile)
        field = self._field(name="Camera", entity_type=CustomFieldEntity.PHOTO)
        response = self.client.get(reverse("custom_fields.photo", args=[image.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Camera")

        response = self.client.post(
            reverse("custom_fields.photo", args=[image.pk]),
            {"field_id": field.id, "value": "X100V"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CustomFieldValue.objects.get(field=field, image=image).value_text, "X100V")

    def test_photo_strip_hidden_for_someone_elses_photo(self) -> None:
        self._field(name="Camera", entity_type=CustomFieldEntity.PHOTO)
        other_image = baker.make(Image, profile=baker.make("auth.User").profile)
        response = self.client.get(reverse("custom_fields.photo", args=[other_image.pk]))
        self.assertEqual(response.status_code, 204)

    def test_markup_map_strip_renders_and_saves(self) -> None:
        markup_map = baker.make(MarkupMap, profile=self.profile)
        field = self._field(name="Route length", entity_type=CustomFieldEntity.MARKUP_MAP, field_type=CustomFieldType.NUMBER)
        response = self.client.get(reverse("custom_fields.markup_map", args=[markup_map.uuid]))
        self.assertEqual(response.status_code, 200)

        self.client.post(
            reverse("custom_fields.markup_map", args=[markup_map.uuid]),
            {"field_id": field.id, "value": "4.2"},
        )
        self.assertEqual(CustomFieldValue.objects.get(field=field, markup_map=markup_map).value_number, Decimal("4.2"))

    def test_markup_map_strip_hidden_for_someone_elses_map(self) -> None:
        self._field(name="Route length", entity_type=CustomFieldEntity.MARKUP_MAP)
        other_map = baker.make(MarkupMap, profile=baker.make("auth.User").profile)
        response = self.client.get(reverse("custom_fields.markup_map", args=[other_map.uuid]))
        self.assertEqual(response.status_code, 204)


class CustomFieldMapFilterTests(CustomFieldTestsBase):
    """SearchForm dynamic fields and PinQuerySet.filter_by_custom_fields."""

    def setUp(self) -> None:
        super().setUp()
        self.text_field = self._field(name="Material", field_type=CustomFieldType.TEXT)
        self.number_field = self._field(name="Floors", field_type=CustomFieldType.NUMBER)
        self.date_field = self._field(name="Permit until", field_type=CustomFieldType.DATE)

        self.brick_pin = baker.make(Pin, profile=self.profile, name="Brick Factory", name_is_user_provided=True)
        CustomFieldValue.objects.create(field=self.text_field, pin=self.brick_pin, value_text="red brick")
        CustomFieldValue.objects.create(field=self.number_field, pin=self.brick_pin, value_number=Decimal("3"))

        self.steel_pin = baker.make(Pin, profile=self.profile, name="Steel Mill", name_is_user_provided=True)
        CustomFieldValue.objects.create(field=self.text_field, pin=self.steel_pin, value_text="steel")
        CustomFieldValue.objects.create(field=self.number_field, pin=self.steel_pin, value_number=Decimal("12"))
        CustomFieldValue.objects.create(field=self.date_field, pin=self.steel_pin, value_date=date(2026, 6, 1))

    def _filtered(self, form_data: dict) -> set[str]:
        form = SearchForm(form_data, profile=self.profile)
        self.assertTrue(form.is_valid(), form.errors)
        criteria = dict(form.cleaned_data)
        if (custom := form.parse_custom_field_criteria()) is not None:
            criteria["custom_fields"] = custom
        return {p.name for p in Pin.objects.filter(profile=self.profile).filter_by_criteria(criteria)}

    def test_form_adds_dynamic_fields(self) -> None:
        form = SearchForm({}, profile=self.profile)
        self.assertIn(f"cf_{self.text_field.pk}", form.fields)
        self.assertIn(f"cf_{self.number_field.pk}_min", form.fields)
        self.assertIn(f"cf_{self.number_field.pk}_max", form.fields)
        self.assertIn(f"cf_{self.date_field.pk}_after", form.fields)
        self.assertIn(f"cf_{self.date_field.pk}_before", form.fields)

    def test_no_profile_means_no_dynamic_fields(self) -> None:
        form = SearchForm({})
        self.assertNotIn(f"cf_{self.text_field.pk}", form.fields)

    def test_text_contains_filter(self) -> None:
        self.assertEqual(self._filtered({f"cf_{self.text_field.pk}": "brick"}), {"Brick Factory"})

    def test_number_range_filter(self) -> None:
        self.assertEqual(self._filtered({f"cf_{self.number_field.pk}_min": "5"}), {"Steel Mill"})
        self.assertEqual(self._filtered({f"cf_{self.number_field.pk}_max": "5"}), {"Brick Factory"})
        self.assertEqual(
            self._filtered({f"cf_{self.number_field.pk}_min": "1", f"cf_{self.number_field.pk}_max": "5"}),
            {"Brick Factory"},
        )

    def test_date_range_filter(self) -> None:
        self.assertEqual(self._filtered({f"cf_{self.date_field.pk}_after": "2026-01-01"}), {"Steel Mill"})
        self.assertEqual(self._filtered({f"cf_{self.date_field.pk}_before": "2025-12-31"}), set())

    def test_combined_filters_intersect(self) -> None:
        self.assertEqual(
            self._filtered({f"cf_{self.text_field.pk}": "e", f"cf_{self.number_field.pk}_min": "10"}),
            {"Steel Mill"},
        )

    def test_no_custom_filters_returns_all(self) -> None:
        names = self._filtered({})
        self.assertIn("Brick Factory", names)
        self.assertIn("Steel Mill", names)
        self.assertIn("Old Mill", names)


class CustomFieldExportTests(CustomFieldTestsBase):
    """custom_fields.json in the data export."""

    def test_export_writes_definitions_and_values(self) -> None:
        field = self._field(name="Gate code")
        CustomFieldValue.objects.create(field=field, pin=self.pin, value_text="1234")
        number_field = self._field(name="Floors", field_type=CustomFieldType.NUMBER)
        CustomFieldValue.objects.create(field=number_field, pin=self.pin, value_number=Decimal("3.5"))

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_custom_fields(self.profile, temp_dir)
            with open(os.path.join(temp_dir, "custom_fields.json"), encoding="utf-8") as fh:
                rows = json.load(fh)

        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["Gate code"]["entity_type"], "pin")
        self.assertEqual(by_name["Gate code"]["field_type"], "text")
        self.assertEqual(len(by_name["Gate code"]["values"]), 1)
        gate_value = by_name["Gate code"]["values"][0]
        self.assertEqual(gate_value["value"], "1234")
        self.assertEqual(gate_value["target_uuid"], str(self.pin.uuid))
        self.assertEqual(by_name["Floors"]["values"][0]["value"], "3.5")

    def test_export_empty_when_no_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _export_custom_fields(self.profile, temp_dir)
            with open(os.path.join(temp_dir, "custom_fields.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), [])
