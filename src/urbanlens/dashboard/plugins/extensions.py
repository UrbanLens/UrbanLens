"""
Typed seams through which plugins extend core behaviour.

Core declares each seam once (see :mod:`urbanlens.dashboard.plugins.points`) and reads it where the behaviour lives;
plugins contribute to it from :meth:`~urbanlens.dashboard.plugins.base.UrbanLensPlugin.setup`. Core never names a
plugin: it reads whatever was contributed.

Three kinds of seam:

- :class:`ExtensionPoint` - an ordered collection of contributed values (providers, panels, routes, templates).
- :class:`FilterHook` - a value passed through every contributed callback in turn.
- :class:`ActionHook` - a notification every contributed callback receives.

Every contribution records the plugin that made it (``None`` for core) and a priority. Lower priorities come first;
a plugin's contributions default to its ``order``, so one plugin can be preferred over another wholesale (a data
provider registered at priority 10 is asked before the public fallback at 100) or per contribution.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import itertools
import logging
from typing import TYPE_CHECKING, Any, ClassVar, Concatenate, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

logger = logging.getLogger(__name__)


#: Priority of a contribution that names none and has no plugin ``order`` to inherit.
DEFAULT_PRIORITY = 100

#: How a hook treats a callback that raises: log it and carry on, or let it propagate to the caller.
OnError = Literal["skip", "raise"]

_sequence = itertools.count()


@dataclass(frozen=True, slots=True, order=True)
class Contribution[T]:
    """
    One value contributed to a seam.

    Attributes:
        priority: Lower comes first.
        sequence: Registration order, which breaks priority ties.
        value: The contributed value (a provider, a callback, a template name...).
        owner: Name of the contributing plugin, or None for core.
    """

    priority: int
    sequence: int
    value: T = field(compare=False)
    owner: str | None = field(default=None, compare=False)


class Seam[T]:
    """
    Base of every seam: a name, a description, and an ordered list of contributions.

    Every seam registers itself in a process-wide catalog by name, so the admin UI can list what a site's plugins
    contribute and so a template can find a slot by its name. Names must be unique.
    """

    kind: ClassVar[str] = "seam"

    def __init__(self, name: str, description: str = "") -> None:
        """
        Declare a seam.

        Raises:
            ValueError: When another seam already has this name.
        """
        if name in _catalog:
            raise ValueError(f"A seam named {name!r} is already declared")
        self.name = name
        self.description = description
        self._contributions: list[Contribution[T]] = []
        self._ordered: tuple[Contribution[T], ...] = ()
        _catalog[name] = self

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}: {len(self._contributions)} contribution(s)>"

    def contributions(self) -> tuple[Contribution[T], ...]:
        """Every contribution, in priority order."""
        return self._ordered

    def owners(self) -> list[str | None]:
        """The distinct contributors, in priority order; None stands for core."""
        return list(dict.fromkeys(contribution.owner for contribution in self._ordered))

    def discard(self, *, owner: str | None) -> int:
        """
        Remove every contribution one contributor made.

        Returns:
            How many were removed.
        """
        kept = [contribution for contribution in self._contributions if contribution.owner != owner]
        removed = len(self._contributions) - len(kept)
        if removed:
            self._replace(kept)
        return removed

    @contextmanager
    def overridden(self, values: Iterable[T]) -> Iterator[None]:
        """
        Hold exactly ``values`` (as core's contributions, in order) for the block, then restore what was there.

        For tests: a seam is process-wide, so a test that swaps what it holds must put it back.
        """
        saved = self._contributions
        self._replace([Contribution(priority=DEFAULT_PRIORITY, sequence=next(_sequence), value=value) for value in values])
        try:
            yield
        finally:
            self._replace(saved)

    def _contribute(self, value: T, *, owner: str | None, priority: int | None) -> Contribution[T]:
        contribution = Contribution(
            priority=DEFAULT_PRIORITY if priority is None else priority,
            sequence=next(_sequence),
            value=value,
            owner=owner,
        )
        self._replace([*self._contributions, contribution])
        return contribution

    def _remove_value(self, value: object) -> bool:
        kept = [contribution for contribution in self._contributions if contribution.value is not value]
        if len(kept) == len(self._contributions):
            return False
        self._replace(kept)
        return True

    def _replace(self, contributions: list[Contribution[T]]) -> None:
        # Readers take the tuple without a lock; it is swapped whole, never mutated.
        self._contributions = contributions
        self._ordered = tuple(sorted(contributions))


class ExtensionPoint[T](Seam[T]):
    """
    An ordered collection of values plugins contribute: providers in a fallback chain, panels on a page, routes.

    Reading is cheap (a cached tuple), so call sites read the point each time rather than caching what it held.
    """

    kind = "extension point"

    def add(self, value: T, *, owner: str | None = None, priority: int | None = None) -> T:
        """
        Contribute one value.

        Args:
            value: The value.
            owner: The contributing plugin's name; plugins contribute through their setup context, which sets it.
            priority: Lower comes first.

        Returns:
            The value, so a decorator can contribute a class and keep it.
        """
        self._contribute(value, owner=owner, priority=priority)
        return value

    def remove(self, value: T) -> bool:
        """
        Withdraw one value.

        Returns:
            True when it had been contributed.
        """
        return self._remove_value(value)

    def items(self) -> list[T]:
        """The contributed values, in priority order."""
        return [contribution.value for contribution in self._ordered]

    def __iter__(self) -> Iterator[T]:
        return (contribution.value for contribution in self._ordered)

    def __len__(self) -> int:
        return len(self._ordered)

    def __bool__(self) -> bool:
        return bool(self._ordered)

    def first(self) -> T | None:
        """The highest-priority value, or None when nothing is contributed."""
        return self._ordered[0].value if self._ordered else None


class Registry[K, T](Seam[tuple[K, T]]):
    """
    Values contributed under unique keys: rate-limit defaults per service, beat entries per name.

    When two contributors claim one key, the higher-priority claim wins and the other is logged and ignored.
    """

    kind = "registry"

    def add(self, key: K, value: T, *, owner: str | None = None, priority: int | None = None) -> T:
        """Contribute one value under a key, returning it so the call can decorate a definition."""
        self._contribute((key, value), owner=owner, priority=priority)
        return value

    def update(self, values: dict[K, T], *, owner: str | None = None, priority: int | None = None) -> None:
        """Contribute several values, each under its key."""
        for key, value in values.items():
            self.add(key, value, owner=owner, priority=priority)

    def as_dict(self) -> dict[K, T]:
        """Each key's winning value, in priority order of the claims."""
        merged: dict[K, T] = {}
        claimed_by: dict[K, str | None] = {}
        for contribution in self._ordered:
            key, value = contribution.value
            if key in merged:
                logger.warning(
                    "%s %r: %s claims %r, already claimed by %s; keeping the first claim",
                    self.kind,
                    self.name,
                    contribution.owner or "core",
                    key,
                    claimed_by[key] or "core",
                )
                continue
            merged[key] = value
            claimed_by[key] = contribution.owner
        return merged

    def get(self, key: K) -> T | None:
        """The winning value for one key, or None."""
        for contribution in self._ordered:
            claimed, value = contribution.value
            if claimed == key:
                return value
        return None


class Choices(Registry[str, str]):
    """
    An extensible set of ``(value, label)`` choices: feature flags, notification kinds, API-key scopes.

    Core contributes its own members; a plugin adds the ones its feature introduces. Code that names a member by
    value (``"example"``) keeps working whether or not the plugin that contributes it is installed - the value is simply
    absent from :meth:`choices` without it.
    """

    kind = "choices"

    def choices(self) -> list[tuple[str, str]]:
        """Every ``(value, label)`` pair, in priority order."""
        return list(self.as_dict().items())

    def values(self) -> frozenset[str]:
        """Every value."""
        return frozenset(self.as_dict())

    def label(self, value: str) -> str | None:
        """The label of one value, or None when no contributor declares it."""
        return self.get(value)


class _Hook(Seam["Callable[..., Any]"]):
    """Shared registration for filters and actions."""

    def __init__(self, name: str, description: str = "", *, on_error: OnError = "skip") -> None:
        """
        Declare a hook.

        Args:
            name: Unique hook name.
            description: What the hook is for, shown to plugin authors.
            on_error: ``"skip"`` logs a raising callback and carries on, so one broken plugin cannot break a request.
                ``"raise"`` propagates instead; declare a hook that way when skipping a callback would be unsafe,
                such as one that narrows what a viewer may see.
        """
        super().__init__(name, description)
        self.on_error = on_error

    def disconnect(self, callback: Callable[..., Any]) -> bool:
        """
        Remove a callback.

        Returns:
            True when it had been connected.
        """
        return self._remove_value(callback)


class FilterHook[T, **P](_Hook):
    """
    A value passed through every connected callback in priority order.

    Each callback receives the current value plus the hook's arguments and returns the next value. Declared as
    ``FilterHook[ValueType, [ArgTypes...]]`` so both sides are type-checked.
    """

    kind = "filter"

    def connect(self, callback: Callable[Concatenate[T, P], T], *, owner: str | None = None, priority: int | None = None) -> Callable[Concatenate[T, P], T]:
        """
        Connect a callback.

        Returns:
            The callback, so this works as a decorator.
        """
        self._contribute(callback, owner=owner, priority=priority)
        return callback

    def apply(self, value: T, *args: P.args, **kwargs: P.kwargs) -> T:
        """
        Pass ``value`` through every callback.

        Returns:
            The final value; ``value`` unchanged when nothing is connected.
        """
        for contribution in self._ordered:
            try:
                value = contribution.value(value, *args, **kwargs)
            except Exception:
                if self.on_error == "raise":
                    raise
                logger.exception("%s %r: callback %r failed", self.kind, self.name, contribution.value)
        return value


class ActionHook[**P](_Hook):
    """A notification every connected callback receives, in priority order."""

    kind = "action"

    def connect(self, callback: Callable[P, object], *, owner: str | None = None, priority: int | None = None) -> Callable[P, object]:
        """
        Connect a callback.

        Returns:
            The callback, so this works as a decorator.
        """
        self._contribute(callback, owner=owner, priority=priority)
        return callback

    def fire(self, *args: P.args, **kwargs: P.kwargs) -> None:
        """Call every connected callback."""
        for contribution in self._ordered:
            try:
                contribution.value(*args, **kwargs)
            except Exception:
                if self.on_error == "raise":
                    raise
                logger.exception("%s %r: callback %r failed", self.kind, self.name, contribution.value)


class TemplateSlot(ExtensionPoint[str]):
    """
    A place in a core template where plugins render fragments: ``{% plugin_slot "name" %}``.

    Contributions are template names, rendered in priority order with the page's context.
    """

    kind = "template slot"


_catalog: dict[str, Seam[Any]] = {}


def catalog() -> dict[str, Seam[Any]]:
    """Every declared seam by name, for the admin UI and for checks."""
    from urbanlens.dashboard.plugins import points  # noqa: F401 - declares the core seams

    return dict(_catalog)


def seam(name: str) -> Seam[Any] | None:
    """The seam declared under ``name``, or None."""
    return catalog().get(name)


def discard_contributions(*, owner: str | None) -> int:
    """
    Withdraw everything one contributor added to any seam.

    Returns:
        How many contributions were removed.
    """
    return sum(declared.discard(owner=owner) for declared in catalog().values())
