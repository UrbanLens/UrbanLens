"""A video upload must honour the same location opt-out photos do.

``_process_video_upload`` used to express "strip the location" by passing
``max_height=None`` to :func:`process_uploaded_video`. That value means "skip
downscaling entirely", so the request to scrub produced the exact opposite: the
original file - location tag and all - was stored untouched and served, while
only the derived ``Image.latitude``/``longitude`` and ``taken_at`` were dropped.

Two things had to change, and both are exercised here against real ffmpeg:

- a small video needing no downscale is now scrubbed by a lossless stream copy;
- ``_reencode`` clears the location tags too, since ffmpeg copies container
  metadata across a transcode (verified: the tags survived the old flags).

Skipped when ffmpeg isn't on PATH, so this stays runnable outside the container.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from django.core.files.base import ContentFile
from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.media.videos import ffmpeg_available, process_uploaded_video

_COORDS = "+42.6526-073.7562/"


def _location_tags(path: Path) -> str:
    """Every container-level tag ffprobe reports, as one lowercase blob."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.lower()


@unittest.skipUnless(ffmpeg_available(), "ffmpeg is not installed")
class VideoLocationStripTests(TestCase):
    def setUp(self) -> None:
        self._media_root = tempfile.mkdtemp(prefix="ul_vid_strip_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        (Path(self._media_root) / "pin_images").mkdir(parents=True, exist_ok=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root)
        overrides.enable()
        self.addCleanup(overrides.disable)

    def _video_with_location(self, height: int) -> bytes:
        out = Path(tempfile.mkdtemp(dir=self._media_root)) / "src.mp4"
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", f"testsrc=size={height * 4 // 3}x{height}:rate=10:duration=1",
                "-c:v", "libx264",
                "-metadata", f"location={_COORDS}",
                "-metadata", f"com.apple.quicktime.location.ISO6709={_COORDS}",
                str(out),
            ],
            capture_output=True,
            check=True,
        )
        return out.read_bytes()

    def _stored_after(self, height: int, *, max_height: int | None, strip: bool) -> Path:
        image = baker.make(Image, image=None)
        image.image.save("clip.mp4", ContentFile(self._video_with_location(height)), save=True)

        process_uploaded_video(image, max_height, strip_location=strip)

        # process_uploaded_video leaves persisting image.image.name to its caller.
        return Path(image.image.path)

    def test_fixture_actually_carries_a_location_tag(self) -> None:
        """Without this, every strip assertion below could pass vacuously."""
        path = Path(tempfile.mkdtemp(dir=self._media_root)) / "probe.mp4"
        path.write_bytes(self._video_with_location(240))

        self.assertIn("location", _location_tags(path))

    def test_small_video_is_scrubbed_even_though_no_downscale_is_needed(self) -> None:
        """The case the old code could never reach - it skipped processing entirely."""
        stored = self._stored_after(240, max_height=720, strip=True)

        self.assertNotIn("location", _location_tags(stored))

    def test_downscaled_video_is_also_scrubbed(self) -> None:
        """ffmpeg copies container metadata across a transcode unless told otherwise."""
        stored = self._stored_after(480, max_height=240, strip=True)

        self.assertNotIn("location", _location_tags(stored))

    def test_a_location_tag_we_cannot_parse_is_still_stripped(self) -> None:
        """The scrub must key off the tag's presence, not off it being readable.

        ``extract_video_metadata`` parses ISO 6709; a tag in any other notation
        yields no coordinates. Gating the strip on parsed *coordinates* - which
        is how this was first written - leaves exactly those tags in place, and
        an unreadable tag discloses the location just as well.

        The fixture is Matroska because the MP4 muxer silently refuses to write
        a non-ISO 6709 ``location`` (verified), while Matroska stores it
        verbatim - and ``mkv``/``webm`` are both accepted uploads. Under the
        parse-gated version this file was left untouched, tag intact.
        """
        out = Path(tempfile.mkdtemp(dir=self._media_root)) / "odd.mkv"
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=1",
                "-c:v", "libx264",
                "-metadata", "location=42 deg 39 min N, 73 deg 45 min W",
                str(out),
            ],
            capture_output=True,
            check=True,
        )
        self.assertIn("location", _location_tags(out), "fixture must carry the odd-notation tag")

        image = baker.make(Image, image=None)
        image.image.save("odd.mkv", ContentFile(out.read_bytes()), save=True)

        process_uploaded_video(image, 720, strip_location=True)

        self.assertNotIn("location", _location_tags(Path(image.image.path)))

    def test_location_is_kept_when_the_user_has_not_opted_out(self) -> None:
        stored = self._stored_after(240, max_height=720, strip=False)

        self.assertIn("location", _location_tags(stored))
