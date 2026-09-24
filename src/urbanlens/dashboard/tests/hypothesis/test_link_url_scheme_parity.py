"""A stored link is http(s), whichever door it came in by (N29 G4-26 and its import incidental).

The forms and API refused ``javascript:`` links, but the archive importer wrote custom-field URL values straight
into the column, and the KML importer turned any ``<a href>`` in a placemark description into a pin link. Both are
rendered as ``href`` attributes, which autoescaping does not defend.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile

from django.contrib.auth.models import User
from django.template.loader import render_to_string
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.custom_fields.model import (
    CustomField,
    CustomFieldEntity,
    CustomFieldType,
    CustomFieldValue,
)
from urbanlens.dashboard.models.links.model import PinLink, WikiLink

_HOSTILE = [
    "javascript:alert(document.cookie)",
    "JaVaScRiPt:alert(1)",
    "javascript://%0aalert(1)",
    "data:text/html,<script>alert(1)</script>",
]


class TheSharedValidatorTests(SimpleTestCase):
    def test_hostile_schemes_are_refused(self) -> None:
        from urbanlens.dashboard.services.security.link_urls import InvalidLinkUrlError, clean_link_url

        for url in _HOSTILE:
            with self.subTest(url=url), self.assertRaises(InvalidLinkUrlError):
                clean_link_url(url, max_length=2000)

    def test_a_bare_domain_can_be_read_as_https(self) -> None:
        from urbanlens.dashboard.services.security.link_urls import clean_link_url

        self.assertEqual(clean_link_url(" example.com/a ", max_length=2000, assume_https=True), "https://example.com/a")

    def test_the_length_cap_holds(self) -> None:
        from urbanlens.dashboard.services.security.link_urls import InvalidLinkUrlError, clean_link_url

        with self.assertRaises(InvalidLinkUrlError):
            clean_link_url("https://example.com/" + "a" * 100, max_length=50)


class PinAndWikiLinkTests(TestCase):
    def test_the_model_refuses_a_hostile_link(self) -> None:
        from urbanlens.dashboard.services.security.link_urls import InvalidLinkUrlError

        pin = baker.make_recipe("dashboard.pin")
        wiki = baker.make_recipe("dashboard.wiki")
        for url in _HOSTILE:
            with self.subTest(url=url):
                with self.assertRaises(InvalidLinkUrlError):
                    PinLink.objects.create(pin=pin, url=url)
                with self.assertRaises(InvalidLinkUrlError):
                    WikiLink.objects.create(wiki=wiki, url=url)
        self.assertFalse(PinLink.objects.exists())
        self.assertFalse(WikiLink.objects.exists())

    def test_a_kml_description_link_is_not_stored_with_a_hostile_scheme(self) -> None:
        from urbanlens.dashboard.services.apis.locations.google.maps import _attach_description_extras
        from urbanlens.dashboard.services.import_formats.html_description import extract_link_urls

        pin = baker.make_recipe("dashboard.pin")
        description = '<a href="javascript:alert(document.cookie)">site</a> and https://example.com/real'
        _attach_description_extras(pin, [], extract_link_urls(description), pin.profile)

        self.assertEqual(
            list(PinLink.objects.filter(pin=pin).values_list("url", flat=True)), ["https://example.com/real"]
        )


class ImportedCustomFieldValueTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.archive = Path(tempfile.mkdtemp(prefix="ul_cf_import_"))
        self.addCleanup(shutil.rmtree, self.archive, ignore_errors=True)

    def _import(self, *fields: dict) -> None:
        from urbanlens.dashboard.services.import_export import import_data

        (self.archive / "custom_fields.json").write_text(json.dumps(list(fields)))
        self.result = import_data.ImportResult()
        import_data._import_custom_fields(
            self.profile, str(self.archive), self.result, pin_uuid_map={"p1": self.pin.pk}, label_uuid_map={}
        )

    def _field(self, field_type: str, value: object, **extra) -> dict:
        return {
            "entity_type": CustomFieldEntity.PIN,
            "name": f"{field_type} field",
            "field_type": field_type,
            "values": [{"target_uuid": "p1", "value": value}],
            **extra,
        }

    def test_a_hostile_url_value_is_not_imported(self) -> None:
        self._import(
            *(self._field(CustomFieldType.URL, url, name=f"link {index}") for index, url in enumerate(_HOSTILE))
        )

        self.assertEqual(CustomField.objects.filter(profile=self.profile).count(), len(_HOSTILE))
        self.assertFalse(CustomFieldValue.objects.filter(pin=self.pin).exists())

    def test_a_good_url_value_is_imported(self) -> None:
        self._import(self._field(CustomFieldType.URL, "https://example.com/x"))

        self.assertEqual(CustomFieldValue.objects.get(pin=self.pin).value_text, "https://example.com/x")

    def test_a_select_value_outside_the_choices_is_not_imported(self) -> None:
        self._import(self._field(CustomFieldType.SELECT, "Nope", config={"choices": ["Yes", "No"]}))

        self.assertFalse(CustomFieldValue.objects.filter(pin=self.pin).exists())

    def test_an_overlong_text_value_is_not_imported(self) -> None:
        from urbanlens.dashboard.services.core.text_limits import MAX_CUSTOM_FIELD_TEXT_LENGTH

        self._import(self._field(CustomFieldType.TEXT, "x" * (MAX_CUSTOM_FIELD_TEXT_LENGTH + 1)))

        self.assertFalse(CustomFieldValue.objects.filter(pin=self.pin).exists())

    def test_typed_values_still_round_trip(self) -> None:
        self._import(
            self._field(CustomFieldType.NUMBER, "12.5"),
            self._field(CustomFieldType.DATE, "2024-05-06"),
            self._field(CustomFieldType.TIME, "13:45"),
            self._field(CustomFieldType.CHECKBOX, False),
            self._field(CustomFieldType.TEXT, "hello"),
        )

        values = {value.field.field_type: value.value for value in CustomFieldValue.objects.filter(pin=self.pin)}
        self.assertEqual(str(values[CustomFieldType.NUMBER]), "12.500000")
        self.assertEqual(str(values[CustomFieldType.DATE]), "2024-05-06")
        self.assertEqual(str(values[CustomFieldType.TIME]), "13:45:00")
        self.assertIs(values[CustomFieldType.CHECKBOX], False)
        self.assertEqual(values[CustomFieldType.TEXT], "hello")

    def test_an_unknown_field_type_is_not_imported(self) -> None:
        self._import(self._field("script", "whatever"))

        self.assertFalse(CustomField.objects.filter(profile=self.profile).exists())


class CustomFieldTextCapTests(TestCase):
    def test_set_value_refuses_an_overlong_value(self) -> None:
        from urbanlens.dashboard.models.custom_fields.model import CustomFieldTextTooLongError
        from urbanlens.dashboard.services.core.text_limits import MAX_CUSTOM_FIELD_TEXT_LENGTH

        field = baker.make(CustomField, entity_type=CustomFieldEntity.PIN, field_type=CustomFieldType.TEXT)
        value = CustomFieldValue(field=field)
        with self.assertRaises(CustomFieldTextTooLongError):
            value.set_value("x" * (MAX_CUSTOM_FIELD_TEXT_LENGTH + 1))

    def test_the_form_path_reports_it(self) -> None:
        from urbanlens.dashboard.controllers.custom_fields import save_value
        from urbanlens.dashboard.services.core.text_limits import MAX_CUSTOM_FIELD_TEXT_LENGTH

        pin = baker.make_recipe("dashboard.pin")
        field = baker.make(
            CustomField, profile=pin.profile, entity_type=CustomFieldEntity.PIN, field_type=CustomFieldType.TEXT
        )
        saved, error = save_value(field, pin, "x" * (MAX_CUSTOM_FIELD_TEXT_LENGTH + 1))
        self.assertIsNone(saved)
        self.assertIsNotNone(error)
        self.assertFalse(CustomFieldValue.objects.exists())


class RenderedCustomFieldLinkTests(TestCase):
    def test_a_stored_hostile_value_never_reaches_an_href(self) -> None:
        """Written past set_value with update(), as a row from before this rule would be."""
        pin = baker.make_recipe("dashboard.pin")
        field = baker.make(
            CustomField, profile=pin.profile, entity_type=CustomFieldEntity.PIN, field_type=CustomFieldType.URL
        )
        value = CustomFieldValue(field=field, pin=pin)
        value.set_value("https://example.com")
        value.save()
        CustomFieldValue.objects.filter(pk=value.pk).update(value_text="javascript:alert(1)")
        value.refresh_from_db()

        html = render_to_string(
            "dashboard/partials/custom_fields/_value_input.html",
            {"field": field, "value": value, "post_url": "/x/", "hx_target": "#x"},
        )
        self.assertNotIn('href="javascript:', html)
