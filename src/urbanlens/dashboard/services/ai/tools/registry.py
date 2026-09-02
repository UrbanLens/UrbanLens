"""The assistant's typed tool registry: one chokepoint for every rule a tool must obey.

Each tool declares itself as a :class:`ToolSpec` - name, description, a
pydantic ``args_model`` (its JSON Schema is what the model actually sees),
a handler, and a handful of access/safety flags. :func:`execute` is the only
way a tool ever runs, and it enforces every generic rule once, here, rather
than trusting each handler to remember all of them:

- Unknown tool name or arguments that fail ``args_model`` validation become
  an error block handed back to the model, never an exception.
- Any string argument containing ``http://``/``https://`` is rejected: no
  tool in this registry takes a URL, so one appearing is the model trying to
  fetch something - see docs/AI_PIPELINE.md's "no web" guarantee.
- Under :attr:`~services.sandbox.guard.ProcessRole.AI`, a tool declaring
  ``read_only=False`` is refused outright. Write tools only ever run through
  the confirm-gated proposal flow, on ``app`` - never inside the tool loop.
  This is defense in depth: the loop (``services/ai/turns.py``) is expected
  to intercept a write tool call and turn it into a proposal *before* ever
  reaching here, so this refusal should never actually fire in production.
- Every field named in a tool's ``user_content_fields`` is truncated, scanned
  for prompt injection (flagged via the scanner's own logging, never
  silently dropped - the wrapper below is what neutralizes it) and wrapped
  in ``<USER_DATA>`` delimiters, so a handler cannot forget to do this itself.
- The serialized result is capped at :data:`MAX_TOOL_RESULT_CHARS`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from urbanlens.dashboard.services.ai.scanner import scan, wrap_user_data
from urbanlens.dashboard.services.sandbox import ProcessRole, current_role

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.subscriptions import SiteFeature

logger = logging.getLogger(__name__)

#: Tool executions allowed per user message. Enforced by the loop
#: (``services/ai/turns.py``, batch 2c), not by :func:`execute` itself -
#: this module has no state across calls in a turn. Lives here so the loop
#: and the registry's own tests share one source of truth for the value.
MAX_TOOL_CALLS = 6
#: Serialized-JSON byte cap on a single tool result. Handlers already row-limit
#: (see each tool module's own row cap) - this is the backstop for when they
#: don't, or can't.
MAX_TOOL_RESULT_CHARS = 4000
#: Character cap applied to each ``user_content_fields`` value before it is
#: wrapped - keeps one long pin name from dominating the prompt budget.
_USER_CONTENT_FIELD_CHAR_LIMIT = 300

_URL_RE = re.compile(r"https?://", re.IGNORECASE)


class DataScope(StrEnum):
    """What a tool's result can contain, for the registry-driven negative-access test.

    Not a Django model or a persisted value - purely a label so a test can
    iterate ``REGISTRY.values()`` and require a negative-access case for every
    tool whose result could plausibly contain another profile's data.

    Attributes:
        NONE: No profile-specific data at all (e.g. a static help lookup).
        OWN_PROFILE: Only rows the requesting profile itself owns outright
            (its own pins). A cross-profile leak here is a straightforward bug.
        VISIBLE_SHARED: Rows the requesting profile can see through a sharing
            relationship it doesn't unilaterally control (trip membership,
            wiki visibility, ...) - the access check is more than "profile
            equals requester", so it deserves its own negative-access case.
    """

    NONE = "none"
    OWN_PROFILE = "own_profile"
    VISIBLE_SHARED = "visible_shared"


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything a tool handler is given besides its own validated arguments.

    Attributes:
        profile: The requesting profile. Every handler must scope its own
            queries to this - the registry does not and cannot do that for
            an arbitrary handler.
        now: A single "current time" for the whole turn, so a multi-tool-call
            turn doesn't see the clock move between calls.
        page: The resolved page context (``services/ai/page_context.py``,
            batch 3), or ``None`` when no page was resolved or the client
            sent none. Untyped for now since that module doesn't exist yet;
            tools with ``needs_page=True`` don't ship until batch 4.
        deadline: A ``time.monotonic()`` value the tool must not run past,
            or ``None`` when the caller (e.g. a direct/local test) imposes
            none. Populated by the turn task (batch 2c); tools that call an
            external gateway (OSRM, weather - batch 4) pass it through
            ``call_with_deadline``.
    """

    profile: Profile
    now: datetime
    page: Any | None = None
    deadline: float | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What :func:`execute` returns, regardless of which tool ran.

    Attributes:
        data: The JSON-safe payload - either the handler's result (possibly
            wrapped/capped) or ``{"error": "..."}`` when execution was
            refused or failed. Callers check for the ``"error"`` key rather
            than a separate boolean, matching the ported handlers' own
            existing convention.
        summary: A short past-tense description of what happened
            (``spec.action_label``), for the turn's action log - ``None``
            when ``data`` is an error.
        proposal: Set by the confirm flow (batch 2d) when this result
            represents a write awaiting confirmation, never by
            :func:`execute` itself in this batch.
        sources: Evidence kinds the result is grounded on (``visits``,
            ``comments``, ``osrm``, ...) - populated by the grounded tools
            added in batch 4. Always empty for a tool that IS the source of
            truth rather than citing one.
    """

    data: dict[str, Any]
    summary: str | None = None
    proposal: dict[str, Any] | None = None
    sources: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ToolSpec[ArgsT: BaseModel]:
    """One tool the assistant may call, and every rule governing it.

    Generic over its own args model (``ArgsT``) so a handler typed to take
    exactly that model - not the erased ``BaseModel`` - type-checks:
    ``Callable`` parameters are contravariant, so a plain
    ``Callable[[ToolContext, BaseModel], ...]`` field would reject every real
    handler, each of which only knows its own narrower args type.
    :data:`REGISTRY` stores ``ToolSpec[Any]`` (it holds every tool's
    differently-typed spec at once) - the type parameter earns its keep at
    each tool's own ``register(ToolSpec(...))`` call site, where mypy checks
    the handler against that exact args model.

    Attributes:
        name: The tool name as the model sees it - must be unique in
            :data:`REGISTRY`.
        description: Shown to the model verbatim as the tool's description.
        args_model: A pydantic model whose ``model_json_schema()`` becomes
            the tool's ``input_schema``, and whose ``model_validate()`` is
            what turns the model's raw JSON arguments into a checked value
            (or a validation error, which :func:`execute` turns into an
            error block rather than letting it propagate).
        handler: ``(ToolContext, args_model instance) -> dict[str, Any]``.
            Must scope every query to ``context.profile`` itself - nothing
            upstream of the handler does that for it.
        read_only: False marks this tool as a write. Under
            :attr:`~services.sandbox.guard.ProcessRole.AI`, :func:`execute`
            refuses to run it at all - see the module docstring.
        requires_confirmation: Whether a write needs the confirm-gated
            proposal flow (batch 2d) before it runs for real. In this batch,
            true whenever ``read_only`` is false - kept as its own field per
            the design so a future tool can decouple the two.
        needs_page: True when the tool only makes sense with a resolved page
            context (batch 3/4) - :func:`execute` refuses it when
            ``context.page`` is ``None``.
        features: Subscription features required to use this tool
            (``user_has_feature``) - empty means no gate beyond whatever
            already gated the assistant turn itself.
        requires_external_apis: True when the tool calls an external gateway
            (OSRM, weather - batch 4) - refused when the profile has
            external APIs turned off, so that existing opt-out keeps meaning
            something for the assistant too.
        user_content_fields: Result dict keys (at any nesting depth, inside
            a list too) whose string value is user-supplied content -
            truncated, scanned, and wrapped by :func:`execute` before the
            model ever sees it.
        client_action: Set when this tool's effect happens in the browser,
            not the database (batch 4's ``reopen_explainer``) - forwarded to
            the client by the turn/poll response, not executed server-side.
        scope: See :class:`DataScope`.
        progress_label: Shown in the pending bubble while this tool runs.
        action_label: A past-tense summary for a completed direct execution
            (``ToolResult.summary``), and - for a tool with
            ``requires_confirmation=True`` - doubles as the confirm button's
            imperative text (e.g. "Create trip"). ``None`` only makes sense
            when a caller intends to override the summary itself.
    """

    name: str
    description: str
    args_model: type[ArgsT]
    handler: Callable[[ToolContext, ArgsT], dict[str, Any]]
    read_only: bool = True
    requires_confirmation: bool = False
    needs_page: bool = False
    features: frozenset[SiteFeature] = field(default_factory=frozenset)
    requires_external_apis: bool = False
    user_content_fields: frozenset[str] = field(default_factory=frozenset)
    client_action: str | None = None
    scope: DataScope = DataScope.NONE
    progress_label: str = ""
    action_label: str | None = None


#: Every registered tool, keyed by name. Populated by each tool module's
#: import-time ``register(...)`` calls - see ``services/ai/tools/__init__.py``
#: for the list of modules that must be imported for this to be complete.
#: ``ToolSpec[Any]``, not a specific args model: this dict holds every tool's
#: differently-typed spec at once, so the type parameter is necessarily
#: erased here - see :data:`ArgsT`'s own comment.
REGISTRY: dict[str, ToolSpec[Any]] = {}


def register(spec: ToolSpec[Any]) -> None:
    """Add ``spec`` to :data:`REGISTRY`.

    Args:
        spec: The tool to register.

    Raises:
        ValueError: A tool with this name is already registered - two tool
            modules picked the same name, which is always a bug.
    """
    if spec.name in REGISTRY:
        raise ValueError(f"Tool {spec.name!r} is already registered")
    REGISTRY[spec.name] = spec


def _contains_url(value: Any) -> bool:
    """Whether any string anywhere in ``value`` (recursively) looks like a URL."""
    if isinstance(value, str):
        return bool(_URL_RE.search(value))
    if isinstance(value, dict):
        return any(_contains_url(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_url(item) for item in value)
    return False


def _wrap_user_content(value: Any, fields: frozenset[str]) -> Any:
    """Recursively truncate, scan, and wrap every ``fields``-named string in ``value``."""
    if isinstance(value, dict):
        wrapped: dict[str, Any] = {}
        for key, item in value.items():
            if key in fields and isinstance(item, str):
                truncated = item[:_USER_CONTENT_FIELD_CHAR_LIMIT]
                scan(truncated, source=f"tool_result:{key}")
                wrapped[key] = wrap_user_data(truncated)
            else:
                wrapped[key] = _wrap_user_content(item, fields)
        return wrapped
    if isinstance(value, list):
        return [_wrap_user_content(item, fields) for item in value]
    return value


def _capped(data: dict[str, Any]) -> dict[str, Any]:
    """Discard ``data`` in favor of an error when its serialized form exceeds the cap.

    Dropping the whole payload rather than truncating the serialized string:
    a naive string truncation would hand the model invalid JSON.
    """
    try:
        serialized = json.dumps(data, default=str)
    except TypeError:
        # A handler returned something json.dumps can't serialize even with
        # default=str (e.g. a value whose str() also isn't JSON-safe) - treat
        # it the same as an oversized result rather than letting this raise
        # out of execute().
        logger.exception("Tool result was not JSON-serializable")
        return {"error": "The tool returned a result that could not be processed."}
    if len(serialized) <= MAX_TOOL_RESULT_CHARS:
        return data
    return {"error": "The tool's result was too large and was discarded."}


def execute(name: str, raw_args: dict[str, Any] | None, context: ToolContext) -> ToolResult:
    """Run ``name`` with ``raw_args``, enforcing every rule in the module docstring.

    Never raises for a tool-level problem (unknown name, bad args, a
    handler's own exception) - those all become an error :class:`ToolResult`
    so the caller can hand the error straight back to the model as a tool
    result and let the loop continue.

    Args:
        name: The tool name the model asked for.
        raw_args: The model's raw (untrusted) JSON arguments.
        context: The scoped execution context.

    Returns:
        The tool's result, or an error result explaining why it didn't run.
    """
    spec = REGISTRY.get(name)
    if spec is None:
        return ToolResult(data={"error": f'Unknown tool "{name}". Use only the tools you were given, or reply.'})

    if spec.features and not any(_user_has_feature(context.profile, feature) for feature in spec.features):
        return ToolResult(data={"error": "This tool isn't available on the user's current plan."})
    if spec.requires_external_apis and not context.profile.external_apis_enabled:
        return ToolResult(data={"error": "This tool requires external APIs, which are turned off for this user."})
    if spec.needs_page and context.page is None:
        return ToolResult(data={"error": "This tool only works with a specific page open."})
    if not spec.read_only and current_role() is ProcessRole.AI:
        return ToolResult(data={"error": "This action needs the user's confirmation and cannot run automatically."})

    try:
        args = spec.args_model.model_validate(raw_args or {})
    except ValidationError as exc:
        return ToolResult(data={"error": f"Invalid arguments: {exc.errors()[0]['msg'] if exc.errors() else exc}"})

    if _contains_url(args.model_dump()):
        return ToolResult(data={"error": "Arguments may not contain a URL."})

    try:
        data = spec.handler(context, args)
    except Exception:
        logger.exception("Tool %r failed", name)
        return ToolResult(data={"error": "The tool failed unexpectedly."})

    data = _wrap_user_content(data, spec.user_content_fields)
    data = _capped(data)
    is_error = isinstance(data, dict) and "error" in data
    return ToolResult(data=data, summary=None if is_error else spec.action_label)


def _user_has_feature(profile: Profile, feature: SiteFeature) -> bool:
    from urbanlens.dashboard.models.subscriptions import user_has_feature

    return user_has_feature(profile.user, feature)
