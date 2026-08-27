"""One ``{"error": ...}`` envelope for every error the external API can emit.

The hand-written error returns throughout this package all use
``{"error": "..."}``, but until this module was applied package-wide two paths
bypassed them and emitted DRF's own shapes instead:

- ``serializer.is_valid(raise_exception=True)`` renders a field-keyed dict
  (``{"name": ["This field is required."]}``).
- An uncaught ``Http404`` renders ``{"detail": "Not found."}``, as do 401,
  403, 405 and 429, which DRF builds from ``APIException.detail``.

Three shapes is one more than a generated API client can parse without
special-casing individual endpoints, and the third shape leaks whichever one
the endpoint happened to hit. :class:`ErrorEnvelopeMixin` collapses all of them
onto two forms:

- ``{"error": "<message>"}`` for anything with a single message, and
- ``{"error": "Invalid request.", "fields": {...}}`` when the failure is
  per-field, so a client can still highlight the offending inputs.

This is deliberately *not* done by pointing
``REST_FRAMEWORK["EXCEPTION_HANDLER"]`` at :func:`uniform_exception_handler`.
That setting is shared with the internal session-authenticated API under
``dashboard/rest/``, whose HTMX/JS callers already read ``detail``; rewriting
it globally would silently break error handling across the web UI, far outside
this package's blast radius. The override therefore stays per-view, on the two
base classes every endpoint in the package already inherits.

The anti-enumeration guarantee this module exists to protect:

    **Every** ``Http404`` raised anywhere under these views must render the
    byte-identical body ``{"error": "Not found."}``.

``resolve_visible_wiki`` returns the same bare ``Http404`` for a location that
doesn't exist, one with no wiki, and a real wiki the caller hasn't pinned - but
that only stays indistinguishable if nothing downstream attaches a
distinguishing message to it. A helpful ``"no wiki for this location"`` would
turn the slug into an oracle for which places other users have pinned, so the
detail on a 404 is discarded rather than forwarded. The same reasoning covers
every other resource here: a trip, photo, or label belonging to someone else
must be indistinguishable from one that was never created.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.http import Http404
from rest_framework.exceptions import NotFound, ParseError
from rest_framework.views import exception_handler as drf_exception_handler

if TYPE_CHECKING:
    from collections.abc import Callable

    from rest_framework.response import Response

#: The single body every 404 under these views renders, regardless of cause.
NOT_FOUND_BODY = {"error": "Not found."}

#: The umbrella message accompanying a field-level validation failure.
INVALID_REQUEST_MESSAGE = "Invalid request."

#: The single message every unparseable request body renders, regardless of
#: where DRF discovers it. A malformed body doesn't only surface from a view's
#: own ``request.data``/``request.POST`` access - ``OAuth2Authentication``
#: (django-oauth-toolkit) unconditionally calls ``oauthlib_core.verify_request``
#: during authentication for *every* request reaching a dual-auth view, credential
#: or plain session alike, and that inspects ``request.POST`` looking for a
#: form-encoded access token. On a DRF ``Request`` that access runs the
#: configured body parser, so the same ``ParseError`` a handler's own try/except
#: would normally catch instead escapes from ``perform_authentication`` - before
#: the view method, and any body-parsing guard it wrote, ever runs. Handling it
#: here catches both paths uniformly instead of leaking DRF's raw parser message
#: whenever the earlier one wins the race.
MALFORMED_JSON_BODY_MESSAGE = "Malformed JSON body"


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

    # Same reasoning as the 404 collapse above: discard whatever message DRF's
    # parser attached (its exact wording depends on which parser ran and where
    # in the request cycle it failed - see MALFORMED_JSON_BODY_MESSAGE) so every
    # unparseable body renders identically regardless of cause.
    if isinstance(exc, ParseError):
        response.data = {"error": MALFORMED_JSON_BODY_MESSAGE}
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


class ErrorEnvelopeMixin:
    """Route a view's exceptions through :func:`uniform_exception_handler`.

    Mixed into ``external_api.views.ExternalApiView`` and
    ``external_api.mixins.DualAuthJsonView`` - the two bases every endpoint in
    this package inherits - so the envelope is the package's default rather
    than something each new endpoint has to remember to opt into. An endpoint
    that forgets to opt in is exactly the failure this replaces: it looks
    correct in review, and only a client parsing its 400s discovers it answers
    in a different shape than its neighbours.

    Must be listed *before* the ``APIView`` base in a class statement, or
    ``APIView.get_exception_handler`` (which returns the global
    ``REST_FRAMEWORK["EXCEPTION_HANDLER"]``) wins the MRO and the override is
    silently inert.
    """

    def get_exception_handler(self) -> Callable[[Exception, dict[str, Any]], Response | None]:
        """Return the ``{"error": ...}``-normalizing handler for this view.

        Returns:
            :func:`uniform_exception_handler`, replacing the project-wide
            ``REST_FRAMEWORK["EXCEPTION_HANDLER"]`` for this view only.
        """
        return uniform_exception_handler


class UniformErrorsMixin(ErrorEnvelopeMixin):
    """Deprecated alias for :class:`ErrorEnvelopeMixin`. Nothing references it.

    It existed for one reason: four class statements in ``views_wiki.py`` used
    to list it *before* ``ExternalApiView`` - e.g. ``class
    WikiApiView(UniformErrorsMixin, ExternalApiView)``. Once ``ExternalApiView``
    itself started inheriting the envelope, making this the *same* class would
    have meant C3 could not linearize those bases (a class may never precede
    its own subclass), so merely importing ``views_wiki`` would have raised
    ``TypeError: Cannot create a consistent method resolution order`` at
    class-creation time - the whole API failing to load. Keeping it as a
    distinct subclass kept both spellings legal during that transition.

    Those four declarations have since dropped the redundant mixin, so this
    class is now dead: as of this writing nothing in ``src/urbanlens``
    imports it. It is retained only so that any endpoint written against the
    older guidance during the in-flight mobile-API build still imports
    cleanly; it should be deleted outright once that build settles. Do not
    list it (or :class:`ErrorEnvelopeMixin`) on a new view - both package view
    bases already provide the envelope, and naming one redundantly is how the
    MRO hazard above gets reintroduced.
    """
