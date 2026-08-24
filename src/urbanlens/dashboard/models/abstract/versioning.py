"""Write-source tracking: who or what is making the writes on this thread.

Provenance has to be recorded at write time - inferring it afterwards from edit
history does not work, because three writers already bypass the history
entirely (a bulk ``update()``, a bare ``save()``, and one that omits
``updated`` from ``update_fields``). See ``docs/designs/versioned-content.md``.

Requiring every call site to declare its source would not survive contact with
several hundred of them, so the source is **inferred from context** and only
declared where the inference is wrong:

- inside an authenticated request  -> ``USER``
- inside a Celery task             -> ``AUTOMATIC``
- anywhere else (shell, migration) -> ``SYSTEM``

``writing_as`` overrides it for the exceptions - enrichment kicked off inline
from a request, or a management command acting for a person.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.abstract.choices import TextChoices

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


class WriteSource(TextChoices):
    """Where a write came from.

    Deliberately three values. A merge resolution is a *reason* rather than a
    source - both user-driven and automatic merge resolution are possible, and
    the provenance of the things being merged is its own question - so that
    belongs in a separate field when merge work starts, not here.
    """

    USER = "user", "User"
    AUTOMATIC = "automatic", "Automatic"
    SYSTEM = "system", "System"


#: The source in force on this thread/task. None means "not inside a context
#: that declared one", which resolves to SYSTEM.
_write_source: ContextVar[str | None] = ContextVar("ul_write_source", default=None)

#: The profile a write is attributable to, when there is one.
_write_actor: ContextVar[int | None] = ContextVar("ul_write_actor", default=None)

#: Set while a deliberately unversioned write is running.
_unversioned: ContextVar[bool] = ContextVar("ul_unversioned", default=False)


def current_write_source() -> str:
    """Return the write source in force, defaulting to SYSTEM."""
    return _write_source.get() or WriteSource.SYSTEM


def current_write_actor() -> int | None:
    """Return the profile pk a write is attributable to, if any."""
    return _write_actor.get()


def is_unversioned() -> bool:
    """Whether the current write is deliberately exempt from versioning."""
    return _unversioned.get()


def bind_write_source(source: str, *, actor: int | None = None) -> None:
    """Set the write source for the rest of this context, without a block.

    For entry points that own their whole context and have no natural place to
    wrap - a Celery task, which gets a fresh context per run. Prefer
    :func:`writing_as` anywhere a block is possible, because it restores the
    previous value.

    Args:
        source: A :class:`WriteSource` value.
        actor: Profile pk to attribute writes to, when the source is USER.
    """
    _write_source.set(source)
    _write_actor.set(actor)


@contextlib.contextmanager
def writing_as(source: str, *, actor: int | None = None) -> Iterator[None]:
    """Declare the write source for the enclosed block.

    Args:
        source: A :class:`WriteSource` value.
        actor: Profile pk to attribute writes to, when the source is USER.

    Yields:
        None.
    """
    source_token = _write_source.set(source)
    actor_token = _write_actor.set(actor)
    try:
        yield
    finally:
        _write_source.reset(source_token)
        _write_actor.reset(actor_token)


@contextlib.contextmanager
def unversioned(*, reason: str) -> Iterator[None]:
    """Run writes without recording revisions.

    For migrations and backfills, which legitimately rewrite history rather
    than extending it. Logs at WARNING and is greppable on purpose: an
    unversioned write is a hole in the provenance record, and one that nobody
    can find later is worse than the write itself.

    Args:
        reason: Why this block is exempt. Recorded in the log line.

    Yields:
        None.
    """
    logger.warning("Versioning suppressed: %s", reason)
    token = _unversioned.set(True)
    try:
        yield
    finally:
        _unversioned.reset(token)
