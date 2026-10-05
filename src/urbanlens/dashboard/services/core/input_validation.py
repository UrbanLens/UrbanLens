"""Refusing a request input that cannot return data, before it costs anything.

A call made with NaN coordinates, an empty query or a malformed id spends the same budget as a
real one - and for a billed source the same money - and can only come back empty or as a 400.
These checks run before rate-limiter accounting, budget spend and network I/O, and refuse such an
input by raising :class:`ImpossibleInputError`.

That error is a :class:`~urbanlens.dashboard.services.core.gateway.GatewayRequestError`, so every
caller that degrades on a gateway failure keeps working, but its ``is_outage`` is False: the source
was never asked, nothing is wrong with it, and asking again with the same input cannot succeed. A
caller may keep the empty answer.

- :func:`check_request_parameters` runs inside every ``_RateLimitedSession`` request, for what no
  API can answer: a non-finite number, or a named latitude/longitude off the globe.
- The ``require_*`` functions are for what a gateway knows and the session cannot: an id's format,
  a radius or page size the provider caps, a date before its archive starts, and whether ``(0, 0)``
  - the placeholder a missing coordinate becomes - can mean anything to that source.

Only what is provably unanswerable is refused; a value these checks cannot parse is left for the
provider to judge. Every refusal writes one ``ApiCallLog`` row flagged ``was_rejected_input``,
which no budget counts, so a caller that keeps sending garbage is visible per service.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from enum import StrEnum
import logging
import math
import re
import threading
import time
from typing import Any, NoReturn

from urbanlens.dashboard.services.core.gateway import GatewayRequestError

logger = logging.getLogger(__name__)


class InputRejection(StrEnum):
    """Why an input was refused; recorded on the call log."""

    #: Missing, not a number, not finite, or off the globe.
    INVALID_COORDINATES = "invalid_coordinates"
    #: Exactly ``(0, 0)``, for a source with nothing at sea.
    NULL_ISLAND = "null_island"
    #: Empty or whitespace only.
    EMPTY_QUERY = "empty_query"
    #: A radius, limit, page, size or length the provider does not accept.
    OUT_OF_RANGE = "out_of_range"
    #: An identifier that cannot be one of the provider's.
    MALFORMED_ID = "malformed_id"
    #: A country or region the provider never covers.
    OUTSIDE_COVERAGE = "outside_coverage"
    #: A date before or after everything the provider holds.
    OUTSIDE_DATE_COVERAGE = "outside_date_coverage"


class ImpossibleInputError(GatewayRequestError):
    """A request input that cannot return data from ``service``, refused before any I/O.

    Attributes:
        service: The rate-limiter service key the call was for.
        reason: Which kind of impossible input it was.
        detail: What was wrong. Never carries a coordinate or a query verbatim.
    """

    def __init__(self, service: str, reason: InputRejection, detail: str) -> None:
        # Not super(): a subclass may also derive from a gateway error with an __init__ of its own.
        GatewayRequestError.__init__(self, f"{service}: {detail}")
        self.service = service
        self.reason = reason
        self.detail = detail

    @property
    def is_outage(self) -> bool:
        """Never: the source was not asked, and nothing about it is unknown."""
        return False


#: How long a (service, reason) pair logs at DEBUG after logging once at INFO.
LOG_INTERVAL_SECONDS = 600.0

_last_logged: dict[tuple[str, InputRejection], float] = {}
_last_logged_lock = threading.Lock()


def _should_log_loudly(service: str, reason: InputRejection) -> bool:
    now = time.monotonic()
    with _last_logged_lock:
        last = _last_logged.get((service, reason))
        if last is not None and now - last < LOG_INTERVAL_SECONDS:
            return False
        _last_logged[(service, reason)] = now
        return True


def reject(service: str, reason: InputRejection, detail: str) -> NoReturn:
    """Count, log and raise one refusal.

    Args:
        service: The rate-limiter service key the call was for.
        reason: Which kind of impossible input it was.
        detail: What was wrong, without the input itself.

    Raises:
        ImpossibleInputError: Always.
    """
    from urbanlens.dashboard.services.core.rate_limiter import log_api_call

    log_api_call(service, success=True, endpoint=f"rejected:{reason}", was_rejected_input=True)
    level = logging.INFO if _should_log_loudly(service, reason) else logging.DEBUG
    logger.log(level, "Refused a %s call before sending it (%s): %s", service, reason, detail)
    raise ImpossibleInputError(service, reason, detail)


def _as_finite_float(value: object) -> float | None:
    """``value`` as a finite float, or None. A bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


_NOT_FINITE = (InputRejection.INVALID_COORDINATES, "latitude and longitude must both be finite numbers")


def _finite_point(latitude: object, longitude: object) -> tuple[float, float] | None:
    lat = _as_finite_float(latitude)
    lng = _as_finite_float(longitude)
    return None if lat is None or lng is None else (lat, lng)


def _point_problem(point: tuple[float, float] | None, *, allow_null_island: bool) -> tuple[InputRejection, str] | None:
    if point is None:
        return _NOT_FINITE
    lat, lng = point
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return InputRejection.INVALID_COORDINATES, "latitude must be within [-90, 90] and longitude within [-180, 180]"
    if lat == 0.0 and lng == 0.0 and not allow_null_island:
        return InputRejection.NULL_ISLAND, "(0, 0) is a missing coordinate, and this source has nothing there"
    return None


def coordinate_problem(latitude: object, longitude: object, *, allow_null_island: bool = False) -> tuple[InputRejection, str] | None:
    """Why a point cannot be somewhere a source has data, or None. Counts and logs nothing.

    Args:
        latitude: The latitude as given.
        longitude: The longitude as given.
        allow_null_island: Accept exactly ``(0, 0)``; see :func:`require_coordinates`.

    Returns:
        ``(reason, detail)``, or None for a usable point.
    """
    return _point_problem(_finite_point(latitude, longitude), allow_null_island=allow_null_island)


def require_coordinates(service: str, latitude: object, longitude: object, *, allow_null_island: bool = False) -> tuple[float, float]:
    """A WGS-84 point that can be somewhere, as floats.

    Args:
        service: The service key the call is for.
        latitude: The latitude as given.
        longitude: The longitude as given.
        allow_null_island: Accept exactly ``(0, 0)``. For a source that answers at sea - a
            weather grid, an encyclopedia with an article about the place - since for anything
            else ``(0, 0)`` is a missing coordinate that became zeros, 570 km from land.

    Returns:
        ``(latitude, longitude)``.

    Raises:
        ImpossibleInputError: The point is missing, not finite, off the globe, or ``(0, 0)``.
    """
    point = _finite_point(latitude, longitude)
    problem = _point_problem(point, allow_null_island=allow_null_island)
    if point is None or problem is not None:
        reject(service, *(problem or _NOT_FINITE))
    return point


def require_query(service: str, query: object, *, name: str = "query", max_length: int | None = None) -> str:
    """A free-text query with something in it, returned unchanged.

    Args:
        service: The service key the call is for.
        query: The query as given.
        name: What the provider calls the parameter, for the message.
        max_length: The longest query the provider accepts, when it documents one.

    Returns:
        ``query``.

    Raises:
        ImpossibleInputError: The query is missing, blank, or longer than ``max_length``.
    """
    if not isinstance(query, str) or not query.strip():
        reject(service, InputRejection.EMPTY_QUERY, f"{name} is empty")
    if max_length is not None and len(query) > max_length:
        reject(service, InputRejection.OUT_OF_RANGE, f"{name} is {len(query)} characters; the provider takes at most {max_length}")
    return query


def require_content(service: str, name: str, content: bytes) -> bytes:
    """Bytes with something in them, such as an image to describe, returned unchanged.

    Args:
        service: The service key the call is for.
        name: What the content is, for the message.
        content: The bytes as given.

    Returns:
        ``content``.

    Raises:
        ImpossibleInputError: ``content`` is empty.
    """
    if not content:
        reject(service, InputRejection.EMPTY_QUERY, f"{name} is empty")
    return content


def require_in_range[N: (int, float)](service: str, name: str, value: N, *, minimum: float | None = None, maximum: float | None = None, exclusive_minimum: bool = False) -> N:
    """A number the provider accepts, returned unchanged.

    Args:
        service: The service key the call is for.
        name: What the provider calls the parameter.
        value: The number as given.
        minimum: The smallest accepted value, if any.
        maximum: The largest accepted value, if any.
        exclusive_minimum: Refuse ``minimum`` itself.

    Returns:
        ``value``.

    Raises:
        ImpossibleInputError: ``value`` is not finite, or falls outside the bounds.
    """
    if _as_finite_float(value) is None:
        reject(service, InputRejection.OUT_OF_RANGE, f"{name} must be a finite number")
    below = minimum is not None and (value <= minimum if exclusive_minimum else value < minimum)
    if below or (maximum is not None and value > maximum):
        low = "(" if exclusive_minimum else "["
        reject(service, InputRejection.OUT_OF_RANGE, f"{name} must be within {low}{minimum}, {maximum}]")
    return value


def require_format(service: str, name: str, value: object, pattern: re.Pattern[str]) -> str:
    """An identifier that matches the provider's format in full, returned unchanged.

    Args:
        service: The service key the call is for.
        name: What the provider calls the identifier.
        value: The identifier as given.
        pattern: The provider's format.

    Returns:
        ``value``.

    Raises:
        ImpossibleInputError: ``value`` is not a string, or ``pattern`` does not match all of it.
    """
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        reject(service, InputRejection.MALFORMED_ID, f"{name} is not in the provider's format")
    return value


#: A Google place id: URL-safe base64 text of no documented length. Only the alphabet is checked, which still catches
#: a resource name (``places/...``), a ``cid:`` reference, a feature id (``0x..:0x..``), a URL or stray whitespace.
GOOGLE_PLACE_ID = re.compile(r"[A-Za-z0-9_-]+")


#: The widest circle Google Places (New) searches or biases within; a wider one is a 400.
MAX_PLACES_RADIUS_METERS = 50_000.0


def require_google_place_id(service: str, place_id: object) -> str:
    """A value that can be a Google place id, returned unchanged.

    Args:
        service: The service key the call is for.
        place_id: The id as given.

    Returns:
        ``place_id``.

    Raises:
        ImpossibleInputError: It is not a string of the place-id alphabet.
    """
    return require_format(service, "place_id", place_id, GOOGLE_PLACE_ID)


def require_date_within[D: date](service: str, name: str, value: D, *, earliest: date | None = None, latest: date | None = None) -> D:
    """A date the provider has data for, returned unchanged.

    Args:
        service: The service key the call is for.
        name: What the provider calls the parameter.
        value: The date as given.
        earliest: The first date the provider holds, if known.
        latest: The last date the provider holds, if known.

    Returns:
        ``value``.

    Raises:
        ImpossibleInputError: ``value`` is before ``earliest`` or after ``latest``.
    """
    if (earliest is not None and value < earliest) or (latest is not None and value > latest):
        reject(service, InputRejection.OUTSIDE_DATE_COVERAGE, f"{name} {value.isoformat()} is outside the provider's coverage ({earliest} to {latest})")
    return value


_LATITUDE_KEYS = frozenset({"lat", "latitude"})
_LONGITUDE_KEYS = frozenset({"lon", "lng", "longitude"})
_NON_FINITE_WORDS = frozenset({"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"})
#: How much of a JSON body :func:`check_request_parameters` walks; past this it is a bulk upload, not a lookup.
_MAX_JSON_NODES = 2_000


def _check_named_coordinate(service: str, key: str, value: object) -> None:
    lowered = key.lower()
    if lowered in _LATITUDE_KEYS:
        limit = 90.0
    elif lowered in _LONGITUDE_KEYS:
        limit = 180.0
    else:
        return
    if isinstance(value, str) and value.strip().lower() in _NON_FINITE_WORDS:
        reject(service, InputRejection.INVALID_COORDINATES, f"{key} is not a finite number")
    number = _as_finite_float(value)
    if number is not None and abs(number) > limit:
        reject(service, InputRejection.INVALID_COORDINATES, f"{key} must be within [-{limit:.0f}, {limit:.0f}]")


def _check_value(service: str, key: str, value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        named_coordinate = key.lower() in _LATITUDE_KEYS | _LONGITUDE_KEYS
        reject(service, InputRejection.INVALID_COORDINATES if named_coordinate else InputRejection.OUT_OF_RANGE, f"{key} is not a finite number")
    _check_named_coordinate(service, key, value)


def _pairs(params: object) -> list[tuple[str, object]]:
    if isinstance(params, Mapping):
        return [(str(key), value) for key, value in params.items()]
    if isinstance(params, list | tuple):
        return [(str(item[0]), item[1]) for item in params if isinstance(item, list | tuple) and len(item) == 2]
    return []


def check_request_parameters(service: str, *, params: object = None, json: object = None) -> None:
    """Refuse a request whose parameters no API could answer.

    Checks query parameters and a JSON body for a non-finite number anywhere, and for a value under
    a latitude/longitude name that is non-finite or off the globe. Anything else - including a value
    that does not parse as a number - is left to the provider.

    Args:
        service: The service key the call is for.
        params: The ``params`` given to ``requests``.
        json: The ``json`` body given to ``requests``.

    Raises:
        ImpossibleInputError: A parameter is one of the above.
    """
    for key, value in _pairs(params):
        values = value if isinstance(value, list | tuple) else (value,)
        for item in values:
            _check_value(service, key, item)

    if json is None or isinstance(json, str | bytes):
        return
    pending: list[tuple[str, Any]] = [("body", json)]
    visited = 0
    while pending and visited < _MAX_JSON_NODES:
        key, node = pending.pop()
        visited += 1
        if isinstance(node, Mapping):
            pending.extend((str(child_key), child) for child_key, child in node.items())
        elif isinstance(node, list | tuple):
            pending.extend((key, child) for child in node)
        else:
            _check_value(service, key, node)
