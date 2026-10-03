"""Coercion for numeric values arriving from form or JSON request data.
Views that call `int(request.POST.get(...))` directly therefore turn a malformed field into a 500 rather than a sensible default - the same shape as an unbounded `CharField` write reaching the database."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, localcontext
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.db.models import DecimalField

#: The range of a Django ``IntegerField`` column. psycopg's binary dumper keeps only the low 32 bits of a value
#: written past it (2**31 is stored as -2**31) and raises past 2**63, so the bound belongs on the parse.
DB_INTEGER_MIN = -(2**31)
DB_INTEGER_MAX = 2**31 - 1
#: The largest ``SmallIntegerField`` value; past it a write keeps the low 16 bits.
DB_SMALLINT_MAX = 2**15 - 1
#: The largest ``BigAutoField`` primary key.
DB_BIGINT_MAX = 2**63 - 1


def safe_int_or_none(value: object) -> int | None:
    """Return ``value`` as an int, or ``None`` when it is not one.
    ``None`` rather than a number is what a row lookup needs.

    Args:
        value: Raw value from `request.POST`/`request.GET`, a parsed JSON body, or a third party's metadata.

    Returns:
        The parsed integer, or ``None`` when ``value`` is missing or unparseable."""
    if isinstance(value, bool):
        # bool is an int subclass; treating True as 1 here is almost never intended.
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str | float | bytes | bytearray):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            # OverflowError is `int(float("inf"))`, and it is reachable: json.loads
            # accepts the bare literals Infinity/-Infinity/NaN, and several views
            # pass a parsed body straight in.
            return None
    return None


#: Bounds for :func:`coordinate_or_none`.
LATITUDE_BOUND = 90.0
LONGITUDE_BOUND = 180.0


def bounded_float_or_none(value: object, *, low: float, high: float) -> float | None:
    """Return ``value`` as a finite float within ``[low, high]``, or ``None``.

    Args:
        value: Raw value from a form post or a parsed JSON body.
        low: The smallest accepted value.
        high: The largest accepted value.

    Returns:
        The number, or ``None`` when ``value`` is missing, not a number, not finite, or out of range.
    """
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        return None
    try:
        parsed = float(value)
    except (ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and low <= parsed <= high else None


def coordinate_or_none(value: object, *, bound: float) -> float | None:
    """Return ``value`` as a finite coordinate within ``±bound``, or ``None``.

    Args:
        value: Raw value from a form post or a parsed JSON body.
        bound: :data:`LATITUDE_BOUND` or :data:`LONGITUDE_BOUND`.

    Returns:
        The coordinate, or ``None`` when ``value`` is missing, not a number, not finite, or out of range.
    """
    return bounded_float_or_none(value, low=-bound, high=bound)


def safe_int(value: object, default: int = 0) -> int:
    """Return ``value`` as an int, or ``default`` when it is not one.

    Args:
        value: Raw value from `request.POST`/`request.GET` or a parsed JSON body.
        default: Returned when ``value`` is missing, or is not something an int can be parsed from.

    Returns:
        The parsed integer, or ``default``."""
    parsed = safe_int_or_none(value)
    return default if parsed is None else parsed


def clamp_int(value: object, *, low: int, high: int, default: int) -> int:
    """Return ``value`` as an int constrained to ``[low, high]``.

    Args:
        value: Raw value from request data.
        low: Minimum allowed value.
        high: Maximum allowed value.
        default: Used when ``value`` is not parseable as an int; it is clamped too, so a caller cannot accidentally widen the range through its own default.

    Returns:
        An integer within ``[low, high]``."""
    parsed = safe_int(value, default)
    return max(low, min(high, parsed))


def decimal_for_column(value: Decimal, column: DecimalField) -> Decimal | None:
    """Return ``value`` as a ``numeric(max_digits, decimal_places)`` column would store it, or ``None`` when it cannot.

    PostgreSQL rounds input to the column's scale, half away from zero, and refuses more integer digits than
    ``max_digits - decimal_places`` with ``numeric field overflow``. Rounding first means a value that only overflows
    once rounded up is refused as well.

    Args:
        value: The parsed number.
        column: The ``DecimalField`` the value is bound for.

    Returns:
        The rounded value, or ``None`` when ``value`` is NaN, infinite, or too large for the column.
    """
    if not value.is_finite():
        return None
    integer_digits = column.max_digits - column.decimal_places
    if not value.is_zero() and value.adjusted() >= integer_digits:
        return None
    with localcontext() as context:
        context.prec = max(context.prec, column.max_digits + 1)
        rounded = value.quantize(Decimal(1).scaleb(-column.decimal_places), rounding=ROUND_HALF_UP)
    if not rounded.is_zero() and rounded.adjusted() >= integer_digits:
        return None
    return rounded
