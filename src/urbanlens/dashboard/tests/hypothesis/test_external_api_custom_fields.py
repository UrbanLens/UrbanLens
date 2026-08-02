"""Tests for the external API's custom-field domain.

Covers both halves the domain owns: field *definitions* (shared across every
entity type) and PHOTO-entity *values*. The invariants mirrored from the rest
of this API's test suite: a missing scope is refused (403), and another
profile's photo or field is indistinguishable from one that doesn't exist
(404, never 403).
"""

from __future__ import annotations

from http import HTTPStatus

from django.contrib.auth.models import User
from django.urls import reverse
from hypothesis import given, settings as hypothesis_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.custom_fields.model import CustomField, CustomFieldEntity, CustomFieldType, CustomFieldValue
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

_CUSTOM_FIELD_SCOPES = [ApiKeyScope.CUSTOM_FIELDS_READ.value, ApiKeyScope.CUSTOM_FIELDS_WRITE.value, ApiKeyScope.PHOTOS_READ.value, ApiKeyScope.PHOTOS_WRITE.value]


def _bearer(raw_key: str) -> dict:
    """Request kwargs carrying an API key as a bearer token."""
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _CustomFieldsApiTestCase(TestCase):
    """Shared fixture: a key owner with a photo, plus an unrelated second user."""

    def setUp(self) -> None:
        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.other_user = baker.make(User)
        self.other_profile = Profile.objects.get(user=self.other_user)

        self.image = baker.make(Image, profile=self.profile)
        self.other_image = baker.make(Image, profile=self.other_profile)

        self.raw_key = self._key_with_scopes(_CUSTOM_FIELD_SCOPES)

    def _key_with_scopes(self, scopes: list[str], user: User | None = None) -> str:
        """Issue a key carrying exactly *scopes* and return its raw value."""
        api_key, raw = generate_api_key(user or self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=scopes)
        return raw


class DefinitionScopeTests(_CustomFieldsApiTestCase):
    """custom_fields:read/write gate the definitions endpoints, independently."""

    def test_missing_scope_is_refused_on_every_method(self) -> None:
        """A key with none of this domain's scopes gets 403 everywhere."""
        raw_key = self._key_with_scopes([ApiKeyScope.PROFILE_READ.value])
        field = CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PHOTO, name="Gate code")
        cases = (
            ("get", reverse("external_api:custom_fields")),
            ("post", reverse("external_api:custom_fields")),
            ("patch", reverse("external_api:custom_fields.detail", kwargs={"field_id": field.pk})),
            ("delete", reverse("external_api:custom_fields.detail", kwargs={"field_id": field.pk})),
        )
        for method, url in cases:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url, **_bearer(raw_key))
                self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)

    def test_read_only_key_cannot_create(self) -> None:
        """custom_fields:read does not confer custom_fields:write."""
        raw_key = self._key_with_scopes([ApiKeyScope.CUSTOM_FIELDS_READ.value])
        response = self.client.post(reverse("external_api:custom_fields"), {"entity_type": "photo", "name": "X"}, content_type="application/json", **_bearer(raw_key))
        self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)

    def test_read_only_key_can_list(self) -> None:
        """The same key is accepted on the list endpoint."""
        raw_key = self._key_with_scopes([ApiKeyScope.CUSTOM_FIELDS_READ.value])
        response = self.client.get(reverse("external_api:custom_fields"), **_bearer(raw_key))
        self.assertEqual(response.status_code, HTTPStatus.OK)


class DefinitionCrudTests(_CustomFieldsApiTestCase):
    """Create, list, patch, and delete a field definition."""

    def test_create_and_list(self) -> None:
        """A created field appears in a same-entity_type-filtered list."""
        response = self.client.post(reverse("external_api:custom_fields"), {"entity_type": "photo", "name": "Gate code", "field_type": "text"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.CREATED, response.content)
        created = response.json()
        self.assertEqual(created["name"], "Gate code")
        self.assertEqual(created["entity_type"], "photo")

        response = self.client.get(reverse("external_api:custom_fields"), {"entity_type": "photo"}, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.OK)
        names = {row["name"] for row in response.json()["results"]}
        self.assertIn("Gate code", names)

    def test_duplicate_name_is_rejected(self) -> None:
        """The same (entity_type, name) pair twice is a 400, not a second row."""
        CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PHOTO, name="Gate code")
        response = self.client.post(reverse("external_api:custom_fields"), {"entity_type": "photo", "name": "Gate code"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)
        self.assertEqual(CustomField.objects.filter(profile=self.profile, entity_type=CustomFieldEntity.PHOTO, name="Gate code").count(), 1)

    def test_select_without_options_is_rejected(self) -> None:
        """A select field needs at least one option to be a coherent field."""
        response = self.client.post(reverse("external_api:custom_fields"), {"entity_type": "photo", "name": "Category", "field_type": "select", "options": []}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)

    def test_select_with_options_round_trips(self) -> None:
        """A select field's options are persisted and read back."""
        response = self.client.post(
            reverse("external_api:custom_fields"),
            {"entity_type": "photo", "name": "Category", "field_type": "select", "options": ["Indoor", "Outdoor", "Indoor"]},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, HTTPStatus.CREATED, response.content)
        self.assertEqual(response.json()["options"], ["Indoor", "Outdoor"])

    def test_patch_renames_field(self) -> None:
        """PATCH applies a partial update without touching other attributes."""
        field = CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PHOTO, name="Gate code", field_type=CustomFieldType.TEXT)
        url = reverse("external_api:custom_fields.detail", kwargs={"field_id": field.pk})
        response = self.client.patch(url, {"name": "Access code"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.OK, response.content)
        field.refresh_from_db()
        self.assertEqual(field.name, "Access code")
        self.assertEqual(field.field_type, CustomFieldType.TEXT)

    def test_patch_cannot_change_type_once_values_exist(self) -> None:
        """A field with stored values refuses a type change (would corrupt existing rows)."""
        field = CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PHOTO, name="Count", field_type=CustomFieldType.NUMBER)
        CustomFieldValue.objects.create(field=field, image=self.image, value_number=3)
        url = reverse("external_api:custom_fields.detail", kwargs={"field_id": field.pk})
        response = self.client.patch(url, {"field_type": "text"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)
        field.refresh_from_db()
        self.assertEqual(field.field_type, CustomFieldType.NUMBER)

    def test_patch_options_without_field_type_is_still_validated(self) -> None:
        """Omitting field_type on a PATCH (the natural way to only edit options) still dedupes,
        rejects an empty list, and enforces the option cap - not just when field_type is repeated.
        """
        field = CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PHOTO, name="Category", field_type=CustomFieldType.SELECT, config={"choices": ["Indoor"]})
        url = reverse("external_api:custom_fields.detail", kwargs={"field_id": field.pk})

        response = self.client.patch(url, {"options": ["Indoor", "Outdoor", "Indoor"]}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.OK, response.content)
        self.assertEqual(response.json()["options"], ["Indoor", "Outdoor"])
        field.refresh_from_db()
        self.assertEqual(field.config["choices"], ["Indoor", "Outdoor"])

        response = self.client.patch(url, {"options": []}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)
        field.refresh_from_db()
        self.assertEqual(field.config["choices"], ["Indoor", "Outdoor"])

        response = self.client.patch(url, {"options": [f"Option {i}" for i in range(101)]}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)
        field.refresh_from_db()
        self.assertEqual(field.config["choices"], ["Indoor", "Outdoor"])

    def test_patch_cannot_remove_option_still_in_use(self) -> None:
        """Removing a select option a stored value still uses is refused."""
        field = CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PHOTO, name="Category", field_type=CustomFieldType.SELECT, config={"choices": ["Indoor", "Outdoor"]})
        CustomFieldValue.objects.create(field=field, image=self.image, value_text="Indoor")
        url = reverse("external_api:custom_fields.detail", kwargs={"field_id": field.pk})
        response = self.client.patch(url, {"field_type": "select", "options": ["Outdoor"]}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)

    def test_delete_removes_field_and_its_values(self) -> None:
        """Deleting a definition cascades to every value stored under it."""
        field = CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PHOTO, name="Gate code", field_type=CustomFieldType.TEXT)
        CustomFieldValue.objects.create(field=field, image=self.image, value_text="1234")
        url = reverse("external_api:custom_fields.detail", kwargs={"field_id": field.pk})
        response = self.client.delete(url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.NO_CONTENT)
        self.assertFalse(CustomField.objects.filter(pk=field.pk).exists())
        self.assertFalse(CustomFieldValue.objects.filter(field_id=field.pk).exists())

    def test_another_profiles_field_is_404_not_403(self) -> None:
        """A field id belonging to someone else is indistinguishable from a nonexistent one."""
        field = CustomField.objects.create(profile=self.other_profile, entity_type=CustomFieldEntity.PHOTO, name="Gate code")
        url = reverse("external_api:custom_fields.detail", kwargs={"field_id": field.pk})
        response = self.client.patch(url, {"name": "Hijacked"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
        response = self.client.delete(url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
        self.assertTrue(CustomField.objects.filter(pk=field.pk).exists())


class PhotoCustomFieldValueTests(_CustomFieldsApiTestCase):
    """Reading and writing PHOTO-entity values on the caller's own photo."""

    def setUp(self) -> None:
        super().setUp()
        self.field = CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PHOTO, name="Gate code", field_type=CustomFieldType.TEXT)

    def test_list_includes_unset_fields_with_null_value(self) -> None:
        """Every defined field is listed, even with no value on this photo yet."""
        url = reverse("external_api:custom_fields.photo", kwargs={"image_uuid": self.image.uuid})
        response = self.client.get(url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.OK)
        rows = response.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], self.field.pk)
        self.assertIsNone(rows[0]["value"])

    def test_put_sets_value_and_get_reflects_it(self) -> None:
        """PUT stores the value; a subsequent GET returns it."""
        url = reverse("external_api:custom_fields.photo.detail", kwargs={"image_uuid": self.image.uuid, "field_id": self.field.pk})
        response = self.client.put(url, {"value": "1234"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.OK, response.content)
        self.assertEqual(response.json()["value"], "1234")

        list_url = reverse("external_api:custom_fields.photo", kwargs={"image_uuid": self.image.uuid})
        response = self.client.get(list_url, **_bearer(self.raw_key))
        self.assertEqual(response.json()[0]["value"], "1234")

    def test_put_empty_value_clears_and_returns_204(self) -> None:
        """An empty value deletes the stored row rather than storing a blank."""
        CustomFieldValue.objects.create(field=self.field, image=self.image, value_text="1234")
        url = reverse("external_api:custom_fields.photo.detail", kwargs={"image_uuid": self.image.uuid, "field_id": self.field.pk})
        response = self.client.put(url, {"value": ""}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.NO_CONTENT)
        self.assertFalse(CustomFieldValue.objects.filter(field=self.field, image=self.image).exists())

    def test_delete_clears_value(self) -> None:
        """DELETE clears the value, same as an empty PUT."""
        CustomFieldValue.objects.create(field=self.field, image=self.image, value_text="1234")
        url = reverse("external_api:custom_fields.photo.detail", kwargs={"image_uuid": self.image.uuid, "field_id": self.field.pk})
        response = self.client.delete(url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.NO_CONTENT)
        self.assertFalse(CustomFieldValue.objects.filter(field=self.field, image=self.image).exists())

    def test_invalid_value_is_rejected(self) -> None:
        """A value that fails the field's own type parsing is a 400, not a 500."""
        number_field = CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PHOTO, name="Count", field_type=CustomFieldType.NUMBER)
        url = reverse("external_api:custom_fields.photo.detail", kwargs={"image_uuid": self.image.uuid, "field_id": number_field.pk})
        response = self.client.put(url, {"value": "not-a-number"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)

    def test_another_users_photo_is_404_not_403(self) -> None:
        """Another profile's photo is indistinguishable from a nonexistent one."""
        cases = (
            ("get", reverse("external_api:custom_fields.photo", kwargs={"image_uuid": self.other_image.uuid})),
            ("put", reverse("external_api:custom_fields.photo.detail", kwargs={"image_uuid": self.other_image.uuid, "field_id": self.field.pk})),
            ("delete", reverse("external_api:custom_fields.photo.detail", kwargs={"image_uuid": self.other_image.uuid, "field_id": self.field.pk})),
        )
        for method, url in cases:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url, {"value": "x"} if method == "put" else None, content_type="application/json", **_bearer(self.raw_key))
                self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)

    def test_missing_photos_scope_is_refused(self) -> None:
        """custom_fields:* alone does not reach photo-scoped values without photos:*."""
        raw_key = self._key_with_scopes([ApiKeyScope.CUSTOM_FIELDS_READ.value, ApiKeyScope.CUSTOM_FIELDS_WRITE.value])
        url = reverse("external_api:custom_fields.photo", kwargs={"image_uuid": self.image.uuid})
        response = self.client.get(url, **_bearer(raw_key))
        self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)


class PhotoCustomFieldValueHypothesisTests(_CustomFieldsApiTestCase):
    """Property test: any non-blank text value round-trips through the API unchanged."""

    @given(st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), min_size=1, max_size=200).filter(lambda s: s.strip()))
    @hypothesis_settings(max_examples=10, deadline=None)
    def test_text_value_round_trips(self, text: str) -> None:
        field = CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PHOTO, name=f"Field-{hash(text) & 0xFFFF}", field_type=CustomFieldType.TEXT)
        url = reverse("external_api:custom_fields.photo.detail", kwargs={"image_uuid": self.image.uuid, "field_id": field.pk})
        response = self.client.put(url, {"value": text}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.OK, response.content)
        self.assertEqual(response.json()["value"], text.strip())
        field.delete()
