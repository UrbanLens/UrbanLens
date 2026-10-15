"""The guards plugins contribute to ``points.TEST_GUARDS``, entered for a whole test run."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def plugin_test_guards() -> Iterator[None]:
    """
    Enter every contributed guard, in priority order, and leave them in reverse when the run ends.

    Yields:
        None, with every guard active.
    """
    from urbanlens.dashboard.plugins.points import TEST_GUARDS

    with ExitStack() as stack:
        for guard in TEST_GUARDS.items():
            stack.enter_context(guard())
        yield
