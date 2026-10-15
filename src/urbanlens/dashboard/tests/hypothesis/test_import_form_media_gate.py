"""Regression tests for the Import Pins dialog's video feature gating."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.subscriptions import SiteFeature

_IMPORT_FORM_URL = reverse("pin.import.form")


class ImportFormMediaGateTests(TestCase):
    """The import dialog's file input accepts video files only when entitled."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to site admin; keep it off the subject
        self.user: User = baker.make(User)
        self.client = Client()
        self.client.force_login(self.user)

    def test_video_accept_hidden_without_feature(self) -> None:
        response = self.client.get(_IMPORT_FORM_URL)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('data-can-upload-videos="false"', content)

    def test_video_accept_shown_with_feature(self) -> None:
        settings_obj = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.VIDEO_UPLOADS)
        response = self.client.get(_IMPORT_FORM_URL)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('data-can-upload-videos="true"', content)
        self.assertIn(",video/*", content)
