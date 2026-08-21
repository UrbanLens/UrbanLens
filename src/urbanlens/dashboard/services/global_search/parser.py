"""Lightweight natural-language parsing for global-search queries.

Turns free text like ``"photos from last summer"``, ``"pins in Cincinnati"``,
``"pins near me"``, ``"messages from Alice"``, or ``"pin from John"`` into a
structured :class:`ParsedQuery`: requested result types, an absolute date
range, an optional place name, near-me intent, and a person name (messages
and pins only, e.g. "who shared this pin with me"), plus the remaining
free-text terms. Parsing is deliberately heuristic - anything it does not
recognize stays in the free-text portion, and the engine falls back to a
plain-text search when a structured interpretation yields nothing.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
import re

from django.utils import timezone

from urbanlens.dashboard.services.global_search.operators import OperatorScan, scan

#: Words users type mapped to RESULT_TYPES slugs. Matched as whole tokens only,
#: and only when the token is the FIRST word of the query (see
#: ``_extract_type_keywords``) - many of these are ordinary English words
#: ("pin", "map", "trip", "visit", "comment") that would otherwise hijack
#: mid-sentence text like "please visit my page" into a visits-only search.
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
    "article": "articles",
    "articles": "articles",
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

#: A single calendar-date phrase, as used inside a "between X and Y" clause.
_DATE_PHRASE = (
    rf"(?:{_MONTH_PATTERN})\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s*\d{{4}}"
    rf"|(?:{_MONTH_PATTERN})\s+\d{{1,2}}(?:st|nd|rd|th)?"
    rf"|(?:{_MONTH_PATTERN})\s+\d{{4}}"
    rf"|\d{{4}}-\d{{1,2}}-\d{{1,2}}"
    rf"|\d{{1,2}}/\d{{1,2}}/\d{{2,4}}"
    rf"|20\d{{2}}"
)


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
        near_me: Whether the query asked for results near the searching user
            ("near me", "nearby", "close to me", ...).
        near_phrase: The near-me words exactly as typed (e.g. "near me"). Kept
            as literal text (not just a boolean) so a query with no other
            terms still finds a result literally named "near me" - see the
            no-free-text-terms branch of ``SearchProvider.apply_text``.
        near_lat: The searching user's latitude, filled in by the engine (which
            has access to the profile) when ``near_me`` is set.
        near_lng: The searching user's longitude, filled in the same way.
        person: A person name parsed from "from <person>" (only recognized
            alongside the ``messages`` or ``pins`` type, e.g. "messages from
            Alice" or "pin from John" for pins John shared with the user).
    """

    raw: str
    text: str = ""
    terms: list[str] = field(default_factory=list)
    types: set[str] = field(default_factory=set)
    date_start: date | None = None
    date_end: date | None = None
    place: str | None = None
    date_phrase: str | None = None
    near_me: bool = False
    near_phrase: str | None = None
    near_lat: float | None = None
    near_lng: float | None = None
    person: str | None = None
    labels: tuple[str, ...] = ()
    exclude_labels: tuple[str, ...] = ()
    has: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
    author: str | None = None
    sort: str | None = None
    date_field: str | None = None
    clauses: list = field(default_factory=list)
    unknown_keys: tuple[str, ...] = ()
    unsupported: tuple[tuple[str, str], ...] = ()

    @property
    def has_structure(self) -> bool:
        """Whether any structured part (type, date, place, person, near-me) was recognized."""
        return bool(self.types or self.date_start or self.place or self.near_me or self.person or self.labels or self.exclude_labels or self.has or self.states or self.author)

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
        if self.near_me:
            chips.append("near you")
        if self.person:
            chips.append(f"from {self.person.title()}")
        if self.author:
            chips.append(f"by {self.author.title()}")
        for label in self.labels:
            chips.append(f"labelled {label}")
        for label in self.exclude_labels:
            chips.append(f"not labelled {label}")
        for facet in self.has:
            chips.append(f"has {facet}")
        chips.extend(self.states)
        if self.sort:
            chips.append(f"sorted by {self.sort.replace('-', ' ')}")
        return [chip for chip in chips if chip]

    def describe_problems(self) -> list[str]:
        """Warnings to show beside the results.

        Covers the two cases where an empty result would otherwise be
        misleading: a key that looks like an operator but is not one, and an
        operator that is real but cannot currently be answered.
        """
        notes = [f"\u201c{key}:\u201d isn't a search operator, so it was searched as text." for key in self.unknown_keys]
        notes.extend(f"\u201c{key}:\u201d {reason}" for key, reason in self.unsupported)
        return notes


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


def _resolve_calendar_date(phrase: str, today: date, year_hint: int | None = None) -> date | None:
    """Resolve a single date phrase (one side of a "between X and Y" clause) to a concrete date.

    Args:
        phrase: A lowercase date phrase, e.g. "march 5, 2024", "3/5/2024",
            "2024-03-05", "march 2024", or "2024".
        today: Anchor date used to infer a missing year for "month day" phrases.
        year_hint: Year to use for a bare "month day" phrase instead of
            guessing from ``today`` - set when the other side of a "between X
            and Y" clause carried an explicit year that should apply to both.

    Returns:
        The resolved date, or None if the phrase isn't a recognized format.
    """
    phrase = phrase.strip(" ,.")
    iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", phrase)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    slash = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", phrase)
    if slash:
        month, day, year = int(slash.group(1)), int(slash.group(2)), int(slash.group(3))
        year += 2000 if year < 100 else 0
        try:
            return date(year, month, day)
        except ValueError:
            return None
    month_day_year = re.match(rf"^({_MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})$", phrase)
    if month_day_year:
        try:
            return date(int(month_day_year.group(3)), _MONTHS[month_day_year.group(1)], int(month_day_year.group(2)))
        except ValueError:
            return None
    month_day = re.match(rf"^({_MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?$", phrase)
    if month_day:
        month = _MONTHS[month_day.group(1)]
        year = year_hint if year_hint is not None else (today.year if month <= today.month else today.year - 1)
        try:
            return date(year, month, int(month_day.group(2)))
        except ValueError:
            return None
    month_year = re.match(rf"^({_MONTH_PATTERN})\s+(\d{{4}})$", phrase)
    if month_year:
        return date(int(month_year.group(2)), _MONTHS[month_year.group(1)], 1)
    year_only = re.match(r"^(20\d{2})$", phrase)
    if year_only:
        return date(int(year_only.group(1)), 1, 1)
    return None


def _between_range(first: str, second: str, today: date) -> tuple[date, date]:
    """Resolve a "between X and Y" clause into an inclusive (start, end) range.

    Args:
        first: The date phrase before "and".
        second: The date phrase after "and".
        today: Anchor date passed through to single-date resolution.

    Returns:
        (start, end) with start <= end.

    Raises:
        ValueError: Either side isn't a recognized date phrase.
    """
    # If only one side spells out a year ("march 1 and march 15 2024"), apply
    # it to the other side too instead of guessing a year from `today`.
    first_year = re.search(r"\d{4}", first)
    second_year = re.search(r"\d{4}", second)
    year_hint = None
    if first_year and not second_year:
        year_hint = int(first_year.group(0))
    elif second_year and not first_year:
        year_hint = int(second_year.group(0))
    start = _resolve_calendar_date(first, today, year_hint=year_hint)
    end = _resolve_calendar_date(second, today, year_hint=year_hint)
    if start is None or end is None:
        raise ValueError(f"Unrecognized date phrase in 'between {first} and {second}'")
    return (start, end) if start <= end else (end, start)


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
        (re.compile(rf"\bbetween\s+({_DATE_PHRASE})\s+and\s+({_DATE_PHRASE})\b"), lambda m: _between_range(m.group(1), m.group(2), today)),
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
        # The preposition is mandatory here (unlike the season/month patterns
        # above, which explicitly allow bare mentions with a comment
        # explaining why): a bare 4-digit token needs no other context to
        # show up in ordinary text ("Building 2024", "Route 2027"), so
        # treating it as an implicit date filter would be too eager.
        (re.compile(r"\b(?:from|during|in)\s+(20\d{2})\b"), lambda m: (date(int(m.group(1)), 1, 1), date(int(m.group(1)), 12, 31))),
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


#: "Near me" phrasings, stripped before the generic place clause so "near me"
#: is never misread as a place named "me".
_NEAR_ME_PATTERN = re.compile(r"\b(?:near|nearby|close to|around)\s+me\b")
_NEARBY_PATTERN = re.compile(r"\bnearby\b")


def _extract_near_me(text: str) -> tuple[str, str | None]:
    """Strip a "near me"/"nearby"/"close to me"/"around me" phrase.

    Args:
        text: Lowercased query text with dates/types already removed.

    Returns:
        (remaining text, the matched phrase verbatim, or None if not found).
        The phrase is kept (not just a boolean) so it can still be matched as
        literal text - see :class:`ParsedQuery.near_phrase`.
    """
    for pattern in (_NEAR_ME_PATTERN, _NEARBY_PATTERN):
        match = pattern.search(text)
        if match:
            remaining = (text[: match.start()] + " " + text[match.end() :]).strip()
            return remaining, match.group(0)
    return text, None


#: Clause-introducing keywords recognized elsewhere in the pipeline (the
#: "in/near/around/at <place>" clause, extracted right after the person
#: clause - see call order in ``parse_query``). The person-clause match stops
#: before one of these so "pins from Alice in Cincinnati" splits into
#: person="alice" + place="cincinnati" instead of swallowing both into the
#: person name.
_CLAUSE_BOUNDARY_KEYWORDS = ("in", "near", "around", "at", "during")


def _extract_person(text: str) -> tuple[str, str | None]:
    """Strip a "from <person>" clause from ``text``, stopping at the next clause.

    Only recognized when the query already named the ``messages`` or ``pins``
    type (see call site), so "photos from paris" keeps "paris" as free
    text/place instead of being misread as a person's name.

    Args:
        text: Query text with dates/types/near-me already removed.

    Returns:
        (remaining text, person name or None).
    """
    # Bound matches Django's User.username max_length (150) so long usernames
    # are still captured in full. The match is non-greedy and stops before a
    # following recognized clause keyword (e.g. "in <place>") rather than
    # running to the end of the string, so "from Alice in Cincinnati" doesn't
    # get read as a single person named "alice in cincinnati".
    boundary = "|".join(_CLAUSE_BOUNDARY_KEYWORDS)
    match = re.search(rf"(?:^|\s)from\s+(.{{2,150}}?)(?=\s+(?:{boundary})\s+|$)", text)
    if not match:
        return text, None
    # Unlike place names, usernames commonly include digits ("john123"), so
    # (unlike _extract_place) digits do not disqualify a person clause.
    person = match.group(1).strip(" .,")
    if not person:
        return text, None
    # Unlike the old end-anchored match, a following clause (e.g. "in
    # cincinnati") is not consumed by the match (only asserted via lookahead),
    # so it must be preserved in the remainder for _extract_place to see.
    remaining = (text[: match.start()] + " " + text[match.end() :]).strip()
    return remaining, person


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


def _extract_type_keywords(working: str) -> tuple[str, set[str]]:
    """Strip type-restricting keywords from ``working``, at either end of the query.

    Many entries in ``TYPE_KEYWORDS`` are ordinary English words ("pin",
    "map", "trip", "visit", "comment"), so a match anywhere in the sentence
    produces false-positive hijacks - "please visit my page" must not become a
    visits-only search. Only the first and last tokens are considered, which
    is where a deliberate type scope actually lands in English: people write
    both "photos of the abandoned mill" and "abandoned mill photos", but a
    type word buried mid-sentence is almost always incidental.

    A trailing keyword that turns out to be part of the target's real name
    (searching for a pin literally called "Road Trip") is recovered by
    ``GlobalSearchEngine``'s zero-result fallback, which retries with the
    inferred types cleared.

    Args:
        working: Lowercased query text with dates already removed.

    Returns:
        (remaining text, set of RESULT_TYPES slugs recognized).
    """
    # Type keywords are whole tokens; hyphens survive tokenization for "check-ins".
    tokens = re.findall(r"[a-z0-9][a-z0-9'-]*", working)
    edges = {0, len(tokens) - 1}
    types: set[str] = set()
    kept_tokens: list[str] = []
    for index, token in enumerate(tokens):
        slug = TYPE_KEYWORDS.get(token) if index in edges else None
        if slug:
            types.add(slug)
        else:
            kept_tokens.append(token)
    return " ".join(kept_tokens), types


def extract_fallback_terms(raw: str) -> list[str]:
    """Reduce a raw query to leftover meaningful words, for the plain-text fallback.

    Applies the same date-phrase, type-keyword, near-me-phrase, and stopword
    stripping the primary parse uses, without imposing any of those as search
    filters. Used by :class:`~urbanlens.dashboard.services.global_search.engine.GlobalSearchEngine`
    when a structured interpretation (parsed place/date/type) matches
    nothing, so e.g. "photos from last summer" retries on "summer" alone
    instead of requiring the literal word "photos" in the target text.

    Args:
        raw: The query exactly as typed (``ParsedQuery.raw``).

    Returns:
        Lowercased leftover tokens with stopwords removed.
    """
    cleaned = " ".join(raw.split())
    if not cleaned:
        return []
    today = timezone.localdate()
    working = cleaned.lower()
    working, _start, _end, _phrase = _extract_dates(working, today)
    working, _types = _extract_type_keywords(working)
    working, _near_phrase = _extract_near_me(working)
    return [token for token in re.findall(r"[a-z0-9][a-z0-9'-]*", working) if token not in _STOPWORDS]


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

    # Operators are exact, so they are read first and the English heuristics
    # below only ever see what is left over. That ordering is what lets
    # `place:"Poughkeepsie, NY"` survive a place-guesser that would otherwise
    # have split it at the comma.
    scanned = scan(cleaned)
    _apply_clauses(parsed, scanned)
    working = scanned.text.lower()
    if not working and not parsed.has_structure:
        return parsed

    # Each heuristic still runs, because its job is also to *consume* the words
    # it recognized so they don't fall through into free-text terms. But an
    # operator already answered is never overwritten: what the user stated
    # outranks what we guessed.
    working, guessed_start, guessed_end, guessed_phrase = _extract_dates(working, today)
    if parsed.date_start is None:
        parsed.date_start, parsed.date_end, parsed.date_phrase = guessed_start, guessed_end, guessed_phrase

    working, guessed_types = _extract_type_keywords(working)
    parsed.types |= guessed_types

    working, guessed_near = _extract_near_me(working)
    if not parsed.near_me:
        parsed.near_phrase = parsed.near_phrase or guessed_near
        parsed.near_me = guessed_near is not None

    # "from <person>" only means a person for types that actually have a
    # sender/sharer concept - messages (from/to) and pins (shared with me by).
    # Otherwise "photos from paris" would misread "paris" as a person's name.
    if parsed.types & {"messages", "pins"}:
        working, guessed_person = _extract_person(working)
        parsed.person = parsed.person or guessed_person

    working, guessed_place = _extract_place(working)
    parsed.place = parsed.place or guessed_place

    parsed.terms = [token for token in re.findall(r"[a-z0-9][a-z0-9'-]*", working) if token not in _STOPWORDS]
    parsed.text = " ".join(parsed.terms)
    return parsed


#: Operator keys that name a date field rather than filtering some other way.
_DATE_OPERATORS = ("visited", "created", "updated", "viewed", "taken", "starts", "ends")


def _apply_clauses(parsed: ParsedQuery, scanned: OperatorScan) -> None:
    """Fold scanned operator clauses into *parsed*, in place.

    Operators set the same fields the English heuristics would, so downstream
    consumers never learn which register the user wrote in.

    Args:
        parsed: The query being built. Mutated.
        scanned: The operator scan of the raw query.
    """
    parsed.clauses = list(scanned.clauses)
    parsed.unknown_keys = tuple(scanned.unknown_keys)
    parsed.unsupported = tuple((clause.key, clause.operator.unsupported_reason) for clause in scanned.clauses if clause.operator.unsupported_reason)

    labels: list[str] = []
    exclude_labels: list[str] = []
    facets: list[str] = []
    states: list[str] = []

    for clause in scanned.clauses:
        key = clause.key
        if key == "type":
            parsed.types |= _resolve_types(clause.values)
        elif key == "label":
            (exclude_labels if clause.negated else labels).extend(clause.values)
        elif key == "has":
            facets.extend(f"-{value}" if clause.negated else value for value in clause.values)
        elif key == "is":
            states.extend(f"-{value}" if clause.negated else value for value in clause.values)
        elif key == "place":
            parsed.place = clause.value
        elif key == "near":
            parsed.near_phrase = clause.raw
            parsed.near_me = clause.value.lower() in {"me", "here"}
        elif key == "from":
            parsed.person = clause.value
        elif key == "by":
            parsed.author = clause.value
        elif key == "sort":
            parsed.sort = clause.value.lower()
        elif key in _DATE_OPERATORS:
            _apply_date_clause(parsed, key, clause.value)

    parsed.labels = tuple(labels)
    parsed.exclude_labels = tuple(exclude_labels)
    parsed.has = tuple(facets)
    parsed.states = tuple(states)


def _apply_date_clause(parsed: ParsedQuery, key: str, value: str) -> None:
    """Resolve one date operator's value onto *parsed*.

    Reuses the same phrase vocabulary the English path uses, so
    ``visited:"last march"`` and "visited last march" resolve identically
    rather than being two date parsers that drift.
    """
    today = timezone.localdate()
    spoken = value.lower().replace("-", " ").strip()
    _, start, end, phrase = _extract_dates(spoken, today)
    if start is None:
        # A bare value ("2019", "march") carries no preposition, which is what
        # most of the phrase patterns key on. Saying it the long way reuses
        # that vocabulary rather than growing a second date parser here - and
        # it keeps a value meaning an *interval* ("2019" is the whole year)
        # rather than the instant _resolve_calendar_date would return.
        _, start, end, phrase = _extract_dates(f"in {spoken}", today)
    if start is None:
        return
    parsed.date_start, parsed.date_end, parsed.date_phrase = start, end, phrase or value
    parsed.date_field = key


def _resolve_types(values: tuple[str, ...]) -> set[str]:
    """Result-type slugs named by a ``type:`` clause.

    Accepts the same words the English path accepts (``photo``, ``photos``,
    ``picture``...), so the two registers share one vocabulary.
    """
    resolved: set[str] = set()
    for value in values:
        slug = TYPE_KEYWORDS.get(value.lower())
        if slug:
            resolved.add(slug)
    return resolved
