"""Absolute links for contexts with no request to take a host from - mail, push, SMS, Celery."""

from __future__ import annotations

from django.conf import settings


def absolute_url(path: str = "") -> str:
    """Join a site-relative path onto ``settings.SITE_URL``.

    Args:
        path: A path starting with ``/``, e.g. from ``reverse()``; empty for the site root.

    Returns:
        The absolute URL.

    Raises:
        ValueError: When ``path`` is not site-relative, since joining an absolute URL on would make a link that
            looks like this site's and is not.
    """
    if path and not path.startswith("/"):
        raise ValueError(f"absolute_url expects a site-relative path, got {path!r}")
    return f"{settings.SITE_URL.rstrip('/')}{path}"
