"""Uniform ``{"error": ...}`` error rendering, scoped to the wiki endpoints.

The external API's hand-written error returns all use ``{"error": "..."}``, but
two paths bypass them and emit DRF's own shapes instead:

- ``serializer.is_valid(raise_exception=True)`` renders a field-keyed dict
  (``{"name": ["This field is required."]}``).
- An uncaught ``Http404`` renders ``{"detail": "Not found."}``.

That inconsistency is pre-existing and is deliberately *not* fixed globally
here: ``REST_FRAMEWORK["EXCEPTION_HANDLER"]`` is shared with the internal
session-authenticated API, whose HTMX/JS callers already parse ``detail``, so
changing it there would be a silent breaking change well outside this task's
blast radius. Instead :class:`UniformErrorsMixin` overrides
``get_exception_handler`` on just the views that opt in.

The anti-enumeration guarantee this module exists to protect:

    **Every** ``Http404`` raised anywhere under a wiki endpoint must render the
    byte-identical body ``{"error": "Not found."}``.

``resolve_visible_wiki`` returns the same bare ``Http404`` for a location that
doesn't exist, one with no wiki, and a real wiki the caller hasn't pinned - but
that only stays indistinguishable if nothing downstream attaches a
distinguishing message to it. A helpful ``"no wiki for this location"`` would
turn the slug into an oracle for which places other users have pinned, so the
detail on a 404 is discarded rather than forwarded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.http import Http404
from rest_framework.exceptions import NotFound
from rest_framework.views import exception_handler as drf_exception_handler

if TYPE_CHECKING:
    from collections.abc import Callable

    from rest_framework.response import Response

#: The single body every 404 under these views renders, regardless of cause.
NOT_FOUND_BODY = {"error": "Not found."}

#: The umbrella message accompanying a field-level validation failure.
INVALID_REQUEST_MESSAGE = "Invalid request."


def uniform_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """Render DRF exceptions in the external API's ``{"error": ...}`` shape.

    Args:
        exc: The exception raised by the view.
        context: DRF's handler context (``view``, ``request``, ``args``,
            ``kwargs``).

    Returns:
        The normalized response, or ``None`` for an exception DRF itself
        declines to handle (which Django then turns into a 500).
    """
    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    # Collapse every not-found - Http404, NotFound, and any subclass - onto one
    # constant body. Done before inspecting response.data so a detail message
    # attached upstream can never survive into the response.
    if isinstance(exc, (Http404, NotFound)):
        response.data = dict(NOT_FOUND_BODY)
        return response

    data = response.data
    if isinstance(data, dict):
        detail = data.get("detail")
        if detail is not None and len(data) == 1:
            # DRF's single-message shape ({"detail": "..."}) - e.g. throttling,
            # permission denied, method not allowed.
            response.data = {"error": str(detail)}
        else:
            # Field-keyed validation errors. Stringify the leaf messages so the
            # payload is plain JSON rather than DRF's ErrorDetail instances.
            response.data = {"error": INVALID_REQUEST_MESSAGE, "fields": _stringify(data)}
    elif isinstance(data, list):
        # A serializer raising a bare non-field error yields a list.
        response.data = {"error": INVALID_REQUEST_MESSAGE, "fields": {"non_field_errors": _stringify(data)}}

    return response


def _stringify(value: Any) -> Any:
    """Recursively convert DRF ``ErrorDetail`` leaves to plain strings.

    Args:
        value: A validation-error structure (dict, list, or leaf).

    Returns:
        The same structure with every leaf coerced to ``str``.
    """
    if isinstance(value, dict):
        return {key: _stringify(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stringify(item) for item in value]
    return str(value)


class UniformErrorsMixin:
    """Opt a view into :func:`uniform_exception_handler`.

    Mix in *before* the view base class so the override takes effect. Applies
    only to the views that inherit it - the internal API and the pre-existing
    external endpoints keep DRF's default handler.
    """

    def get_exception_handler(self) -> Callable[[Exception, dict[str, Any]], Response | None]:
        """Return the ``{"error": ...}``-normalizing handler for this view."""
        return uniform_exception_handler
