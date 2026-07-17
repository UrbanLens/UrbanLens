"""Tests for custom fields: models, settings CRUD, value editing, map filtering, and export."""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
import json
import os
import tempfile

from django.urls import reverse
from hypothesis import given, settings as hypothesis_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.forms.search import SearchForm
from urbanlens.dashboard.models.custom_fields.model import CustomField, CustomFieldDisplay, CustomFieldEntity, CustomFieldStyle, CustomFieldType, CustomFieldValue
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

    def test_export_includes_style_config_and_new_value_types(self) -> None:
        select_field = CustomField.objects.create(
            profile=self.profile, entity_type=CustomFieldEntity.PIN, name="Access", field_type=CustomFieldType.SELECT, config={"choices": ["Open", "Locked"]},
        )
        CustomFieldValue.objects.create(field=select_field, pin=self.pin, value_text="Open")
        checkbox_field = self._field(name="Has power", field_type=CustomFieldType.CHECKBOX)
        CustomFieldValue.objects.create(field=checkbox_field, pin=self.pin, value_boolean=True)
        time_field = self._field(name="Best hour", field_type=CustomFieldType.TIME)
        CustomFieldValue.objects.create(field=time_field, pin=self.pin, value_time=time(6, 30))
        stars_field = CustomField.objects.create(
            profile=self.profile, entity_type=CustomFieldEntity.PIN, name="Photogenic", field_type=CustomFieldType.NUMBER, style=CustomFieldStyle.STARS,
        )
        CustomFieldValue.objects.create(field=stars_field, pin=self.pin, value_number=Decimal("4"))

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_custom_fields(self.profile, temp_dir)
            with open(os.path.join(temp_dir, "custom_fields.json"), encoding="utf-8") as fh:
                rows = json.load(fh)

        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["Access"]["config"], {"choices": ["Open", "Locked"]})
        self.assertEqual(by_name["Access"]["values"][0]["value"], "Open")
        self.assertEqual(by_name["Photogenic"]["style"], "stars")
        self.assertIs(by_name["Has power"]["values"][0]["value"], True)
        self.assertEqual(by_name["Best hour"]["values"][0]["value"], "06:30:00")


class NewTypeValueParsingTests(TestCase):
    """set_value/display_value behavior for the time/select/checkbox/url types."""

    def _value(self, field_type: str, config: dict | None = None) -> CustomFieldValue:
        return CustomFieldValue(field=CustomField(field_type=field_type, config=config or {}))

    @given(st.times().map(lambda t: t.replace(second=0, microsecond=0)))
    @hypothesis_settings(max_examples=25, deadline=None)
    def test_time_round_trip(self, moment: time) -> None:
        value = self._value(CustomFieldType.TIME)
        value.set_value(moment.isoformat("minutes"))
        self.assertEqual(value.value_time, moment)
        self.assertEqual(value.display_value, moment.isoformat("minutes"))

    def test_invalid_time_raises(self) -> None:
        value = self._value(CustomFieldType.TIME)
        with self.assertRaises(ValueError):
            value.set_value("25:99")

    def test_checkbox_parses_truthy_and_falsy_words(self) -> None:
        value = self._value(CustomFieldType.CHECKBOX)
        for raw in ("true", "1", "on", "YES"):
            value.set_value(raw)
            self.assertIs(value.value_boolean, True, raw)
        for raw in ("false", "0", "off", "No"):
            value.set_value(raw)
            self.assertIs(value.value_boolean, False, raw)
        self.assertEqual(value.display_value, "No")

    def test_checkbox_rejects_garbage(self) -> None:
        value = self._value(CustomFieldType.CHECKBOX)
        with self.assertRaises(ValueError):
            value.set_value("maybe")

    def test_select_accepts_only_configured_choices(self) -> None:
        value = self._value(CustomFieldType.SELECT, config={"choices": ["Open", "Locked"]})
        value.set_value("Locked")
        self.assertEqual(value.value_text, "Locked")
        with self.assertRaises(ValueError):
            value.set_value("Ajar")

    def test_url_validates_and_prefixes_scheme(self) -> None:
        value = self._value(CustomFieldType.URL)
        value.set_value("example.com/history")
        self.assertEqual(value.value_text, "https://example.com/history")
        value.set_value("http://example.org")
        self.assertEqual(value.value_text, "http://example.org")
        with self.assertRaises(ValueError):
            value.set_value("not a url at all")
        with self.assertRaises(ValueError):
            value.set_value("javascript://alert(1)")

    def test_set_value_clears_other_typed_columns(self) -> None:
        value = self._value(CustomFieldType.CHECKBOX)
        value.value_text = "stale"
        value.value_number = Decimal("7")
        value.set_value("true")
        self.assertEqual(value.value_text, "")
        self.assertIsNone(value.value_number)
        self.assertIs(value.value_boolean, True)


class CustomFieldStyleTests(TestCase):
    """effective_style resolution and select/slider config helpers."""

    def test_effective_style_defaults_per_type(self) -> None:
        self.assertEqual(CustomField(field_type=CustomFieldType.TEXT).effective_style, CustomFieldStyle.SHORT_TEXT)
        self.assertEqual(CustomField(field_type=CustomFieldType.NUMBER).effective_style, CustomFieldStyle.NUMBER_INPUT)
        self.assertEqual(CustomField(field_type=CustomFieldType.DATE).effective_style, "")

    def test_effective_style_honors_valid_choice(self) -> None:
        field = CustomField(field_type=CustomFieldType.NUMBER, style=CustomFieldStyle.STARS)
        self.assertEqual(field.effective_style, CustomFieldStyle.STARS)

    def test_effective_style_ignores_style_from_another_type(self) -> None:
        field = CustomField(field_type=CustomFieldType.TEXT, style=CustomFieldStyle.SLIDER)
        self.assertEqual(field.effective_style, CustomFieldStyle.SHORT_TEXT)

    def test_select_choices_tolerate_bad_config(self) -> None:
        self.assertEqual(CustomField(field_type=CustomFieldType.SELECT, config={"choices": "oops"}).select_choices, [])
        self.assertEqual(CustomField(field_type=CustomFieldType.SELECT, config=None).select_choices, [])
        self.assertEqual(CustomField(field_type=CustomFieldType.TEXT, config={"choices": ["x"]}).select_choices, [])

    def test_slider_bounds_default_and_override(self) -> None:
        field = CustomField(field_type=CustomFieldType.NUMBER, style=CustomFieldStyle.SLIDER)
        self.assertEqual(field.slider_min, Decimal(0))
        self.assertEqual(field.slider_max, Decimal(100))
        field.config = {"min": 1, "max": 10}
        self.assertEqual(field.slider_min, Decimal(1))
        self.assertEqual(field.slider_max, Decimal(10))


class CustomFieldDefinitionStyleViewTests(CustomFieldTestsBase):
    """Creating/updating fields with styles, options, and slider bounds."""

    def test_create_select_field_with_options(self) -> None:
        self.client.post(
            reverse("custom_fields.settings"),
            {"entity_type": "pin", "name": "Access", "field_type": "select", "options": "Open\nLocked, Guarded\nOpen"},
        )
        field = CustomField.objects.get(profile=self.profile, name="Access")
        self.assertEqual(field.select_choices, ["Open", "Locked", "Guarded"])

    def test_select_field_requires_options(self) -> None:
        self.client.post(
            reverse("custom_fields.settings"),
            {"entity_type": "pin", "name": "Access", "field_type": "select", "options": "   "},
        )
        self.assertFalse(CustomField.objects.filter(profile=self.profile, name="Access").exists())

    def test_style_must_match_type(self) -> None:
        self.client.post(
            reverse("custom_fields.settings"),
            {"entity_type": "pin", "name": "Notes", "field_type": "text", "style": "stars"},
        )
        self.assertFalse(CustomField.objects.filter(profile=self.profile, name="Notes").exists())

    def test_create_slider_field_with_bounds(self) -> None:
        self.client.post(
            reverse("custom_fields.settings"),
            {"entity_type": "pin", "name": "Decay", "field_type": "number", "style": "slider", "slider_min": "1", "slider_max": "10"},
        )
        field = CustomField.objects.get(profile=self.profile, name="Decay")
        self.assertEqual(field.style, CustomFieldStyle.SLIDER)
        self.assertEqual(field.slider_min, Decimal(1))
        self.assertEqual(field.slider_max, Decimal(10))

    def test_slider_bounds_must_be_ordered(self) -> None:
        self.client.post(
            reverse("custom_fields.settings"),
            {"entity_type": "pin", "name": "Decay", "field_type": "number", "style": "slider", "slider_min": "10", "slider_max": "1"},
        )
        self.assertFalse(CustomField.objects.filter(profile=self.profile, name="Decay").exists())

    def test_update_changes_style_and_options(self) -> None:
        field = CustomField.objects.create(
            profile=self.profile, entity_type=CustomFieldEntity.PIN, name="Access", field_type=CustomFieldType.SELECT, config={"choices": ["Open"]},
        )
        self.client.post(
            reverse("custom_fields.update", args=[field.id]),
            {"name": "Access", "field_type": "select", "options": "Open\nLocked"},
        )
        field.refresh_from_db()
        self.assertEqual(field.select_choices, ["Open", "Locked"])

    def test_update_to_stars_style(self) -> None:
        field = self._field(name="Photogenic", field_type=CustomFieldType.NUMBER)
        self.client.post(
            reverse("custom_fields.update", args=[field.id]),
            {"name": "Photogenic", "field_type": "number", "style": "stars"},
        )
        field.refresh_from_db()
        self.assertEqual(field.style, CustomFieldStyle.STARS)


class CustomFieldDisplayTests(CustomFieldTestsBase):
    """The display placement (default/section/fixed) and drag-position memory."""

    def _post_position(self, field: CustomField, left: object, top: object):
        return self.client.post(
            reverse("custom_fields.position", args=[field.id]),
            data=json.dumps({"left": left, "top": top}),
            content_type="application/json",
        )

    def test_create_with_display(self) -> None:
        self.client.post(
            reverse("custom_fields.settings"),
            {"entity_type": "pin", "name": "History", "field_type": "text", "display": "section"},
        )
        field = CustomField.objects.get(profile=self.profile, name="History")
        self.assertEqual(field.display, CustomFieldDisplay.SECTION)

    def test_display_defaults_when_omitted(self) -> None:
        self.client.post(
            reverse("custom_fields.settings"),
            {"entity_type": "photo", "name": "Camera", "field_type": "text"},
        )
        field = CustomField.objects.get(profile=self.profile, name="Camera")
        self.assertEqual(field.display, CustomFieldDisplay.DEFAULT)

    def test_invalid_display_rejected(self) -> None:
        self.client.post(
            reverse("custom_fields.settings"),
            {"entity_type": "pin", "name": "History", "field_type": "text", "display": "floating"},
        )
        self.assertFalse(CustomField.objects.filter(profile=self.profile, name="History").exists())

    def test_update_changes_display(self) -> None:
        field = self._field(name="Gate code")
        self.client.post(
            reverse("custom_fields.update", args=[field.id]),
            {"name": "Gate code", "field_type": "text", "display": "fixed"},
        )
        field.refresh_from_db()
        self.assertEqual(field.display, CustomFieldDisplay.FIXED)

    def test_pin_panel_places_fields_by_display(self) -> None:
        self._field(name="Plain field")
        CustomField.objects.create(profile=self.profile, entity_type="pin", name="Section field", display=CustomFieldDisplay.SECTION)
        fixed = CustomField.objects.create(profile=self.profile, entity_type="pin", name="Fixed field", display=CustomFieldDisplay.FIXED)

        response = self.client.get(reverse("pin.custom_fields", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()

        self.assertContains(response, "cf-section-card")
        self.assertContains(response, "Section field")
        self.assertContains(response, "cf-fixed-item")
        self.assertContains(response, reverse("custom_fields.position", args=[fixed.id]))
        # The default card's rows hold only the default-display field.
        rows_html = html.split('class="cf-value-rows"')[1].split("</div>")[0] if 'class="cf-value-rows"' in html else ""
        self.assertIn("Plain field", rows_html)
        self.assertNotIn("Section field", rows_html)

    def test_fixed_default_positions_stagger(self) -> None:
        CustomField.objects.create(profile=self.profile, entity_type="pin", name="Fixed A", display=CustomFieldDisplay.FIXED)
        CustomField.objects.create(profile=self.profile, entity_type="pin", name="Fixed B", display=CustomFieldDisplay.FIXED)
        response = self.client.get(reverse("pin.custom_fields", args=[self.pin.slug]))
        html = response.content.decode()
        # Never-dragged fields must not overlap: each gets a distinct top offset.
        self.assertIn("top: 16.00%", html)
        self.assertIn("top: 27.00%", html)

    def test_position_saves_and_clamps(self) -> None:
        field = CustomField.objects.create(profile=self.profile, entity_type="pin", name="Fixed", display=CustomFieldDisplay.FIXED)
        response = self._post_position(field, 150, -20)
        self.assertEqual(response.status_code, 204)
        field.refresh_from_db()
        self.assertEqual(field.config["fixed_pos"], {"left": 92.0, "top": 0.0})
        self.assertEqual(field.fixed_position, {"left": 92.0, "top": 0.0})

    def test_saved_position_renders_on_the_panel(self) -> None:
        field = CustomField.objects.create(profile=self.profile, entity_type="pin", name="Fixed", display=CustomFieldDisplay.FIXED)
        self._post_position(field, 12.5, 33.25)
        response = self.client.get(reverse("pin.custom_fields", args=[self.pin.slug]))
        self.assertContains(response, "left: 12.50%; top: 33.25%;")

    def test_position_rejects_garbage(self) -> None:
        field = CustomField.objects.create(profile=self.profile, entity_type="pin", name="Fixed", display=CustomFieldDisplay.FIXED)
        self.assertEqual(self._post_position(field, "left", "top").status_code, 400)
        self.assertEqual(self._post_position(field, float("nan"), 5).status_code, 400)
        response = self.client.post(reverse("custom_fields.position", args=[field.id]), data="not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        field.refresh_from_db()
        self.assertNotIn("fixed_pos", field.config or {})

    def test_position_404_for_another_users_field(self) -> None:
        other_profile = baker.make("auth.User").profile
        field = CustomField.objects.create(profile=other_profile, entity_type="pin", name="Theirs", display=CustomFieldDisplay.FIXED)
        self.assertEqual(self._post_position(field, 10, 10).status_code, 404)

    def test_position_survives_definition_edit(self) -> None:
        field = CustomField.objects.create(profile=self.profile, entity_type="pin", name="Fixed", display=CustomFieldDisplay.FIXED)
        self._post_position(field, 40, 40)
        self.client.post(
            reverse("custom_fields.update", args=[field.id]),
            {"name": "Renamed", "field_type": "text", "display": "fixed"},
        )
        field.refresh_from_db()
        self.assertEqual(field.name, "Renamed")
        self.assertEqual(field.config["fixed_pos"], {"left": 40.0, "top": 40.0})

    def test_fixed_position_property_tolerates_bad_config(self) -> None:
        self.assertIsNone(CustomField(config={"fixed_pos": "oops"}).fixed_position)
        self.assertIsNone(CustomField(config={"fixed_pos": {"left": "x", "top": 1}}).fixed_position)
        self.assertIsNone(CustomField(config=None).fixed_position)
        clamped = CustomField(config={"fixed_pos": {"left": 500, "top": -3}}).fixed_position
        self.assertEqual(clamped, {"left": 92.0, "top": 0.0})

    def test_settings_panel_offers_display_only_for_pins(self) -> None:
        response = self.client.get(reverse("custom_fields.settings"))
        html = response.content.decode()
        pin_group = html.split('id="cf-group-pin"')[1].split('id="cf-group-')[0]
        photo_group = html.split('id="cf-group-photo"')[1].split('id="cf-group-')[0]
        self.assertIn("cf-display-select", pin_group)
        self.assertNotIn("cf-display-select", photo_group)


class NewTypeValueEndpointTests(CustomFieldTestsBase):
    """Saving select/checkbox/time/url values on a pin."""

    def _post(self, field: CustomField, raw: str):
        return self.client.post(reverse("pin.custom_fields.value", args=[self.pin.slug, field.id]), {"value": raw})

    def test_select_value_saves_and_rejects_unknown(self) -> None:
        field = CustomField.objects.create(
            profile=self.profile, entity_type=CustomFieldEntity.PIN, name="Access", field_type=CustomFieldType.SELECT, config={"choices": ["Open", "Locked"]},
        )
        self._post(field, "Open")
        self.assertEqual(CustomFieldValue.objects.get(field=field, pin=self.pin).value_text, "Open")
        self._post(field, "Ajar")
        self.assertEqual(CustomFieldValue.objects.get(field=field, pin=self.pin).value_text, "Open")

    def test_checkbox_checks_and_unchecks(self) -> None:
        field = self._field(name="Has power", field_type=CustomFieldType.CHECKBOX)
        self._post(field, "true")
        self.assertIs(CustomFieldValue.objects.get(field=field, pin=self.pin).value_boolean, True)
        # An unchecked checkbox posts no value at all, which clears the row.
        self._post(field, "")
        self.assertFalse(CustomFieldValue.objects.filter(field=field, pin=self.pin).exists())

    def test_time_value_saves(self) -> None:
        field = self._field(name="Best hour", field_type=CustomFieldType.TIME)
        self._post(field, "06:30")
        self.assertEqual(CustomFieldValue.objects.get(field=field, pin=self.pin).value_time, time(6, 30))

    def test_url_value_saves_with_prefix(self) -> None:
        field = self._field(name="History link", field_type=CustomFieldType.URL)
        self._post(field, "example.com/mill")
        self.assertEqual(CustomFieldValue.objects.get(field=field, pin=self.pin).value_text, "https://example.com/mill")


class NewTypeMapFilterTests(CustomFieldTestsBase):
    """SearchForm dynamic fields and queryset filtering for the new types."""

    def setUp(self) -> None:
        super().setUp()
        self.select_field = CustomField.objects.create(
            profile=self.profile, entity_type=CustomFieldEntity.PIN, name="Access", field_type=CustomFieldType.SELECT, config={"choices": ["Open", "Locked"]},
        )
        self.checkbox_field = self._field(name="Has power", field_type=CustomFieldType.CHECKBOX)
        self.time_field = self._field(name="Best hour", field_type=CustomFieldType.TIME)
        self.url_field = self._field(name="History link", field_type=CustomFieldType.URL)

        self.open_pin = baker.make(Pin, profile=self.profile, name="Open Asylum", name_is_user_provided=True)
        CustomFieldValue.objects.create(field=self.select_field, pin=self.open_pin, value_text="Open")
        CustomFieldValue.objects.create(field=self.checkbox_field, pin=self.open_pin, value_boolean=True)
        CustomFieldValue.objects.create(field=self.time_field, pin=self.open_pin, value_time=time(6, 0))
        CustomFieldValue.objects.create(field=self.url_field, pin=self.open_pin, value_text="https://example.com/asylum")

        self.locked_pin = baker.make(Pin, profile=self.profile, name="Locked Mill", name_is_user_provided=True)
        CustomFieldValue.objects.create(field=self.select_field, pin=self.locked_pin, value_text="Locked")
        CustomFieldValue.objects.create(field=self.time_field, pin=self.locked_pin, value_time=time(22, 15))

    def _filtered(self, form_data: dict) -> set[str]:
        form = SearchForm(form_data, profile=self.profile)
        self.assertTrue(form.is_valid(), form.errors)
        criteria = dict(form.cleaned_data)
        if (custom := form.parse_custom_field_criteria()) is not None:
            criteria["custom_fields"] = custom
        return {p.name for p in Pin.objects.filter(profile=self.profile).filter_by_criteria(criteria)}

    def test_form_adds_expected_field_kinds(self) -> None:
        form = SearchForm({}, profile=self.profile)
        self.assertIn(f"cf_{self.select_field.pk}", form.fields)
        self.assertIn(f"cf_{self.checkbox_field.pk}", form.fields)
        self.assertIn(f"cf_{self.time_field.pk}_after", form.fields)
        self.assertIn(f"cf_{self.time_field.pk}_before", form.fields)
        self.assertIn(f"cf_{self.url_field.pk}", form.fields)

    def test_select_equals_filter(self) -> None:
        self.assertEqual(self._filtered({f"cf_{self.select_field.pk}": "Open"}), {"Open Asylum"})
        self.assertEqual(self._filtered({f"cf_{self.select_field.pk}": "Locked"}), {"Locked Mill"})

    def test_checkbox_checked_filter(self) -> None:
        self.assertEqual(self._filtered({f"cf_{self.checkbox_field.pk}": "checked"}), {"Open Asylum"})

    def test_checkbox_unchecked_includes_pins_without_a_value(self) -> None:
        names = self._filtered({f"cf_{self.checkbox_field.pk}": "unchecked"})
        self.assertNotIn("Open Asylum", names)
        self.assertIn("Locked Mill", names)
        self.assertIn("Old Mill", names)

    def test_time_range_filter(self) -> None:
        self.assertEqual(self._filtered({f"cf_{self.time_field.pk}_after": "12:00"}), {"Locked Mill"})
        self.assertEqual(self._filtered({f"cf_{self.time_field.pk}_before": "12:00"}), {"Open Asylum"})

    def test_url_contains_filter(self) -> None:
        self.assertEqual(self._filtered({f"cf_{self.url_field.pk}": "asylum"}), {"Open Asylum"})


class NewTypeCriteriaRoundTripTests(CustomFieldTestsBase):
    """serialize_form_criteria / deserialize_criteria for the new criterion shapes."""

    def test_round_trip_preserves_new_shapes(self) -> None:
        from urbanlens.dashboard.services.filter_criteria import deserialize_criteria, serialize_form_criteria

        select_field = CustomField.objects.create(
            profile=self.profile, entity_type=CustomFieldEntity.PIN, name="Access", field_type=CustomFieldType.SELECT, config={"choices": ["Open", "Locked"]},
        )
        checkbox_field = self._field(name="Has power", field_type=CustomFieldType.CHECKBOX)
        time_field = self._field(name="Best hour", field_type=CustomFieldType.TIME)

        criteria = [
            {"field": select_field, "equals": "Open"},
            {"field": checkbox_field, "checked": False},
            {"field": time_field, "after_time": time(6, 0), "before_time": None},
        ]
        stored = serialize_form_criteria({}, None, criteria)
        self.assertEqual(json.loads(json.dumps(stored)), stored)

        restored = deserialize_criteria(stored, self.profile)["custom_fields"]
        by_field = {c["field"].pk: c for c in restored}
        self.assertEqual(by_field[select_field.pk]["equals"], "Open")
        self.assertIs(by_field[checkbox_field.pk]["checked"], False)
        self.assertEqual(by_field[time_field.pk]["after_time"], time(6, 0))
        self.assertIsNone(by_field[time_field.pk]["before_time"])


class ReferenceFieldTests(CustomFieldTestsBase):
    """Reference-type fields: access scoping, value endpoints, filters, and export."""

    def _reference_field(self, ref_type: str, name: str = "Related") -> CustomField:
        return CustomField.objects.create(
            profile=self.profile, entity_type=CustomFieldEntity.PIN, name=name, field_type=CustomFieldType.REFERENCE, config={"ref_type": ref_type},
        )

    def _post_value(self, field: CustomField, raw) -> None:
        self.client.post(reverse("pin.custom_fields.value", args=[self.pin.slug, field.id]), {"value": raw})

    def test_pin_reference_saves_and_displays(self) -> None:
        field = self._reference_field("pin")
        other_pin = baker.make(Pin, profile=self.profile, name="Boiler House", name_is_user_provided=True)
        self._post_value(field, other_pin.pk)
        value = CustomFieldValue.objects.get(field=field, pin=self.pin)
        self.assertEqual(value.ref_pin_id, other_pin.pk)
        self.assertEqual(value.display_value, "Boiler House")
        self.assertIn(other_pin.slug, value.reference_url)

    def test_cannot_reference_another_users_pin(self) -> None:
        field = self._reference_field("pin")
        foreign_pin = baker.make(Pin, profile=baker.make("auth.User").profile, name="Not Yours", name_is_user_provided=True)
        self._post_value(field, foreign_pin.pk)
        self.assertFalse(CustomFieldValue.objects.filter(field=field, pin=self.pin).exists())

    def test_cannot_reference_another_users_photo_or_map_or_list(self) -> None:
        other_profile = baker.make("auth.User").profile
        cases = [
            ("photo", baker.make(Image, profile=other_profile)),
            ("markup_map", baker.make(MarkupMap, profile=other_profile)),
            ("list", baker.make("dashboard.PinList", profile=other_profile, name="Their list")),
        ]
        for ref_type, foreign_target in cases:
            field = self._reference_field(ref_type, name=f"Ref {ref_type}")
            self._post_value(field, foreign_target.pk)
            self.assertFalse(CustomFieldValue.objects.filter(field=field, pin=self.pin).exists(), ref_type)

    def test_wiki_reference_requires_pinned_location(self) -> None:
        from urbanlens.dashboard.models.wiki.model import Wiki

        pinned_wiki = baker.make(Wiki, location=self.pin.location, name="Pinned Place")
        unpinned_wiki = baker.make(Wiki, location=baker.make("dashboard.Location", latitude="40.1", longitude="-75.1"), name="Elsewhere")
        field = self._reference_field("wiki")
        self._post_value(field, unpinned_wiki.pk)
        self.assertFalse(CustomFieldValue.objects.filter(field=field, pin=self.pin).exists())
        self._post_value(field, pinned_wiki.pk)
        self.assertEqual(CustomFieldValue.objects.get(field=field, pin=self.pin).ref_wiki_id, pinned_wiki.pk)

    def test_deleting_referenced_object_deletes_value(self) -> None:
        field = self._reference_field("markup_map")
        markup_map = baker.make(MarkupMap, profile=self.profile, title="Route A")
        self._post_value(field, markup_map.pk)
        self.assertTrue(CustomFieldValue.objects.filter(field=field, pin=self.pin).exists())
        markup_map.delete()
        self.assertFalse(CustomFieldValue.objects.filter(field=field, pin=self.pin).exists())

    def test_create_reference_field_requires_kind(self) -> None:
        self.client.post(
            reverse("custom_fields.settings"),
            {"entity_type": "pin", "name": "Related", "field_type": "reference"},
        )
        self.assertFalse(CustomField.objects.filter(profile=self.profile, name="Related").exists())
        self.client.post(
            reverse("custom_fields.settings"),
            {"entity_type": "pin", "name": "Related", "field_type": "reference", "ref_type": "trip"},
        )
        self.assertEqual(CustomField.objects.get(profile=self.profile, name="Related").reference_kind, "trip")

    def test_kind_change_blocked_when_values_exist(self) -> None:
        field = self._reference_field("pin")
        other_pin = baker.make(Pin, profile=self.profile, name="Boiler House", name_is_user_provided=True)
        self._post_value(field, other_pin.pk)
        self.client.post(
            reverse("custom_fields.update", args=[field.id]),
            {"name": "Related", "field_type": "reference", "ref_type": "trip"},
        )
        field.refresh_from_db()
        self.assertEqual(field.reference_kind, "pin")

    def test_reference_filter_matches_pins(self) -> None:
        field = self._reference_field("trip")
        trip = baker.make("dashboard.Trip", name="Autumn Run")
        baker.make("dashboard.TripMembership", trip=trip, profile=self.profile)
        self._post_value(field, trip.pk)

        form = SearchForm({f"cf_{field.pk}": str(trip.pk)}, profile=self.profile)
        self.assertTrue(form.is_valid(), form.errors)
        criteria = dict(form.cleaned_data)
        criteria["custom_fields"] = form.parse_custom_field_criteria()
        names = {p.name for p in Pin.objects.filter(profile=self.profile).filter_by_criteria(criteria)}
        self.assertEqual(names, {"Old Mill"})

    def test_reference_criteria_round_trip(self) -> None:
        from urbanlens.dashboard.services.filter_criteria import deserialize_criteria, serialize_form_criteria

        field = self._reference_field("pin")
        other_pin = baker.make(Pin, profile=self.profile, name="Boiler House", name_is_user_provided=True)
        stored = serialize_form_criteria({}, None, [{"field": field, "ref_id": other_pin.pk}])
        self.assertEqual(json.loads(json.dumps(stored)), stored)
        restored = deserialize_criteria(stored, self.profile)["custom_fields"]
        self.assertEqual(restored[0]["ref_id"], other_pin.pk)

    def test_reference_choices_scoped_and_labeled(self) -> None:
        field = self._reference_field("pin")
        mine = baker.make(Pin, profile=self.profile, name="Boiler House", name_is_user_provided=True)
        theirs = baker.make(Pin, profile=baker.make("auth.User").profile, name="Foreign", name_is_user_provided=True)
        choices = dict(field.reference_choices())
        self.assertIn(mine.pk, choices)
        self.assertEqual(choices[mine.pk], "Boiler House")
        self.assertNotIn(theirs.pk, choices)

    def test_export_reference_value(self) -> None:
        field = self._reference_field("pin")
        other_pin = baker.make(Pin, profile=self.profile, name="Boiler House", name_is_user_provided=True)
        self._post_value(field, other_pin.pk)

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_custom_fields(self.profile, temp_dir)
            with open(os.path.join(temp_dir, "custom_fields.json"), encoding="utf-8") as fh:
                rows = json.load(fh)

        row = next(r for r in rows if r["name"] == "Related")
        self.assertEqual(row["config"], {"ref_type": "pin"})
        exported = row["values"][0]["value"]
        self.assertEqual(exported, {"kind": "pin", "uuid": str(other_pin.uuid), "label": "Boiler House"})
