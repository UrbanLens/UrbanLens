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


# -- The four previously-importerless categories (decision 2026-07-23: build all) --


class RoundTripCommentsTests(TestCase):
    """comments.json round-trips: target matched by uuid, never by name."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.location.model import Location

        self.exporter = baker.make(User).profile
        self.importer = baker.make(User).profile
        self.location = baker.make(Location, latitude=41.11, longitude=-73.11)
        self.pin = baker.make(Pin, profile=self.exporter, location=self.location, name="Old Mill")

    def _export_and_import(self, importer_profile, pin_uuid_map) -> "import_data.ImportResult":
        result = import_data.ImportResult()
        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_comments(self.exporter, temp_dir)
            import_data._import_comments(importer_profile, temp_dir, result, pin_uuid_map=pin_uuid_map, label_uuid_map={})
        return result

    def test_export_carries_target_uuid(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment

        Comment.objects.create(profile=self.exporter, pin=self.pin, text="Found a way in.")
        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_comments(self.exporter, temp_dir)
            rows = _read(temp_dir, "comments.json")
        self.assertEqual(rows[0]["target_uuid"], str(self.pin.uuid))

    def test_comment_reattaches_via_pin_uuid_map(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment
        from urbanlens.dashboard.models.location.model import Location

        Comment.objects.create(profile=self.exporter, pin=self.pin, text="Found a way in.")
        my_pin = baker.make(Pin, profile=self.importer, location=baker.make(Location, latitude=41.12, longitude=-73.12))

        result = self._export_and_import(self.importer, {str(self.pin.uuid): my_pin.pk})

        imported = Comment.objects.get(profile=self.importer)
        self.assertEqual(imported.pin_id, my_pin.pk)
        self.assertEqual(imported.text, "Found a way in.")
        self.assertEqual(result.created.get("comments"), 1)

    def test_unresolvable_target_is_skipped_with_warning(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment

        Comment.objects.create(profile=self.exporter, pin=self.pin, text="Found a way in.")

        result = self._export_and_import(self.importer, {})

        self.assertFalse(Comment.objects.filter(profile=self.importer).exists())
        self.assertEqual(result.skipped.get("comments"), 1)
        self.assertTrue(any("could not be matched" in w for w in result.warnings))

    def test_target_uuid_never_matches_another_users_pin(self) -> None:
        """The archive is user-supplied: a uuid pointing at someone else's pin
        must not attach the importer's comment to it."""
        from urbanlens.dashboard.models.comments.model import Comment

        Comment.objects.create(profile=self.exporter, pin=self.pin, text="Found a way in.")

        # No mapping; the pin uuid resolves only to the EXPORTER's pin.
        self._export_and_import(self.importer, {})

        self.assertFalse(Comment.objects.filter(profile=self.importer).exists())
        self.assertFalse(Comment.objects.filter(pin=self.pin, profile=self.importer).exists())

    def test_wiki_comment_requires_wiki_access(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.wiki.model import Wiki

        wiki_location = baker.make(Location, latitude=41.13, longitude=-73.13)
        wiki = baker.make(Wiki, location=wiki_location)
        Comment.objects.create(profile=self.exporter, wiki=wiki, text="Wiki note.")

        # Importer has no pin at that location: no access, no import.
        result = self._export_and_import(self.importer, {})
        self.assertFalse(Comment.objects.filter(profile=self.importer).exists())
        self.assertEqual(result.skipped.get("comments"), 1)

        # With a pin at the wiki location, access exists and the note lands.
        baker.make(Pin, profile=self.importer, location=wiki_location)
        result = self._export_and_import(self.importer, {})
        imported = Comment.objects.get(profile=self.importer)
        self.assertEqual(imported.wiki_id, wiki.pk)

    def test_reimport_is_idempotent(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment

        Comment.objects.create(profile=self.exporter, pin=self.pin, text="Found a way in.")
        pin_uuid_map = {str(self.pin.uuid): baker.make(Pin, profile=self.importer).pk}

        self._export_and_import(self.importer, pin_uuid_map)
        result = self._export_and_import(self.importer, pin_uuid_map)

        self.assertEqual(Comment.objects.filter(profile=self.importer).count(), 1)
        self.assertEqual(result.skipped.get("comments"), 1)

    def test_exported_created_timestamp_is_preserved(self) -> None:
        from datetime import timedelta

        from django.utils import timezone

        from urbanlens.dashboard.models.comments.model import Comment

        original = Comment.objects.create(profile=self.exporter, pin=self.pin, text="Found a way in.")
        long_ago = timezone.now() - timedelta(days=365)
        Comment.objects.filter(pk=original.pk).update(created=long_ago)

        self._export_and_import(self.importer, {str(self.pin.uuid): baker.make(Pin, profile=self.importer).pk})

        imported = Comment.objects.get(profile=self.importer)
        self.assertEqual(imported.created, long_ago)


class RoundTripPhotosTests(TestCase):
    """photos/ round-trips: quota-checked re-upload, uuid-matched targets."""

    def setUp(self) -> None:
        super().setUp()
        self.importer = baker.make(User).profile

    def _archive(self, temp_dir: str, rows: list[dict], files: dict[str, bytes]) -> None:
        photos_dir = os.path.join(temp_dir, "photos")
        os.makedirs(photos_dir, exist_ok=True)
        for name, content in files.items():
            with open(os.path.join(photos_dir, name), "wb") as fh:
                fh.write(content)
        with open(os.path.join(photos_dir, "metadata.json"), "w", encoding="utf-8") as fh:
            json.dump(rows, fh)

    def _import(self, temp_dir: str, pin_uuid_map=None, label_uuid_map=None) -> "import_data.ImportResult":
        result = import_data.ImportResult()
        import_data._import_photos(self.importer, temp_dir, result, pin_uuid_map=pin_uuid_map or {}, label_uuid_map=label_uuid_map or {})
        return result

    def test_photo_reimports_and_attaches_to_mapped_pin(self) -> None:
        my_pin = baker.make(Pin, profile=self.importer)
        row = {"uuid": "8a4f0a53-1111-4f77-9111-000000000001", "filename": "mill.jpg", "caption": "Turbine hall", "media_type": "photo", "target_type": "pin", "target_uuid": "8a4f0a53-2222-4f77-9111-000000000002"}
        with tempfile.TemporaryDirectory() as temp_dir:
            self._archive(temp_dir, [row], {"mill.jpg": b"fake-jpeg-bytes"})
            result = self._import(temp_dir, pin_uuid_map={"8a4f0a53-2222-4f77-9111-000000000002": my_pin.pk})

        image = Image.objects.get(profile=self.importer)
        self.assertEqual(image.pin_id, my_pin.pk)
        self.assertEqual(image.caption, "Turbine hall")
        self.assertEqual(image.file_size, len(b"fake-jpeg-bytes"))
        self.assertEqual(result.created.get("photos"), 1)

    def test_unresolvable_target_still_imports_as_unattached(self) -> None:
        row = {"uuid": "8a4f0a53-1111-4f77-9111-000000000003", "filename": "mill.jpg", "target_type": "pin", "target_uuid": "8a4f0a53-2222-4f77-9111-000000000099"}
        with tempfile.TemporaryDirectory() as temp_dir:
            self._archive(temp_dir, [row], {"mill.jpg": b"fake-jpeg-bytes"})
            self._import(temp_dir)

        image = Image.objects.get(profile=self.importer)
        self.assertIsNone(image.pin_id)
        self.assertIsNone(image.wiki_id)

    def test_missing_file_is_skipped_with_warning(self) -> None:
        row = {"uuid": "8a4f0a53-1111-4f77-9111-000000000004", "filename": "gone.jpg"}
        with tempfile.TemporaryDirectory() as temp_dir:
            self._archive(temp_dir, [row], {})
            result = self._import(temp_dir)

        self.assertFalse(Image.objects.filter(profile=self.importer).exists())
        self.assertTrue(any("missing from the archive" in w for w in result.warnings))

    def test_traversal_filename_is_neutralized(self) -> None:
        """metadata.json is untrusted archive content - a path-traversal
        filename must resolve inside photos/ (and thus, absent, skip)."""
        row = {"uuid": "8a4f0a53-1111-4f77-9111-000000000005", "filename": "../../../etc/passwd"}
        with tempfile.TemporaryDirectory() as temp_dir:
            self._archive(temp_dir, [row], {})
            result = self._import(temp_dir)

        self.assertFalse(Image.objects.filter(profile=self.importer).exists())
        self.assertEqual(result.skipped.get("photos"), 1)

    def test_quota_exceeded_skips_with_warning(self) -> None:
        from unittest import mock

        row = {"uuid": "8a4f0a53-1111-4f77-9111-000000000006", "filename": "mill.jpg"}
        with tempfile.TemporaryDirectory() as temp_dir:
            self._archive(temp_dir, [row], {"mill.jpg": b"fake-jpeg-bytes"})
            with mock.patch("urbanlens.dashboard.services.storage.quota_error_for_upload", return_value="over quota"):
                result = self._import(temp_dir)

        self.assertFalse(Image.objects.filter(profile=self.importer).exists())
        self.assertTrue(any("storage quota" in w for w in result.warnings))

    def test_labels_reattach_via_label_uuid_map(self) -> None:
        label = Label.objects.create(profile=self.importer, name="Abandoned", kind="tag")
        row = {"uuid": "8a4f0a53-1111-4f77-9111-000000000007", "filename": "mill.jpg", "label_uuids": ["8a4f0a53-3333-4f77-9111-000000000001"]}
        with tempfile.TemporaryDirectory() as temp_dir:
            self._archive(temp_dir, [row], {"mill.jpg": b"fake-jpeg-bytes"})
            self._import(temp_dir, label_uuid_map={"8a4f0a53-3333-4f77-9111-000000000001": label.pk})

        image = Image.objects.get(profile=self.importer)
        self.assertEqual(list(image.labels.all()), [label])

    def test_reimport_is_idempotent(self) -> None:
        row = {"uuid": "8a4f0a53-1111-4f77-9111-000000000008", "filename": "mill.jpg"}
        with tempfile.TemporaryDirectory() as temp_dir:
            self._archive(temp_dir, [row], {"mill.jpg": b"fake-jpeg-bytes"})
            self._import(temp_dir)
            result = self._import(temp_dir)

        self.assertEqual(Image.objects.filter(profile=self.importer).count(), 1)
        self.assertEqual(result.skipped.get("photos"), 1)


class RoundTripTripsTests(TestCase):
    """trips.json round-trips: owned trips rebuilt, members become invitations."""

    def setUp(self) -> None:
        super().setUp()
        self.exporter = baker.make(User).profile
        self.importer = baker.make(User).profile

    def _export_and_import(self) -> "import_data.ImportResult":
        result = import_data.ImportResult()
        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_trips(self.exporter, temp_dir)
            import_data._import_trips(self.importer, temp_dir, result, pin_uuid_map={}, label_uuid_map={})
        return result

    def _make_trip(self, creator, name="Detroit Run", **kwargs):
        from urbanlens.dashboard.models.trips.model import Trip, TripMembership

        trip = Trip.objects.create(name=name, creator=creator, **kwargs)
        TripMembership.objects.get_or_create(trip=trip, profile=creator, defaults={"rsvp": "yes", "status": TripMembership.STATUS_JOINED})
        return trip

    def test_owned_trip_is_rebuilt_with_creator_membership(self) -> None:
        """Restore flow: export, original gone (deleted/another instance), import."""
        from urbanlens.dashboard.models.trips.model import Trip, TripMembership

        original = self._make_trip(self.exporter, description="Factories.")
        original_uuid = original.uuid

        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_trips(self.exporter, temp_dir)
            original.delete()
            result = import_data.ImportResult()
            import_data._import_trips(self.importer, temp_dir, result, pin_uuid_map={}, label_uuid_map={})

        rebuilt = Trip.objects.get(creator=self.importer)
        self.assertEqual(rebuilt.name, "Detroit Run")
        self.assertEqual(rebuilt.description, "Factories.")
        self.assertEqual(rebuilt.uuid, original_uuid)
        self.assertTrue(TripMembership.objects.filter(trip=rebuilt, profile=self.importer, status=TripMembership.STATUS_JOINED).exists())
        self.assertEqual(result.created.get("trips"), 1)

    def test_membership_in_someone_elses_trip_is_not_rebuilt(self) -> None:
        from urbanlens.dashboard.models.trips.model import Trip, TripMembership

        other = baker.make(User).profile
        their_trip = self._make_trip(other)
        TripMembership.objects.create(trip=their_trip, profile=self.exporter, status=TripMembership.STATUS_JOINED)

        result = self._export_and_import()

        self.assertFalse(Trip.objects.filter(creator=self.importer).exists())
        self.assertTrue(any("created by someone else" in w for w in result.warnings))

    def test_existing_trip_uuid_is_never_claimed(self) -> None:
        from urbanlens.dashboard.models.trips.model import Trip

        trip = self._make_trip(self.exporter)

        result = self._export_and_import()

        # Same instance, uuid still lives on the exporter's trip: skipped, not stolen.
        self.assertEqual(Trip.objects.filter(uuid=trip.uuid).count(), 1)
        self.assertEqual(Trip.objects.get(uuid=trip.uuid).creator_id, self.exporter.pk)
        self.assertEqual(result.skipped.get("trips"), 1)

    def test_members_reinvited_only_when_connected(self) -> None:
        """Only the importer's own connections are re-invited (never trust the
        archive's identifiers), and always as an invitation, never as joined."""
        from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
        from urbanlens.dashboard.models.friendship.model import Friendship
        from urbanlens.dashboard.models.trips.model import Trip, TripMembership

        friend = baker.make(User).profile
        stranger = baker.make(User).profile
        trip = self._make_trip(self.exporter)
        TripMembership.objects.create(trip=trip, profile=friend, status=TripMembership.STATUS_JOINED)
        TripMembership.objects.create(trip=trip, profile=stranger, status=TripMembership.STATUS_JOINED)
        Friendship.objects.create(from_profile=self.importer, to_profile=friend, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)

        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_trips(self.exporter, temp_dir)
            trip.delete()
            result = import_data.ImportResult()
            import_data._import_trips(self.importer, temp_dir, result, pin_uuid_map={}, label_uuid_map={})

        rebuilt = Trip.objects.get(creator=self.importer)
        self.assertTrue(TripMembership.objects.filter(trip=rebuilt, profile=friend, status=TripMembership.STATUS_INVITED).exists())
        self.assertFalse(TripMembership.objects.filter(trip=rebuilt, profile=stranger).exists())


class RoundTripDirectMessagesTests(TestCase):
    """direct_messages.json restore: own sent plaintext only, spam-free."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

        self.me = baker.make(User).profile
        self.partner = baker.make(User).profile
        # profile_visibility "anyone" matters too: the export withholds
        # partner_uuid whenever the partner's identity is masked from the
        # exporter (masked partner => nothing restorable, by design), and two
        # bare baker strangers mask each other under the default setting.
        ProfileModel.objects.filter(pk__in=[self.me.pk, self.partner.pk]).update(community_enabled=True, direct_message_visibility="anyone", profile_visibility="anyone")

    def _export_and_import(self, exporter=None, importer=None) -> "import_data.ImportResult":
        result = import_data.ImportResult()
        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_direct_messages(exporter or self.me, temp_dir)
            import_data._import_direct_messages(importer or self.me, temp_dir, result, pin_uuid_map={}, label_uuid_map={})
        return result

    def test_sent_plaintext_message_is_restored_after_deletion(self) -> None:
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage

        original = DirectMessage.objects.create(sender=self.me, recipient=self.partner, body="Meet at the gate")
        original_created = DirectMessage.objects.get(pk=original.pk).created

        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_direct_messages(self.me, temp_dir)
            DirectMessage.objects.all().delete()
            result = import_data.ImportResult()
            import_data._import_direct_messages(self.me, temp_dir, result, pin_uuid_map={}, label_uuid_map={})

        restored = DirectMessage.objects.get(sender=self.me, recipient=self.partner)
        self.assertEqual(restored.body, "Meet at the gate")
        self.assertEqual(restored.created, original_created)
        self.assertEqual(result.created.get("direct_messages"), 1)

    def test_received_messages_are_never_imported(self) -> None:
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage

        DirectMessage.objects.create(sender=self.partner, recipient=self.me, body="hello")

        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_direct_messages(self.me, temp_dir)
            DirectMessage.objects.all().delete()
            result = import_data.ImportResult()
            import_data._import_direct_messages(self.me, temp_dir, result, pin_uuid_map={}, label_uuid_map={})

        self.assertFalse(DirectMessage.objects.exists())
        self.assertEqual(result.skipped.get("direct_messages"), 1)
        self.assertTrue(any("archive-only" in w for w in result.warnings))

    def test_encrypted_messages_are_never_imported(self) -> None:
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage

        DirectMessage.objects.create(sender=self.me, recipient=self.partner, body="", ciphertext="c2VhbGVk", nonce="bm9uY2U=", key_version=1)

        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_direct_messages(self.me, temp_dir)
            DirectMessage.objects.all().delete()
            result = import_data.ImportResult()
            import_data._import_direct_messages(self.me, temp_dir, result, pin_uuid_map={}, label_uuid_map={})

        self.assertFalse(DirectMessage.objects.exists())
        self.assertEqual(result.skipped.get("direct_messages"), 1)

    def test_partner_dm_privacy_is_honored_on_restore(self) -> None:
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage
        from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

        DirectMessage.objects.create(sender=self.me, recipient=self.partner, body="Meet at the gate")

        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_direct_messages(self.me, temp_dir)
            DirectMessage.objects.all().delete()
            ProfileModel.objects.filter(pk=self.partner.pk).update(direct_message_visibility="no_one")
            result = import_data.ImportResult()
            import_data._import_direct_messages(self.me, temp_dir, result, pin_uuid_map={}, label_uuid_map={})

        self.assertFalse(DirectMessage.objects.exists())
        self.assertEqual(result.skipped.get("direct_messages"), 1)

    def test_reimport_is_idempotent(self) -> None:
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage

        DirectMessage.objects.create(sender=self.me, recipient=self.partner, body="Meet at the gate")

        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_direct_messages(self.me, temp_dir)
            result = import_data.ImportResult()
            import_data._import_direct_messages(self.me, temp_dir, result, pin_uuid_map={}, label_uuid_map={})

        self.assertEqual(DirectMessage.objects.count(), 1)
        self.assertEqual(result.skipped.get("direct_messages"), 1)
