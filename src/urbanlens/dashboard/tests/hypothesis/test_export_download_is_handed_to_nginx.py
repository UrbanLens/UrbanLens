"""A multi-gigabyte download must not occupy a worker for its whole duration.

`ExportDownloadView` answered with a `FileResponse` over the export ZIP, so the
bytes travelled through gunicorn. nginx absorbs a response up to
`proxy_max_temp_file_size` and then switches to synchronous mode, matching the
client's pace - so past that ceiling one person on a slow connection holds a
worker for as long as their download takes, and an export is every photo the
account owns.

Every other large file in this application is handed to nginx instead: the
media gate authorizes and answers with an `X-Accel-Redirect` into the
internal-only `/_protected_media/`, and exports already sit under
`MEDIA_ROOT/exports/`, which is the volume that location aliases.

The authorization is the part that must not move. It runs exactly as before and
these tests re-assert each refusal, because the hand-off replaces the response
and nothing else - handing nginx a path for a job somebody else owns would turn
a download into a data leak.

`media.conf.template` records which headers survive an X-Accel-Redirect on this
image, measured: `Content-Disposition` does, which is what keeps the filename.
"""

from __future__ import annotations

import os
import pathlib
import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.nginx_config import NGINX_DIR, directive_arguments, parsed_directives
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.import_export.export import ExportJobStatus, export_dir


class ExportDownloadHandOffTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.job_id = str(uuid.uuid4())
        directory = export_dir(self.job_id)
        os.makedirs(directory, exist_ok=True)
        self.zip_path = pathlib.Path(directory) / "export.zip"
        self.zip_path.write_bytes(b"PK\x03\x04 not really a zip")
        self.addCleanup(self._cleanup)
        ExportJobStatus(self.job_id).write("done", 100, "ready", user_id=self.user.pk)

    def _cleanup(self) -> None:
        directory = pathlib.Path(export_dir(self.job_id))
        if self.zip_path.exists():
            self.zip_path.unlink()
        if directory.exists():
            directory.rmdir()

    def _get(self):
        return self.client.get(reverse("tools.export.download", kwargs={"job_id": self.job_id}))

    @override_settings(MEDIA_X_ACCEL=True)
    def test_nginx_is_handed_the_file_rather_than_the_worker_streaming_it(self) -> None:
        response = self._get()

        self.assertEqual(response.status_code, 200)
        target = response["X-Accel-Redirect"]
        self.assertEqual(target, f"{settings.MEDIA_X_ACCEL_PREFIX}exports/{self.job_id}/export.zip")
        self.assertTrue(target.startswith(settings.MEDIA_X_ACCEL_PREFIX), "the hand-off escapes the internal location")
        self.assertEqual(b"".join(response.streaming_content) if response.streaming else response.content, b"")

    @override_settings(MEDIA_X_ACCEL=True)
    def test_the_filename_survives_the_hand_off(self) -> None:
        """Content-Disposition is on the measured allow-list nginx forwards."""
        response = self._get()

        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn("urbanlens_export_", response["Content-Disposition"])

    @override_settings(MEDIA_X_ACCEL=False)
    def test_without_nginx_the_file_is_still_streamed(self) -> None:
        """The negative half, and what local development actually runs."""
        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("X-Accel-Redirect", response)
        self.assertEqual(b"".join(response.streaming_content), self.zip_path.read_bytes())

    @override_settings(MEDIA_X_ACCEL=True)
    def test_another_users_job_is_still_refused(self) -> None:
        intruder = baker.make(User)
        self.client.force_login(intruder)

        response = self._get()

        self.assertNotIn("X-Accel-Redirect", response)
        self.assertEqual(response.status_code, 302)

    @override_settings(MEDIA_X_ACCEL=True)
    def test_an_unfinished_job_is_still_refused(self) -> None:
        ExportJobStatus(self.job_id).write("running", 40, "working", user_id=self.user.pk)

        response = self._get()

        self.assertNotIn("X-Accel-Redirect", response)
        self.assertEqual(response.status_code, 302)

    @override_settings(MEDIA_X_ACCEL=True)
    def test_a_missing_file_is_still_refused(self) -> None:
        self.zip_path.unlink()

        response = self._get()

        self.assertNotIn("X-Accel-Redirect", response)
        self.assertEqual(response.status_code, 302)

    @override_settings(MEDIA_X_ACCEL=True)
    def test_a_job_id_that_is_not_a_uuid_is_still_refused(self) -> None:
        response = self.client.get(reverse("tools.export.download", kwargs={"job_id": "not-a-uuid"}))

        self.assertNotIn("X-Accel-Redirect", response)
        self.assertEqual(response.status_code, 302)

    @override_settings(MEDIA_X_ACCEL=True)
    def test_a_traversal_never_reaches_the_view_at_all(self) -> None:
        """Two independent reasons the hand-off cannot escape its location.

        The route captures ``[^/]+``, so a path separator cannot be smuggled
        through the URL, and the view parses what does arrive as a uuid before
        anything is built from it. Asserted rather than reasoned about, because
        the value is interpolated straight into a filesystem path nginx trusts.
        """
        response = self.client.get("/dashboard/tools/export/download/../../etc/")

        self.assertNotIn("X-Accel-Redirect", response)
        self.assertNotEqual(response.status_code, 200)


class TheInternalLocationCarriesItsOwnHeadersTests(SimpleTestCase):
    """nginx drops these four when it follows an X-Accel-Redirect, measured.

    `media.conf.template` says so in as many words and sets them; `django.conf`
    aliases the same volume from the same kind of internal location and set
    none, so everything the app's own vhost hands to nginx lost them.
    """

    #: The set media.conf.template records as dropped, and therefore states.
    REQUIRED = ("X-Content-Type-Options", "Referrer-Policy", "Content-Security-Policy")

    INTERNAL = ("location", "/_protected_media/")

    def _headers_on_internal_location(self, vhost: str) -> set[str]:
        text = (NGINX_DIR / vhost).read_text()
        inside = {
            tokens[1]
            for context, tokens, _ in parsed_directives(text)
            if tokens[0] == "add_header" and self.INTERNAL in context
        }
        # media.conf.template states them at server level, which a location
        # carrying no add_header of its own inherits.
        if not inside:
            inside = {arguments[0] for arguments in directive_arguments(text, "add_header")}
        return inside

    def test_both_vhosts_state_the_dropped_headers(self) -> None:
        for vhost in ("django.conf", "media.conf.template"):
            headers = self._headers_on_internal_location(vhost)
            for required in self.REQUIRED:
                with self.subTest(vhost=vhost, header=required):
                    self.assertIn(required, headers)

    def test_the_headers_are_marked_always(self) -> None:
        """Without `always` nginx omits them on the error responses too."""
        text = (NGINX_DIR / "django.conf").read_text()
        stated = [
            (tokens, line)
            for context, tokens, line in parsed_directives(text)
            if tokens[0] == "add_header" and self.INTERNAL in context
        ]
        self.assertTrue(stated, "no headers are stated there, so this asserts nothing")
        for tokens, line in stated:
            with self.subTest(header=tokens[1], line=line):
                self.assertEqual(tokens[-1], "always")
