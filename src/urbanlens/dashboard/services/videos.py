"""Video processing utilities - ffmpeg-based downscaling and metadata extraction.

Requires the ``ffmpeg``/``ffprobe`` binaries on PATH (see the Dockerfile).
Every function here degrades gracefully (logs and returns None/empty) when
the binaries are missing or a given file can't be processed, rather than
failing the upload - a video is still usable at its original resolution
even if downscaling isn't available.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
import json
import logging
import posixpath
import re
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any

from django.utils import timezone

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image

logger = logging.getLogger(__name__)

_FFMPEG_TIMEOUT_SECONDS = 600
_FFPROBE_TIMEOUT_SECONDS = 30


def ffmpeg_available() -> bool:
    """Whether the ffmpeg/ffprobe binaries are present on PATH."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe_video(path: str) -> dict[str, Any] | None:
    """Run ffprobe on a local file and return its parsed JSON output.

    Args:
        path: Local filesystem path to the video file.

    Returns:
        The parsed ffprobe JSON (``format``/``streams`` keys), or None if
        ffprobe is unavailable or the file can't be probed.
    """
    if not ffmpeg_available():
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True,
            timeout=_FFPROBE_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ffprobe failed for %s: %s", path, exc, exc_info=True)
        return None
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("ffprobe returned unparseable JSON for %s: %s", path, exc)
        return None


def _parse_iso6709(location: str) -> tuple[float, float] | None:
    """Parse an ISO 6709 location tag (e.g. ``+40.6892-074.0445/``) into (lat, lng)."""
    match = re.match(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", location.strip())
    if not match:
        return None
    try:
        return float(match.group(1)), float(match.group(2))
    except ValueError:
        return None


def extract_video_metadata(path: str) -> dict[str, Any]:
    """Best-effort metadata extraction for an uploaded video.

    Args:
        path: Local filesystem path to the video file.

    Returns:
        Dict with any of ``taken_at`` (datetime), ``latitude``/``longitude``
        (float), ``width``/``height`` (int) that could be determined. Missing
        keys mean that piece of metadata wasn't present or ffprobe/the file
        didn't yield it - never raises.
    """
    metadata: dict[str, Any] = {}
    probed = probe_video(path)
    if not probed:
        return metadata

    fmt_tags = (probed.get("format") or {}).get("tags") or {}
    creation_time = fmt_tags.get("creation_time")
    if creation_time:
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(creation_time)
            metadata["taken_at"] = parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)

    location_tag = fmt_tags.get("location") or fmt_tags.get("com.apple.quicktime.location.ISO6709")
    if location_tag and (coords := _parse_iso6709(location_tag)):
        metadata["latitude"], metadata["longitude"] = coords

    for stream in probed.get("streams") or []:
        if stream.get("codec_type") == "video" and stream.get("width") and stream.get("height"):
            metadata["width"] = int(stream["width"])
            metadata["height"] = int(stream["height"])
            break

    return metadata


def _reencode(src_path: str, out_path: str, max_height: int) -> bool:
    """Run the ffmpeg re-encode; returns True on success."""
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                src_path,
                "-vf",
                f"scale=-2:{max_height}",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                out_path,
            ],
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ffmpeg re-encode failed for %s: %s", src_path, exc, exc_info=True)
        return False
    return True


def process_uploaded_video(image: Image, max_height: int | None) -> tuple[dict[str, Any], int | None]:
    """Extract metadata from an uploaded video and downscale it if oversized.

    Copies the stored file to a local temp path once (ffmpeg/ffprobe need a
    real file, not a stream) and reuses that copy for both metadata probing
    and, if needed, re-encoding - so the file is only fetched from storage a
    single time regardless of storage backend.

    Args:
        image: The Image row whose stored video to process.
        max_height: Vertical resolution cap in pixels, or None to skip
            downscaling (metadata is still extracted).

    Returns:
        (metadata, new_size): metadata is as :func:`extract_video_metadata`;
        new_size is the new stored size in bytes if the file was replaced,
        else None.
    """
    old_name = image.image.name
    if not old_name or not ffmpeg_available():
        return {}, None

    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = posixpath.join(tmpdir, "source" + posixpath.splitext(old_name)[1])
        with image.image.open("rb") as stored_file, open(src_path, "wb") as src_file:
            shutil.copyfileobj(stored_file, src_file)

        metadata = extract_video_metadata(src_path)
        current_height = metadata.get("height")

        if max_height is None or (current_height is not None and current_height <= max_height):
            return metadata, None

        old_size = image.image.size
        out_path = posixpath.join(tmpdir, "output.mp4")
        if not _reencode(src_path, out_path, max_height):
            return metadata, None

        with open(out_path, "rb") as f:
            new_bytes = f.read()

    if not new_bytes or len(new_bytes) >= old_size:
        return metadata, None

    from django.core.files.base import ContentFile

    stem = posixpath.splitext(posixpath.basename(old_name))[0]
    image.image.save(f"{stem}.mp4", ContentFile(new_bytes), save=False)
    if image.image.name != old_name:
        with contextlib.suppress(OSError):
            image.image.storage.delete(old_name)
    logger.info("Downscaled video %s: %s -> %s bytes", image.pk, old_size, len(new_bytes))
    return metadata, len(new_bytes)
