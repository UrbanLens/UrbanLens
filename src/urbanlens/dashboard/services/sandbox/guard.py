"""
Keeps parsing of untrusted uploads inside the container built to contain it.
So the mitigation is not detection, it is placement: decode somewhere that a successful exploit gains little."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
import functools
import logging
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from django.conf import settings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

#: Set inside :func:`allow_untrusted_parse`.
#: A ContextVar rather than a thread-local because ``celery-worker-panels`` runs a thread pool and
#: the async views share threads across requests - both would let a thread-local exemption leak into
#: unrelated work.
_exempt: ContextVar[str | None] = ContextVar("urbanlens_untrusted_parse_exempt", default=None)

#: Operations whose stack has already been logged in this process under the
#: ``warn`` policy. See :func:`check_untrusted_parse` for why the stack is worth
#: paying for once and not eight times per upload.
_stack_logged: set[str] = set()


class ProcessRole(StrEnum):
    """
    What this process is, as declared by ``UL_PROCESS_ROLE``.
    Only :attr:`SANDBOX` may parse untrusted uploads; the rest are named so a violation's log line says which container broke the rule rather than just "not the sandbox".

    Attributes:
        WEB: gunicorn, serving HTTP.
        WEBSOCKET: Daphne, serving WebSocket traffic.
        WORKER: The general-purpose Celery worker.
        PANELS: The external-data panel-fetch worker.
        BEAT: The Celery scheduler.
        SANDBOX: The isolated media/parsing worker.
        UNSPECIFIED: No ``UL_PROCESS_ROLE`` was set - a local checkout, a management command, or a container that predates this setting.

    Plugins name further roles; see :func:`is_locked_down_role`.
    """

    WEB = "web"
    WEBSOCKET = "websocket"
    WORKER = "worker"
    PANELS = "panels"
    BEAT = "beat"
    SANDBOX = "sandbox"
    UNSPECIFIED = "unspecified"


class UntrustedParsePolicy(StrEnum):
    """
    How strictly the sandbox boundary is enforced in this process.

    Attributes:
        ALLOW: No enforcement.
        WARN: Log a warning naming the operation and the offending role, then proceed.
        DENY: Raise :class:`UnsandboxedParseError`."""

    ALLOW = "allow"
    WARN = "warn"
    DENY = "deny"


class UnsandboxedParseError(RuntimeError):
    """
    An untrusted-parse operation was attempted outside the sandbox worker.
    Raised rather than logged so the violation surfaces where it was introduced."""


def _plugin_roles() -> frozenset[str]:
    """The ``UL_PROCESS_ROLE`` values plugins name, as locked-down roles or as the tier a database role belongs to."""
    from urbanlens.dashboard.plugins.points import DATABASE_ROLES, LOCKED_DOWN_PROCESS_ROLES

    return frozenset(LOCKED_DOWN_PROCESS_ROLES.items()) | {role.process_role for role in DATABASE_ROLES.items()}


def current_role() -> str:
    """
    The role of the process making this call.

    Returns:
        The ``UL_PROCESS_ROLE`` value when this version or a plugin knows it, else :attr:`ProcessRole.UNSPECIFIED`: an unrecognised role is some other container, never the sandbox."""
    raw = str(getattr(settings, "UL_PROCESS_ROLE", "") or "").strip().lower()
    try:
        return ProcessRole(raw)
    except ValueError:
        return raw if raw in _plugin_roles() else ProcessRole.UNSPECIFIED


def is_locked_down_role(role: str) -> bool:
    """
    Whether *role* is a process of reduced trust, which the plugins that run one declare through ``LOCKED_DOWN_PROCESS_ROLES``.

    Args:
        role: A value :func:`current_role` returned.

    Returns:
        True when such a process must deny untrusted parsing and never reach credentialled data services."""
    from urbanlens.dashboard.plugins.points import LOCKED_DOWN_PROCESS_ROLES

    return role in LOCKED_DOWN_PROCESS_ROLES.items()


def current_policy() -> UntrustedParsePolicy:
    """
    How this process should react to an out-of-sandbox parse.

    Returns:
        :attr:`UntrustedParsePolicy.DENY` unconditionally in a locked-down role, regardless of ``UL_UNTRUSTED_PARSE_POLICY`` - the worker's compose env sets that variable to ``deny`` too, but the tool loop calls arbitrary, registry-dispatched..."""
    if is_locked_down_role(current_role()):
        return UntrustedParsePolicy.DENY
    raw = str(getattr(settings, "UL_UNTRUSTED_PARSE_POLICY", "") or "").strip().lower()
    try:
        return UntrustedParsePolicy(raw)
    except ValueError:
        return UntrustedParsePolicy.WARN


@contextmanager
def allow_untrusted_parse(reason: str) -> Iterator[None]:
    """
    Permit untrusted-parse calls inside this block.
    For the cases where a parser is pointed at bytes the server itself produced - a thumbnail this app encoded, a test fixture - rather than at something a user uploaded.

    Args:
        reason: Why these bytes are not untrusted.

    Yields:
        None.
    """
    token = _exempt.set(reason)
    try:
        yield
    finally:
        _exempt.reset(token)


def check_untrusted_parse(operation: str) -> None:
    """
    Enforce the sandbox boundary for one operation, without decorating it.

    Args:
        operation: What is about to be parsed, e.g. ``"image.decode"``.

    Raises:
        UnsandboxedParseError: The policy is ``deny`` and this process is not the sandbox worker."""
    if (exemption := _exempt.get()) is not None:
        logger.debug("Untrusted-parse exemption used for %s: %s", operation, exemption)
        return

    policy = current_policy()
    if policy is UntrustedParsePolicy.ALLOW:
        return

    role = current_role()
    if role == ProcessRole.SANDBOX:
        return

    message = f"Untrusted-parse operation {operation!r} ran in the {role!r} process, which is not the sandbox worker"
    if policy is UntrustedParsePolicy.DENY:
        raise UnsandboxedParseError(message)

    # The stack is what makes the warn log a worklist - the operation name alone says a decoder ran
    # in the wrong container, not which caller put it there.
    # But it is also the expensive part (formatting a full traceback), and these helpers are called
    # eight times per photo: a bulk import would spend real time producing thousands of copies of
    first_time = operation not in _stack_logged
    _stack_logged.add(operation)
    logger.warning("%s (policy=warn)", message, stack_info=first_time)


def untrusted_parse(operation: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Mark a function as handing untrusted bytes to a parser.
    Decorated functions run only in the sandbox worker (or under an explicit :func:`allow_untrusted_parse` block).

    Args:
        operation: A stable dotted name for the parse, e.g. ``"image.exif"``, ``"video.probe"``, ``"archive.zip"``.

    Returns:
        A decorator preserving the wrapped function's signature."""

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            check_untrusted_parse(operation)
            return func(*args, **kwargs)

        return wrapper

    return decorate
