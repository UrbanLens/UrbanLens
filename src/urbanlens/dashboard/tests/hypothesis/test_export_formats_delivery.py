"""Tests for full-account single-file pin downloads (UL-382) and emailed exports (UL-373).

Covers the ``tools.export.format`` endpoint (auth, per-format responses,
ownership scoping, unknown-format 404) and ``services.export.send_export_email``
(attach-vs-link decision around ``EMAIL_ATTACHMENT_MAX_BYTES``, and the
no-email-address skip), plus the ExportStartView -> Celery threading of the
``email_export`` form flag.
"""

from __future__ import annotations

import functools
import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch
import uuid

from django.core import mail
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.export import send_export_email
from urbanlens.dashboard.services.export_formats import EXPORT_FORMATS


class ExportFormatDownloadViewTests(TestCase):
    """GET tools/export/format/<fmt>/ serves all of the requester's root pins."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin_one = baker.make(
            Pin,
            profile=self.profile,
            name="Abandoned Mill",
            name_is_user_provided=True,
            location=baker.make(Location, latitude="41.100000", longitude="-73.100000"),
        )
        self.pin_two = baker.make(
            Pin,
            profile=self.profile,
            name="Old Asylum",
            name_is_user_provided=True,
            location=baker.make(Location, latitude="42.200000", longitude="-74.200000"),
        )
        self.client.force_login(self.user)

    def test_login_required(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("tools.export.format", kwargs={"fmt": "geojson"}))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_each_format_returns_file_with_both_pins(self) -> None:
        for fmt, (_writer, extension, content_type) in EXPORT_FORMATS.items():
            with self.subTest(fmt=fmt):
                response = self.client.get(reverse("tools.export.format", kwargs={"fmt": fmt}))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], content_type)
                self.assertIn(f'.{extension}"', response["Content-Disposition"])
                self.assertIn("urbanlens_pins_", response["Content-Disposition"])
                body = response.content.decode("utf-8")
                self.assertTrue(body)
                self.assertIn("Abandoned Mill", body)
                self.assertIn("Old Asylum", body)

    def test_unknown_format_404s(self) -> None:
        response = self.client.get(reverse("tools.export.format", kwargs={"fmt": "shapefile"}))
        self.assertEqual(response.status_code, 404)

    def test_other_users_pins_never_appear(self) -> None:
        other_user = baker.make("auth.User")
        baker.make(
            Pin,
            profile=Profile.objects.get(user=other_user),
            name="Secret Bunker",
            name_is_user_provided=True,
            location=baker.make(Location, latitude="43.300000", longitude="-75.300000"),
        )
        for fmt in EXPORT_FORMATS:
            with self.subTest(fmt=fmt):
                response = self.client.get(reverse("tools.export.format", kwargs={"fmt": fmt}))
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("Secret Bunker", response.content.decode("utf-8"))

    def test_child_pins_are_excluded(self) -> None:
        baker.make(
            Pin,
            profile=self.profile,
            parent_pin=self.pin_one,
            name="Mill Boiler Room",
            name_is_user_provided=True,
            location=baker.make(Location, latitude="41.100100", longitude="-73.100100"),
        )
        response = self.client.get(reverse("tools.export.format", kwargs={"fmt": "geojson"}))
        body = response.content.decode("utf-8")
        self.assertIn("Abandoned Mill", body)
        self.assertNotIn("Mill Boiler Room", body)


class SendExportEmailTests(TestCase):
    """send_export_email attaches small archives, links large ones, skips no-address users."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User", email="explorer@example.com")
        self.job_id = str(uuid.uuid4())

    def _make_export_dir(self, zip_bytes: bytes) -> str:
        export_dir_path = tempfile.mkdtemp()
        self.addCleanup(functools.partial(shutil.rmtree, export_dir_path, ignore_errors=True))
        with open(os.path.join(export_dir_path, "export.zip"), "wb") as fh:
            fh.write(zip_bytes)
        return export_dir_path

    def test_small_archive_is_attached(self) -> None:
        export_dir_path = self._make_export_dir(b"tiny zip payload")
        note = send_export_email(self.user, export_dir_path, "https://example.com/", job_id=self.job_id)

        self.assertEqual(note, "A copy was emailed to you.")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["explorer@example.com"])
        self.assertEqual(len(message.attachments), 1)
        filename, content, mimetype = message.attachments[0]
        self.assertTrue(filename.startswith("urbanlens_export_"))
        self.assertTrue(filename.endswith(".zip"))
        self.assertEqual(content, b"tiny zip payload")
        self.assertEqual(mimetype, "application/zip")

    def test_large_archive_sends_download_link_instead(self) -> None:
        export_dir_path = self._make_export_dir(b"x" * 64)
        download_path = reverse("tools.export.download", kwargs={"job_id": self.job_id})

        with patch("urbanlens.dashboard.services.export.EMAIL_ATTACHMENT_MAX_BYTES", 10):
            note = send_export_email(self.user, export_dir_path, "https://example.com/", job_id=self.job_id)

        self.assertEqual(note, "A download link was emailed to you (the archive was too large to attach).")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.attachments, [])
        self.assertIn(f"https://example.com{download_path}", message.body)

    def test_user_without_email_is_skipped_without_sending(self) -> None:
        self.user.email = ""
        self.user.save(update_fields=["email"])
        export_dir_path = self._make_export_dir(b"tiny zip payload")

        note = send_export_email(self.user, export_dir_path, "https://example.com/", job_id=self.job_id)

        self.assertEqual(note, "Your account has no email address, so the export was not emailed.")
        self.assertEqual(mail.outbox, [])


class ExportStartViewEmailFlagTests(TestCase):
    """The email_export checkbox is threaded through to the Celery export task."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User", email="explorer@example.com")
        self.client.force_login(self.user)

    def _start_export(self, mock_enqueue: MagicMock, *, email_export: bool) -> None:
        mock_enqueue.return_value = MagicMock(id="task-id")
        data: dict[str, object] = {"export_types": ["pins"]}
        if email_export:
            data["email_export"] = "1"
        response = self.client.post(reverse("tools.export.start"), data)
        self.assertEqual(response.status_code, 200)

    @patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_checkbox_checked_enqueues_with_email_flag(self, mock_enqueue: MagicMock) -> None:
        self._start_export(mock_enqueue, email_export=True)
        args = mock_enqueue.call_args.args
        self.assertTrue(args[-1], "email_to_user should be True when the checkbox is submitted")

    @patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_checkbox_unchecked_enqueues_without_email_flag(self, mock_enqueue: MagicMock) -> None:
        self._start_export(mock_enqueue, email_export=False)
        args = mock_enqueue.call_args.args
        self.assertFalse(args[-1], "email_to_user should be False when the checkbox is absent")
