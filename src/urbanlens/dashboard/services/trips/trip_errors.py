"""Transport-neutral error vocabulary for the shared trip services.

The trip services (``trip_access``, ``trip_membership``, ``trip_crud``,
``trip_activities``, ``trip_comments``) are consumed by two very different
callers: the internal HTMX controllers, which answer with plain-text bodies
that surface as toasts, and the external REST API, which answers with the
``{"error": ...}`` JSON envelope. Neither can be the service layer's concern,
so services raise these instead of building responses.

Each class maps to exactly one HTTP status, and callers translate once (see
``external_api.views.TripErrorResponseMixin``) rather than at every call site.

Messages are stored as plain, *unescaped* text. A caller rendering into HTML
must escape at render time - baking ``django.utils.html.escape`` into the
message would leak HTML entities into the JSON API, which needs the raw
characters.
"""

from __future__ import annotations


class TripError(ValueError):
    """Base class for every expected (i.e. non-bug) trip service failure.

    Subclasses ``ValueError`` so a caller that has not been taught about the
    trip vocabulary still treats it as bad input rather than letting it
    escape as a 500.
    """

    def __init__(self, message: str) -> None:
        """Store the human-readable, unescaped failure message.

        Args:
            message: What went wrong, phrased for the end user.
        """
        super().__init__(message)
        self.message = message


class TripNotFoundError(TripError):
    """The trip, activity, comment, or member does not exist *for this viewer* - 404.

    Deliberately also raised when the row exists but the viewer has no
    standing access to know that. Answering "forbidden" there would confirm
    the row's existence, which is exactly the enumeration leak
    :func:`~urbanlens.dashboard.services.trips.trip_access.get_trip_for_viewer`
    closes.
    """


class TripPermissionError(TripError):
    """The viewer may see the trip but not perform this particular action - 403."""


class TripValidationError(TripError):
    """The request was well-formed but its content is unacceptable - 400."""


class TripQuotaError(TripValidationError):
    """A ``SiteSettings`` cap (members, activities, upcoming trips) is already reached - 400.

    A subclass of :class:`TripValidationError` so existing handlers keep
    answering 400, while a caller that wants to say something specific about
    quotas (e.g. offer an upgrade path) can catch it on its own.
    """


class TripMemberNotFoundError(TripNotFoundError):
    """No user matches a submitted username - 404.

    Carries the submitted ``username`` separately from the formatted message
    so each caller can present it safely in its own medium: the internal HTMX
    view HTML-escapes it, while the external API puts it into a JSON string
    unescaped. Escaping inside the message itself would corrupt the JSON
    response.
    """

    def __init__(self, message: str, username: str) -> None:
        """Record the message plus the raw username that could not be resolved.

        Args:
            message: Human-readable failure message, unescaped.
            username: The username exactly as the caller submitted it.
        """
        super().__init__(message)
        self.username = username
