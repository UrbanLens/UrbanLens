"""What a pin's News panel asks GDELT, and which of its answers are about the place.

GDELT's DOC index is translingual: it matches a query against machine translations of articles in
dozens of languages. A query built from a generic phrase, such as a street name, therefore matches
foreign-language coverage that merely translates to the same words, so the query names the place and
the answers are checked against those names again.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import TYPE_CHECKING, Any
import unicodedata

from django.conf import settings

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

#: Most place names the query ORs together.
MAX_NAMES = 6

#: REData's ceiling on a search query's length.
MAX_QUERY_CHARACTERS = 500

#: Shortest name worth sending; GDELT rejects very short keywords outright.
_MIN_NAME_CHARACTERS = 4

#: ISO 639-1 codes to GDELT ``sourcelang`` values.
_GDELT_LANGUAGES: dict[str, str] = {
    "en": "english",
    "es": "spanish",
    "fr": "french",
    "de": "german",
    "it": "italian",
    "pt": "portuguese",
    "nl": "dutch",
    "pl": "polish",
    "sv": "swedish",
    "ru": "russian",
    "ja": "japanese",
    "zh": "chinese",
    "ko": "korean",
    "ar": "arabic",
}

#: Languages whose headlines are written in Latin script, for the script backstop.
_LATIN_SCRIPT_LANGUAGES = frozenset({"english", "spanish", "french", "german", "italian", "portuguese", "dutch", "polish", "swedish"})

#: Largest share of a headline's letters that may be non-Latin in a Latin-script language.
_MAX_FOREIGN_LETTER_SHARE = 0.2

#: Country spellings to GDELT ``sourcecountry`` values; any other full name is used with its spaces removed.
_GDELT_COUNTRIES: dict[str, str] = {
    "us": "unitedstates",
    "usa": "unitedstates",
    "unitedstates": "unitedstates",
    "unitedstatesofamerica": "unitedstates",
    "gb": "unitedkingdom",
    "uk": "unitedkingdom",
    "unitedkingdom": "unitedkingdom",
    "greatbritain": "unitedkingdom",
    "ca": "canada",
    "au": "australia",
    "nz": "newzealand",
    "ie": "ireland",
    "de": "germany",
    "fr": "france",
    "it": "italy",
    "es": "spain",
    "nl": "netherlands",
    "be": "belgium",
}

#: Register providers whose names identify a site well enough to search the news for.
_NAMING_REGISTERS = frozenset({"nps_nrhp"})

_PARENTHETICAL = re.compile(r"\([^)]*\)")
_NON_PHRASE_CHARACTERS = re.compile(r"[^\w\s'-]+|(?<!\w)-|-(?!\w)", re.UNICODE)
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_SINGLE_WORD = re.compile(r"\w+", re.UNICODE)


def _normalized(text: str) -> str:
    """Casefolded, accent-free words separated by single spaces and padded with one on each side."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(character for character in decomposed if not unicodedata.combining(character))
    return f" {_NON_WORD.sub(' ', stripped.casefold()).strip()} "


def _phrase(name: str) -> str:
    """``name`` as a GDELT exact phrase: parentheticals dropped, operator characters turned to spaces."""
    without_notes = _PARENTHETICAL.sub(" ", name)
    return _WHITESPACE.sub(" ", _NON_PHRASE_CHARACTERS.sub(" ", without_notes)).strip()


def _term(term: str) -> str:
    """A phrase quoted, a single word bare: GDELT rejects a quoted one-word phrase as too short."""
    return term if _SINGLE_WORD.fullmatch(term) else f'"{term}"'


def _group(terms: tuple[str, ...]) -> str:
    """Terms ORed in parentheses when there is more than one (GDELT rejects a parenthesised single term)."""
    rendered = [_term(term) for term in terms]
    return rendered[0] if len(rendered) == 1 else f"({' OR '.join(rendered)})"


def _is_street_name(name: str) -> bool:
    """True when the name ends in a street-type word ("Courtyard Drive", "Main St")."""
    from urbanlens.dashboard.services.locations.naming import contains_street_type_word

    words = name.split()
    return bool(words) and contains_street_type_word(words[-1])


def _foreign_letter_share(text: str) -> float:
    """Share of the letters in ``text`` that are not Latin script."""
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return 0.0
    foreign = sum(1 for character in letters if not unicodedata.name(character, "").startswith("LATIN"))
    return foreign / len(letters)


def site_language() -> str:
    """The GDELT ``sourcelang`` for the site's language, English when it has no GDELT equivalent."""
    code = (settings.LANGUAGE_CODE or "en").split("-")[0].casefold()
    return _GDELT_LANGUAGES.get(code, "english")


def gdelt_country(country: str | None, latitude: float | None, longitude: float | None) -> str | None:
    """The GDELT ``sourcecountry`` for a place, or None when it cannot be told.

    Args:
        country: The place's country as geocoded: a name or an ISO code, possibly blank.
        latitude: The place's latitude, used when ``country`` is blank.
        longitude: The place's longitude, used when ``country`` is blank.

    Returns:
        A GDELT country name, or None.
    """
    from urbanlens.dashboard.services.geo.geo_filter import is_usa_coordinates

    key = re.sub(r"[^a-z]", "", (country or "").casefold())
    if key in _GDELT_COUNTRIES:
        return _GDELT_COUNTRIES[key]
    if len(key) > 3:
        return key
    if not key and is_usa_coordinates(latitude, longitude):
        return "unitedstates"
    return None


def _register_names(location: Location) -> list[str]:
    """Names historic registers give this location, each also cut at its first comma.

    "Hudson River State Hospital, Main Building" names a building on the site; the part before the comma names the site.
    A one-word head is not kept: in "Roosevelt, Isaac, House" it is a surname, not a place.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.plugins.builtin.redata_historic_registers import register_rows

    row = LocationCache.objects.filter(location=location, source="redata_historic_registers").only("data").first()
    resources = (row.data or {}).get("resources") if row is not None else None
    if not isinstance(resources, list):
        return []
    naming = [resource for resource in resources if isinstance(resource, dict) and resource.get("provider") in _NAMING_REGISTERS]
    names: list[str] = []
    for register_row in register_rows(naming):
        head = register_row["name"].split(",")[0]
        if len(head.split()) > 1:
            names.append(head)
        names.append(register_row["name"])
    return names


def _raw_names(pin: Pin) -> list[str]:
    """Every name the place is known by, most specific first, before any filtering."""
    from urbanlens.dashboard.models.aliases.model import AliasType

    names: list[str | None] = [pin.name, pin.effective_official_name]
    wiki = pin.community_wiki
    if wiki is not None:
        names.append(wiki.name)
    if pin.pk:
        names.extend(pin.aliases.exclude(kind=AliasType.NICKNAME).values_list("name", flat=True))
    if wiki is not None:
        names.extend(wiki.aliases.exclude(kind=AliasType.NICKNAME).values_list("name", flat=True))
    if pin.location is not None:
        names.extend(_register_names(pin.location))
    names.extend(pin.ancestor_search_names())
    return [name for name in names if name]


def place_names(pin: Pin) -> tuple[str, ...]:
    """The names a news article about this pin's place would use.

    Street names and address fragments are left out: they name the surroundings, and are the generic
    phrases that match unrelated coverage. A name that contains another kept name is redundant in an
    OR query and is dropped too.

    Args:
        pin: The pin whose place is being searched for.

    Returns:
        Up to :data:`MAX_NAMES` exact-phrase names.
    """
    from urbanlens.dashboard.services.locations.naming import is_address_derived_name, is_meaningful_name

    kept: list[str] = []
    for raw in _raw_names(pin):
        name = _phrase(raw)
        if not is_meaningful_name(name) or len(_NON_WORD.sub("", name)) < _MIN_NAME_CHARACTERS or _is_street_name(name):
            continue
        if pin.location is not None and is_address_derived_name(name, pin.location):
            continue
        normalized = _normalized(name)
        if any(_normalized(existing) in normalized for existing in kept):
            continue
        kept = [existing for existing in kept if normalized not in _normalized(existing)]
        kept.append(name)
    return tuple(kept[:MAX_NAMES])


def place_localities(pin: Pin) -> tuple[str, ...]:
    """The town and county the place is in, as geocoded."""
    localities: list[str] = []
    for value in (pin.effective_city, pin.effective_county):
        phrase = _phrase(value or "")
        if phrase and len(_NON_WORD.sub("", phrase)) >= _MIN_NAME_CHARACTERS and phrase not in localities:
            localities.append(phrase)
    return tuple(localities)


@dataclass(frozen=True, slots=True)
class NewsQuery:
    """A news search for one place.

    Attributes:
        names: Exact-phrase names of the place; an article must use one.
        localities: Its town and county; the query requires one when any is known.
        language: GDELT ``sourcelang`` value.
        country: GDELT ``sourcecountry`` value, or None when the country is unknown.
    """

    names: tuple[str, ...]
    localities: tuple[str, ...]
    language: str
    country: str | None

    @classmethod
    def for_pin(cls, pin: Pin) -> NewsQuery | None:
        """The news search for a pin's place, or None when the place has no name worth searching for."""
        names = place_names(pin)
        if not names:
            return None
        query = cls(
            names=names,
            localities=place_localities(pin),
            language=site_language(),
            country=gdelt_country(pin.effective_country, pin.effective_latitude, pin.effective_longitude),
        )
        while len(query.gdelt_query()) > MAX_QUERY_CHARACTERS and len(query.names) > 1:
            query = replace(query, names=query.names[:-1])
        return query if len(query.gdelt_query()) <= MAX_QUERY_CHARACTERS else None

    def gdelt_query(self) -> str:
        """The query string in GDELT DOC 2.0 syntax."""
        parts = [_group(self.names)]
        if self.localities:
            parts.append(_group(self.localities))
        parts.append(f"sourcelang:{self.language}")
        if self.country:
            parts.append(f"sourcecountry:{self.country}")
        return " ".join(parts)

    def mentions_place(self, text: str) -> bool:
        """Whether ``text`` uses one of the place's names or localities as whole words."""
        haystack = _normalized(text)
        return any(_normalized(term) in haystack for term in (*self.names, *self.localities))

    def is_relevant(self, article: Mapping[str, Any]) -> bool:
        """Whether a search result is plausibly about this place, in the requested language.

        GDELT gives a headline and a domain, no body text, so the headline is what is judged.

        Args:
            article: A normalized REData news result.

        Returns:
            True when the headline names the place or its locality and is in the requested script.
        """
        title = str(article.get("title") or "")
        if not title or not self.mentions_place(title):
            return False
        return self.language not in _LATIN_SCRIPT_LANGUAGES or _foreign_letter_share(title) <= _MAX_FOREIGN_LETTER_SHARE

    def relevant(self, articles: Iterable[Any]) -> list[dict[str, Any]]:
        """The articles :meth:`is_relevant` keeps, in their original order."""
        return [article for article in articles if isinstance(article, dict) and self.is_relevant(article)]
