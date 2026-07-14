"""Tests for the media label kind - labels applied to photos/videos/documents.

Media labels are a private, per-profile label kind (like tags) that attach to
Image rows (never Pin/Wiki) purely to help the owner find that media item via
the main site search.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.model import KIND_MEDIA, KIND_TAG, KIND_USER, Label
from urbanlens.dashboard.models.labels.signals import DEFAULT_MEDIA_LABELS
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import PhotoSearchProvider


class MediaLabelSignupSeedingTests(TestCase):
    """New profiles get a small starter set of media labels."""

    def test_default_media_labels_created_on_signup(self) -> None:
        user = baker.make(User)
        profile = user.profile
        names = set(Label.objects.filter(profile=profile, kind=KIND_MEDIA).values_list("name", flat=True))
        self.assertEqual(names, {d["name"] for d in DEFAULT_MEDIA_LABELS})

    def test_seeding_is_idempotent(self) -> None:
        user = baker.make(User)
        profile = user.profile
        # Re-saving the profile (created=False path) must not duplicate labels.
        profile.save()
        count = Label.objects.filter(profile=profile, kind=KIND_MEDIA).count()
        self.assertEqual(count, len(DEFAULT_MEDIA_LABELS))


class MediaLabelQuerySetTests(TestCase):
    """Label.objects.media() and location_labels() correctly scope the new kind."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_media_returns_only_media_kind(self) -> None:
        media = baker.make(Label, profile=self.profile, kind=KIND_MEDIA, name="Custom Media Label")
        baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Some Tag")
        result = set(Label.objects.media().filter(profile=self.profile).values_list("id", flat=True))
        self.assertIn(media.id, result)
        self.assertEqual(len(result), 1 + len(DEFAULT_MEDIA_LABELS))

    def test_location_labels_excludes_media_and_user(self) -> None:
        media = baker.make(Label, profile=self.profile, kind=KIND_MEDIA, name="Custom Media Label")
        person = baker.make(Label, profile=self.profile, kind=KIND_USER, name="Some Person")
        tag = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Some Tag")
        result = set(Label.objects.location_labels().filter(profile=self.profile).values_list("id", flat=True))
        self.assertNotIn(media.id, result)
        self.assertNotIn(person.id, result)
        self.assertIn(tag.id, result)


class OrganizeMediaTabTests(TestCase):
    """The Organize page's Media tab renders and supports CRUD."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_media_tab_renders_seeded_labels(self) -> None:
        response = self.client.get(reverse("organize.index"), {"tab": "media"})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(DEFAULT_MEDIA_LABELS[0]["name"], content)

    def test_create_media_label(self) -> None:
        response = self.client.post(
            reverse("label.create", kwargs={"label_kind": "media"}),
            data={"name": "Drone Shot"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Label.objects.filter(profile=self.profile, kind=KIND_MEDIA, name="Drone Shot").exists())

    def test_edit_media_label_cannot_change_kind(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_MEDIA, name="Original")
        response = self.client.post(
            reverse("label.edit", kwargs={"label_kind": "media", "label_id": label.id}),
            data={"name": "Renamed", "kind": "tag"},
        )
        self.assertEqual(response.status_code, 200)
        label.refresh_from_db()
        self.assertEqual(label.name, "Renamed")
        self.assertEqual(label.kind, KIND_MEDIA)

    def test_delete_media_label(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_MEDIA, name="Deletable")
        response = self.client.post(reverse("label.delete", kwargs={"label_kind": "media", "label_id": label.id}))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Label.objects.filter(id=label.id).exists())

    def test_single_merge_disabled_for_media(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_MEDIA, name="A")
        response = self.client.get(reverse("label.merge", kwargs={"label_kind": "media", "label_id": label.id}))
        self.assertEqual(response.status_code, 404)


class LabelImageMembershipViewTests(TestCase):
    """Add/remove media labels on a photo, scoped to the owner."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.image = baker.make(Image, profile=self.profile)
        self.label = baker.make(Label, profile=self.profile, kind=KIND_MEDIA, name="Interior")

    def test_add_and_remove_label(self) -> None:
        add_response = self.client.post(
            reverse("label.image", kwargs={"image_uuid": self.image.uuid}),
            data={"label_id": self.label.id, "action": "add"},
        )
        self.assertEqual(add_response.status_code, 200)
        self.assertIn(self.label.id, set(self.image.labels.values_list("id", flat=True)))

        remove_response = self.client.post(
            reverse("label.image", kwargs={"image_uuid": self.image.uuid}),
            data={"label_id": self.label.id, "action": "remove"},
        )
        self.assertEqual(remove_response.status_code, 200)
        self.assertNotIn(self.label.id, set(self.image.labels.values_list("id", flat=True)))

    def test_cannot_manage_labels_on_other_users_image(self) -> None:
        other_image = baker.make(Image, profile=baker.make(User).profile)
        response = self.client.get(reverse("label.image", kwargs={"image_uuid": other_image.uuid}))
        self.assertEqual(response.status_code, 404)

    def test_cannot_add_non_media_label(self) -> None:
        tag = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Not Media")
        response = self.client.post(
            reverse("label.image", kwargs={"image_uuid": self.image.uuid}),
            data={"label_id": tag.id, "action": "add"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(tag.id, set(self.image.labels.values_list("id", flat=True)))


class MediaLabelMultiMergeTests(TestCase):
    """Multi-merge correctly transfers image associations for media labels."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_merge_transfers_image_labels(self) -> None:
        target = baker.make(Label, profile=self.profile, kind=KIND_MEDIA, name="Target")
        source = baker.make(Label, profile=self.profile, kind=KIND_MEDIA, name="Source")
        image = baker.make(Image, profile=self.profile)
        image.labels.add(source)

        response = self.client.post(
            reverse("label.multi_merge", kwargs={"label_kind": "media"}),
            data=json.dumps({"target_id": target.id, "source_ids": [source.id]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Label.objects.filter(id=source.id).exists())
        self.assertIn(target.id, set(image.labels.values_list("id", flat=True)))


class MediaLabelSearchTests(TestCase):
    """Photos tagged with a media label are findable by that label's name via search."""

    def test_photo_search_matches_media_label_name(self) -> None:
        user = baker.make(User)
        profile = user.profile
        image = baker.make(
            Image,
            profile=profile,
            caption="",
            image=SimpleUploadedFile("photo.jpg", b"photo-bytes", content_type="image/jpeg"),
        )
        label = baker.make(Label, profile=profile, kind=KIND_MEDIA, name="Zephyrhills")
        image.labels.add(label)

        provider = PhotoSearchProvider()
        parsed = parse_query("Zephyrhills")
        results = provider.search(profile, parsed, limit=10)
        self.assertTrue(any(r.url for r in results), "Expected at least one photo result matching the media label name")
