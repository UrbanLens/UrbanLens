"""A video's location is not only in its tags: drones caption every frame with their position, and action cameras
record GPS telemetry in a track of its own. A stored video keeps its pictures and sound and none of those tracks.

DJI writes an SRT subtitle track (``[latitude: ...] [longitude: ...]`` per frame); GoPro writes GPMF in a ``gpmd``
data track. Neither was looked at, so a video carrying only those was stored as uploaded, and nothing kept a rewrite
from carrying one across.
"""

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
from urbanlens.dashboard.services.media import videos
from urbanlens.dashboard.services.media.videos import extract_video_metadata, ffmpeg_available, process_uploaded_video

_DJI_CAPTION = (
    "1\n00:00:00,000 --> 00:00:01,000\n[iso: 100] [latitude: 42.6526] [longitude: -73.7562] [rel_alt: 50.0]\n"
)


def _metadata(streams: list[dict]) -> dict:
    with patch(
        "urbanlens.dashboard.services.media.videos.probe_video",
        return_value={"format": {"tags": {}}, "streams": streams},
    ):
        return extract_video_metadata("video.mp4")


_VIDEO = {"codec_type": "video", "codec_name": "h264", "width": 320, "height": 240}
_AUDIO = {"codec_type": "audio", "codec_name": "aac"}


class WhichTracksCountTests(SimpleTestCase):
    def test_a_subtitle_track_counts(self) -> None:
        """DJI's flight captions are a subtitle track; what one says is not read here, so every one counts."""
        self.assertTrue(
            _metadata([_VIDEO, {"codec_type": "subtitle", "codec_name": "mov_text", "codec_tag_string": "tx3g"}]).get(
                "has_location_track"
            )
        )

    def test_a_telemetry_track_counts(self) -> None:
        for tag in ("gpmd", "djmd", "camm", "mebx", "rtmd"):
            with self.subTest(tag=tag):
                self.assertTrue(
                    _metadata([_VIDEO, {"codec_type": "data", "codec_name": "bin_data", "codec_tag_string": tag}]).get(
                        "has_location_track"
                    )
                )

    def test_a_cover_picture_counts(self) -> None:
        """A cover picture is a JPEG or PNG with metadata of its own."""
        cover = {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}}
        self.assertTrue(_metadata([_VIDEO, cover]).get("has_location_track"))

    def test_a_timecode_track_does_not(self) -> None:
        """It says only where in the recording a frame is, and cameras write one to most of their files."""
        self.assertNotIn(
            "has_location_track", _metadata([_VIDEO, _AUDIO, {"codec_type": "data", "codec_tag_string": "tmcd"}])
        )

    def test_pictures_and_sound_do_not(self) -> None:
        self.assertNotIn("has_location_track", _metadata([_VIDEO, _AUDIO]))


class WhatARewriteKeepsTests(SimpleTestCase):
    """The rewrite names the streams it keeps, rather than taking ffmpeg's default pick, which includes a subtitle."""

    def _args(self, rewrite) -> list[str]:  # noqa: ANN001 - one of the two rewrites
        with patch.object(videos, "_run_ffmpeg", return_value=True) as run:
            rewrite()
        return run.call_args.args[0]

    def test_both_rewrites_keep_only_pictures_and_sound(self) -> None:
        for name, rewrite in (
            ("remux", lambda: videos._remux_without_location("in.mkv", "out.mp4")),  # noqa: SLF001 - the arguments under test
            ("re-encode", lambda: videos._reencode("in.mkv", "out.mp4", 720, strip_location=True)),  # noqa: SLF001
        ):
            with self.subTest(name):
                args = self._args(rewrite)
                maps = [args[i + 1] for i, arg in enumerate(args) if arg == "-map"]
                self.assertEqual(maps, ["0:V:0?", "0:a:0?"])


class AVideoWithOnlyATrackTests(TestCase):
    def test_it_is_rewritten(self) -> None:
        image = baker.make(Image, image=None)
        image.image.save("clip.mp4", ContentFile(b"not really a video, but probed as one"), save=True)

        def remux(src_path: str, out_path: str) -> bool:
            Path(out_path).write_bytes(b"x")
            return True

        with (
            patch.object(videos, "ffmpeg_available", return_value=True),
            patch.object(videos, "extract_video_metadata", return_value={"height": 240, "has_location_track": True}),
            patch.object(videos, "_remux_without_location", side_effect=remux) as rewrite,
        ):
            _metadata_out, replacement = process_uploaded_video(image, 720)

        rewrite.assert_called_once()
        self.assertIsNotNone(replacement)


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, check=True)


def _streams(path: Path) -> list[dict]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)["streams"]


@unittest.skipUnless(ffmpeg_available(), "ffmpeg is not installed")
class GeneratedDroneAndActionCameraFootageTests(TestCase):
    def setUp(self) -> None:
        self._media_root = tempfile.mkdtemp(prefix="ul_vid_tracks_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        (Path(self._media_root) / "pin_images").mkdir(parents=True, exist_ok=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root)
        overrides.enable()
        self.addCleanup(overrides.disable)
        self.work = Path(tempfile.mkdtemp(dir=self._media_root))

    def _stored(self, source: Path, max_height: int | None = None) -> Path:
        image = baker.make(Image, image=None)
        image.image.save(source.name, ContentFile(source.read_bytes()), save=True)
        process_uploaded_video(image, max_height)
        return Path(image.image.path)

    def _drone_footage(self) -> Path:
        captions = self.work / "flight.srt"
        captions.write_text(_DJI_CAPTION)
        source = self.work / "DJI_0001.mp4"
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=10:duration=1",
            "-i",
            str(captions),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:s",
            "mov_text",
            str(source),
        )
        self.assertIn(b"42.6526", source.read_bytes(), "fixture must carry the caption")
        return source

    def test_a_drones_flight_captions_are_dropped(self) -> None:
        stored = self._stored(self._drone_footage())

        self.assertNotIn(b"42.6526", stored.read_bytes())
        self.assertEqual([stream["codec_type"] for stream in _streams(stored)], ["video"])

    def test_a_downscaled_drones_flight_captions_are_dropped(self) -> None:
        """A video over the height cap is re-encoded rather than remuxed; that path keeps the same streams."""
        stored = self._stored(self._drone_footage(), max_height=120)

        self.assertNotIn(b"42.6526", stored.read_bytes())
        streams = _streams(stored)
        self.assertEqual([stream["codec_type"] for stream in streams], ["video"])
        self.assertEqual(streams[0]["height"], 120, "the fixture was not re-encoded")

    def test_an_action_cameras_telemetry_track_is_dropped(self) -> None:
        """ffmpeg cannot write GPMF, so the fixture is a timecode track relabelled ``gpmd``, as GoPro labels its own."""
        timecoded = self.work / "timecoded.mov"
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=10:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-timecode",
            "00:00:00:00",
            str(timecoded),
        )
        data = bytearray(timecoded.read_bytes())
        at = -1
        while (at := data.find(b"stsd", at + 1)) != -1:
            if data[at + 16 : at + 20] == b"tmcd":
                data[at + 16 : at + 20] = b"gpmd"
        source = self.work / "GX010001.mov"
        source.write_bytes(bytes(data))
        self.assertIn(
            "gpmd", [stream.get("codec_tag_string") for stream in _streams(source)], "fixture must carry a gpmd track"
        )

        stored = self._stored(source)

        kinds = [(stream["codec_type"], stream.get("codec_tag_string")) for stream in _streams(stored)]
        self.assertNotIn("gpmd", [tag for _kind, tag in kinds])
        self.assertEqual(sorted(kind for kind, _tag in kinds), ["audio", "video"])
