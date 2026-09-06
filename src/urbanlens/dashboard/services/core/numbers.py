"""Coercion for numeric values arriving from form or JSON request data.

`int("abc")` raises `ValueError`, and a request body is free to contain "abc" wherever
a view expects a number. Views that call `int(request.POST.get(...))` directly therefore
turn a malformed field into a 500 rather than a sensible default - the same shape as an
unbounded `CharField` write reaching the database.

The codebase had solved this three times locally before this module existed
(`controllers/labels._safe_int`, `controllers/saved_filters._clamp_opacity`,
`controllers/map_overlays._clamped_opacity`), and about nineteen other call sites had not
solved it at all.

`safe_int_or_none` holds the parsing rule and the other two are built on it, so a value
these helpers disagree about is not possible.
"""

from __future__ import annotations


def safe_int_or_none(value: object) -> int | None:
    """Return ``value`` as an int, or ``None`` when it is not one.

    The parsing rule for this module; `safe_int` is this with a default
    substituted for the ``None``.

    ``None`` rather than a number is what a row lookup needs. `filter(pk=0)`
    happens to match nothing, but only because no row holds that pk, whereas
    `filter(pk=None)` is an `IS NULL` comparison a primary key can never satisfy
    - so it degrades to the empty queryset the caller's "not found" branch is
    already written for. Without it, `filter(pk=request.POST.get("id", ""))`
    raises `ValueError: Field 'id' expected a number but got ''` and returns a
    500 for what is usually just an omitted field.

    Args:
        value: Raw value from `request.POST`/`request.GET`, a parsed JSON body, or
            a third party's metadata.

    Returns:
        The parsed integer, or ``None`` when ``value`` is missing or unparseable.
    """
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


def safe_int(value: object, default: int = 0) -> int:
    """Return ``value`` as an int, or ``default`` when it is not one.

    Args:
        value: Raw value from `request.POST`/`request.GET` or a parsed JSON body.
        default: Returned when ``value`` is missing, or is not something an int can be
            parsed from.

    Returns:
        The parsed integer, or ``default``.
    """
    parsed = safe_int_or_none(value)
    return default if parsed is None else parsed


def clamp_int(value: object, *, low: int, high: int, default: int) -> int:
    """Return ``value`` as an int constrained to ``[low, high]``.

    Args:
        value: Raw value from request data.
        low: Minimum allowed value.
        high: Maximum allowed value.
        default: Used when ``value`` is not parseable as an int; it is clamped too, so a
            caller cannot accidentally widen the range through its own default.

    Returns:
        An integer within ``[low, high]``.
    """
    parsed = safe_int(value, default)
    return max(low, min(high, parsed))
