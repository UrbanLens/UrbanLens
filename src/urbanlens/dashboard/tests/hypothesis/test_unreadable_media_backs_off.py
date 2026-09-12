"""A file the database references and the disk does not have must stop being retried.

`backfill_image_thumbnails` walks by primary key, advances its cursor past
failures, and resets when the cursor is exhausted - so a row whose stored file
can never be read is retried on every wrap, forever, logging a full traceback
each time. Both sweeps do it over the same rows, so each unreadable file is
reported twice per cycle. Staging showed 50 such tracebacks per wrap (N22 H64).

The shape of the fix matters more than the fix. Two things it must not be:

* **Not a cache sentinel.** `write_image_preview` marks a bad document
  UNPREVIEWABLE in the cache, which suits a request path answering about one
  document. Here the question is asked of a *queryset* - excluding rows by
  consulting N cache keys after the query would filter in Python after the
  batch was already cut, which is the defect that made comment pages come back
  short (H47). The marker has to be a column so the exclusion is SQL.
* **Not a permanent blacklist.** A boolean "this file is broken" is one storage
  outage away from marking the entire library unreadable, silently and
  irreversibly, in a single sweep. So the column records *when* the file was
  last found unreadable and the sweep skips it for a window - a backoff, not a
  verdict. A genuinely missing file costs one retry a week instead of one an
  hour; a transient outage heals on its own; a restored file is picked up on
  the next pass and clears the mark.
"""

from __future__ import annotations

from datetime import timedelta
import io
from pathlib import Path
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.media.images import (
    THUMBNAIL_RETRY_AFTER_UNREADABLE,
    photos_missing_marker_thumbnails,
    photos_missing_thumbnails,
)
from urbanlens.dashboard.tasks import generate_image_marker_thumbnails, generate_image_thumbnails


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (800, 600), (20, 80, 140)).save(buffer, format="JPEG")
    return buffer.getvalue()


class _MediaCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._media_root = tempfile.mkdtemp(prefix="ul_unreadable_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        for part in ("pin_images", "pin_images/thumbs", "pin_images/markers"):
            (Path(self._media_root) / part).mkdir(parents=True, exist_ok=True)
        self._settings = override_settings(MEDIA_ROOT=self._media_root)
        self._settings.enable()
        self.addCleanup(self._settings.disable)

    def _photo(self) -> Image:
        image = baker.make_recipe("dashboard.image")
        image.image.save(f"shot-{image.pk}.jpg", ContentFile(_jpeg()), save=True)
        return image

    def _photo_with_no_file(self) -> Image:
        """A row pointing at a path that does not exist - staging's whole library."""
        image = self._photo()
        Path(self._media_root, image.image.name).unlink()
        return image


class TheSweepStopsRetryingWhatItCannotReadTests(_MediaCase):
    def test_a_missing_file_is_marked_rather_than_retried(self) -> None:
        image = self._photo_with_no_file()

        generate_image_thumbnails([image.pk])

        image.refresh_from_db()
        self.assertIsNotNone(image.media_unreadable_at)

    def test_a_marked_row_is_skipped_by_the_next_sweep(self) -> None:
        image = self._photo_with_no_file()
        generate_image_thumbnails([image.pk])

        self.assertNotIn(image.pk, photos_missing_thumbnails())

    def test_the_marker_sweep_skips_it_too(self) -> None:
        """One unreadable file, one fact - not one per thumbnail flavour."""
        image = self._photo_with_no_file()
        generate_image_thumbnails([image.pk])

        self.assertNotIn(image.pk, photos_missing_marker_thumbnails())

    def test_the_marker_sweep_also_records_it(self) -> None:
        image = self._photo_with_no_file()

        generate_image_marker_thumbnails([image.pk])

        image.refresh_from_db()
        self.assertIsNotNone(image.media_unreadable_at)

    def test_a_readable_photo_is_still_swept(self) -> None:
        """Anti-vacuity: a sweep that returned nothing would pass every test
        above while backfilling nothing, forever."""
        healthy = self._photo()

        self.assertIn(healthy.pk, photos_missing_thumbnails())
        self.assertIn(healthy.pk, photos_missing_marker_thumbnails())


class TheMarkIsABackoffNotAVerdictTests(_MediaCase):
    """The failure mode a plain boolean would create: one storage outage marks
    the whole library broken, silently, with nothing that ever revisits it."""

    def test_an_old_mark_is_retried_again(self) -> None:
        image = self._photo()
        Image.objects.filter(pk=image.pk).update(
            media_unreadable_at=timezone.now() - THUMBNAIL_RETRY_AFTER_UNREADABLE - timedelta(hours=1)
        )

        self.assertIn(image.pk, photos_missing_thumbnails())
        self.assertIn(image.pk, photos_missing_marker_thumbnails())

    def test_a_fresh_mark_is_not(self) -> None:
        """Paired with the test above: together they say the window is real,
        rather than that the column is ignored in one direction."""
        image = self._photo()
        Image.objects.filter(pk=image.pk).update(media_unreadable_at=timezone.now())

        self.assertNotIn(image.pk, photos_missing_thumbnails())
        self.assertNotIn(image.pk, photos_missing_marker_thumbnails())

    def test_a_restored_file_clears_its_mark(self) -> None:
        """The file came back. Nothing should carry a scar that outlives it."""
        image = self._photo()
        Image.objects.filter(pk=image.pk).update(
            media_unreadable_at=timezone.now() - THUMBNAIL_RETRY_AFTER_UNREADABLE - timedelta(hours=1)
        )

        generate_image_thumbnails([image.pk])

        image.refresh_from_db()
        self.assertIsNone(image.media_unreadable_at)
        self.assertTrue(image.thumbnail)

    def test_the_window_is_long_enough_to_matter(self) -> None:
        """The whole point is fewer retries than hourly. A window under an hour
        would satisfy every other test here and fix nothing."""
        self.assertGreaterEqual(THUMBNAIL_RETRY_AFTER_UNREADABLE, timedelta(days=1))
