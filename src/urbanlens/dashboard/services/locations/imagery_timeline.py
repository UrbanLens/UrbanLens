"""Flatten REData's imagery timeline into one chronology a UI can scrub.

REData answers with two shapes because its sources genuinely differ, and
neither can be expressed as the other:

* ``captures`` - concrete dated images. An aerial survey flown on a date.
* ``providers_timeline[].time_series`` - continuous-coverage layers where the
  available dates are a *range* with a step, not a list. NASA GIBS alone
  publishes four such layers.

A time slider needs one ordered set of offerings, so this merges them without
pretending they are the same thing: a range stays a range, described by its
bounds, and is materialised into an image only when a user picks a date from it
(``POST /imagery/capture/``).

The distinction that must survive the merge is ``continuous``. A continuous
layer has an image for every date in its interval; a granule-based one (a
satellite overpass) may have nothing on a given date inside the same range, and
REData documents that as a ``404 no_imagery`` rather than an error. Losing that
flag means presenting a date as available and then failing to load it.
"""

from __future__ import annotations

from typing import Any


def flatten_timeline(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge dated captures and continuous ranges into one chronology.

    Args:
        envelope: The body of ``GET /imagery/timeline/``.

    Returns:
        Offerings newest first. A ``kind="capture"`` entry has a ``captured_on``
        and an ``asset`` ready to display; a ``kind="range"`` entry carries
        ``start``/``end``/``step`` plus the ``time_series_asset_uuid`` needed to
        materialise a date, and ``continuous`` saying whether every date in it
        is guaranteed to exist.
    """
    offerings: list[dict[str, Any]] = []

    for capture in envelope.get("captures") or []:
        if not isinstance(capture, dict) or not capture.get("captured_on"):
            continue
        offerings.append(
            {
                "kind": "capture",
                "provider": capture.get("provider") or "",
                "captured_on": capture["captured_on"],
                # False means captured_on is Esri's *publication* date, months
                # off the real acquisition; REData resolves it in the
                # background, so a re-read later corrects it. The flag lives in
                # `attributes`, not at the capture's top level - reading it
                # from the wrong place fails silently as "always exact", which
                # is precisely the caption this exists to prevent.
                "date_is_exact": _resolved_flag(capture) is not False,
                "asset": capture.get("asset") or {},
            },
        )

    for provider_entry in envelope.get("providers_timeline") or []:
        if not isinstance(provider_entry, dict):
            continue
        provider = provider_entry.get("provider") or ""
        for series in provider_entry.get("time_series") or []:
            if not isinstance(series, dict):
                continue
            for interval in series.get("intervals") or []:
                if not isinstance(interval, dict) or not interval.get("start"):
                    continue
                offerings.append(
                    {
                        "kind": "range",
                        "provider": provider,
                        "start": interval["start"],
                        "end": interval.get("end"),
                        "step": interval.get("step"),
                        # Granule-based sources can be empty on a date inside
                        # their own range; the UI must not promise otherwise.
                        "continuous": bool(series.get("continuous")),
                        "time_series_asset_uuid": series.get("time_series_asset_uuid"),
                    },
                )

    return sorted(offerings, key=_sort_key, reverse=True)


def _resolved_flag(capture: dict[str, Any]) -> Any:
    """Read ``capture_date_resolved`` from wherever this REData version puts it.

    It belongs to the capture's ``attributes`` blob; the top level is checked
    too so a deployment that promotes it later keeps working.

    Args:
        capture: One ``captures`` entry.

    Returns:
        ``True``/``False``/``None`` as REData reports it, or ``None`` when
        absent - which means the source publishes no acquisition date at all,
        and is not the same claim as ``False``.
    """
    attributes = capture.get("attributes")
    if isinstance(attributes, dict) and "capture_date_resolved" in attributes:
        return attributes["capture_date_resolved"]
    return capture.get("capture_date_resolved")


def _sort_key(entry: dict[str, Any]) -> str:
    """Sortable date for one offering, newest first.

    A range sorts by its end, since that is the most recent imagery it can
    produce - falling back to its start when the range is open-ended.

    Non-string dates are coerced rather than compared raw: one integer year in
    an otherwise valid envelope would otherwise raise ``TypeError`` mid-sort
    and take down the whole carousel, which is a poor trade for a malformed
    field on one capture.

    Args:
        entry: One flattened offering.

    Returns:
        A string safe to compare against every other entry's key.
    """
    for field_name in ("captured_on", "end", "start"):
        value = entry.get(field_name)
        if value not in (None, ""):
            return str(value)
    return ""


def timeline_years(envelope: dict[str, Any]) -> list[int]:
    """Years with imagery, newest first.

    Prefers REData's own ``years``, which already accounts for both shapes.
    Falls back to deriving them from dated captures, so a deployment whose
    REData predates that field still gets a usable answer rather than none.

    Args:
        envelope: The body of ``GET /imagery/timeline/``.

    Returns:
        Distinct years, descending.
    """
    years = [year for year in (envelope.get("years") or []) if isinstance(year, int)]
    if years:
        return sorted(set(years), reverse=True)

    derived = set()
    for capture in envelope.get("captures") or []:
        captured_on = isinstance(capture, dict) and capture.get("captured_on")
        if isinstance(captured_on, str) and len(captured_on) >= 4 and captured_on[:4].isdigit():
            derived.add(int(captured_on[:4]))
    return sorted(derived, reverse=True)
