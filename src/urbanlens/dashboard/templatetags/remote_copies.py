"""Template access to ``services.media.remote_copies``, for pages that show a provider's image."""

from __future__ import annotations

from django import template

register = template.Library()


@register.filter
def remote_copy(url: str | None, provider: str) -> str:
    """This site's address for a remote image; an in-app or empty URL is returned as it is.

    Args:
        url: The image's address.
        provider: Which feature or provider it came from, kept as the copy's provenance.

    Returns:
        The address to put in the page.
    """
    from urbanlens.dashboard.services.media.remote_copies import copy_url

    return copy_url(url, provider=provider) if url else ""
