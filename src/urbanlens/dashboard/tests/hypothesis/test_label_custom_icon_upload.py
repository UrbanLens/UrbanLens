"""Tests for uploading a custom icon file via LabelCreateView/LabelEditView."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.images import PNG_BYTES
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label


def _png(name: str = "icon.png") -> SimpleUploadedFile:
    """A real PNG.

    Photos now need a positive identification, so a placeholder string is refused - which is right, and means a
    test wanting a successful upload has to supply an actual image."""
    return SimpleUploadedFile(name, PNG_BYTES, content_type="image/png")


class LabelCreateCustomIconTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.url = reverse("label.create", kwargs={"label_kind": "tag"})

    def test_scoped_field_name_is_accepted(self) -> None:
        resp = self.client.post(self.url, data={"name": "Urbex", "custom_icon-new-tag": _png()})
        self.assertEqual(resp.status_code, 200)
        label = Label.objects.get(profile=self.profile, name="Urbex")
        self.assertTrue(label.custom_icon)

    def test_bare_field_name_still_accepted(self) -> None:
        """Backward compatible: any client posting the old bare field name still works."""
        resp = self.client.post(self.url, data={"name": "Urbex", "custom_icon": _png()})
        self.assertEqual(resp.status_code, 200)
        label = Label.objects.get(profile=self.profile, name="Urbex")
        self.assertTrue(label.custom_icon)

    def test_unrelated_file_field_is_ignored(self) -> None:
        """A file under some other field name must not be mistaken for the icon."""
        resp = self.client.post(self.url, data={"name": "Urbex", "not_an_icon": _png()})
        self.assertEqual(resp.status_code, 200)
        label = Label.objects.get(profile=self.profile, name="Urbex")
        self.assertFalse(label.custom_icon)


class LabelEditCustomIconTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Urbex")
        self.url = reverse("label.edit", kwargs={"label_kind": "tag", "label_id": self.label.id})

    def test_scoped_field_name_sets_custom_icon(self) -> None:
        resp = self.client.post(
            self.url,
            data={"name": "Urbex", f"custom_icon-{self.label.id}": _png()},
        )
        self.assertEqual(resp.status_code, 200)
        self.label.refresh_from_db()
        self.assertTrue(self.label.custom_icon)

    def test_no_file_leaves_existing_custom_icon_untouched(self) -> None:
        self.label.custom_icon = _png()
        self.label.save()
        resp = self.client.post(self.url, data={"name": "Urbex"})
        self.assertEqual(resp.status_code, 200)
        self.label.refresh_from_db()
        self.assertTrue(self.label.custom_icon)
