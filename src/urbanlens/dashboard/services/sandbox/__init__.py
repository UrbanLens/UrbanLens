"""Isolation of work that must not run next to the app's secrets.

Two things live here, and they are two halves of one rule:

- :mod:`~urbanlens.dashboard.services.sandbox.queues` says *where* isolated
  work runs - a queue per container, each container with its own reachability.
- :mod:`~urbanlens.dashboard.services.sandbox.guard` enforces that placement at
  runtime, so "decoding happens in the sandbox" stays a checkable property
  rather than a comment.

Today the only isolated tier is the media/parsing sandbox. Model inference is
the next one (:attr:`~urbanlens.dashboard.services.sandbox.queues.Queue.AI_INFERENCE`),
and it wants the same shape for a different reason - a GPU box with its own
resource envelope rather than a container that must not be trusted with
network access. Read ``docs/MEDIA_PIPELINE.md`` for the deployment topology.
"""

from urbanlens.dashboard.services.sandbox.guard import (
    ProcessRole,
    UnsandboxedParseError,
    UntrustedParsePolicy,
    allow_untrusted_parse,
    check_untrusted_parse,
    current_policy,
    current_role,
    untrusted_parse,
)
from urbanlens.dashboard.services.sandbox.queues import Queue, sandbox_queue

__all__ = [
    "ProcessRole",
    "Queue",
    "UnsandboxedParseError",
    "UntrustedParsePolicy",
    "allow_untrusted_parse",
    "check_untrusted_parse",
    "current_policy",
    "current_role",
    "sandbox_queue",
    "untrusted_parse",
]
