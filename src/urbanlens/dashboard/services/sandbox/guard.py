"""Keeps parsing of untrusted uploads inside the container built to contain it.

The threat this addresses is not malware. A signature scanner (``services.security.malware_scan``)
catches a trojan renamed ``photo.jpg``; it has nothing to say about a
well-formed file engineered to corrupt memory inside the library that decodes
it. CVE-2023-4863 - a heap overflow reachable by handing libwebp an ordinary
looking WebP - is the shape of the problem: no payload to match, no signature to
write, and every process that decodes the file is a target. The same is true of
ffmpeg's container parsers, LibreOffice's document filters, GDAL's format
drivers, and every other parser this app points at bytes a stranger uploaded.

So the mitigation is not detection, it is placement: decode somewhere that a
successful exploit gains little. ``media-worker`` (docker-compose.yml) joins
only an ``internal`` Docker network, carries no third-party API keys, drops
every Linux capability, and is bounded well below the app's own limits. An
exploit there lands in a process with no route off the host and nothing worth
stealing in its environment.

Placement only holds if it is checked. This module is that check: every
function that hands untrusted bytes to a parser is decorated with
:func:`untrusted_parse`, which refuses to run outside the sandbox when
``UL_UNTRUSTED_PARSE_POLICY`` is ``deny``. Without it the boundary is a claim
about where code *currently* gets called from, and the first view that calls
``extract_exif_data`` directly quietly moves a decoder back into gunicorn with
nothing to notice.

The policy is a setting rather than a constant because the boundary is enforced
at different strengths in different places: ``deny`` in the real deployment,
``allow`` under pytest (which calls these functions directly, by design), and
``warn`` for a rollout that wants the log lines before the failures.

Legitimate exceptions use :func:`allow_untrusted_parse`, which is deliberately
noisy to write and scoped to a block - an exemption should be visible in review,
not a flag someone sets globally and forgets.
"""

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

#: Set inside :func:`allow_untrusted_parse`. A ContextVar rather than a
#: thread-local because ``celery-worker-panels`` runs a thread pool and the
#: async views share threads across requests - both would let a thread-local
#: exemption leak into unrelated work.
_exempt: ContextVar[str | None] = ContextVar("urbanlens_untrusted_parse_exempt", default=None)

#: Operations whose stack has already been logged in this process under the
#: ``warn`` policy. See :func:`check_untrusted_parse` for why the stack is worth
#: paying for once and not eight times per upload.
_stack_logged: set[str] = set()


class ProcessRole(StrEnum):
    """What this process is, as declared by ``UL_PROCESS_ROLE``.

    Set per service in ``docker-compose.yml``. Only :attr:`SANDBOX` may parse
    untrusted uploads; the rest are named so a violation's log line says which
    container broke the rule rather than just "not the sandbox".

    Attributes:
        WEB: gunicorn, serving HTTP.
        WEBSOCKET: Daphne, serving WebSocket traffic.
        WORKER: The general-purpose Celery worker.
        PANELS: The external-data panel-fetch worker.
        BEAT: The Celery scheduler.
        SANDBOX: The isolated media/parsing worker. The only role allowed to
            hand untrusted bytes to a parser.
        AI: The Celery worker draining the AI tool-loop queue. Holds DB and
            encryption-key credentials (it reads encrypted content) but no
            REData/OAuth/provider credentials - see
            ``docs/AI_PIPELINE.md`` and :func:`check_direct_inference`.
        INFERENCE: The Django-free ``ai-inference`` service. Never runs
            Django, so this value is never actually read via
            :func:`current_role` - it exists so ``UL_PROCESS_ROLE=inference``
            in that service's compose entry documents itself against the
            same enum every other role is named from.
        UNSPECIFIED: No ``UL_PROCESS_ROLE`` was set - a local checkout, a
            management command, or a container that predates this setting.
    """

    WEB = "web"
    WEBSOCKET = "websocket"
    WORKER = "worker"
    PANELS = "panels"
    BEAT = "beat"
    SANDBOX = "sandbox"
    AI = "ai"
    INFERENCE = "inference"
    UNSPECIFIED = "unspecified"


class UntrustedParsePolicy(StrEnum):
    """How strictly the sandbox boundary is enforced in this process.

    Attributes:
        ALLOW: No enforcement. What pytest runs under, since the test suite
            calls the parsers directly.
        WARN: Log a warning naming the operation and the offending role, then
            proceed. For a deployment that wants to find violations before it
            starts failing on them.
        DENY: Raise :class:`UnsandboxedParseError`.
    """

    ALLOW = "allow"
    WARN = "warn"
    DENY = "deny"


class UnsandboxedParseError(RuntimeError):
    """An untrusted-parse operation was attempted outside the sandbox worker.

    Raised rather than logged so the violation surfaces where it was introduced.
    If the call is genuinely safe - the bytes are this server's own, not a
    user's - wrap it in :func:`allow_untrusted_parse` with a reason.
    """


class DirectInferencePolicy(StrEnum):
    """How strictly a direct, in-process AI provider call is enforced.

    Mirrors :class:`UntrustedParsePolicy` for the same reason: the boundary
    is "provider API keys live only in the ``ai-inference`` process", and
    :class:`~urbanlens.dashboard.services.ai.inference_client.LocalInferenceClient`
    - the one thing this policy gates - is a deliberate escape hatch for
    local development, not a code path any deployed container should reach.

    Attributes:
        ALLOW: No enforcement. What pytest and a bare local checkout run
            under - there is no ``ai-inference`` container to talk to.
        WARN: Log a warning naming the role that made the call, then proceed.
        DENY: Raise :class:`DirectInferenceError`.
    """

    ALLOW = "allow"
    WARN = "warn"
    DENY = "deny"


class DirectInferenceError(RuntimeError):
    """A direct, in-process AI provider call was attempted from a deployed role.

    Raised rather than logged so the violation surfaces where it was
    introduced: a role-tagged container reaching this path almost always
    means ``UL_AI_INFERENCE_URL`` was left unset, and the alternative is a
    provider API key silently loaded into a container that was never meant
    to hold one.
    """


def current_role() -> ProcessRole:
    """The role of the process making this call.

    Returns:
        The ``UL_PROCESS_ROLE`` value, or :attr:`ProcessRole.UNSPECIFIED` when
        it is unset or not a role this version knows about. An unrecognised
        value is *not* an error: rolling a new role name out to compose before
        the code that understands it should degrade to "some other container",
        not crash the container.
    """
    raw = str(getattr(settings, "UL_PROCESS_ROLE", "") or "").strip().lower()
    try:
        return ProcessRole(raw)
    except ValueError:
        return ProcessRole.UNSPECIFIED


def current_policy() -> UntrustedParsePolicy:
    """How this process should react to an out-of-sandbox parse.

    Returns:
        The configured policy, defaulting to :attr:`UntrustedParsePolicy.WARN`
        for an unset or unrecognised value - the setting is a safety rail, and
        a typo in it should not silently disable the rail *or* take the site
        down.
    """
    raw = str(getattr(settings, "UL_UNTRUSTED_PARSE_POLICY", "") or "").strip().lower()
    try:
        return UntrustedParsePolicy(raw)
    except ValueError:
        return UntrustedParsePolicy.WARN


def current_direct_inference_policy() -> DirectInferencePolicy:
    """How this process should react to a direct, in-process AI provider call.

    Returns:
        The configured policy, defaulting to :attr:`DirectInferencePolicy.WARN`
        for an unset or unrecognised value - same rationale as
        :func:`current_policy`.
    """
    raw = str(getattr(settings, "UL_DIRECT_INFERENCE_POLICY", "") or "").strip().lower()
    try:
        return DirectInferencePolicy(raw)
    except ValueError:
        return DirectInferencePolicy.WARN


def check_direct_inference() -> None:
    """Enforce that a direct, in-process AI provider call only happens outside a deployed role.

    Every role-tagged container (``web``, ``worker``, ``ai``, ...) is
    expected to have ``UL_AI_INFERENCE_URL`` configured and go through
    :class:`~urbanlens.dashboard.services.ai.inference_client.RemoteInferenceClient`
    instead; :attr:`ProcessRole.UNSPECIFIED` (a local checkout, a management
    command) is the one role this call is expected from, since it has no
    ``ai-inference`` container to talk to.

    Raises:
        DirectInferenceError: The policy is ``deny`` and this process has a
            real ``UL_PROCESS_ROLE``.
    """
    policy = current_direct_inference_policy()
    if policy is DirectInferencePolicy.ALLOW:
        return

    role = current_role()
    if role is ProcessRole.UNSPECIFIED:
        return

    message = f"A direct, in-process AI provider call ran in the {role.value!r} process instead of going through ai-inference"
    if policy is DirectInferencePolicy.DENY:
        raise DirectInferenceError(message)

    logger.warning("%s (policy=warn)", message)


@contextmanager
def allow_untrusted_parse(reason: str) -> Iterator[None]:
    """Permit untrusted-parse calls inside this block.

    For the cases where a parser is pointed at bytes the server itself
    produced - a thumbnail this app encoded, a test fixture - rather than at
    something a user uploaded. The reason is recorded on the exemption and
    logged at debug level, so a grep for this function turns up *why* each
    exemption exists rather than just where.

    Not a way to run user uploads on the web process. If the bytes came from
    outside, the work belongs on the sandbox queue.

    Args:
        reason: Why these bytes are not untrusted. Written into the log line.

    Yields:
        None.
    """
    token = _exempt.set(reason)
    try:
        yield
    finally:
        _exempt.reset(token)


def check_untrusted_parse(operation: str) -> None:
    """Enforce the sandbox boundary for one operation, without decorating it.

    The body of :func:`untrusted_parse`, exposed separately for a caller that
    parses inline - a ``subprocess`` invocation, say - and has no single
    function to decorate.

    Args:
        operation: What is about to be parsed, e.g. ``"image.decode"``. Used in
            the log line and the exception message, so name the *parser*, not
            the caller.

    Raises:
        UnsandboxedParseError: The policy is ``deny`` and this process is not
            the sandbox worker.
    """
    if (exemption := _exempt.get()) is not None:
        logger.debug("Untrusted-parse exemption used for %s: %s", operation, exemption)
        return

    policy = current_policy()
    if policy is UntrustedParsePolicy.ALLOW:
        return

    role = current_role()
    if role is ProcessRole.SANDBOX:
        return

    message = f"Untrusted-parse operation {operation!r} ran in the {role.value!r} process, which is not the sandbox worker"
    if policy is UntrustedParsePolicy.DENY:
        raise UnsandboxedParseError(message)

    # The stack is what makes the warn log a worklist - the operation name alone
    # says a decoder ran in the wrong container, not which caller put it there.
    # But it is also the expensive part (formatting a full traceback), and these
    # helpers are called eight times per photo: a bulk import would spend real
    # time producing thousands of copies of the same stack. Once per operation is
    # enough to identify the caller; after that the plain line is enough to show
    # it is still happening.
    #
    # Per process, and never reset - a worker recycles often enough that a
    # violation reappears in the logs on its own, and the alternative is a cache
    # this module would then have to invalidate.
    first_time = operation not in _stack_logged
    _stack_logged.add(operation)
    logger.warning("%s (policy=warn)", message, stack_info=first_time)


def untrusted_parse(operation: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Mark a function as handing untrusted bytes to a parser.

    Decorated functions run only in the sandbox worker (or under an explicit
    :func:`allow_untrusted_parse` block). Applying this to a *new* parser is
    the whole extension point: adding a format means decorating its entry
    point and routing whatever task calls it to
    :func:`~urbanlens.dashboard.services.sandbox.queues.sandbox_queue`, with no
    change to this module.

    Args:
        operation: A stable dotted name for the parse, e.g. ``"image.exif"``,
            ``"video.probe"``, ``"archive.zip"``. Grouped by parser so a log
            search can answer "what is still decoding in gunicorn".

    Returns:
        A decorator preserving the wrapped function's signature.
    """

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            check_untrusted_parse(operation)
            return func(*args, **kwargs)

        return wrapper

    return decorate
