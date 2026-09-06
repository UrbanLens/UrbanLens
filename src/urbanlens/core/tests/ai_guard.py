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
from typing import TYPE_CHECKING
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


@contextmanager
def patched_ai_gateway() -> Iterator[None]:
    """Stop every AI chokepoint from reaching a provider, for the duration.

    Yields:
        None, with every entry in :data:`AI_CHOKEPOINTS` patched to return None.
    """
    with ExitStack() as stack:
        for target in AI_CHOKEPOINTS:
            stack.enter_context(patch(target, return_value=None))
        yield
