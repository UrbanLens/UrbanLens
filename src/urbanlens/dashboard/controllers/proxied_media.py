"""
Views that fetch one file from an upstream whose credentials must stay server-side, and serve it under this site's own origin.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from django.http import HttpRequest, HttpResponse

from urbanlens.dashboard.services.core.bounded_cache import get_or_none, set_if_small
from urbanlens.dashboard.services.security.throttle import Rate

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

PROXIED_MEDIA_CACHE_TTL = 3600

#: Largest proxied body worth putting in the shared Dragonfly, larger than
#: ``bounded_cache.MAX_CACHED_BODY_BYTES``: that ceiling is sized for thumbnails
#: while these are scanned PDFs and TIFFs, so inheriting it would turn every view
#: into a fresh download.
PROXIED_MEDIA_MAX_CACHED_BYTES = 4 * 1024 * 1024

#: How often one caller may pull these proxies. Charged to the account when there
#: is one and to the address otherwise (a poor identity behind NAT and a poor isolation boundary).
#: Generous either way because a panel is many requests.
PROXIED_MEDIA_RATE = Rate(limit=600, window_seconds=300)

#: GET is the expensive method here, which the throttle's default set excludes.
PROXIED_MEDIA_METHODS = frozenset({"GET"})

#: The throttle scope the proxies' routes share.
PROXIED_MEDIA_THROTTLE_SCOPE = "proxied.media"


class ProxiedMediaMixin:
    """
    Shared caching + preview handling for views that proxy one upstream file.

    Each view fetches one file's bytes from an upstream whose API key must never reach the browser and serves them
    through ``proxied_media_response``: the routes are unauthenticated and the bytes are a third party's, so only an
    allow-listed type is ever displayed inline.
    The conversion also belongs here rather than behind the generic ``media_preview`` endpoint, which
    would only re-download what this view already has.
    """

    #: What ``download`` raises to mean "not available" (a 404, an unconfigured gateway, ...), unless the call names its own.
    unavailable_errors: ClassVar[tuple[type[Exception], ...]] = (ValueError,)

    def serve_media(self, request: HttpRequest, cache_key: str, download: Callable[[], tuple[bytes, str]], *, unavailable_errors: tuple[type[Exception], ...] | None = None) -> HttpResponse:
        """
        Serve one upstream file, or a rendering of it a browser can show.

        Args:
            request: The current request. ``?preview=thumb`` asks for a gallery tile's rendering and ``?preview=1`` for
            the lightbox's (an original a browser can already show is served as it is for the lightbox).
            cache_key: Django cache key for the *original* bytes.
            download: Zero-argument callable returning ``(content, content_type)``.
            unavailable_errors: Exception types ``download`` raises to mean "not available" (a 404, an
            unconfigured gateway, ...), each turned into a 404 response; the view's own when None.

        Returns:
            The file or its kept rendering; a 503 with ``Retry-After`` while the rendering is being made, while the
            upstream is throttled, its source is down or this process already has as many downloads in flight as it may;
            a 502 when the download failed; or a 404 when the upstream couldn't supply it or it could not be rendered.
        """
        from urbanlens.dashboard.services.apis.request_upstreams import ProxiedMediaUpstream
        from urbanlens.dashboard.services.core.gateway import UpstreamBusyError
        from urbanlens.dashboard.services.media import proxied_renders
        from urbanlens.dashboard.services.media.previews import PREVIEW_RETRY_AFTER_SECONDS, is_web_safe, needs_server_side_preview
        from urbanlens.dashboard.services.media.proxied_media import inline_media_type, proxied_media_response, retry_later_response

        if unavailable_errors is None:
            unavailable_errors = self.unavailable_errors

        size = request.GET.get("preview", "")
        if size not in proxied_renders.SIZES:
            size = ""
        label = f"Proxied media {cache_key}"
        if size:
            if (kept := self._serve_kept_render(cache_key, size)) is not None:
                return kept
            if proxied_renders.has_failed(cache_key, size):
                return HttpResponse(status=404)
            if proxied_renders.is_pending(cache_key, size):
                return retry_later_response(PREVIEW_RETRY_AFTER_SECONDS)

        original = get_or_none(cache_key, label=label)
        if original is None:

            def download_and_keep() -> tuple[bytes, str]:
                body = download()
                # In the fetching thread, so a download that outlives the request is still kept for the next one.
                # Refusing to cache never means refusing to answer: what it stops is one oversized document
                # evicting other people's entries from the shared instance.
                set_if_small(cache_key, body[0], body[1], PROXIED_MEDIA_CACHE_TTL, label=label, max_bytes=PROXIED_MEDIA_MAX_CACHED_BYTES)
                return body

            fetched = ProxiedMediaUpstream.call(download_and_keep, errors=unavailable_errors)
            if fetched.value is None:
                if isinstance(fetched.error, UpstreamBusyError):
                    return retry_later_response(fetched.error.retry_after)
                if isinstance(fetched.error, unavailable_errors):
                    return HttpResponse(status=404)
                if fetched.error is not None:
                    return HttpResponse(status=502)
                return retry_later_response(fetched.retry_after or ProxiedMediaUpstream.busy_retry_seconds)
            original = fetched.value

        content, content_type = original
        # The lightbox shows a displayable original as it is; re-encoding it would only cost quality. A tile still gets
        # a small rendering, since the original may be any size.
        if not size or (size == proxied_renders.VIEW and is_web_safe(request.path, content_type) and inline_media_type(content, content_type) is not None):
            return proxied_media_response(content, content_type)

        declared = content_type.split(";")[0].strip().lower()
        # Pillow tries any image type, so only a known non-image no renderer handles is refused outright.
        if declared not in ("", "application/octet-stream") and not declared.startswith("image/") and not needs_server_side_preview(request.path, declared):
            return HttpResponse(status=404)
        # The decode runs in the sandbox worker, not here - these are a third party's document bytes and
        # render_preview reaches Pillow and poppler.
        proxied_renders.request_render(cache_key, size, original)
        return retry_later_response(PREVIEW_RETRY_AFTER_SECONDS)

    @staticmethod
    def _serve_kept_render(cache_key: str, size: str) -> HttpResponse | None:
        # Through proxied_media_response, not an X-Accel hand-off: nginx would drop the headers it sets.
        from urbanlens.dashboard.services.media import proxied_renders
        from urbanlens.dashboard.services.media.proxied_media import proxied_media_response

        render = proxied_renders.kept(cache_key, size)
        if render is None:
            return None
        try:
            with render.file.open("rb") as stored:
                content = stored.read()
        except OSError:
            logger.warning("Kept rendering %s is missing its file", render.pk)
            render.delete()
            return None
        response = proxied_media_response(content, render.content_type)
        response["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
