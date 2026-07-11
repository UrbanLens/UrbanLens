"""Actions and filters for UrbanLens plugins.

A small WordPress-style hook bus. Core code declares extension points by
firing named hooks; plugins attach callbacks to those names:

* **Actions** are fire-and-forget notifications (``do_action``); callbacks
  receive the arguments and their return values are ignored.
* **Filters** transform a value (``apply_filters``); each callback receives
  the current value (plus any extra arguments) and returns the next value.

Callbacks run in ascending ``priority`` order (default 10), with registration
order breaking ties. A callback that raises is logged and skipped so a broken
plugin can never take down a request; for filters the value simply passes
through unchanged to the next callback.

Most plugin integration should use the typed contribution methods on
:class:`~urbanlens.dashboard.plugins.base.UrbanLensPlugin` instead - the hook
bus exists for extension points that don't warrant a dedicated method, and
for plugins that need to react to lifecycle events (e.g. the
``plugins_loaded`` action fired after discovery).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import itertools
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Default priority for hook callbacks. Lower runs earlier.
DEFAULT_PRIORITY = 10

#: Action fired once after plugin discovery completes. Receives the
#: :class:`~urbanlens.dashboard.plugins.registry.PluginRegistry` instance.
ACTION_PLUGINS_LOADED = "plugins_loaded"


@dataclass(frozen=True, slots=True, order=True)
class _HookCallback:
    """One registered callback, ordered by (priority, registration sequence)."""

    priority: int
    sequence: int
    callback: Callable[..., Any] = field(compare=False)


class HookRegistry:
    """Named action and filter hooks with priority-ordered callbacks.

    Attributes are internal; use the ``add_*``/``remove_*``/``do_action``/
    ``apply_filters`` methods. A module-level singleton, :data:`hooks`, is the
    instance shared by core code and plugins.
    """

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._actions: dict[str, list[_HookCallback]] = {}
        self._filters: dict[str, list[_HookCallback]] = {}
        self._sequence = itertools.count()

    def add_action(self, name: str, callback: Callable[..., Any], *, priority: int = DEFAULT_PRIORITY) -> None:
        """Register a callback for an action hook.

        Args:
            name: The action hook name (e.g. ``"plugins_loaded"``).
            callback: Called with the arguments passed to :meth:`do_action`.
            priority: Execution order; lower runs earlier. Defaults to 10.
        """
        self._add(self._actions, name, callback, priority)

    def add_filter(self, name: str, callback: Callable[..., Any], *, priority: int = DEFAULT_PRIORITY) -> None:
        """Register a callback for a filter hook.

        Args:
            name: The filter hook name.
            callback: Called with the current value (plus any extra arguments
                passed to :meth:`apply_filters`); must return the next value.
            priority: Execution order; lower runs earlier. Defaults to 10.
        """
        self._add(self._filters, name, callback, priority)

    def remove_action(self, name: str, callback: Callable[..., Any]) -> bool:
        """Unregister an action callback.

        Args:
            name: The action hook name.
            callback: The exact callable previously registered.

        Returns:
            True when a registration was found and removed.
        """
        return self._remove(self._actions, name, callback)

    def remove_filter(self, name: str, callback: Callable[..., Any]) -> bool:
        """Unregister a filter callback.

        Args:
            name: The filter hook name.
            callback: The exact callable previously registered.

        Returns:
            True when a registration was found and removed.
        """
        return self._remove(self._filters, name, callback)

    def do_action(self, name: str, *args: Any, **kwargs: Any) -> None:
        """Run every callback registered for an action hook.

        Callbacks run in priority order; exceptions are logged and swallowed
        so one broken plugin cannot break the others or the caller.

        Args:
            name: The action hook name.
            *args: Positional arguments passed to each callback.
            **kwargs: Keyword arguments passed to each callback.
        """
        for entry in sorted(self._actions.get(name, [])):
            try:
                entry.callback(*args, **kwargs)
            except Exception:
                logger.exception("Action hook '%s' callback %r failed", name, entry.callback)

    def apply_filters(self, name: str, value: Any, *args: Any, **kwargs: Any) -> Any:
        """Pass a value through every callback registered for a filter hook.

        Callbacks run in priority order, each receiving the previous
        callback's return value. A callback that raises is logged and skipped;
        the value flows through unchanged to the next callback.

        Args:
            name: The filter hook name.
            value: The initial value to filter.
            *args: Extra positional arguments passed to each callback.
            **kwargs: Extra keyword arguments passed to each callback.

        Returns:
            The value after all callbacks have been applied.
        """
        for entry in sorted(self._filters.get(name, [])):
            try:
                value = entry.callback(value, *args, **kwargs)
            except Exception:
                logger.exception("Filter hook '%s' callback %r failed", name, entry.callback)
        return value

    def clear(self, name: str | None = None) -> None:
        """Remove registered callbacks, primarily for test isolation.

        Args:
            name: Clear only this hook name (actions and filters); clear
                everything when None.
        """
        if name is None:
            self._actions.clear()
            self._filters.clear()
            return
        self._actions.pop(name, None)
        self._filters.pop(name, None)

    def _add(self, table: dict[str, list[_HookCallback]], name: str, callback: Callable[..., Any], priority: int) -> None:
        """Append a callback to one hook table."""
        table.setdefault(name, []).append(_HookCallback(priority=priority, sequence=next(self._sequence), callback=callback))

    @staticmethod
    def _remove(table: dict[str, list[_HookCallback]], name: str, callback: Callable[..., Any]) -> bool:
        """Remove all registrations of a callback from one hook table."""
        entries = table.get(name, [])
        remaining = [entry for entry in entries if entry.callback is not callback]
        if len(remaining) == len(entries):
            return False
        table[name] = remaining
        return True


#: The shared hook registry used by core code and every plugin.
hooks = HookRegistry()
