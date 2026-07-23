"""Regression tests for the Import Pins dialog's video/AI feature gating.

The dialog template (dashboard/pages/location/import/csv.html) accepts photo/
video drops and gates video + AI-parsed-file support on
`can_upload_videos`/`can_use_ai_features`. Both come from the
`add_feature_access` context processor (settings/base.py), not from
PinController.import_form's own context dict - these tests guard that
wiring so a future change to the context-processor list can't silently
disable the gate for this page.
"""

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
    """The import dialog's file input accepts video/AI-parsed files only when entitled."""

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
        self.assertIn("var canUploadVideos = false;", content)

    def test_video_accept_shown_with_feature(self) -> None:
        settings_obj = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.VIDEO_UPLOADS)
        response = self.client.get(_IMPORT_FORM_URL)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("var canUploadVideos = true;", content)
        self.assertIn(",video/*", content)

    def test_ai_file_types_hidden_without_feature(self) -> None:
        response = self.client.get(_IMPORT_FORM_URL)
        content = response.content.decode()
        self.assertNotIn(",.txt,.docx", content)

    def test_ai_file_types_shown_with_feature(self) -> None:
        settings_obj = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)
        self.user.profile.ai_enabled = True
        self.user.profile.save(update_fields=["ai_enabled"])
        response = self.client.get(_IMPORT_FORM_URL)
        content = response.content.decode()
        self.assertIn(",.txt,.docx", content)
