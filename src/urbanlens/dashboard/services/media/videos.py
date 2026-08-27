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


#: Container tags a phone writes the capture coordinates into. ffmpeg copies
#: global metadata across both a re-encode and a stream copy, so these have to
#: be cleared explicitly; assigning an empty value is how ffmpeg deletes a tag.
#: Only the location tags are cleared, never the whole metadata block - this
#: mirrors the photo path, which drops the GPS IFD and leaves the rest of the
#: EXIF alone.
_LOCATION_TAGS = ("location", "location-eng", "com.apple.quicktime.location.ISO6709")


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

    # Tag names are case-folded: MP4/MOV report them lowercase, but Matroska
    # (an accepted upload container) reports them uppercase, so a lowercase-only
    # lookup silently found no location at all in a .mkv/.webm.
    fmt_tags = {str(k).lower(): v for k, v in ((probed.get("format") or {}).get("tags") or {}).items()}
    creation_time = fmt_tags.get("creation_time")
    if creation_time:
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(creation_time)
            metadata["taken_at"] = parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)

    # `has_location_tag` is reported separately from the parsed coordinates: a
    # scrub has to key off the tag being *present*, not off it being readable.
    # A tag in a notation _parse_iso6709 doesn't handle still discloses where
    # the video was taken, and gating the strip on successful parsing would
    # leave exactly those behind.
    location_tag = next((fmt_tags.get(tag) for tag in _LOCATION_TAGS if fmt_tags.get(tag)), None)
    if location_tag:
        metadata["has_location_tag"] = True
        if coords := _parse_iso6709(location_tag):
            metadata["latitude"], metadata["longitude"] = coords

    for stream in probed.get("streams") or []:
        if stream.get("codec_type") == "video" and stream.get("width") and stream.get("height"):
            metadata["width"] = int(stream["width"])
            metadata["height"] = int(stream["height"])
            break

    return metadata


def _clear_location_args() -> list[str]:
    """Build the ffmpeg args that delete every container-level location tag."""
    args: list[str] = []
    for tag in _LOCATION_TAGS:
        args += ["-metadata", f"{tag}="]
    return args


def _run_ffmpeg(args: list[str], src_path: str, what: str) -> bool:
    """Run one ffmpeg invocation; returns True on success."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src_path, *args],
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ffmpeg %s failed for %s: %s", what, src_path, exc, exc_info=True)
        return False
    return True


def _reencode(src_path: str, out_path: str, max_height: int, *, strip_location: bool = False) -> bool:
    """Downscale to ``max_height``, optionally dropping the location tags; True on success."""
    args = [
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
    ]
    if strip_location:
        args += _clear_location_args()
    return _run_ffmpeg([*args, out_path], src_path, "re-encode")


def _remux_without_location(src_path: str, out_path: str) -> bool:
    """Drop the location tags without touching the streams; True on success.

    A stream copy, so it costs a file rewrite rather than a transcode and loses
    no quality. This is what lets a video be scrubbed even when it is already
    small enough that no downscale was warranted.
    """
    return _run_ffmpeg(["-c", "copy", *_clear_location_args(), "-movflags", "+faststart", out_path], src_path, "location strip")


def process_uploaded_video(image: Image, max_height: int | None, *, strip_location: bool = False) -> tuple[dict[str, Any], int | None]:
    """Extract metadata from an uploaded video, downscale it if oversized, and optionally scrub its location.

    Copies the stored file to a local temp path once (ffmpeg/ffprobe need a
    real file, not a stream) and reuses that copy for both metadata probing
    and, if needed, re-encoding - so the file is only fetched from storage a
    single time regardless of storage backend.

    ``strip_location`` is independent of ``max_height``: a video small enough to
    need no downscale must still be scrubbed, via a lossless stream copy. The
    caller controlling this previously expressed it by passing ``max_height=None``,
    which meant "skip processing entirely" and so guaranteed the opposite - the
    original file, location tag and all, was what got stored and served.

    Args:
        image: The Image row whose stored video to process.
        max_height: Vertical resolution cap in pixels, or None to skip
            downscaling (metadata is still extracted, and a location strip
            still happens if asked for).
        strip_location: Remove the container's location tags from the stored
            file. Independent of the derived ``Image.latitude``/``longitude``
            fields, which the caller controls separately.

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
        needs_downscale = max_height is not None and not (current_height is not None and current_height <= max_height)
        # Keyed off the tag's presence, not off parsed coordinates - see
        # extract_video_metadata. Only worth rewriting a file that carries one.
        needs_strip = strip_location and bool(metadata.get("has_location_tag"))

        if not needs_downscale and not needs_strip:
            return metadata, None

        old_size = image.image.size
        out_path = posixpath.join(tmpdir, "output.mp4")
        # `needs_downscale` already implies a non-None max_height; spelling it out
        # here is what narrows the type without an assert.
        if needs_downscale and max_height is not None:
            succeeded = _reencode(src_path, out_path, max_height, strip_location=needs_strip)
        else:
            succeeded = _remux_without_location(src_path, out_path)
        if not succeeded:
            return metadata, None

        with open(out_path, "rb") as f:
            new_bytes = f.read()

    # A rewrite that grew the file is not worth keeping - unless scrubbing the
    # location was the point, in which case keeping the smaller-but-still-tagged
    # original would defeat it. Mirrors the photo path's has_gps exemption.
    if not new_bytes or (len(new_bytes) >= old_size and not needs_strip):
        return metadata, None

    from django.core.files.base import ContentFile

    stem = posixpath.splitext(posixpath.basename(old_name))[0]
    image.image.save(f"{stem}.mp4", ContentFile(new_bytes), save=False)
    if image.image.name != old_name:
        with contextlib.suppress(OSError):
            image.image.storage.delete(old_name)
    logger.info("Rewrote video %s: %s -> %s bytes (downscale=%s, location strip=%s)", image.pk, old_size, len(new_bytes), needs_downscale, needs_strip)
    return metadata, len(new_bytes)
