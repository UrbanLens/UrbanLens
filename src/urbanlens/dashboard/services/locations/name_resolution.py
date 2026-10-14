"""
Plugin-driven place-name candidates and the resolver that picks a winner.
The resolver is a strategy interface: :class:`RuleBasedNameResolver` is the default (source agreement first, then admin-configured priority), and a different arbiter can slot in behind :func:`default_name_resolver` without any caller changing."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.locations.name_tiers import DEFAULT_TIER, NameTier, NamingScope, rank_key
from urbanlens.dashboard.services.locations.naming import normalize_name_for_comparison

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NameCandidate:
    """
    One cleaned place-name candidate from one source.

    Attributes:
        name: The cleaned surface form of the candidate name.
        source: The provider slug the candidate came from.
        tier: What kind of name it is - see :mod:`~urbanlens.dashboard.services.locations.name_tiers`."""

    name: str
    source: str
    tier: NameTier = DEFAULT_TIER


class NameProvider:
    """One source of place-name candidates, contributed by a plugin."""

    def __init__(self, *, source: str, verbose_name: str = "", tier: NameTier | None = None, child_pin_preference: int | None = None) -> None:
        """
        Initialize the provider.

        Args:
            source: Stable slug identifying this source (e.g. ``"wikipedia"``).
            verbose_name: Human-readable name for admin UI; defaults to the slug.
            tier: What kind of name the source always gives; None for a source that does not declare one, whose names
                rank as :data:`~urbanlens.dashboard.services.locations.name_tiers.DEFAULT_TIER`.
            child_pin_preference: Set to have this source's candidate win outright when naming a detail (child) pin's
                location; lower wins among the sources that set it. A building child pin is named from its own building's record.
        """
        self.source = source
        self.verbose_name = verbose_name or source
        self.tier = tier
        self.child_pin_preference = child_pin_preference

    def stands_on_location(self, location: Location) -> bool:
        """
        Whether a building name from this source is the building the location's own page describes.

        Args:
            location: A location that describes one building.

        Returns:
            False when the source's building is a neighbour's, so its name must not name the page.
        """
        return True

    def stands_on_parcel(self, location: Location, parcel: Place) -> bool:
        """
        Whether a building name from this source is that of a building on ``parcel``.

        Args:
            location: The location being named.
            parcel: The property the location is on.

        Returns:
            False when the source's building is off the parcel.
        """
        return True

    def candidates(self, location: Location) -> list[str | None]:
        """
        Return raw name candidates for a location.
        Values are cleaned and quality-gated by the caller, so returning ``None`` or junk entries is acceptable.

        Args:
            location: The location to name.

        Returns:
            Raw candidate values in this provider's own preference order.
        """
        return []


class LocationCacheNameProvider(NameProvider):
    """Declarative provider reading top-level keys from a fresh LocationCache row."""

    def __init__(
        self,
        *,
        source: str,
        cache_source: str,
        keys: tuple[str, ...],
        verbose_name: str = "",
        tier: NameTier | None = None,
        child_pin_preference: int | None = None,
    ) -> None:
        """
        Initialize the provider.

        Args:
            source: Stable slug identifying this source.
            cache_source: The ``LocationCache.source`` value to read.
            keys: Top-level keys of the cached payload that may hold a name,
                in preference order.
            verbose_name: Human-readable name for admin UI; defaults to the slug.
            tier: See :class:`NameProvider`.
            child_pin_preference: See :class:`NameProvider`.
        """
        super().__init__(source=source, verbose_name=verbose_name, tier=tier, child_pin_preference=child_pin_preference)
        self.cache_source = cache_source
        self.keys = keys

    def candidates(self, location: Location) -> list[str | None]:
        """
        Read the configured keys from the location's fresh cache row.

        Args:
            location: The location to name.

        Returns:
            The raw values at each configured key, or an empty list when no fresh cache row exists or the payload is not a dict.
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
        """
        Pick the best candidate for a location.

        Args:
            candidates: Cleaned, quality-gated candidates in source-priority
                arrival order (plugin ``(order, name)`` order).
            location: The location being named, for resolvers that want
                address or geographic context.

        Returns:
            The winning candidate, or None when there is no acceptable one.
        """


class RuleBasedNameResolver(NameResolver):
    """
    Default resolver: the better tier wins, then source agreement, then priority, then arrival order.
    Candidates are grouped by :func:`~urbanlens.dashboard.services.locations.naming.normalize_name_for_comparison` so trivially different spellings of the same name count as agreement."""

    def __init__(self, priority: Sequence[str] = (), *, override_sources: Sequence[str] = (), scope: NamingScope = NamingScope.PARCEL) -> None:
        """
        Initialize the resolver.

        Args:
            priority: Source slugs in descending priority. Unknown slugs are
                ignored; sources missing from the list rank after listed ones.
            override_sources: Sources whose candidate wins outright, bypassing the agreement/priority ranking
                entirely: the first of them that has a candidate.
            scope: Whether the location names a property or one building, which orders the tiers.
        """
        self._priority_rank: dict[str, int] = {slug: rank for rank, slug in enumerate(priority)}
        self._override_sources = tuple(override_sources)
        self._scope = scope

    def _rank(self, source: str, arrival_index: int) -> tuple[int, int]:
        """Sort key for one source: listed sources first, then arrival order."""
        rank = self._priority_rank.get(source)
        if rank is None:
            return (1, arrival_index)
        return (0, rank)

    def resolve(self, candidates: Sequence[NameCandidate], location: Location | None) -> NameCandidate | None:
        """
        Pick the best candidate per the tier-then-agreement-then-priority rules.

        Args:
            candidates: Cleaned, quality-gated candidates in arrival order.
            location: The location being named (unused by this resolver).

        Returns:
            The winning candidate, or None when ``candidates`` is empty.
        """
        for source in self._override_sources:
            override = next((candidate for candidate in candidates if candidate.source == source), None)
            if override is not None:
                return override

        groups: dict[str, list[tuple[int, NameCandidate]]] = {}
        order: list[str] = []
        for index, candidate in enumerate(candidates):
            key = normalize_name_for_comparison(candidate.name)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append((index, candidate))

        best_key: str | None = None
        best_rank: tuple[int, ...] | None = None
        for seen, key in enumerate(order):
            members = groups[key]
            distinct_sources = {candidate.source for _index, candidate in members}
            source_rank = min(self._rank(candidate.source, index) for index, candidate in members)
            tier_rank = min(rank_key(candidate.tier, self._scope) for _index, candidate in members)
            rank = (*tier_rank, 0 if len(distinct_sources) >= 2 else 1, *source_rank, seen)
            if best_rank is None or rank < best_rank:
                best_key = key
                best_rank = rank

        if best_key is None:
            return None
        return min(groups[best_key], key=lambda item: (rank_key(item[1].tier, self._scope), self._rank(item[1].source, item[0])))[1]


def child_pin_preferred_sources() -> tuple[str, ...]:
    """The sources whose candidate wins outright when naming a detail (child) pin's location, most preferred first."""
    from urbanlens.dashboard.plugins.points import NAME_PROVIDERS

    preferring = [provider for provider in NAME_PROVIDERS if provider.child_pin_preference is not None]
    return tuple(provider.source for provider in sorted(preferring, key=lambda provider: provider.child_pin_preference or 0))


def default_name_resolver(profile: Profile | None = None, *, location: Location | None = None) -> NameResolver:
    """
    Return the resolver used for official-name selection.

    Args:
        profile: The profile whose action triggered this resolution, if any.
        location: The location being named, if known.

    Returns:
        The resolver to use for official-name selection."""
    from urbanlens.dashboard.models.site_settings.model import SiteSettings
    from urbanlens.dashboard.services.locations.name_tiers import naming_scope

    override_sources: tuple[str, ...] = ()
    if location is not None and location.pk and location.pins.filter(parent_pin__isnull=False).exists():
        override_sources = child_pin_preferred_sources()

    return RuleBasedNameResolver(SiteSettings.get_current().name_source_priority_list, override_sources=override_sources, scope=naming_scope(location))
