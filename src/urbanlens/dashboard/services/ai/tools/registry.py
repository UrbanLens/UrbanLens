"""The assistant's typed tool registry: one chokepoint for every rule a tool must obey.
Write tools only ever run through the confirm-gated proposal flow, on ``app`` - never inside the tool loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from urbanlens.dashboard.services.ai.scanner import scan, wrap_user_data
from urbanlens.dashboard.services.sandbox import ProcessRole, current_role

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.subscriptions import SiteFeature
    from urbanlens.dashboard.services.ai.dismissals import DismissalEntry
    from urbanlens.dashboard.services.ai.page_context import PageObject

logger = logging.getLogger(__name__)

#: Tool executions allowed per user message.
#: Enforced by the loop (``services/ai/turns.py``, batch 2c), not by :func:`execute` itself - this
#: module has no state across calls in a turn.
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
    Not a Django model or a persisted value - purely a label so a test can iterate ``REGISTRY.values()`` and require a negative-access case for every tool whose result could plausibly contain another profile's data.

    Attributes:
        NONE: No profile-specific data at all (e.g. a static help lookup).
        OWN_PROFILE: Only rows the requesting profile itself owns outright (its own pins).
        VISIBLE_SHARED: Rows the requesting profile can see through a sharing relationship it doesn't unilaterally control (trip membership, wiki visibility, ...) - the access check is more than "profile equals requester", so it deserves its own negative-access case."""

    NONE = "none"
    OWN_PROFILE = "own_profile"
    VISIBLE_SHARED = "visible_shared"


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything a tool handler is given besides its own validated arguments.

    Attributes:
        profile: The requesting profile.
        now: A single "current time" for the whole turn, so a multi-tool-call turn doesn't see the clock move between calls.
        page: The resolved page's own object (``services.ai.page_context.PageObject``), or ``None`` when no page was resolved, the client sent none, or the resolved page has no single object of its own (e.g. the map).
        deadline: A ``time.monotonic()`` value the tool must not run past, or ``None`` when the caller (e.g. a direct/local test) imposes none.
        dismissals: The explainers/tour cards the client's own ring reported dismissed this turn (``services.ai.dismissals``), already re-verified and re-capped - never a server-side lookup."""

    profile: Profile
    now: datetime
    page: PageObject | None = None
    deadline: float | None = None
    dismissals: tuple[DismissalEntry, ...] = ()
    #: Per-turn memo for :func:`_has_feature`.
    #: ``user_has_feature`` runs up to three queries and is asked once per tool by
    #: :func:`available_tools` and again per :func:`execute` call - roughly sixty redundant queries
    #: across a full turn, for an answer that cannot change inside one.
    _feature_cache: dict[SiteFeature, bool] = field(default_factory=dict, compare=False, repr=False)


#: Fallback budget for a tool's external-gateway call when ``context.deadline`` is unset (e.g. a
#: direct/local test) - matches ``services.core.timeout_utils.EXTERNAL_CALL_DEADLINE``'s own default
#: so a tool behaves the same as any other request-path caller of that helper.
DEFAULT_GATEWAY_TIMEOUT_SECONDS = 20.0


def remaining_deadline(context: ToolContext, *, cap: float = DEFAULT_GATEWAY_TIMEOUT_SECONDS) -> float:
    """Seconds a tool may still spend on one external-gateway call, for ``call_with_deadline``.

    Args:
        context: The tool's execution context.
        cap: Upper bound in seconds - never wait longer than this even when ``context.deadline`` is unset or far in the future.

    Returns:
        ``cap`` when ``context.deadline`` is ``None``; otherwise the time left until it, clamped to ``[0, cap]`` - never negative, so a deadline already passed still gives the gateway call a (zero-length) chance to fail fast through the normal timeout path rather than skipping it."""
    if context.deadline is None:
        return cap
    return max(0.0, min(cap, context.deadline - time.monotonic()))


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What :func:`execute` returns, regardless of which tool ran.

    Attributes:
        data: The JSON-safe payload - either the handler's result (possibly wrapped/capped) or ``{"error": "..."}`` when execution was refused or failed.
        summary: A short past-tense description of what happened (``spec.action_label``), for the turn's action log - ``None`` when ``data`` is an error or a proposal (nothing happened yet).
        proposal: Set by :func:`execute` when ``confirmed=False`` and the tool is a write (``read_only=False``) - the write was *not* run; this is what the confirm endpoint needs to run it for real later: ``{"tool", "args", "confirm_label"}``.
        sources: Evidence kinds the result is grounded on (``visits``, ``comments``, ``osrm``, ...) - populated by the grounded tools added in batch 4."""

    data: dict[str, Any]
    summary: str | None = None
    proposal: dict[str, Any] | None = None
    sources: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ToolSpec[ArgsT: BaseModel]:
    """One tool the assistant may call, and every rule governing it.

    Attributes:
        name: The tool name as the model sees it - must be unique in :data:`REGISTRY`.
        description: Shown to the model verbatim as the tool's description.
        args_model: A pydantic model whose ``model_json_schema()`` becomes the tool's ``input_schema``, and whose ``model_validate()`` is what turns the model's raw JSON arguments into a checked value (or a validation error, which :func:`execute` turns into an error...
        handler: ``(ToolContext, args_model instance) -> dict[str, Any]``.
        read_only: False marks this tool as a write.
        requires_confirmation: Whether a write needs the confirm-gated proposal flow (batch 2d) before it runs for real.
        needs_page: True when the tool only makes sense with a resolved page context (batch 3/4) - :func:`execute` refuses it when ``context.page`` is ``None``.
        features: Subscription features required to use this tool (``user_has_feature``) - empty means no gate beyond whatever already gated the assistant turn itself.
        requires_external_apis: True when the tool calls an external gateway (OSRM, weather - batch 4) - refused when the profile has external APIs turned off, so that existing opt-out keeps meaning something for the assistant too.
        user_content_fields: Result dict keys (at any nesting depth, inside a list too) whose string value is user-supplied content - truncated, scanned, and wrapped by :func:`execute` before the model ever sees it.
        client_action: Set when this tool's effect happens in the browser, not the database (batch 4's ``reopen_explainer``) - forwarded to the client by the turn/poll response, not executed server-side.
        scope: See :class:`DataScope`.
        progress_label: Shown in the pending bubble while this tool runs.
        action_label: A past-tense summary for a completed execution (``ToolResult.summary``), shown in the turn's action log - e.g. "Created a trip".
        confirm_label: The confirm button's imperative text for a tool with ``requires_confirmation=True`` (e.g. "Create trip") - carried on :attr:`ToolResult.proposal` so the UI has something to put on the button."""

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
    confirm_label: str | None = None


#: Every registered tool, keyed by name.
#: Populated by each tool module's import-time ``register(...)`` calls - see
#: ``services/ai/tools/__init__.py`` for the list of modules that must be imported for this to be
#: complete.
REGISTRY: dict[str, ToolSpec[Any]] = {}


def register(spec: ToolSpec[Any]) -> None:
    """Add ``spec`` to :data:`REGISTRY`.

    Args:
        spec: The tool to register.

    Raises:
        ValueError: A tool with this name is already registered - two tool modules picked the same name, which is always a bug - or a write tool (``read_only=False``) did not set ``requires_confirmation``. :func:`execute` gates every write on the confirm flow using..."""
    if spec.name in REGISTRY:
        raise ValueError(f"Tool {spec.name!r} is already registered")
    if not spec.read_only and not spec.requires_confirmation:
        raise ValueError(f"Tool {spec.name!r} is a write (read_only=False) and must set requires_confirmation=True")
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
    Dropping the whole payload rather than truncating the serialized string: a naive string truncation would hand the model invalid JSON."""
    try:
        serialized = json.dumps(data, default=str)
    except TypeError:
        # A handler returned something json.dumps can't serialize even with default=str (e.g. a
        # value whose str() also isn't JSON-safe) - treat it the same as an oversized result rather
        # than letting this raise out of execute().
        logger.exception("Tool result was not JSON-serializable")
        return {"error": "The tool returned a result that could not be processed."}
    if len(serialized) <= MAX_TOOL_RESULT_CHARS:
        return data
    return {"error": "The tool's result was too large and was discarded."}


def available_tools(context: ToolContext) -> list[ToolSpec[Any]]:
    """Every registered tool whose ``features``/``requires_external_apis``/``needs_page`` gate ``context`` currently satisfies.

    Args:
        context: The scoped execution context.

    Returns:
        The available tools, in registration order."""
    return [spec for spec in REGISTRY.values() if _is_available(spec, context)]


def _is_available(spec: ToolSpec[Any], context: ToolContext) -> bool:
    # all(), not any(): ``features`` is every feature the tool requires, so a tool naming two of
    # them needs both.
    if not all(_has_feature(context, feature) for feature in spec.features):
        return False
    if spec.requires_external_apis and not context.profile.external_apis_enabled:
        return False
    return not (spec.needs_page and context.page is None)


def execute(name: str, raw_args: dict[str, Any] | None, context: ToolContext, *, confirmed: bool = True) -> ToolResult:
    """Run ``name`` with ``raw_args``, enforcing every rule in the module docstring.
    Never raises for a tool-level problem (unknown name, bad args, a handler's own exception) - those all become an error :class:`ToolResult` so the caller can hand the error straight back to the model as a tool result and let the loop continue.

    Args:
        name: The tool name the model asked for.
        raw_args: The model's raw (untrusted) JSON arguments.
        context: The scoped execution context.
        confirmed: Whether this call is allowed to actually run a write.

    Returns:
        The tool's result: real data, a proposal awaiting confirmation, or an error result explaining why it didn't run."""
    spec = REGISTRY.get(name)
    if spec is None:
        return ToolResult(data={"error": f'Unknown tool "{name}". Use only the tools you were given, or reply.'})

    if not _is_available(spec, context):
        return ToolResult(data={"error": "This tool isn't available for this user or context right now."})

    try:
        args = spec.args_model.model_validate(raw_args or {})
    except ValidationError as exc:
        return ToolResult(data={"error": f"Invalid arguments: {exc.errors()[0]['msg'] if exc.errors() else exc}"})

    if _contains_url(args.model_dump()):
        return ToolResult(data={"error": "Arguments may not contain a URL."})

    if not spec.read_only and not confirmed:
        label = spec.confirm_label or spec.action_label or spec.name
        return ToolResult(
            data={"status": "proposed", "message": f"{label!r} was proposed for the user to confirm - it has not run yet."},
            proposal={"tool": spec.name, "args": args.model_dump(mode="json"), "confirm_label": label},
        )
    if not spec.read_only and current_role() is ProcessRole.AI:
        return ToolResult(data={"error": "This action needs the user's confirmation and cannot run automatically."})

    try:
        data = spec.handler(context, args)
    except Exception:
        logger.exception("Tool %r failed", name)
        return ToolResult(data={"error": "The tool failed unexpectedly."})

    data = _wrap_user_content(data, spec.user_content_fields)
    data = _capped(data)
    is_error = isinstance(data, dict) and "error" in data
    return ToolResult(data=data, summary=None if is_error else spec.action_label)


def _has_feature(context: ToolContext, feature: SiteFeature) -> bool:
    """``user_has_feature``, memoized for the life of ``context`` - see its ``_feature_cache``."""
    from urbanlens.dashboard.models.subscriptions import user_has_feature

    cached = context._feature_cache.get(feature)  # noqa: SLF001 -- the cache belongs to this module; ToolContext...
    if cached is None:
        cached = user_has_feature(context.profile.user, feature)
        context._feature_cache[feature] = cached  # noqa: SLF001 -- as above
    return cached
