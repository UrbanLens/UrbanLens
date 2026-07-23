"""Coverage for the data-export-completeness pass: articles, ratings, security
indicators, media labels, and the many previously-unexported Profile settings
are now included in a user's data export - and, where a local object can be
resolved, round-trip back in through the importer.
"""

from __future__ import annotations

import json
import os
import tempfile

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.article.model import Article
from urbanlens.dashboard.models.custom_fields.model import CustomField, CustomFieldEntity, CustomFieldType, CustomFieldValue
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference
from urbanlens.dashboard.models.notifications.model import NotificationPreference
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.services import export as export_service, import_data
from urbanlens.dashboard.services.articles import save_article


def _read(temp_dir: str, filename: str):
    with open(os.path.join(temp_dir, filename), encoding="utf-8") as fh:
        return json.load(fh)


class ExportPinsCompletenessTests(TestCase):
    """_export_pins now carries rating, security indicators, and a private article."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.pin = baker.make(Pin, profile=self.profile, name="Old Mill", vulnerability=4, danger=2, fences="everywhere", locked="some")
        Review.objects.create(profile=self.profile, pin=self.pin, rating=5)
        save_article(editor=self.profile, content="# History\n\nSome text.", pin=self.pin)

    def _export_pins(self) -> list[dict]:
        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_pins(self.profile, temp_dir)
            return _read(temp_dir, "pins.json")

    def test_vulnerability_and_danger_are_exported(self) -> None:
        row = self._export_pins()[0]
        self.assertEqual(row["vulnerability"], 4)
        self.assertEqual(row["danger"], 2)

    def test_security_indicators_are_exported(self) -> None:
        row = self._export_pins()[0]
        self.assertEqual(row["security"]["fences"], "everywhere")
        self.assertEqual(row["security"]["locked"], "some")

    def test_rating_is_exported(self) -> None:
        row = self._export_pins()[0]
        self.assertEqual(row["rating"], 5)

    def test_unrated_pin_exports_null_rating(self) -> None:
        baker.make(Pin, profile=self.profile, name="Unrated")
        rows = self._export_pins()
        unrated = next(r for r in rows if r["name"] == "Unrated")
        self.assertIsNone(unrated["rating"])

    def test_article_content_is_exported(self) -> None:
        row = self._export_pins()[0]
        self.assertIn("History", row["article"]["content"])

    def test_pin_with_no_article_exports_null(self) -> None:
        baker.make(Pin, profile=self.profile, name="No Article")
        rows = self._export_pins()
        plain = next(r for r in rows if r["name"] == "No Article")
        self.assertIsNone(plain["article"])


class ExportPhotosLabelsTests(TestCase):
    """_export_photos now carries media label assignments."""

    def test_photo_export_includes_label_uuids(self) -> None:
        profile = baker.make(User).profile
        label = Label.objects.create(profile=profile, name="Abandoned", kind="tag")
        image = baker.make(Image, profile=profile)
        image.labels.add(label)

        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_photos(profile, temp_dir)
            metadata = _read(temp_dir, os.path.join("photos", "metadata.json"))

        self.assertEqual(metadata[0]["label_uuids"], [str(label.uuid)])


class ExportProfileContactTests(TestCase):
    """_export_profile now carries the contact-info fields."""

    def test_contact_fields_are_exported(self) -> None:
        profile = baker.make(User).profile
        profile.phone_number = "+1 555 0100"
        profile.discord_username = "explorer#1234"
        profile.save()

        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_profile(profile, temp_dir)
            data = _read(temp_dir, "profile.json")

        self.assertEqual(data["contact"]["phone_number"], "+1 555 0100")
        self.assertEqual(data["contact"]["discord_username"], "explorer#1234")


class ExportSettingsCompletenessTests(TestCase):
    """_export_settings now carries the many previously-unexported Profile fields."""

    def _export_settings(self, profile) -> dict:
        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_settings(profile, temp_dir)
            return _read(temp_dir, "settings.json")

    def test_ai_and_keyword_and_community_groups_are_exported(self) -> None:
        profile = baker.make(User).profile
        profile.ai_label_tags = True
        profile.sync_vulnerability_to_wiki = False
        profile.save()

        data = self._export_settings(profile)
        self.assertTrue(data["ai"]["ai_label_tags"])
        self.assertFalse(data["community"]["sync_vulnerability_to_wiki"])

    def test_common_pins_visibility_is_exported(self) -> None:
        profile = baker.make(User).profile
        profile.common_pins_visibility = "friends"
        profile.save()

        data = self._export_settings(profile)
        self.assertEqual(data["privacy"]["common_pins_visibility"], "friends")

    def test_notification_preferences_are_exported(self) -> None:
        profile = baker.make(User).profile
        NotificationPreference.objects.create(profile=profile, message=DeliveryPreference.EMAIL, message_sms=True)

        data = self._export_settings(profile)
        self.assertEqual(data["notification_preferences"]["message"], DeliveryPreference.EMAIL)
        self.assertTrue(data["notification_preferences"]["message_sms"])

    def test_missing_notification_preferences_row_exports_empty_dict(self) -> None:
        profile = baker.make(User).profile
        data = self._export_settings(profile)
        self.assertEqual(data["notification_preferences"], {})


class ImportPinCompletenessTests(TestCase):
    """Exported vulnerability/danger/security/rating/article round-trip through import."""

    def setUp(self) -> None:
        super().setUp()
        self.exporter = baker.make(User).profile
        self.importer = baker.make(User).profile
        self.pin = baker.make(Pin, profile=self.exporter, name="Old Mill", vulnerability=3, danger=5, cameras="everywhere")
        Review.objects.create(profile=self.exporter, pin=self.pin, rating=4)
        save_article(editor=self.exporter, content="An old mill.", pin=self.pin)

    def test_round_trip_creates_matching_pin(self) -> None:
        from urbanlens.dashboard.services.import_data import ImportResult

        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_pins(self.exporter, temp_dir)
            result = ImportResult()
            import_data._import_pins(self.importer, temp_dir, result, pin_uuid_map={}, label_uuid_map={})

        imported = Pin.objects.get(profile=self.importer, name="Old Mill")
        self.assertEqual(imported.vulnerability, 3)
        self.assertEqual(imported.danger, 5)
        self.assertEqual(imported.cameras, "everywhere")
        self.assertEqual(Review.objects.get(profile=self.importer, pin=imported).rating, 4)
        self.assertEqual(Article.objects.get(pin=imported).content, "An old mill.")

    def test_reimporting_an_existing_pin_does_not_duplicate_rating_or_article(self) -> None:
        """Idempotency: a pin that already exists for this user is skipped
        entirely (matching the pre-existing label behavior), so re-running an
        import never creates a second Review or Article for the same pin."""
        from urbanlens.dashboard.services.import_data import ImportResult

        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_pins(self.exporter, temp_dir)
            pin_uuid_map: dict[str, int] = {}
            result = ImportResult()
            import_data._import_pins(self.importer, temp_dir, result, pin_uuid_map=pin_uuid_map, label_uuid_map={})
            import_data._import_pins(self.importer, temp_dir, result, pin_uuid_map=pin_uuid_map, label_uuid_map={})

        imported = Pin.objects.get(profile=self.importer, name="Old Mill")
        self.assertEqual(Review.objects.filter(profile=self.importer, pin=imported).count(), 1)
        self.assertEqual(Article.objects.filter(pin=imported).count(), 1)


class ImportSettingsCompletenessTests(TestCase):
    """Exported settings groups and notification preferences round-trip through import."""

    def test_round_trip_applies_grouped_settings(self) -> None:
        from urbanlens.dashboard.services.import_data import ImportResult

        exporter = baker.make(User).profile
        exporter.ai_label_categories = True
        exporter.track_geolocation = False
        exporter.common_pins_visibility = "friends"
        exporter.save()
        NotificationPreference.objects.create(profile=exporter, friend_request=DeliveryPreference.EMAIL)

        importer = baker.make(User).profile

        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_settings(exporter, temp_dir)
            result = ImportResult()
            import_data._import_settings(importer, temp_dir, result, pin_uuid_map={}, label_uuid_map={})

        importer.refresh_from_db()
        self.assertTrue(importer.ai_label_categories)
        self.assertFalse(importer.track_geolocation)
        self.assertEqual(importer.common_pins_visibility, "friends")
        prefs = NotificationPreference.objects.get(profile=importer)
        self.assertEqual(prefs.friend_request, DeliveryPreference.EMAIL)


class ImportCustomFieldsTests(TestCase):
    """New _import_custom_fields: definitions always import; pin-targeted values round-trip."""

    def test_definition_and_pin_value_round_trip(self) -> None:
        from urbanlens.dashboard.services.import_data import ImportResult

        exporter = baker.make(User).profile
        pin = baker.make(Pin, profile=exporter, name="Gatehouse")
        field = CustomField.objects.create(profile=exporter, entity_type=CustomFieldEntity.PIN, name="Gate code", field_type=CustomFieldType.TEXT)
        value = CustomFieldValue(field=field, pin=pin)
        value.set_value("1234")
        value.save()

        importer = baker.make(User).profile

        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_pins(exporter, temp_dir)
            export_service._export_custom_fields(exporter, temp_dir)
            result = ImportResult()
            pin_uuid_map: dict[str, int] = {}
            import_data._import_pins(importer, temp_dir, result, pin_uuid_map=pin_uuid_map, label_uuid_map={})
            import_data._import_custom_fields(importer, temp_dir, result, pin_uuid_map=pin_uuid_map, label_uuid_map={})

        imported_field = CustomField.objects.get(profile=importer, name="Gate code")
        imported_pin = Pin.objects.get(profile=importer, name="Gatehouse")
        imported_value = CustomFieldValue.objects.get(field=imported_field, pin=imported_pin)
        self.assertEqual(imported_value.value_text, "1234")

    def test_non_pin_entity_definition_imports_without_values(self) -> None:
        """Photo/profile/map-targeted field definitions still import (useful on
        their own); their values are skipped with a warning since this import
        pass can't resolve those entity types to a local object."""
        from urbanlens.dashboard.services.import_data import ImportResult

        exporter = baker.make(User).profile
        CustomField.objects.create(profile=exporter, entity_type=CustomFieldEntity.PROFILE, name="Relationship", field_type=CustomFieldType.TEXT)
        importer = baker.make(User).profile

        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_custom_fields(exporter, temp_dir)
            result = ImportResult()
            import_data._import_custom_fields(importer, temp_dir, result, pin_uuid_map={}, label_uuid_map={})

        self.assertTrue(CustomField.objects.filter(profile=importer, name="Relationship", entity_type=CustomFieldEntity.PROFILE).exists())
