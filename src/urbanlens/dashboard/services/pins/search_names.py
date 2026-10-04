"""Which names a pin's outbound searches may use, and which cached results each viewer may read.

A Location's *shared* names are the ones anyone who can see the place knows it by: its official name, and its wiki's
name and non-nickname aliases. A pin's *custom* names are its owner's: the pin's name and non-nickname aliases, less
anything restating a shared name, together with the names of the pins the owner filed it under. A search built from
shared names alone is cached once for every viewer of the Location (audience ``""``). A search built from custom names
is cached under a digest of that set, so it is read only by pins whose names produce the same digest.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

#: The audience of the row every viewer of a Location reads.
SHARED_AUDIENCE = ""


def normalize_search_name(name: str) -> str:
    """The form two names are compared in: casefolded, with whitespace collapsed and trimmed.

    Args:
        name: A place name.

    Returns:
        The normalised name.
    """
    return " ".join(name.casefold().split())


def _distinct(names: Iterable[str | None]) -> list[str]:
    """Meaningful names with whitespace collapsed, keeping the first of each normalised form, in order."""
    from urbanlens.dashboard.services.locations.naming import is_meaningful_name

    seen: set[str] = set()
    kept: list[str] = []
    for name in names:
        if not name or not is_meaningful_name(name):
            continue
        cleaned = " ".join(name.split())
        key = normalize_search_name(cleaned)
        if key not in seen:
            seen.add(key)
            kept.append(cleaned)
    return kept


def _canonical(names: Iterable[str]) -> tuple[str, ...]:
    """A name set in the one order every holder of it searches with: normalised, longest first."""
    distinct = {normalize_search_name(name) for name in _distinct(names)}
    return tuple(sorted(distinct, key=lambda name: (-len(name), name)))


@dataclass(frozen=True, slots=True)
class SearchScope:
    """One cached search: who may read it, and the names it is built from.

    Attributes:
        audience: The ``LocationCache.audience`` its results are cached under; ``""`` for the shared search.
        names: Names of the place, most specific first; empty when there is nothing to search for.
        context: Names of the pins the owner filed this one under, nearest first; empty for the shared search.
    """

    audience: str
    names: tuple[str, ...]
    context: tuple[str, ...] = ()

    @property
    def is_shared(self) -> bool:
        """Whether every viewer of the Location reads this search's results."""
        return self.audience == SHARED_AUDIENCE

    def provenance(self) -> dict[str, list[str]]:
        """The names that built this search, for the cached row, so its results can be judged against them."""
        return {"names": list(self.names), "context": list(self.context)}


#: The shared search of a source whose query uses no names at all.
SHARED_SCOPE = SearchScope(SHARED_AUDIENCE, ())


@dataclass(frozen=True, slots=True)
class SearchNames:
    """A pin's names, split by who may see what a search for them finds.

    Attributes:
        shared: The Location's public names, official name first.
        custom: The owner's own names that restate no shared name.
        context: Names of the pins the owner filed this one under, nearest first.
    """

    shared: tuple[str, ...]
    custom: tuple[str, ...]
    context: tuple[str, ...]

    @property
    def base(self) -> SearchScope:
        """The search every viewer of the Location shares."""
        return SearchScope(SHARED_AUDIENCE, self.shared)

    @property
    def own(self) -> SearchScope | None:
        """The search for the owner's own names, or None when they have none worth a search of their own.

        Keyed by the names it searches, so a nested pin searching the shared names reads a new row when they change.
        """
        custom = _canonical(self.custom)
        context = tuple(normalize_search_name(name) for name in _distinct(self.context))
        names = custom or (self.shared if context else ())
        if not names:
            return None
        key = json.dumps({"names": sorted(_canonical(names)), "context": list(context)}, separators=(",", ":"), ensure_ascii=False)
        return SearchScope(hashlib.sha256(key.encode()).hexdigest(), names, context)

    @property
    def audience(self) -> str:
        """The audience of the owner's own search, ``""`` when there is none."""
        own = self.own
        return own.audience if own is not None else SHARED_AUDIENCE

    @property
    def scopes(self) -> tuple[SearchScope, ...]:
        """Every search this pin reads, the shared one first."""
        own = self.own
        return (self.base,) if own is None else (self.base, own)


def shared_names(location: Location | None) -> tuple[str, ...]:
    """The names anyone who can see ``location`` knows it by.

    Args:
        location: The shared Location, or None.

    Returns:
        Its provider-sourced official name, then its wiki's name and non-nickname aliases, without repeats.
    """
    from urbanlens.dashboard.models.aliases.model import AliasType
    from urbanlens.dashboard.models.wiki.model import Wiki

    if location is None:
        return ()
    names: list[str | None] = [location.provider_name]
    wiki = Wiki.objects.existing_for_location(location) if location.pk else None
    if wiki is not None:
        names.append(wiki.name)
        names.extend(wiki.aliases.exclude(kind=AliasType.NICKNAME).values_list("name", flat=True))
    return tuple(_distinct(names))


def owner_label_scope(pin: Pin) -> SearchScope:
    """The names to label ``pin`` with for its owner, who may see all of them; never a search to cache.

    Args:
        pin: The owner's pin.

    Returns:
        Its meaningful official name, then its own name.
    """
    return SearchScope(SHARED_AUDIENCE, tuple(_distinct([pin.meaningful_official_name, pin.meaningful_name])))


_remembered: ContextVar[dict[int, SearchNames] | None] = ContextVar("remembered_search_names", default=None)


@contextmanager
def names_remembered() -> Iterator[None]:
    """Read each pin's search names once inside this block, for a caller asking every panel source about one pin."""
    token = _remembered.set({})
    try:
        yield
    finally:
        _remembered.reset(token)


def search_names(pin: Pin, *, shared: tuple[str, ...] | None = None) -> SearchNames:
    """Split ``pin``'s names into shared and custom.

    Args:
        pin: The pin whose searches are being built.
        shared: ``shared_names(pin.location)``, when the caller already has it.

    Returns:
        The pin's names, by who may see what a search for them finds.
    """
    from django.core.exceptions import ObjectDoesNotExist

    from urbanlens.dashboard.models.aliases.model import AliasType

    remembered = _remembered.get() if shared is None and pin.pk else None
    if remembered is not None and pin.pk in remembered:
        return remembered[pin.pk]
    try:
        location = pin.location
    except ObjectDoesNotExist:
        location = None
    if shared is None:
        shared = shared_names(location)
    own: list[str | None] = [pin.name]
    if pin.pk:
        own.extend(pin.aliases.exclude(kind=AliasType.NICKNAME).values_list("name", flat=True))
    taken = {normalize_search_name(name) for name in shared}
    custom = tuple(name for name in _canonical(name for name in own if name) if name not in taken)
    context = tuple(_distinct(pin.ancestor_search_names())) if pin.pk and pin.parent_pin_id else ()
    names = SearchNames(shared=shared, custom=custom, context=context)
    if remembered is not None:
        remembered[pin.pk] = names
    return names
