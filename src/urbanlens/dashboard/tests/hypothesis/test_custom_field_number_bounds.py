"""A custom field's number must fit ``value_number``'s column, on every route that writes one."""

from __future__ import annotations

from decimal import Decimal
from http import HTTPStatus
import json
import pathlib
import tempfile

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from hypothesis import given, settings as hypothesis_settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.custom_fields.model import (
    CustomField,
    CustomFieldEntity,
    CustomFieldType,
    CustomFieldValue,
    InvalidNumberError,
)
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

_COLUMN = CustomFieldValue._meta.get_field("value_number")
_INTEGER_DIGITS = _COLUMN.max_digits - _COLUMN.decimal_places
_LARGEST = Decimal(f"{'9' * _INTEGER_DIGITS}.{'9' * _COLUMN.decimal_places}")

#: Each overflowed numeric(24,6) or failed DecimalField.to_python on save before the bound existed.
UNSTORABLE_NUMBERS = (
    "1e30",
    "1e18",
    "-1e18",
    "1000000000000000000.000000",
    "1e400",
    "-1e400",
    f"{_LARGEST}9",  # rounds up past the column at its scale
    "NaN",
    "-NaN",
    "sNaN",
    "Infinity",
    "-Infinity",
    "inf",
)


class NumberParsingBoundsTests(SimpleTestCase):
    """``set_value`` refuses a number its column cannot hold, before anything reaches the database."""

    def _value(self) -> CustomFieldValue:
        return CustomFieldValue(field=CustomField(field_type=CustomFieldType.NUMBER))

    def test_an_unstorable_number_is_an_invalid_number(self) -> None:
        for raw in UNSTORABLE_NUMBERS:
            with self.subTest(raw=raw), self.assertRaises(InvalidNumberError):
                self._value().set_value(raw)

    def test_the_largest_storable_numbers_are_kept_exactly(self) -> None:
        for number in (_LARGEST, -_LARGEST):
            with self.subTest(number=number):
                value = self._value()
                value.set_value(str(number))
                self.assertEqual(value.value_number, number)

    def test_digits_past_the_column_scale_are_rounded_as_the_column_would(self) -> None:
        value = self._value()
        value.set_value("1.1234565")
        self.assertEqual(value.value_number, Decimal("1.123457"))

    @given(st.decimals(allow_nan=False, allow_infinity=False, min_value=-_LARGEST, max_value=_LARGEST, places=6))
    @hypothesis_settings(max_examples=50, deadline=None)
    def test_every_number_in_range_round_trips(self, number: Decimal) -> None:
        value = self._value()
        value.set_value(str(number))
        self.assertEqual(value.value_number, number)


class _BoundsFixture(TestCase):
    def setUp(self) -> None:
        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def _number_field(self, entity_type: str) -> CustomField:
        return CustomField.objects.create(
            profile=self.profile, entity_type=entity_type, name="Count", field_type=CustomFieldType.NUMBER
        )

    def assertRefusedWithErrorToast(self, response) -> None:
        self.assertEqual(response.status_code, HTTPStatus.OK)
        toast = json.loads(response.headers["HX-Trigger"])["showToast"]
        self.assertEqual(toast["level"], "error", toast)


class DashboardNumberBoundsTests(_BoundsFixture):
    """Each dashboard value route answers an unstorable number with its own validation error, storing nothing."""

    def test_pin_value_route(self) -> None:
        pin = baker.make(Pin, profile=self.profile)
        field = self._number_field(CustomFieldEntity.PIN)
        url = reverse("pin.custom_fields.value", args=[pin.slug, field.pk])
        for raw in UNSTORABLE_NUMBERS:
            with self.subTest(raw=raw):
                self.assertRefusedWithErrorToast(self.client.post(url, {"value": raw}))
                self.assertFalse(CustomFieldValue.objects.filter(field=field).exists())

    def test_profile_annotation_route(self) -> None:
        subject = Profile.objects.get(user=baker.make(User))
        subject.ensure_slug()
        Profile.objects.filter(pk=subject.pk).update(profile_visibility=VisibilityChoice.ANYONE)
        field = self._number_field(CustomFieldEntity.PROFILE)
        url = reverse("profile.custom_field_value", args=[subject.slug, field.pk])
        for raw in UNSTORABLE_NUMBERS:
            with self.subTest(raw=raw):
                self.assertRefusedWithErrorToast(self.client.post(url, {"value": raw}))
                self.assertFalse(CustomFieldValue.objects.filter(field=field).exists())

    def test_photo_strip_route(self) -> None:
        image = baker.make(Image, profile=self.profile)
        field = self._number_field(CustomFieldEntity.PHOTO)
        url = reverse("custom_fields.photo", args=[image.pk])
        for raw in UNSTORABLE_NUMBERS:
            with self.subTest(raw=raw):
                self.assertRefusedWithErrorToast(self.client.post(url, {"field_id": field.pk, "value": raw}))
                self.assertFalse(CustomFieldValue.objects.filter(field=field).exists())

    def test_markup_map_strip_route(self) -> None:
        markup_map = baker.make(MarkupMap, profile=self.profile)
        field = self._number_field(CustomFieldEntity.MARKUP_MAP)
        url = reverse("custom_fields.markup_map", args=[markup_map.uuid])
        for raw in UNSTORABLE_NUMBERS:
            with self.subTest(raw=raw):
                self.assertRefusedWithErrorToast(self.client.post(url, {"field_id": field.pk, "value": raw}))
                self.assertFalse(CustomFieldValue.objects.filter(field=field).exists())

    def test_the_largest_storable_number_is_saved(self) -> None:
        pin = baker.make(Pin, profile=self.profile)
        field = self._number_field(CustomFieldEntity.PIN)
        url = reverse("pin.custom_fields.value", args=[pin.slug, field.pk])

        self.client.post(url, {"value": str(-_LARGEST)})

        self.assertEqual(CustomFieldValue.objects.get(field=field).value_number, -_LARGEST)


class ExternalApiNumberBoundsTests(_BoundsFixture):
    """The external API's photo value route answers an unstorable number with a 400."""

    def test_photo_value_put(self) -> None:
        api_key, raw_key = generate_api_key(self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(
            scopes=[ApiKeyScope.CUSTOM_FIELDS_WRITE.value, ApiKeyScope.PHOTOS_WRITE.value]
        )
        image = baker.make(Image, profile=self.profile)
        field = self._number_field(CustomFieldEntity.PHOTO)
        url = reverse(
            "external_api:custom_fields.photo.detail", kwargs={"image_uuid": image.uuid, "field_id": field.pk}
        )
        for raw in UNSTORABLE_NUMBERS:
            with self.subTest(raw=raw):
                response = self.client.put(
                    url, {"value": raw}, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw_key}"
                )
                self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST, response.content)
                self.assertIn("error", response.json())
                self.assertFalse(CustomFieldValue.objects.filter(field=field).exists())


class ImportNumberBoundsTests(_BoundsFixture):
    """An archive's unstorable number is skipped, not an aborted import."""

    def test_an_unstorable_imported_value_is_skipped(self) -> None:
        from urbanlens.dashboard.services.import_export.import_data import ImportResult, _import_custom_fields

        pins = [baker.make(Pin, profile=self.profile) for _ in UNSTORABLE_NUMBERS]
        kept = baker.make(Pin, profile=self.profile)
        rows = [
            {
                "entity_type": CustomFieldEntity.PIN,
                "name": "Count",
                "field_type": CustomFieldType.NUMBER,
                "values": [
                    *(
                        {"target_uuid": str(pin.uuid), "value": raw}
                        for pin, raw in zip(pins, UNSTORABLE_NUMBERS, strict=True)
                    ),
                    {"target_uuid": str(kept.uuid), "value": "12.5"},
                ],
            }
        ]
        result = ImportResult()

        with tempfile.TemporaryDirectory() as data_dir:
            (pathlib.Path(data_dir) / "custom_fields.json").write_text(json.dumps(rows), encoding="utf-8")
            _import_custom_fields(
                self.profile,
                data_dir,
                result,
                pin_uuid_map={str(pin.uuid): pin.pk for pin in [*pins, kept]},
                label_uuid_map={},
            )

        stored = CustomFieldValue.objects.filter(field__profile=self.profile)
        self.assertEqual([value.pin_id for value in stored], [kept.pk])
        self.assertEqual(stored.get().value_number, Decimal("12.5"))
