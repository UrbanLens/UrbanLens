"""Responses for third-party bytes served from the app's own origin."""

from __future__ import annotations

from django.http import HttpResponse

#: Types a proxied file may be served inline as: raster images, video and PDF, none of which runs script in a
#: browser. An allow-list, because ``image/svg+xml``, ``text/html``, XML and JS all do, and a deny-list forgets one.
INLINE_MEDIA_TYPES = frozenset(
    {
        "image/avif",
        "image/bmp",
        "image/gif",
        "image/heic",
        "image/heif",
        "image/jp2",
        "image/jpeg",
        "image/jpg",
        "image/jpx",
        "image/pjpeg",
        "image/png",
        "image/tiff",
        "image/vnd.microsoft.icon",
        "image/webp",
        "image/x-icon",
        "image/x-ms-bmp",
        "image/x-tiff",
        "video/mp4",
        "video/ogg",
        "video/quicktime",
        "video/webm",
        "application/pdf",
    },
)

PDF_MAGIC = b"%PDF-"
#: Browsers accept a PDF header anywhere in the first kilobyte.
PDF_HEADER_WINDOW = 1024

#: ``sandbox`` is left off the PDF policy because Chrome will not run its PDF viewer in a sandboxed document.
_PDF_CSP = "default-src 'none'; frame-ancestors 'self'"
_MEDIA_CSP = f"{_PDF_CSP}; sandbox"


def looks_like_pdf(content: bytes) -> bool:
    """Whether bytes carry a PDF header where a browser would look for one.

    Args:
        content: The file's bytes.

    Returns:
        True when the PDF magic appears in the header window.
    """
    return PDF_MAGIC in content[:PDF_HEADER_WINDOW]


def inline_media_type(content: bytes, content_type: str | None) -> str | None:
    """The type a proxied file may be displayed inline as, or None when it must be a download.

    Args:
        content: The file's bytes.
        content_type: What the upstream declared, parameters and all.

    Returns:
        The normalised declared type when it is allow-listed (and, for a PDF, the bytes are one), else None.
    """
    declared = (content_type or "").partition(";")[0].strip().lower()
    if declared not in INLINE_MEDIA_TYPES:
        return None
    if declared == "application/pdf" and not looks_like_pdf(content):
        return None
    return declared


def proxied_media_response(content: bytes, content_type: str | None) -> HttpResponse:
    """Serve bytes a third party supplied without letting them act as a page on this origin.

    An allow-listed type is served inline under a policy that loads nothing; anything else becomes an
    ``application/octet-stream`` attachment. ``nosniff`` keeps a browser from second-guessing either.

    Args:
        content: The file's bytes.
        content_type: What the upstream declared.

    Returns:
        The response, with its own ``Content-Security-Policy`` so the site policy (report-only by default, and
        allowing inline script when enforced) never governs it.
    """
    served_type = inline_media_type(content, content_type)
    response = HttpResponse(content, content_type=served_type or "application/octet-stream")
    if served_type is None:
        response["Content-Disposition"] = "attachment"
    response["Content-Security-Policy"] = _PDF_CSP if served_type == "application/pdf" else _MEDIA_CSP
    response["X-Content-Type-Options"] = "nosniff"
    # The lightbox frames documents from this origin; Django's default is DENY.
    response["X-Frame-Options"] = "SAMEORIGIN"
    return response
