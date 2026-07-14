"""Unit tests for services.videos - ffmpeg/ffprobe-backed video processing.

External binaries (ffmpeg/ffprobe) are mocked throughout: these tests verify
the Python-side logic (JSON parsing, ISO 6709 parsing, decision-making about
whether to re-encode), not the actual media processing, which needs Docker.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.videos import (
    _parse_iso6709,
    extract_video_metadata,
    ffmpeg_available,
    process_uploaded_video,
)


class FfmpegAvailableTests(TestCase):
    def test_true_when_both_binaries_found(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            self.assertTrue(ffmpeg_available())

    def test_false_when_missing(self) -> None:
        with patch("shutil.which", return_value=None):
            self.assertFalse(ffmpeg_available())


class ParseIso6709Tests(TestCase):
    def test_parses_positive_and_negative(self) -> None:
        self.assertEqual(_parse_iso6709("+40.6892-074.0445/"), (40.6892, -74.0445))

    def test_parses_positive_and_positive(self) -> None:
        self.assertEqual(_parse_iso6709("+35.6812+139.7671/"), (35.6812, 139.7671))

    def test_returns_none_for_garbage(self) -> None:
        self.assertIsNone(_parse_iso6709("not a coordinate"))

    def test_returns_none_for_empty(self) -> None:
        self.assertIsNone(_parse_iso6709(""))


class ExtractVideoMetadataTests(TestCase):
    def test_no_ffmpeg_returns_empty(self) -> None:
        with patch("urbanlens.dashboard.services.videos.ffmpeg_available", return_value=False):
            self.assertEqual(extract_video_metadata("video.mp4"), {})

    def test_parses_creation_time_location_and_dimensions(self) -> None:
        probed = {
            "format": {"tags": {"creation_time": "2026-06-01T12:00:00.000000Z", "location": "+40.6892-074.0445/"}},
            "streams": [{"codec_type": "audio"}, {"codec_type": "video", "width": 1920, "height": 1080}],
        }
        with patch("urbanlens.dashboard.services.videos.probe_video", return_value=probed):
            metadata = extract_video_metadata("video.mp4")
        self.assertIn("taken_at", metadata)
        self.assertEqual((metadata["latitude"], metadata["longitude"]), (40.6892, -74.0445))
        self.assertEqual((metadata["width"], metadata["height"]), (1920, 1080))

    def test_missing_probe_result_yields_empty(self) -> None:
        with patch("urbanlens.dashboard.services.videos.probe_video", return_value=None):
            self.assertEqual(extract_video_metadata("video.mp4"), {})


class ProcessUploadedVideoTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.image = baker.make(
            Image,
            profile=self.user.profile,
            image=SimpleUploadedFile("clip.mp4", b"fake-video-bytes", content_type="video/mp4"),
        )

    def test_no_ffmpeg_returns_no_metadata_no_resize(self) -> None:
        with patch("urbanlens.dashboard.services.videos.ffmpeg_available", return_value=False):
            metadata, new_size = process_uploaded_video(self.image, 1080)
        self.assertEqual(metadata, {})
        self.assertIsNone(new_size)

    def test_already_within_max_height_skips_reencode(self) -> None:
        with (
            patch("urbanlens.dashboard.services.videos.ffmpeg_available", return_value=True),
            patch("urbanlens.dashboard.services.videos.extract_video_metadata", return_value={"height": 720}),
            patch("urbanlens.dashboard.services.videos._reencode") as mock_reencode,
        ):
            metadata, new_size = process_uploaded_video(self.image, 1080)
        mock_reencode.assert_not_called()
        self.assertEqual(metadata["height"], 720)
        self.assertIsNone(new_size)

    def test_oversized_video_triggers_reencode(self) -> None:
        def fake_reencode(src_path: str, out_path: str, max_height: int) -> bool:
            with open(out_path, "wb") as f:
                f.write(b"x")  # smaller than the 17-byte source, so it's kept
            return True

        with (
            patch("urbanlens.dashboard.services.videos.ffmpeg_available", return_value=True),
            patch("urbanlens.dashboard.services.videos.extract_video_metadata", return_value={"height": 2160}),
            patch("urbanlens.dashboard.services.videos._reencode", side_effect=fake_reencode) as mock_reencode,
        ):
            metadata, new_size = process_uploaded_video(self.image, 1080)
        mock_reencode.assert_called_once()
        self.assertEqual(metadata["height"], 2160)
        self.assertEqual(new_size, 1)

    def test_reencode_failure_returns_no_new_size(self) -> None:
        with (
            patch("urbanlens.dashboard.services.videos.ffmpeg_available", return_value=True),
            patch("urbanlens.dashboard.services.videos.extract_video_metadata", return_value={"height": 2160}),
            patch("urbanlens.dashboard.services.videos._reencode", return_value=False),
        ):
            metadata, new_size = process_uploaded_video(self.image, 1080)
        self.assertEqual(metadata["height"], 2160)
        self.assertIsNone(new_size)
