"""Per-field merging of multiple tiers' results for the same jurisdiction.

Implements ``docs/property-records-plan.md`` section 4: "Prefer the most
authoritative tier available for each field rather than taking one source
wholesale... Flag mismatches... rather than silently picking one." Tier
number is used as the authority ranking - lower is more structured/trusted,
matching the tiers' own definitions (a free structured government REST API
outranks a scraped HTML page) - so for each field, the lowest-tier record
that actually populated it wins.
"""

from __future__ import annotations

import dataclasses
import json

from urbanlens.dashboard.services.apis.property_records.schema import PropertyRecord

#: Record-level fields excluded from per-field merging: `source`/`confidence`
#: describe the record as a whole (see schema.py's docstring), and
#: `county`/`state`/`fips` are jurisdiction identity, identical by
#: construction across every tier's attempt at the same jurisdiction.
_EXCLUDED_FIELDS = frozenset({"source", "confidence", "field_sources", "field_mismatches", "county", "state", "fips"})

_CONTENT_FIELDS: tuple[str, ...] = tuple(f.name for f in dataclasses.fields(PropertyRecord) if f.name not in _EXCLUDED_FIELDS)


def _has_value(value: object) -> bool:
    """Whether a field's value counts as "this tier actually knows this field" (0/0.0 does; None/""/() doesn't)."""
    if value is None:
        return False
    if isinstance(value, (str, tuple, dict)):
        return bool(value)
    return True


def _comparable(value: object) -> object:
    """Normalize a field value for mismatch detection.

    Different tiers routinely format the *same* fact differently - ``"123
    MAIN ST"`` from a GIS layer vs ``"123 Main St"`` from a scraped page - so
    strings are compared casefolded with whitespace collapsed; only a
    difference that survives that normalization counts as a genuine
    disagreement worth flagging to the user.

    Dict-valued fields (currently only ``parcel_geometry``) are converted to
    a canonical JSON string - the caller collects these into a ``set()`` to
    count distinct values, and a plain ``dict`` isn't hashable. Only Tier 1
    ever populates ``parcel_geometry`` today so this never actually triggers
    in practice, but a latent crash on a future dict-valued field is worse
    than a few extra bytes of string conversion here.
    """
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, tuple):
        return tuple(_comparable(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def merge_records(records: list[PropertyRecord]) -> PropertyRecord:
    """Merge one or more tiers' successful results for one jurisdiction into a single record.

    Args:
        records: Successful ``PropertyRecord`` results for the same
            jurisdiction/point, any order, any tiers. Must be non-empty.

    Returns:
        A single ``PropertyRecord``. Identical to the sole input when only
        one record is given - the overwhelmingly common case, since most
        jurisdictions have at most one tier configured.
        ``field_sources``/``field_mismatches`` are populated only when more
        than one tier genuinely contributed data.
    """
    ordered = sorted(records, key=lambda r: r.source.tier)
    primary = ordered[0]
    if len(ordered) == 1:
        return primary

    winners: dict[str, object] = {}
    field_sources: dict[str, int] = {}
    mismatches: list[str] = []

    for name in _CONTENT_FIELDS:
        contributors = [(record.source.tier, getattr(record, name)) for record in ordered]
        contributors = [(tier, value) for tier, value in contributors if _has_value(value)]
        if not contributors:
            continue
        winning_tier, winning_value = contributors[0]
        winners[name] = winning_value
        field_sources[name] = winning_tier
        if len({_comparable(value) for _, value in contributors}) > 1:
            mismatches.append(name)

    # winners is built generically from dataclasses.fields(PropertyRecord) above,
    # so mypy can't verify each value against its specific field's type the way
    # spelling out ~20 fields by name would - the per-tier construction that
    # produced each value (normalize.build_property_record) is what's already
    # type-checked; this is a runtime reducer over already-valid records, not a
    # place new type errors could hide. Covered by test_property_records_merge.py.
    return dataclasses.replace(primary, **winners, field_sources=field_sources, field_mismatches=tuple(sorted(mismatches)))  # type: ignore[arg-type]
