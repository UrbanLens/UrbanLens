"""The one list of LLM chokepoints tests must never call for real.

Defined here rather than in either runner because there are two, and they kept
diverging. ``TestRunner`` patches these in ``setup_test_environment``, which
``manage.py test`` calls and **pytest never does** - pytest-django does not use
``TEST_RUNNER`` at all. So for as long as the patching lived only in the runner,
it protected the runner nobody uses, and every ordinary ``pytest`` run and (as of
2026-09-05) CI had no gateway patch at all.

That was never the only guard - ``settings/test.py`` pins every provider
credential to a placeholder, and the localhost-only network guard blocks the
socket - so this is defense in depth. It is defense in depth that was absent from
the path everyone takes.

Adding a chokepoint means adding it to :data:`AI_CHOKEPOINTS` and nowhere else.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import importlib
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Every method that can reach a provider over the network.
#:
#: ``send_prompt`` is the ``<ANSWER>``-protocol path every older ``LLMGateway``
#: feature uses. ``send_with_tools`` is the provider-native tool-calling path the
#: assistant's loop uses (``services/ai/assistant.py``); it was added after the
#: patching was written, and for a while the comment beside it said "the single
#: chokepoint" while the assistant's whole turn ran unpatched.
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

    The split between module and attribute is not written down in the target, so
    this walks it back from the longest importable prefix - the same thing
    `mock.patch` does internally.

    Args:
        target: A dotted path, e.g. ``"pkg.mod.Class.method"``.

    Returns:
        The attribute it names.

    Raises:
        ImportError: No prefix of ``target`` is an importable module.
    """
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

    The guard replaces every entry in :data:`AI_CHOKEPOINTS` with a Mock for the
    whole session, which is right for every test except the ones covering a
    chokepoint's own logic - those get the Mock instead of the method and assert
    against a call that never happened. Narrow by construction: it restores one
    named target and leaves the rest of the guard standing, so a test of
    ``send_with_tools`` still cannot reach a provider through ``send_prompt``.

    The restored method still cannot reach the network - the localhost-only
    socket guard and ``settings/test.py``'s placeholder credentials are both
    untouched - so a test using this must mock its own inference client.

    Args:
        target: One entry from :data:`AI_CHOKEPOINTS`.

    Yields:
        None, with ``target`` restored to the callable it names.

    Raises:
        ValueError: ``target`` is not a known chokepoint, which usually means a
            typo rather than an intent to unguard something else.
    """
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
