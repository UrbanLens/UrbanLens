"""Lightweight natural-language parsing for global-search queries.

Turns free text like ``"photos from last summer"`` or ``"pins in Cincinnati"``
into a structured :class:`ParsedQuery`: requested result types, an absolute
date range, an optional place name, and the remaining free-text terms. Parsing
is deliberately heuristic - anything it does not recognize stays in the
free-text portion, and the engine falls back to a plain-text search when a
structured interpretation yields nothing.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
import re

from django.utils import timezone

#: Words users type mapped to RESULT_TYPES slugs. Matched as whole tokens only.
TYPE_KEYWORDS: dict[str, str] = {
    "photo": "photos",
    "photos": "photos",
    "picture": "photos",
    "pictures": "photos",
    "pic": "photos",
    "pics": "photos",
    "image": "photos",
    "images": "photos",
    "pin": "pins",
    "pins": "pins",
    "location": "pins",
    "locations": "pins",
    "wiki": "wikis",
    "wikis": "wikis",
    "trip": "trips",
    "trips": "trips",
    "visit": "visits",
    "visits": "visits",
    "message": "messages",
    "messages": "messages",
    "dm": "messages",
    "dms": "messages",
    "map": "maps",
    "maps": "maps",
    "markup": "maps",
    "markups": "maps",
    "checkin": "safety",
    "checkins": "safety",
    "check-in": "safety",
    "check-ins": "safety",
    "safety": "safety",
    "comment": "comments",
    "comments": "comments",
}

#: Season name to inclusive (start month, end month). Winter wraps the year.
_SEASONS: dict[str, tuple[int, int]] = {
    "spring": (3, 5),
    "summer": (6, 8),
    "fall": (9, 11),
    "autumn": (9, 11),
    "winter": (12, 2),
}

_MONTHS: dict[str, int] = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})

#: Filler tokens dropped from the leftover free text ("my pins in ohio").
_STOPWORDS = {"my", "our", "from", "of", "the", "a", "an", "with", "about", "for", "show", "me", "find", "search"}

_MONTH_PATTERN = "|".join(sorted(_MONTHS, key=len, reverse=True))
_SEASON_PATTERN = "|".join(_SEASONS)


@dataclass(slots=True)
class ParsedQuery:
    """A structured interpretation of a raw search query.

    Attributes:
        raw: The query exactly as typed.
        text: Remaining free text after structured parts were extracted.
        terms: Lowercased tokens of ``text`` (stopwords removed).
        types: RESULT_TYPES slugs the user asked for; empty means all types.
        date_start: Inclusive start of a parsed date range, or None.
        date_end: Inclusive end of a parsed date range, or None.
        place: A place name parsed from "in/near/at <place>", or None.
        date_phrase: The date words as typed, for echoing back in the UI.
    """

    raw: str
    text: str = ""
    terms: list[str] = field(default_factory=list)
    types: set[str] = field(default_factory=set)
    date_start: date | None = None
    date_end: date | None = None
    place: str | None = None
    date_phrase: str | None = None

    @property
    def has_structure(self) -> bool:
        """Whether any structured part (type, date, place) was recognized."""
        return bool(self.types or self.date_start or self.place)

    @property
    def is_empty(self) -> bool:
        """Whether the query carries no usable signal at all."""
        return not (self.terms or self.has_structure)

    def describe_filters(self) -> list[str]:
        """Human-readable chips describing the structured filters in effect.

        Returns:
            Short strings like ``"Photos"``, ``"Jun 1 - Aug 31, 2025"``,
            ``"in Cincinnati"`` for the results header.
        """
        from urbanlens.dashboard.services.global_search.results import RESULT_TYPES

        chips: list[str] = []
        for slug in sorted(self.types):
            meta = RESULT_TYPES.get(slug)
            if meta:
                chips.append(meta.label)
        if self.date_start and self.date_end:

            def _fmt(day: date) -> str:
                return f"{calendar.month_abbr[day.month]} {day.day}"

            if self.date_start.year == self.date_end.year:
                chips.append(f"{_fmt(self.date_start)} - {_fmt(self.date_end)}, {self.date_end.year}")
            else:
                chips.append(f"{_fmt(self.date_start)}, {self.date_start.year} - {_fmt(self.date_end)}, {self.date_end.year}")
        if self.place:
            chips.append(f"in {self.place.title()}")
        return [chip for chip in chips if chip]


def _month_range(year: int, month: int) -> tuple[date, date]:
    """Inclusive first/last day of one calendar month."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _season_range(name: str, year: int) -> tuple[date, date]:
    """Inclusive date range of a season occurrence labelled by its starting year.

    Args:
        name: A key of ``_SEASONS``.
        year: The year the season starts in (winter 2025 = Dec 2025 - Feb 2026).

    Returns:
        (start, end) dates.
    """
    start_month, end_month = _SEASONS[name]
    start = date(year, start_month, 1)
    end_year = year + 1 if end_month < start_month else year
    end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
    return start, end


def _last_season(name: str, today: date) -> tuple[date, date]:
    """The most recent fully completed occurrence of a season."""
    year = today.year
    while True:
        start, end = _season_range(name, year)
        if end < today:
            return start, end
        year -= 1


def _recent_season(name: str, today: date) -> tuple[date, date]:
    """The current occurrence of a season if it has started, else the previous one."""
    year = today.year
    while True:
        start, end = _season_range(name, year)
        if start <= today:
            return start, end
        year -= 1


def _recent_month(month: int, today: date) -> tuple[date, date]:
    """The most recent occurrence of a month (this year if started, else last year)."""
    year = today.year if month <= today.month else today.year - 1
    return _month_range(year, month)


def _last_month_occurrence(month: int, today: date) -> tuple[date, date]:
    """The most recent *completed* occurrence of a month ("last June")."""
    year = today.year if month < today.month else today.year - 1
    return _month_range(year, month)


def _extract_dates(text: str, today: date) -> tuple[str, date | None, date | None, str | None]:
    """Find and strip the first recognized date phrase from ``text``.

    Args:
        text: Lowercased query text.
        today: Anchor date for relative phrases.

    Returns:
        (remaining text, start, end, matched phrase) - start/end/phrase are
        None when no date phrase was found.
    """
    # Ordered longest/most-specific first so e.g. "summer 2024" wins over "2024".
    patterns: list[tuple[re.Pattern[str], object]] = [
        (re.compile(rf"\b(?:from |during |in )?last ({_SEASON_PATTERN})\b"), lambda m: _last_season(m.group(1), today)),
        (re.compile(rf"\b(?:from |during |in )?this ({_SEASON_PATTERN})\b"), lambda m: _recent_season(m.group(1), today)),
        (re.compile(rf"\b(?:from |during |in )?({_SEASON_PATTERN}) (20\d{{2}})\b"), lambda m: _season_range(m.group(1), int(m.group(2)))),
        # Bare season/month names require a preposition ("photos from june") so a
        # pin literally named "Summer Street Mill" still text-searches normally.
        (re.compile(rf"\b(?:from|during|in) ({_SEASON_PATTERN})\b"), lambda m: _recent_season(m.group(1), today)),
        (re.compile(rf"\b(?:from |during |in )?last ({_MONTH_PATTERN})\b"), lambda m: _last_month_occurrence(_MONTHS[m.group(1)], today)),
        (re.compile(rf"\b(?:from |during |in )?({_MONTH_PATTERN}) (20\d{{2}})\b"), lambda m: _month_range(int(m.group(2)), _MONTHS[m.group(1)])),
        (re.compile(rf"\b(?:from|during|in) ({_MONTH_PATTERN})\b(?! \d)"), lambda m: _recent_month(_MONTHS[m.group(1)], today)),
        (re.compile(r"\b(?:from |during |in )?last year\b"), lambda _m: (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31))),
        (re.compile(r"\b(?:from |during |in )?this year\b"), lambda _m: (date(today.year, 1, 1), today)),
        (re.compile(r"\blast month\b"), lambda _m: _month_range((today.replace(day=1) - timedelta(days=1)).year, (today.replace(day=1) - timedelta(days=1)).month)),
        (re.compile(r"\bthis month\b"), lambda _m: (today.replace(day=1), today)),
        (re.compile(r"\blast week\b"), lambda _m: (today - timedelta(days=today.weekday() + 7), today - timedelta(days=today.weekday() + 1))),
        (re.compile(r"\bthis week\b"), lambda _m: (today - timedelta(days=today.weekday()), today)),
        (re.compile(r"\byesterday\b"), lambda _m: (today - timedelta(days=1), today - timedelta(days=1))),
        (re.compile(r"\btoday\b"), lambda _m: (today, today)),
        (re.compile(r"\b(?:from |during |in )?(20\d{2})\b"), lambda m: (date(int(m.group(1)), 1, 1), date(int(m.group(1)), 12, 31))),
    ]

    for pattern, resolver in patterns:
        match = pattern.search(text)
        if not match:
            continue
        try:
            start, end = resolver(match)  # type: ignore[operator]
        except (ValueError, KeyError):
            continue
        remaining = (text[: match.start()] + " " + text[match.end() :]).strip()
        return remaining, start, end, match.group(0).strip()

    return text, None, None, None


def _extract_place(text: str) -> tuple[str, str | None]:
    """Strip a trailing "in/near/around/at <place>" clause from ``text``.

    Only a *trailing* clause is treated as a place so mid-sentence usages stay
    part of the free text. The engine retries without the place filter when a
    parsed place produces no results, keeping false positives harmless.

    Args:
        text: Query text with dates/types already removed.

    Returns:
        (remaining text, place or None).
    """
    match = re.search(r"(?:^|\s)(?:in|near|around|at)\s+(.{2,60})$", text)
    if not match:
        return text, None
    place = match.group(1).strip(" .,")
    if not place or any(char.isdigit() for char in place):
        return text, None
    remaining = text[: match.start()].strip()
    return remaining, place


def parse_query(raw: str) -> ParsedQuery:
    """Parse a raw global-search query into structured filters plus free text.

    Args:
        raw: The query exactly as the user typed it.

    Returns:
        A populated :class:`ParsedQuery`. Unrecognized content is preserved in
        ``text``/``terms`` so nothing the user typed is silently dropped.
    """
    today = timezone.localdate()
    cleaned = " ".join(raw.split())
    parsed = ParsedQuery(raw=raw, text=cleaned)
    if not cleaned:
        return parsed

    working = cleaned.lower()

    working, parsed.date_start, parsed.date_end, parsed.date_phrase = _extract_dates(working, today)

    # Type keywords are whole tokens; hyphens survive tokenization for "check-ins".
    tokens = re.findall(r"[a-z0-9][a-z0-9'-]*", working)
    kept_tokens: list[str] = []
    for token in tokens:
        slug = TYPE_KEYWORDS.get(token)
        if slug:
            parsed.types.add(slug)
        else:
            kept_tokens.append(token)
    working = " ".join(kept_tokens)

    working, parsed.place = _extract_place(working)

    parsed.terms = [token for token in re.findall(r"[a-z0-9][a-z0-9'-]*", working) if token not in _STOPWORDS]
    parsed.text = " ".join(parsed.terms)
    return parsed
