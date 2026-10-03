"""Whether an external media item is about the place it was found for (P196).

An item is about the place when it is geolocated inside the place's bounding box, or when it names the place: one of
the place's names with a geographic indicator consistent with it, or a distinctive name alone when nothing geographic
contradicts it. The rules, thresholds and their reasons are in ``docs/archive/PROBLEMS-ARCHIVE.md`` under P196.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from functools import cached_property
import html
import math
import re
from typing import TYPE_CHECKING
import unicodedata

from urbanlens.dashboard.services.geo import gazetteer
from urbanlens.dashboard.services.geo.distance import haversine_km
from urbanlens.dashboard.services.locations.display import canonical_country
from urbanlens.dashboard.services.locations.naming import canonical_state

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaItem

#: Words that say what sort of place something is rather than which one.
GENERIC_WORDS: frozenset[str] = frozenset(
    {
        "abandoned",
        "academy",
        "american",
        "annex",
        "apartment",
        "apartments",
        "armory",
        "arsenal",
        "asylum",
        "bank",
        "barn",
        "barracks",
        "baptist",
        "bay",
        "beach",
        "big",
        "block",
        "brewery",
        "brick",
        "bridge",
        "brook",
        "building",
        "buildings",
        "cabin",
        "camp",
        "castle",
        "catholic",
        "cathedral",
        "cemetery",
        "center",
        "central",
        "centre",
        "chapel",
        "christian",
        "church",
        "cinema",
        "city",
        "clinic",
        "co",
        "college",
        "community",
        "company",
        "congregational",
        "corp",
        "corporation",
        "cottage",
        "county",
        "court",
        "courthouse",
        "creek",
        "dairy",
        "dam",
        "depot",
        "district",
        "east",
        "eastern",
        "elementary",
        "episcopal",
        "estate",
        "factory",
        "falls",
        "farm",
        "farmhouse",
        "fifth",
        "first",
        "former",
        "fort",
        "foundry",
        "fourth",
        "garage",
        "garden",
        "gardens",
        "general",
        "grand",
        "great",
        "grove",
        "hall",
        "harbor",
        "heights",
        "high",
        "hill",
        "hills",
        "historic",
        "historical",
        "holy",
        "home",
        "hospital",
        "hotel",
        "house",
        "inc",
        "industrial",
        "infirmary",
        "inn",
        "institute",
        "iron",
        "island",
        "jail",
        "junior",
        "lake",
        "library",
        "little",
        "lodge",
        "lower",
        "lutheran",
        "main",
        "mall",
        "manor",
        "mansion",
        "market",
        "medical",
        "memorial",
        "methodist",
        "middle",
        "mill",
        "mills",
        "mine",
        "monument",
        "motel",
        "mount",
        "mountain",
        "municipal",
        "museum",
        "national",
        "new",
        "north",
        "northern",
        "office",
        "old",
        "opera",
        "orphanage",
        "palace",
        "park",
        "penitentiary",
        "plant",
        "plaza",
        "point",
        "pool",
        "post",
        "power",
        "presbyterian",
        "prison",
        "private",
        "psychiatric",
        "public",
        "quarry",
        "railroad",
        "railway",
        "reformatory",
        "regional",
        "resort",
        "river",
        "road",
        "royal",
        "ruins",
        "sacred",
        "saint",
        "san",
        "sanatorium",
        "sanitarium",
        "santa",
        "santo",
        "sao",
        "school",
        "schoolhouse",
        "second",
        "seminary",
        "senior",
        "shop",
        "site",
        "south",
        "southern",
        "spring",
        "springs",
        "square",
        "st",
        "stadium",
        "state",
        "station",
        "ste",
        "steel",
        "store",
        "street",
        "synagogue",
        "temple",
        "terminal",
        "theater",
        "theatre",
        "third",
        "tower",
        "town",
        "tunnel",
        "union",
        "united",
        "university",
        "upper",
        "valley",
        "villa",
        "village",
        "ward",
        "warehouse",
        "west",
        "western",
        "wing",
        "woods",
        "works",
    },
)
#: Words a name's other words hang on, which count for nothing either way.
STOP_WORDS: frozenset[str] = frozenset({"a", "an", "and", "at", "by", "de", "del", "der", "des", "di", "du", "for", "in", "la", "le", "les", "of", "on", "the", "to", "upon", "van", "von"})
#: A word after one of these is a saint's name, and saints' names are shared by thousands of places.
_SAINT_WORDS = frozenset({"st", "ste", "saint", "sainte", "san", "santa", "santo", "sao"})
#: Gazetteer names that are also everyday words, never read as a place.
_EVERYDAY_PLACE_NAMES = frozenset(
    {
        "aurora",
        "bath",
        "batman",
        "bay",
        "best",
        "bury",
        "central",
        "chad",
        "china",
        "college",
        "commerce",
        "concord",
        "deal",
        "eagle",
        "enterprise",
        "forest",
        "green",
        "guinea",
        "harmony",
        "hope",
        "hull",
        "independence",
        "industry",
        "jersey",
        "jordan",
        "liberty",
        "mali",
        "march",
        "mission",
        "mobile",
        "nice",
        "niger",
        "normal",
        "orange",
        "paradise",
        "phoenix",
        "providence",
        "reading",
        "rugby",
        "sale",
        "salt",
        "sandy",
        "split",
        "spring",
        "springs",
        "temple",
        "turkey",
        "union",
        "university",
        "van",
        "victory",
        "vista",
    },
)
#: State postal codes that are also English words; ignored in text written all in capitals.
_WORDLIKE_STATE_CODES = frozenset({"IN", "OR", "ME", "OK", "HI"})
#: Words after which a place name is part of something else's name ("Hudson River", "Washington Street").
_FEATURE_WORDS = GENERIC_WORDS | {"avenue", "ave", "blvd", "boulevard", "dr", "drive", "highway", "lane", "parkway", "place", "rd", "route", "way"}
_COUNTY_SUFFIXES = (" county", " parish", " borough")

#: Padding around a place's outline, so a photo taken from the street outside it still counts as inside.
OUTLINE_PADDING_METERS = 100.0
#: Half the side of the box around a place with no known outline.
POINT_RADIUS_METERS = 250.0
#: How far outside the box an item may be geolocated before its coordinates place it somewhere else.
COORDINATE_CONFLICT_KM = 5.0
#: A named city this close to the place is the place's own.
CITY_CONSISTENT_KM = 15.0
#: A named city at least this far from the place is somewhere else.
CITY_CONFLICT_KM = 40.0
#: How far the nearest gazetteer city may be when it stands in for a place's unknown state or country.
_REGION_FALLBACK_KM = 50.0
_MAX_PLACE_WORDS = 4
_METERS_PER_DEGREE = 111_320.0

_INITIALISM = re.compile(r"\b(?:[^\W\d_]\.){2,}")
_POSSESSIVE = re.compile(r"['’ʼ]s\b", re.IGNORECASE)
_APOSTROPHE = re.compile(r"['’ʼ`]")
_WORD = re.compile(r"[^\W_]+")
_BREAK = re.compile(r"[,;:()\[\]{}|/.!?\"]")
_FILE_EXTENSION = re.compile(r"\.[A-Za-z0-9]{2,4}$")


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """A latitude/longitude box."""

    south: float
    west: float
    north: float
    east: float

    @classmethod
    def around(cls, latitude: float, longitude: float, meters: float) -> BoundingBox:
        """The box reaching ``meters`` from a point in each direction."""
        return cls(latitude, longitude, latitude, longitude).padded(meters)

    def padded(self, meters: float) -> BoundingBox:
        """This box grown by ``meters`` on every side."""
        middle = math.radians((self.south + self.north) / 2)
        d_lat = meters / _METERS_PER_DEGREE
        d_lng = meters / (_METERS_PER_DEGREE * max(math.cos(middle), 0.01))
        return BoundingBox(self.south - d_lat, self.west - d_lng, self.north + d_lat, self.east + d_lng)

    def contains(self, latitude: float, longitude: float) -> bool:
        """Whether a point lies in the box."""
        return self.south <= latitude <= self.north and self.west <= longitude <= self.east

    def distance_km(self, latitude: float, longitude: float) -> float:
        """Kilometres from a point to the nearest point of the box; 0 inside it."""
        return haversine_km(latitude, longitude, min(max(latitude, self.south), self.north), min(max(longitude, self.west), self.east))


class _Level(IntEnum):
    """How precisely a geographic indicator places an item, most precise first."""

    LOCAL = 0
    COUNTY = 1
    STATE = 2
    COUNTRY = 3


@dataclass(slots=True)
class _Token:
    text: str
    folded: str
    after_break: bool
    used: bool = False

    @property
    def capitalized(self) -> bool:
        return self.text[:1].isupper()


def _plain(text: str) -> str:
    """``text`` with entities decoded, accents dropped, initialisms closed up and apostrophes removed."""
    text = unicodedata.normalize("NFKD", html.unescape(text).replace("&", " and "))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = _INITIALISM.sub(lambda match: match.group(0).replace(".", ""), text)
    return _APOSTROPHE.sub("", _POSSESSIVE.sub("", text))


def _tokens(text: str) -> list[_Token]:
    plain = _plain(text)
    tokens: list[_Token] = []
    end = 0
    for match in _WORD.finditer(plain):
        word = match.group(0)
        tokens.append(_Token(word, word.casefold(), not tokens or _BREAK.search(plain, end, match.start()) is not None))
        end = match.end()
    return tokens


def _is_acronym(word: str) -> bool:
    return len(word) >= 3 and word.isalpha() and word.isupper()


def _has_two_specific_words(words: Sequence[str]) -> bool:
    content = [word for word in words if word not in STOP_WORDS]
    specific = 0
    after_saint = False
    for word in content:
        if not (after_saint or word in GENERIC_WORDS or word.isdigit()):
            specific += 1
        after_saint = word in _SAINT_WORDS
    return specific >= 2 or (specific >= 1 and len(content) >= 3)


@dataclass(frozen=True, slots=True)
class _Name:
    """A name of the place, as it is looked for in text.

    Attributes:
        words: Its folded words, without a leading "the".
        acronym: The name exactly as written, when it is an acronym; it then matches only in capitals.
        distinctive: Whether it identifies one place without help.
    """

    words: tuple[str, ...]
    acronym: str
    distinctive: bool

    @property
    def compact(self) -> str:
        return "".join(self.words)

    @classmethod
    def parse(cls, text: str) -> _Name | None:
        tokens = _tokens(text)
        while tokens and tokens[0].folded == "the":
            tokens.pop(0)
        if not tokens:
            return None
        acronym = tokens[0].text if len(tokens) == 1 and _is_acronym(tokens[0].text) else ""
        words = tuple(token.folded for token in tokens)
        return cls(words, acronym, bool(acronym) or _has_two_specific_words(words))

    def find(self, segment: list[_Token]) -> bool:
        """Mark every unused occurrence of this name in ``segment`` as used; whether there was one."""
        size = len(self.words)
        found = False
        index = 0
        while index + size <= len(segment):
            window = segment[index : index + size]
            if not any(token.used for token in window) and self._is(window):
                for token in window:
                    token.used = True
                found = True
                index += size
            else:
                index += 1
        return found

    def _is(self, window: list[_Token]) -> bool:
        if self.acronym:
            return window[0].text == self.acronym
        return all(token.folded == word for token, word in zip(window, self.words, strict=True))

    def is_tag(self, segment: list[_Token]) -> bool:
        """Whether ``segment`` is this name written as one tag, without spaces ("hudsonriverstatehospital")."""
        return len(segment) == 1 and segment[0].folded == self.compact


def is_distinctive(name: str) -> bool:
    """Whether a name identifies one place without a geographic indicator.

    An acronym of three or more capitals is distinctive, and so is a name with two words that are not generic (see
    :data:`GENERIC_WORDS`), or one such word among three or more.

    Args:
        name: A place name.

    Returns:
        True for "HRSH" or "Hudson River State Hospital"; False for "Historic Mansion" or "Smith House".
    """
    parsed = _Name.parse(name)
    return parsed is not None and parsed.distinctive


@dataclass(frozen=True, slots=True)
class Judgement:
    """Whether an item is about the place, and why.

    Attributes:
        relevant: Whether it is.
        reason: Which rule decided it.
    """

    relevant: bool
    reason: str


def _coordinate(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class MediaSubject:
    """The place media is being judged against.

    Attributes:
        names: Its names, in any order.
        latitude: Its point's latitude.
        longitude: Its point's longitude.
        bbox: Its bounding box, or None to judge by text alone.
        city: Its city or town.
        county: Its county.
        state: Its state, as a name or postal code.
        zipcode: Its postal code.
        country: Its country.
        context: Names of the places it stands in (a building's site); they place an item, but are not its name.
    """

    names: tuple[str, ...]
    latitude: float | None = None
    longitude: float | None = None
    bbox: BoundingBox | None = None
    city: str = ""
    county: str = ""
    state: str = ""
    zipcode: str = ""
    country: str = ""
    context: tuple[str, ...] = ()

    @cached_property
    def _names(self) -> tuple[_Name, ...]:
        parsed = (_Name.parse(name) for name in self.names)
        return tuple(sorted((name for name in parsed if name is not None), key=lambda name: -len(name.words)))

    @cached_property
    def _context(self) -> tuple[_Name, ...]:
        parsed = (_Name.parse(name) for name in self.context)
        return tuple(sorted((name for name in parsed if name is not None), key=lambda name: -len(name.words)))

    @cached_property
    def _city(self) -> str:
        return gazetteer.fold_place_name(self.city)

    @cached_property
    def _counties(self) -> frozenset[str]:
        county = gazetteer.fold_place_name(self.county)
        if not county:
            return frozenset()
        stem = next((county.removesuffix(suffix) for suffix in _COUNTY_SUFFIXES if county.endswith(suffix)), county)
        return frozenset({county, stem, f"{stem} county"})

    @cached_property
    def _state(self) -> str:
        return canonical_state(self.state) if self.state else ""

    @cached_property
    def _country(self) -> str:
        return canonical_country(self.country)

    @cached_property
    def _is_usa(self) -> bool:
        return self._country == canonical_country("United States")

    @cached_property
    def _zipcode(self) -> str:
        return self.zipcode.strip()[:5]

    @cached_property
    def _tag_indicators(self) -> dict[str, _Level]:
        """This place's own indicators written as one tag, without spaces ("newyork")."""
        tags: dict[str, _Level] = {}
        for county in self._counties:
            tags[county.replace(" ", "")] = _Level.COUNTY
        if self._country:
            tags[self._country.replace(" ", "")] = _Level.COUNTRY
        if self._state:
            tags[self._state.replace(" ", "")] = _Level.STATE
            tags.update({name.replace(" ", ""): _Level.STATE for name, code in gazetteer.us_states().items() if code.casefold() == self._state})
        if self._city:
            tags[self._city.replace(" ", "")] = _Level.LOCAL
        if self._zipcode:
            tags[self._zipcode] = _Level.LOCAL
        return tags

    def matches(self, item: MediaItem) -> bool:
        """Whether ``item`` is about this place."""
        return self.judge(item).relevant

    def judge(self, item: MediaItem) -> Judgement:
        """Whether ``item`` is about this place, with the rule that decided it.

        Args:
            item: An item a provider returned for this place.

        Returns:
            The judgement.
        """
        located = self._located(item)
        if located is not None:
            return Judgement(relevant=located, reason="geolocated inside the place" if located else "geolocated somewhere else")

        texts = [_FILE_EXTENSION.sub("", item.title), item.caption, item.description]
        segments = [_tokens(text) for text in texts if text]
        tags = [_tokens(keyword) for keyword in item.keywords.split("|") if keyword.strip()]
        named = [name for name in self._names if _find_everywhere(name, segments, tags)]
        if not named:
            return Judgement(relevant=False, reason="does not name the place")

        evidence: list[tuple[_Level, bool]] = []
        if [name for name in self._context if _find_everywhere(name, segments, tags)]:
            evidence.append((_Level.LOCAL, True))
        for tag in tags:
            if len(tag) == 1 and not tag[0].used and (level := self._tag_indicators.get(tag[0].folded)) is not None:
                tag[0].used = True
                evidence.append((level, True))
        for segment in segments + tags:
            self._scan(segment, evidence)

        distinctive = any(name.distinctive for name in named)
        verdict = _decide(evidence)
        if verdict is None:
            return Judgement(relevant=distinctive, reason="names the place" if distinctive else "names the place only by a generic name")
        level, consistent = verdict
        if not consistent:
            return Judgement(relevant=False, reason=f"names the place, but the {level.name.lower()} named is elsewhere")
        if distinctive or level <= _Level.COUNTY:
            return Judgement(relevant=True, reason=f"names the place and its {level.name.lower()}")
        return Judgement(relevant=False, reason=f"names the place by a generic name, placed only by {level.name.lower()}")

    def _located(self, item: MediaItem) -> bool | None:
        """True inside the box, False well outside it, None when the item's coordinates cannot decide."""
        latitude, longitude = _coordinate(item.latitude), _coordinate(item.longitude)
        if self.bbox is None or latitude is None or longitude is None or (latitude == 0 and longitude == 0):
            return None
        if self.bbox.contains(latitude, longitude):
            return True
        if self.bbox.distance_km(latitude, longitude) > COORDINATE_CONFLICT_KM:
            return False
        return None

    def _scan(self, segment: list[_Token], evidence: list[tuple[_Level, bool]]) -> None:
        """Record each place ``segment`` names, longest phrase first, as consistent with this place or not."""
        shouting = not any(token.text != token.text.upper() for token in segment)
        index = 0
        while index < len(segment):
            step = 1
            if not segment[index].used:
                for size in range(min(_MAX_PLACE_WORDS, len(segment) - index), 0, -1):
                    span = segment[index : index + size]
                    if any(token.used for token in span):
                        continue
                    found = self._classify(segment, index, size, shouting=shouting)
                    if found is None:
                        continue
                    level, consistent = found
                    if consistent is not None:
                        evidence.append((level, consistent))
                    for token in span:
                        token.used = True
                    step = size
                    break
            index += step

    def _classify(self, segment: list[_Token], index: int, size: int, *, shouting: bool) -> tuple[_Level, bool | None] | None:
        """The level of the place the phrase at ``index`` names and whether it is this place's; None when it names none.

        A consistent phrase always counts. A conflicting one counts only where it reads as a place name, not as part
        of another proper noun ("Washington Street") or after "the" ("the Hudson").
        """
        found = self._indicator(segment, index, size, shouting=shouting)
        if found is not None and found[1] is not True and not self._reads_as_place(segment, index, size, shouting=shouting):
            return None
        return found

    def _indicator(self, segment: list[_Token], index: int, size: int, *, shouting: bool) -> tuple[_Level, bool | None] | None:
        span = segment[index : index + size]
        phrase = " ".join(token.folded for token in span)
        if phrase in STOP_WORDS or phrase in _EVERYDAY_PLACE_NAMES:
            return None
        if size == 1 and self._zipcode and phrase == self._zipcode:
            return _Level.LOCAL, True
        if self._city and phrase == self._city:
            return _Level.LOCAL, True
        if phrase in self._counties:
            return _Level.COUNTY, True
        if len(self._state) > 2 and phrase == self._state:
            return _Level.STATE, True
        if (state := _state_at(span, shouting=shouting)) is not None:
            if state == self._state:
                return _Level.STATE, True
            return (_Level.STATE, None) if not self._state and (not self._country or self._is_usa) else (_Level.STATE, False)
        if (country := _country_at(span)) is not None:
            return _Level.COUNTRY, (country == self._country) if self._country else None
        if phrase.endswith(" county") and phrase in gazetteer.us_counties():
            return _Level.COUNTY, False
        return self._city_at(segment, index + size, phrase)

    def _city_at(self, segment: list[_Token], following: int, phrase: str) -> tuple[_Level, bool | None] | None:
        """Whether the city ``phrase`` names is this place's, judged by its distance; a state named after it narrows the candidates."""
        cities = gazetteer.cities_named(phrase)
        if not cities:
            return None
        if (state := _state_following(segment, following)) is not None:
            cities = tuple(city for city in cities if city.country_code == "US" and city.admin1.casefold() == state)
        if not cities or self.latitude is None or self.longitude is None:
            return _Level.LOCAL, None
        latitude, longitude = self.latitude, self.longitude
        nearest = min(city.km_from(latitude, longitude) for city in cities)
        if nearest <= CITY_CONSISTENT_KM:
            return _Level.LOCAL, True
        return _Level.LOCAL, (False if nearest >= CITY_CONFLICT_KM else None)

    def _reads_as_place(self, segment: list[_Token], index: int, size: int, *, shouting: bool) -> bool:
        """Whether a phrase stands alone as a place name rather than inside another name ("Washington Street")."""
        span = segment[index : index + size]
        if not (span[0].capitalized and span[-1].capitalized):
            return False
        before = segment[index - 1] if index > 0 else None
        if before is not None and not span[0].after_break and not before.used:
            if before.folded == "the":
                return False
            if before.capitalized and before.folded not in STOP_WORDS and not shouting:
                return False
        after = segment[index + size] if index + size < len(segment) else None
        if after is not None and not after.after_break and not after.used:
            if after.folded in _FEATURE_WORDS:
                return False
            if after.capitalized and not shouting and not _is_state_code(after):
                return False
        return True


def _find_everywhere(name: _Name, segments: list[list[_Token]], tags: list[list[_Token]]) -> bool:
    """Mark every occurrence of ``name`` in the item's text and tags as used; whether there was one."""
    found = [name.find(segment) for segment in [*segments, *tags]]
    for tag in tags:
        if name.is_tag(tag):
            tag[0].used = True
            found.append(True)
    return any(found)


def _is_state_code(token: _Token) -> bool:
    return len(token.text) == 2 and token.text.isupper() and _state_at([token], shouting=False) is not None


def _state_at(span: list[_Token], *, shouting: bool) -> str | None:
    """The lowercase postal code of the US state ``span`` names: its full name, or its code written in capitals."""
    if len(span) == 1 and len(span[0].text) == 2 and span[0].text.isupper():
        if shouting and span[0].text in _WORDLIKE_STATE_CODES:
            return None
        codes = {code.casefold() for code in gazetteer.us_states().values()}
        return span[0].folded if span[0].folded in codes else None
    code = gazetteer.us_states().get(" ".join(token.folded for token in span))
    return code.casefold() if code else None


def _state_following(segment: list[_Token], index: int) -> str | None:
    """The state named right after a city ("Salem OR", "Salem, Oregon"), as a lowercase postal code.

    A full state name counts only after punctuation: "George Washington" is a man, not a George in Washington.
    """
    for size in (1, 2, 3):
        span = segment[index : index + size]
        if len(span) == size and (span[0].after_break or _is_state_code(span[0])) and (state := _state_at(span, shouting=False)) is not None:
            return state
    return None


def _country_at(span: list[_Token]) -> str | None:
    """The canonical name of the country ``span`` names; "US" and "USA" only in capitals."""
    if len(span) == 1 and span[0].text in {"US", "USA"}:
        return canonical_country("USA")
    phrase = " ".join(token.folded for token in span)
    if phrase in {"united states of america", "united states"}:
        return canonical_country(phrase)
    code = gazetteer.countries().get(phrase)
    if code is None:
        return None
    return canonical_country(gazetteer.country_names_by_code().get(code, phrase))


def _decide(evidence: list[tuple[_Level, bool]]) -> tuple[_Level, bool] | None:
    """The most precise level any indicator names, and whether an indicator at that level is this place's."""
    for level in _Level:
        found = [consistent for found_level, consistent in evidence if found_level == level]
        if found:
            return level, any(found)
    return None


def _distinct_names(names: Iterable[str | None]) -> tuple[str, ...]:
    from urbanlens.dashboard.services.locations.naming import is_meaningful_name

    seen: set[str] = set()
    kept: list[str] = []
    for name in names:
        if not name or not is_meaningful_name(name):
            continue
        cleaned = " ".join(name.split())
        if (key := cleaned.casefold()) not in seen:
            seen.add(key)
            kept.append(cleaned)
    return tuple(kept)


def _bounding_box(location: Location) -> BoundingBox:
    """The box around the place's outline when it has one, else around its point."""
    place = location.place if location.place_id else None
    geometry = place.geometry if place is not None else None
    if geometry is not None and not geometry.empty:
        west, south, east, north = geometry.extent
        return BoundingBox(south, west, north, east).padded(OUTLINE_PADDING_METERS)
    return BoundingBox.around(float(location.latitude), float(location.longitude), POINT_RADIUS_METERS)


def subject_for_location(location: Location, *, names: Iterable[str | None] = (), context: Sequence[str] = ()) -> MediaSubject:
    """The subject everyone who can see a place shares: its public names, outline and address.

    A state or country the address lacks is taken from the nearest gazetteer city within 50 km.

    Args:
        location: The place.
        names: More names to judge by, e.g. a pin's own.
        context: Names of the places it stands in.

    Returns:
        The subject.
    """
    from urbanlens.dashboard.services.pins.search_names import shared_names

    latitude, longitude = float(location.latitude), float(location.longitude)
    state, country = location.state or "", location.country or ""
    if not (state and country) and (city := gazetteer.nearest_city(round(latitude, 2), round(longitude, 2), within_km=_REGION_FALLBACK_KM)) is not None:
        country = country or gazetteer.country_names_by_code().get(city.country_code, "")
        state = state or (city.admin1 if city.country_code == "US" else "")
    return MediaSubject(
        names=_distinct_names([*shared_names(location), *names]),
        latitude=latitude,
        longitude=longitude,
        bbox=_bounding_box(location),
        city=location.city or "",
        county=location.county or "",
        state=state,
        zipcode=location.zipcode or "",
        country=country,
        context=tuple(context),
    )


def subject_for_pin(pin: Pin) -> MediaSubject:
    """The subject a pin's owner sees media judged against: the place's public names, the pin's own, and its site's.

    Args:
        pin: A pin with a location.

    Returns:
        The subject.
    """
    from urbanlens.dashboard.models.aliases.model import AliasType
    from urbanlens.dashboard.services.pins.search_names import search_names

    own: list[str | None] = [pin.name]
    if pin.pk:
        own.extend(pin.aliases.exclude(kind=AliasType.NICKNAME).values_list("name", flat=True))
    return subject_for_location(pin.location, names=own, context=search_names(pin).context)
