"""Renderings of the files the in-app REData proxies serve (CRIS attachments, LoopNet photos), made once and kept.

A proxied file a browser cannot show is decoded in the sandbox worker, at a gallery tile's size or the lightbox's, and
the result is stored as a :class:`ProxiedMediaRender`, so a later view neither downloads the file from REData nor
decodes it again (P189). Third-party images are kept the same way (``remote_copies``).
"""

from __future__ import annotations

import hashlib

from django.core.cache import cache
from django.core.files.base import ContentFile

from urbanlens.dashboard.models.proxied_media_render.model import ProxiedMediaRender
from urbanlens.dashboard.services.media.previews import PREVIEW_MAX_DIMENSION, RENDER_QUEUED, RENDER_QUEUED_TTL

#: ``?preview=`` value a gallery tile asks for.
TILE = "thumb"
#: ``?preview=`` value the lightbox asks for.
VIEW = "1"

#: Longest edge of each rendering. A tile's matches the 400 px thumbnail an upload gets for grids.
SIZES = {TILE: 400, VIEW: PREVIEW_MAX_DIMENSION}

#: How long a file that could not be rendered answers 404 before it is tried again.
RENDER_FAILED_TTL = 3600

_FAILED = "failed"


def render_digest(source_key: str, size: str) -> str:
    """The stable identity of one rendering.

    Args:
        source_key: The proxy's cache key for the original file.
        size: A key of :data:`SIZES`.

    Returns:
        A hex SHA-256.
    """
    return hashlib.sha256(f"{source_key}\0{size}".encode()).hexdigest()


def _marker(digest: str) -> str:
    return f"proxied_render:{digest}"


def kept(source_key: str, size: str) -> ProxiedMediaRender | None:
    """The stored rendering, if one was made."""
    return ProxiedMediaRender.objects.filter(render_digest=render_digest(source_key, size)).first()


def is_pending(source_key: str, size: str) -> bool:
    """Whether a render is queued and not yet finished."""
    return cache.get(_marker(render_digest(source_key, size))) == RENDER_QUEUED


def has_failed(source_key: str, size: str) -> bool:
    """Whether the last render produced nothing, recently enough not to try again."""
    return cache.get(_marker(render_digest(source_key, size))) == _FAILED


def request_render(source_key: str, size: str, original: tuple[bytes, str]) -> None:
    """Queue one rendering in the sandbox worker, at most once at a time.

    The original is staged on disk rather than read back from the cache, which keeps only small files.

    Args:
        source_key: The proxy's cache key for the original file.
        size: A key of :data:`SIZES`.
        original: The file's ``(bytes, content_type)``.
    """
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.services.media.previews import discard_preview_source, stage_preview_source
    from urbanlens.dashboard.tasks import render_proxied_media

    digest = render_digest(source_key, size)
    if not cache.add(_marker(digest), RENDER_QUEUED, RENDER_QUEUED_TTL):
        return
    descriptor = stage_preview_source(f"proxied_{digest}", *original)
    if safely_enqueue_task(render_proxied_media, source_key, size, descriptor, durable=False) is None:
        discard_preview_source(descriptor)
        cache.delete(_marker(digest))


def release(source_key: str, size: str) -> None:
    """Drop the queued mark of a render that ended without an answer, so the next view queues it again."""
    cache.delete(_marker(render_digest(source_key, size)))


def finish(source_key: str, size: str, rendered: tuple[bytes, str] | None) -> None:
    """Keep a finished rendering, or remember that there is none.

    Args:
        source_key: The proxy's cache key for the original file.
        size: A key of :data:`SIZES`.
        rendered: The image and its type, or None when the file could not be rendered.
    """
    digest = render_digest(source_key, size)
    if rendered is None:
        cache.set(_marker(digest), _FAILED, RENDER_FAILED_TTL)
        return
    content, content_type = rendered
    extension = "png" if content_type == "image/png" else "jpg"
    render = ProxiedMediaRender.objects.filter(render_digest=digest).first() or ProxiedMediaRender(render_digest=digest, source_key=source_key[:255], size=size)
    render.content_type = content_type
    render.file_size = len(content)
    render.file.save(f"{digest}.{extension}", ContentFile(content), save=False)
    render.save()
    cache.delete(_marker(digest))
