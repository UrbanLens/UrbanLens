"""An empty HTMX response that shows a toast, for a review queue's card actions.

The client shows a toast's message as text (``shared/site-runtime.ts::showTriggeredToast``), so a link travels beside
it as ``link: {label, href}``; markup in the message would be shown to the user as markup.
"""

from __future__ import annotations

import json
from typing import Any

from django.http import HttpResponse


def queue_toast(message: str, level: str = "success", *, status: int = 200, refresh_queue: bool = False, view_pin_url: str | None = None) -> HttpResponse:
    """An empty response whose ``HX-Trigger`` shows a toast, swapping the acted-on card out.

    Args:
        message: The toast's text.
        level: The toastr level: "success", "info", "warning" or "error".
        status: The response's status code.
        refresh_queue: Also fire ``refreshQueue``, so the page reloads its queue.
        view_pin_url: A pin page to offer as a "View pin" link after the message.

    Returns:
        The response.
    """
    toast: dict[str, Any] = {"message": message, "level": level}
    if view_pin_url:
        toast["link"] = {"label": "View pin", "href": view_pin_url}
    triggers: dict[str, Any] = {"showToast": toast}
    if refresh_queue:
        triggers["refreshQueue"] = True
    response = HttpResponse("", status=status)
    response["HX-Trigger"] = json.dumps(triggers)
    return response
