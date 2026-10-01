"""A pin payload's link to its own page.
Lives beside the payload rather than in the controller because the map document is also built by a Celery task, and a task importing a controller to serialize a pin is the wrong way round."""

from __future__ import annotations

from typing import Any
import urllib.parse

from django.urls import reverse

#: Reversed once against this, then substituted per pin.
_URL_PLACEHOLDER = "pin-slug-placeholder"


def with_view_urls(pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach each pin's detail-page URL to its payload.
    Reversed once against a placeholder rather than per pin: `reverse` is not free, and the map serializes whole accounts at a time.

    Args:
        pins: Map payloads, each carrying a ``slug``.

    Returns:
        The same list, each payload given a ``viewLocationUrl``."""
    prefix, _, suffix = reverse("pin.details", kwargs={"pin_slug": _URL_PLACEHOLDER}).partition(_URL_PLACEHOLDER)
    for pin in pins:
        pin["viewLocationUrl"] = f"{prefix}{urllib.parse.quote(str(pin['slug']))}{suffix}"
    return pins
