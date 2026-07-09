"""Plugin-driven place-name candidates and the resolver that picks a winner.

Plugins contribute :class:`NameProvider` objects (via
``UrbanLensPlugin.get_name_providers``) that yield raw name candidates for a
:class:`~urbanlens.dashboard.models.location.model.Location`, usually read
from :class:`~urbanlens.dashboard.models.cache.location_cache.LocationCache`
rows their panels already populate. The candidates are cleaned and
quality-gated in :mod:`urbanlens.dashboard.services.locations.naming`, then a
:class:`NameResolver` picks the official name.

The resolver is a strategy interface: :class:`RuleBasedNameResolver` is the
default (source agreement first, then admin-configured priority), and a future
AI-backed arbiter can slot in behind :func:`default_name_resolver` without any
caller changing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.locations.naming import normalize_name_for_comparison

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NameCandidate:
    """One cleaned place-name candidate from one source.

    Attributes:
        name: The cleaned surface form of the candidate name.
        source: The provider slug the candidate came from. Doubles as the
            alias ``source`` value when persisted and as the key looked up in
            the admin-configured priority list.
    """

    name: str
    source: str


class NameProvider:
    """One source of place-name candidates, contributed by a plugin.

    Providers are instantiated by plugins at discovery time and must not touch
    the database or the network in ``__init__``; :meth:`candidates` runs
    lazily at request/Celery time and should only read already-cached data
    (fetching happens in the plugin's panel/task machinery, not here).
    """

    def __init__(self, *, source: str, verbose_name: str = "") -> None:
        """Initialize the provider.

        Args:
            source: Stable slug identifying this source (e.g. ``"wikipedia"``).
            verbose_name: Human-readable name for admin UI; defaults to the slug.
        """
        self.source = source
        self.verbose_name = verbose_name or source

    def candidates(self, location: Location) -> list[str | None]:
        """Return raw name candidates for a location.

        Values are cleaned and quality-gated by the caller, so returning
        ``None`` or junk entries is acceptable.

        Args:
            location: The location to name.

        Returns:
            Raw candidate values in this provider's own preference order.
        """
        return []


class LocationCacheNameProvider(NameProvider):
    """Declarative provider reading top-level keys from a fresh LocationCache row.

    Covers the common case where a plugin's panel already caches an API payload
    per location and the place name lives at one or more top-level keys of
    that payload (e.g. Wikipedia's ``title``, NPS's ``fullName``).
    """

    def __init__(self, *, source: str, cache_source: str, keys: tuple[str, ...], verbose_name: str = "") -> None:
        """Initialize the provider.

        Args:
            source: Stable slug identifying this source.
            cache_source: The ``LocationCache.source`` value to read.
            keys: Top-level keys of the cached payload that may hold a name,
                in preference order.
            verbose_name: Human-readable name for admin UI; defaults to the slug.
        """
        super().__init__(source=source, verbose_name=verbose_name)
        self.cache_source = cache_source
        self.keys = keys

    def candidates(self, location: Location) -> list[str | None]:
        """Read the configured keys from the location's fresh cache row.

        Args:
            location: The location to name.

        Returns:
            The raw values at each configured key, or an empty list when no
            fresh cache row exists or the payload is not a dict.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        cached = LocationCache.get_fresh(location, self.cache_source)
        data = cached.data if cached else None
        if not isinstance(data, dict):
            return []
        return [data.get(key) for key in self.keys]


class NameResolver(ABC):
    """Strategy interface for choosing the best name among candidates."""

    @abstractmethod
    def resolve(self, candidates: Sequence[NameCandidate], location: Location) -> NameCandidate | None:
        """Pick the best candidate for a location.

        Args:
            candidates: Cleaned, quality-gated candidates in source-priority
                arrival order (plugin ``(order, name)`` order).
            location: The location being named, for resolvers that want
                address or geographic context.

        Returns:
            The winning candidate, or None when there is no acceptable one.
        """


class RuleBasedNameResolver(NameResolver):
    """Default resolver: source agreement beats priority, priority beats arrival order.

    Candidates are grouped by
    :func:`~urbanlens.dashboard.services.locations.naming.normalize_name_for_comparison`
    so trivially different spellings of the same name count as agreement.
    Groups are ranked by:

    1. Whether two or more distinct sources agree on the name (agreement wins
       over any single source, however prioritized).
    2. The best priority rank among the group's sources. Rank is the index in
       the configured priority list; sources not in the list rank after all
       listed ones, in arrival order.
    3. First-seen order, as a stable tiebreak.

    The winning group's surface form is the member from its highest-priority
    source. There are deliberately no numeric confidence scores - agreement
    count and admin-configured priority are the only signals.
    """

    def __init__(self, priority: Sequence[str] = ()) -> None:
        """Initialize the resolver.

        Args:
            priority: Source slugs in descending priority. Unknown slugs are
                ignored; sources missing from the list rank after listed ones.
        """
        self._priority_rank: dict[str, int] = {slug: rank for rank, slug in enumerate(priority)}

    def _rank(self, source: str, arrival_index: int) -> tuple[int, int]:
        """Sort key for one source: listed sources first, then arrival order."""
        rank = self._priority_rank.get(source)
        if rank is None:
            return (1, arrival_index)
        return (0, rank)

    def resolve(self, candidates: Sequence[NameCandidate], location: Location) -> NameCandidate | None:
        """Pick the best candidate per the agreement-then-priority rules.

        Args:
            candidates: Cleaned, quality-gated candidates in arrival order.
            location: The location being named (unused by this resolver).

        Returns:
            The winning candidate, or None when ``candidates`` is empty.
        """
        groups: dict[str, list[tuple[int, NameCandidate]]] = {}
        order: list[str] = []
        for index, candidate in enumerate(candidates):
            key = normalize_name_for_comparison(candidate.name)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append((index, candidate))

        best_key: str | None = None
        best_rank: tuple[int, int, int, int] | None = None
        for seen, key in enumerate(order):
            members = groups[key]
            distinct_sources = {candidate.source for _index, candidate in members}
            source_rank = min(self._rank(candidate.source, index) for index, candidate in members)
            rank = (0 if len(distinct_sources) >= 2 else 1, *source_rank, seen)
            if best_rank is None or rank < best_rank:
                best_key = key
                best_rank = rank

        if best_key is None:
            return None
        return min(groups[best_key], key=lambda item: self._rank(item[1].source, item[0]))[1]


def default_name_resolver() -> NameResolver:
    """Return the resolver used for official-name selection.

    This is the single seam where a future AI-backed resolver plugs in
    (e.g. switched by a SiteSettings choice); today it is always the
    rule-based resolver driven by the admin-configured source priority.

    Returns:
        The resolver to use for official-name selection.
    """
    from urbanlens.dashboard.models.site_settings.model import SiteSettings

    return RuleBasedNameResolver(SiteSettings.get_current().name_source_priority_list)
