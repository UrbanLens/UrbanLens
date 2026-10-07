"""A stored video never carries the container location tag it arrived with."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.media.videos import (
    _clear_location_args,
    extract_video_metadata,
    ffmpeg_available,
    process_uploaded_video,
)

_COORDS = "+42.6526-073.7562/"


def _every_tag(path: Path) -> str:
    """Every container- and stream-level tag ffprobe reports, as one lowercase blob."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_entries", "format_tags:stream_tags", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.dumps(json.loads(result.stdout)).lower()


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
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=size={height * 4 // 3}x{height}:rate=10:duration=1",
                "-c:v",
                "libx264",
                "-metadata",
                f"location={_COORDS}",
                "-metadata",
                f"com.apple.quicktime.location.ISO6709={_COORDS}",
                str(out),
            ],
            capture_output=True,
            check=True,
        )
        return out.read_bytes()

    def _stored_after(self, height: int, *, max_height: int | None) -> Path:
        image = baker.make(Image, image=None)
        image.image.save("clip.mp4", ContentFile(self._video_with_location(height)), save=True)

        process_uploaded_video(image, max_height)

        # process_uploaded_video leaves persisting image.image.name to its caller.
        return Path(image.image.path)

    def test_fixture_actually_carries_a_location_tag(self) -> None:
        """Without this, every strip assertion below could pass vacuously."""
        path = Path(tempfile.mkdtemp(dir=self._media_root)) / "probe.mp4"
        path.write_bytes(self._video_with_location(240))

        self.assertIn("location", _location_tags(path))

    def test_small_video_is_scrubbed_even_though_no_downscale_is_needed(self) -> None:
        """The case the old code could never reach - it skipped processing entirely."""
        stored = self._stored_after(240, max_height=720)

        self.assertNotIn("location", _location_tags(stored))

    def test_downscaled_video_is_also_scrubbed(self) -> None:
        """ffmpeg copies container metadata across a transcode unless told otherwise."""
        stored = self._stored_after(480, max_height=240)

        self.assertNotIn("location", _location_tags(stored))

    def test_a_location_tag_we_cannot_parse_is_still_stripped(self) -> None:
        """The scrub must key off the tag's presence, not off it being readable.

        ``extract_video_metadata`` parses ISO 6709; a tag in any other notation yields no coordinates."""
        out = Path(tempfile.mkdtemp(dir=self._media_root)) / "odd.mkv"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=10:duration=1",
                "-c:v",
                "libx264",
                "-metadata",
                "location=42 deg 39 min N, 73 deg 45 min W",
                str(out),
            ],
            capture_output=True,
            check=True,
        )
        self.assertIn("location", _location_tags(out), "fixture must carry the odd-notation tag")

        image = baker.make(Image, image=None)
        image.image.save("odd.mkv", ContentFile(out.read_bytes()), save=True)

        process_uploaded_video(image, 720)

        self.assertNotIn("location", _location_tags(Path(image.image.path)))

    def test_the_uploaders_visit_tracking_setting_does_not_keep_the_tag(self) -> None:
        """There is no setting that leaves coordinates in a served file."""
        stored = self._stored_after(240, max_height=720)

        self.assertNotIn("location", _location_tags(stored))

    def test_a_video_with_no_downscale_policy_is_still_scrubbed(self) -> None:
        """max_height=None means "do not resize", never "do not scrub"."""
        stored = self._stored_after(240, max_height=None)

        self.assertNotIn("location", _location_tags(stored))

    def test_a_location_on_a_stream_is_found_and_stripped(self) -> None:
        """Matroska tags a track as readily as the file, and the scrub used to look only at the file."""
        out = Path(tempfile.mkdtemp(dir=self._media_root)) / "tracks.mkv"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=10:duration=1",
                "-c:v",
                "libx264",
                "-metadata:s:v:0",
                f"location={_COORDS}",
                "-metadata:s:v:0",
                "language=eng",
                str(out),
            ],
            capture_output=True,
            check=True,
        )
        self.assertNotIn("location", _location_tags(out), "fixture must carry its location only on the stream")
        self.assertIn("42.6526", _every_tag(out), "fixture must carry the stream-level tag")

        image = baker.make(Image, image=None)
        image.image.save("tracks.mkv", ContentFile(out.read_bytes()), save=True)

        metadata, _replacement = process_uploaded_video(image, 720)

        self.assertEqual((metadata["latitude"], metadata["longitude"]), (42.6526, -73.7562))
        stored = _every_tag(Path(image.image.path))
        self.assertNotIn("location", stored)
        self.assertNotIn("42.6526", stored)


class StreamLevelLocationTests(SimpleTestCase):
    """What runs without ffmpeg: finding a stream's location, and the arguments that clear it."""

    def _metadata(self, probed: dict) -> dict:
        with patch("urbanlens.dashboard.services.media.videos.probe_video", return_value=probed):
            return extract_video_metadata("video.mkv")

    def test_a_location_on_a_stream_is_found(self) -> None:
        metadata = self._metadata(
            {
                "format": {"tags": {"ENCODER": "Lavf"}},
                "streams": [{"codec_type": "video", "width": 320, "height": 240, "tags": {"LOCATION": _COORDS}}],
            }
        )

        self.assertTrue(metadata.get("has_location_tag"))
        self.assertEqual((metadata["latitude"], metadata["longitude"]), (42.6526, -73.7562))

    def test_apples_own_key_on_the_file_is_found(self) -> None:
        """An iPhone writes only this one. The lookup compared it, mixed case, against case-folded tag names."""
        metadata = self._metadata(
            {"format": {"tags": {"com.apple.quicktime.location.ISO6709": _COORDS}}, "streams": []}
        )

        self.assertTrue(metadata.get("has_location_tag"))
        self.assertEqual((metadata["latitude"], metadata["longitude"]), (42.6526, -73.7562))

    def test_a_location_named_some_other_way_is_still_found(self) -> None:
        for key in ("GPS_COORDS", "com.apple.quicktime.location.accuracy.horizontal", "location_name"):
            with self.subTest(key=key):
                metadata = self._metadata(
                    {"format": {"tags": {}}, "streams": [{"codec_type": "video", "tags": {key: "somewhere"}}]}
                )

                self.assertTrue(metadata.get("has_location_tag"))

    def test_a_video_without_one_is_left_alone(self) -> None:
        metadata = self._metadata(
            {
                "format": {"tags": {"encoder": "Lavf"}},
                "streams": [{"codec_type": "video", "tags": {"language": "eng", "handler_name": "VideoHandler"}}],
            }
        )

        self.assertNotIn("has_location_tag", metadata)

    def test_every_location_tag_is_cleared_on_every_stream_too(self) -> None:
        args = _clear_location_args()
        pairs = set(zip(args[::2], args[1::2], strict=True))

        for tag in ("location", "location-eng", "com.apple.quicktime.location.ISO6709"):
            with self.subTest(tag=tag):
                self.assertIn(("-metadata", f"{tag}="), pairs)
                self.assertIn(("-metadata:s", f"{tag}="), pairs)

    def test_nothing_but_location_is_cleared(self) -> None:
        """Rotation is a display matrix, not a tag, and creation time and languages stay, as the photo path keeps EXIF."""
        self.assertNotIn("-map_metadata", _clear_location_args())
