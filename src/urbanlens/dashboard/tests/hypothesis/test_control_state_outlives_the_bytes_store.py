"""State that controls work is kept out of the proxied-bytes store, which may evict anything at any time.

``da3f6c885`` pointed every ``bounded_cache`` helper at that store, which moved an import's cancel request and the
media-usage measurement into it along with the tile bytes.
"""

from __future__ import annotations

from uuid import uuid4

from django.conf import settings
from django.core.cache import caches
from django.utils import timezone

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.admin import media_usage
from urbanlens.dashboard.services.pins.confirmed_import import (
    ConfirmedImportStatus,
    cancel_confirmed_import,
    import_cancel_requested,
)


class ControlStateOutlivesTheBytesStoreTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(caches["default"].clear)
        self.addCleanup(caches[settings.PROXIED_BYTES_CACHE].clear)

    def test_an_import_cancel_survives_the_bytes_store_emptying(self) -> None:
        job_id = str(uuid4())
        ConfirmedImportStatus(job_id).write("running", 10, "Importing...", user_id=7)

        self.assertTrue(cancel_confirmed_import(7, job_id))
        caches[settings.PROXIED_BYTES_CACHE].clear()

        self.assertTrue(import_cancel_requested(job_id))

    def test_a_media_measurement_survives_the_bytes_store_emptying(self) -> None:
        caches["default"].set(media_usage.GUARD_KEY, "held", 60)
        stored = {"megabytes": 12.5, "measured_at": timezone.now().isoformat()}
        caches["default"].set(media_usage.CACHE_KEY, stored, 60)
        caches[settings.PROXIED_BYTES_CACHE].clear()

        usage = media_usage.measured_media_usage()

        self.assertIsNotNone(usage)
        self.assertEqual(usage.megabytes if usage else None, 12.5)
