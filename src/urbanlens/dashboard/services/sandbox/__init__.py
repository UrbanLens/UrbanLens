"""Isolation of work that must not run next to the app's secrets.

Two things live here, and they are two halves of one rule:

- :mod:`~urbanlens.dashboard.services.sandbox.queues` says *where* isolated
  work runs - a queue per container, each container with its own reachability.
- :mod:`~urbanlens.dashboard.services.sandbox.guard` enforces that placement at
  runtime, so "decoding happens in the sandbox" stays a checkable property
  rather than a comment.

A second, differently-shaped tier lives alongside it: AI provider calls run
in ``ai-inference`` (Django-free, holds provider keys and nothing else), and
the tool loop that calls it runs in ``ai-worker`` (draining ``Queue.AI``) -
isolated because a provider SDK CVE or a compromised model response should
have nothing to steal, not because the input is untrusted bytes. See
:func:`~urbanlens.dashboard.services.sandbox.guard.check_direct_inference`
and ``docs/AI_PIPELINE.md`` for that topology; ``docs/MEDIA_PIPELINE.md``
covers the media/parsing sandbox this module started with.
"""

from urbanlens.dashboard.services.sandbox.guard import (
    DirectInferenceError,
    DirectInferencePolicy,
    ProcessRole,
    UnsandboxedParseError,
    UntrustedParsePolicy,
    allow_untrusted_parse,
    check_direct_inference,
    check_untrusted_parse,
    current_direct_inference_policy,
    current_policy,
    current_role,
    untrusted_parse,
)
from urbanlens.dashboard.services.sandbox.queues import Queue, ai_queue, sandbox_queue

__all__ = [
    "DirectInferenceError",
    "DirectInferencePolicy",
    "ProcessRole",
    "Queue",
    "UnsandboxedParseError",
    "UntrustedParsePolicy",
    "ai_queue",
    "allow_untrusted_parse",
    "check_direct_inference",
    "check_untrusted_parse",
    "current_direct_inference_policy",
    "current_policy",
    "current_role",
    "sandbox_queue",
    "untrusted_parse",
]
