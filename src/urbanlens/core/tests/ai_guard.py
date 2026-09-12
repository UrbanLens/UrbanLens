"""The one list of LLM chokepoints tests must never call for real."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import importlib
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Every method that can reach a provider over the network.
#: ``send_prompt`` is the ``<ANSWER>``-protocol path every older ``LLMGateway`` feature uses.
AI_CHOKEPOINTS: tuple[str, ...] = (
    "urbanlens.dashboard.services.ai.gateway.LLMGateway.send_prompt",
    "urbanlens.dashboard.services.ai.gateway.LLMGateway.send_with_tools",
)


#: The real callables, captured as the guard goes on. A test whose *subject* is
#: one of these methods needs the method itself, not the Mock standing in for it,
#: and cannot recover it once the session-scoped patch is in place.
_ORIGINALS: dict[str, Any] = {}


def _resolve(target: str) -> Any:
    """Return the attribute a dotted `mock.patch` target names.

    The split between module and attribute is not written down in the target, so this walks it back from the
    longest importable prefix - the same thing `mock.patch` does internally.

    Args:
        target: A dotted path, e.g.

    Returns:
        The attribute it names.

    Raises:
        ImportError: No prefix of ``target`` is an importable module."""
    parts = target.split(".")
    for split in range(len(parts) - 1, 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        for name in parts[split:]:
            obj = getattr(obj, name)
        return obj
    raise ImportError(target)


@contextmanager
def patched_ai_gateway() -> Iterator[None]:
    """Stop every AI chokepoint from reaching a provider, for the duration.

    Yields:
        None, with every entry in :data:`AI_CHOKEPOINTS` patched to return None.
    """
    with ExitStack() as stack:
        for target in AI_CHOKEPOINTS:
            _ORIGINALS.setdefault(target, _resolve(target))
            stack.enter_context(patch(target, return_value=None))
        yield


@contextmanager
def real_ai_chokepoint(target: str) -> Iterator[None]:
    """Put one chokepoint back, for a test whose subject is that method itself.

    The guard replaces every entry in :data:`AI_CHOKEPOINTS` with a Mock for the whole session, which is right
    for every test except the ones covering a chokepoint's own logic - those get the Mock instead of the method
    and assert against a call that never happened.

    Args:
        target: One entry from :data:`AI_CHOKEPOINTS`.

    Yields:
        None, with ``target`` restored to the callable it names.

    Raises:
        ValueError: ``target`` is not a known chokepoint, which usually means a typo rather than an intent to unguard something else."""
    if target not in AI_CHOKEPOINTS:
        raise ValueError(f"{target!r} is not one of AI_CHOKEPOINTS")
    original = _ORIGINALS.get(target)
    if original is None:
        # The guard never went on (a bare import, or a runner that skips it);
        # nothing to restore.
        yield
        return
    with patch(target, original):
        yield
