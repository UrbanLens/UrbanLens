"""What one import preview may hold in the sandbox worker, and how many are read at once (P95).

`media-worker` reads previews two at a time in 3 GB. A preview kept every extracted entry until the
parser returned - up to the 2 GB extraction budget - so two side by side could not fit, and an OOM kill
takes the other preview and any photo work on that worker with it. Entries are now parsed as they come
out of the archive, and only a set number of previews are read site-wide at once; the rest wait.
"""

from __future__ import annotations

from datetime import timedelta
import io
import os
import tracemalloc
from unittest import mock
import zipfile

from celery.exceptions import Retry
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.core import single_flight
from urbanlens.dashboard.services.pins import import_preview
from urbanlens.dashboard.tasks import parse_import_preview_task

ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"

KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>Test Spot</name>
<Point><coordinates>-73.9251,41.7003,0</coordinates></Point></Placemark></Document></kml>
"""

_ENTRY_BYTES = 8 * 1024 * 1024
_ENTRIES = 10


def _zip_of_unreadable_entries() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for index in range(_ENTRIES):
            # Not UTF-8, so each entry is refused at the first byte and the parser itself holds nothing.
            zf.writestr(f"part{index}.csv", b"\xff" * _ENTRY_BYTES)
    return buf.getvalue()


class _PreviewCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        for index in range(3):
            self.addCleanup(single_flight.release, import_preview.parse_slot_key(index))

    def _profile(self):
        profile = baker.make(User).profile
        self.addCleanup(single_flight.release, import_preview.guard_key(profile.pk))
        return profile

    def _upload(self, profile, name: str, data: bytes) -> str:
        with mock.patch(ENQUEUE, return_value=mock.Mock()):
            job_id = import_preview.start_import_preview(profile, [SimpleUploadedFile(name, data)])
        self.addCleanup(import_preview.shutil.rmtree, import_preview.job_dir(job_id), True)
        return job_id

    def _parse(self, profile, job_id: str) -> bool:
        with (
            override_settings(UL_PROCESS_ROLE="sandbox", UL_UNTRUSTED_PARSE_POLICY="deny"),
            mock.patch(ENQUEUE, return_value=mock.Mock()),
        ):
            return import_preview.parse_import_preview(profile.pk, job_id)


class EntriesAreParsedAsTheyAreExtractedTests(_PreviewCase):
    def test_a_preview_does_not_hold_every_extracted_entry_at_once(self) -> None:
        profile = self._profile()
        job_id = self._upload(profile, "export.zip", _zip_of_unreadable_entries())

        tracemalloc.start()
        try:
            self._parse(profile, job_id)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(import_preview.ImportPreviewStatus(job_id).read()["status"], "error", "the premise failed")
        held = _ENTRY_BYTES * _ENTRIES
        self.assertLess(peak, held // 2, f"peak {peak:,} bytes against {held:,} extracted")

    def test_a_kmz_still_takes_the_name_it_was_uploaded_under(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("doc.kml", KML)
        profile = self._profile()
        job_id = self._upload(profile, "Urbex Sites.kmz", buf.getvalue())

        self.assertTrue(self._parse(profile, job_id))

        result = import_preview.read_preview(profile.user_id, job_id)
        self.assertEqual([entry["stem"] for entry in result["result"]["lists"]], ["Urbex Sites"])


class PreviewsAreReadAFewAtATimeSiteWideTests(_PreviewCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertTrue(hasattr(settings, "IMPORT_PREVIEW_MAX_CONCURRENT_PARSES"), "the setting does not exist")
        self.first_profile = self._profile()
        self.second_profile = self._profile()
        self.first = self._upload(self.first_profile, "First.kml", KML)
        self.second = self._upload(self.second_profile, "Second.kml", KML)

    def _while_the_first_is_read(self, during):
        """Run *during* from inside the first preview's parse, returning what it returned."""
        seen: list[object] = []
        real = GoogleMapsGateway.parse_for_preview

        def parse(gateway, files, profile):
            if not seen:
                seen.append(None)
                seen[0] = during()
            return real(gateway, files, profile)

        with mock.patch.object(GoogleMapsGateway, "parse_for_preview", autospec=True, side_effect=parse):
            self.assertTrue(self._parse(self.first_profile, self.first))
        self.assertEqual(len(seen), 1, "the premise failed: the first preview never reached its parse")
        return seen[0]

    def _status(self, job_id: str) -> dict:
        return import_preview.ImportPreviewStatus(job_id).read()

    @override_settings(IMPORT_PREVIEW_MAX_CONCURRENT_PARSES=1)
    def test_a_second_accounts_preview_waits_while_one_is_being_read(self) -> None:
        finished = self._while_the_first_is_read(lambda: self._parse(self.second_profile, self.second))

        self.assertFalse(finished)
        self.assertEqual(self._status(self.second)["status"], "pending")
        self.assertTrue(os.listdir(os.path.join(import_preview.job_dir(self.second), "uploads")))
        self.assertEqual(single_flight.holder(import_preview.guard_key(self.second_profile.pk)), self.second)

        self.assertTrue(self._parse(self.second_profile, self.second))
        self.assertEqual(self._status(self.second)["status"], "done")

    @override_settings(IMPORT_PREVIEW_MAX_CONCURRENT_PARSES=2)
    def test_a_preview_within_the_limit_is_read_at_once(self) -> None:
        finished = self._while_the_first_is_read(lambda: self._parse(self.second_profile, self.second))

        self.assertTrue(finished)
        self.assertEqual(self._status(self.second)["status"], "done")

    @override_settings(IMPORT_PREVIEW_MAX_CONCURRENT_PARSES=1)
    def test_a_preview_left_waiting_past_its_time_gives_up_and_frees_the_account(self) -> None:
        def parse_later() -> bool:
            with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(minutes=30)):
                return self._parse(self.second_profile, self.second)

        finished = self._while_the_first_is_read(parse_later)

        self.assertTrue(finished, "it would be retried until the broker gave up")
        self.assertEqual(self._status(self.second)["status"], "error")
        self.assertFalse(os.path.exists(os.path.join(import_preview.job_dir(self.second), "uploads")))
        self.assertIsNone(single_flight.holder(import_preview.guard_key(self.second_profile.pk)))

    @override_settings(IMPORT_PREVIEW_MAX_CONCURRENT_PARSES=1)
    def test_the_slot_is_given_back_when_the_parse_ends(self) -> None:
        self.assertTrue(self._parse(self.first_profile, self.first))

        self.assertTrue(self._parse(self.second_profile, self.second))


class TheWaitingTaskIsRetriedTests(TestCase):
    def test_a_preview_with_no_free_slot_is_retried_later(self) -> None:
        with (
            mock.patch.object(import_preview, "parse_import_preview", return_value=False),
            mock.patch.object(parse_import_preview_task, "retry", side_effect=Retry()) as retry,
            self.assertRaises(Retry),
        ):
            parse_import_preview_task(1, "job")

        retry.assert_called_once_with(countdown=import_preview.PARSE_SLOT_RETRY_SECONDS)

    def test_a_preview_that_was_read_is_not_retried(self) -> None:
        with (
            mock.patch.object(import_preview, "parse_import_preview", return_value=True),
            mock.patch.object(parse_import_preview_task, "retry") as retry,
        ):
            parse_import_preview_task(1, "job")

        retry.assert_not_called()
