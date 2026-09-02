"""The client's own dismissal ring (plan §10, batch 4) - never a server-side registry.

Explainer and onboarding-tour-card ids are include args, and several are
generated dynamically, so a Python registry keyed by id would drift the same
way a hand-maintained ``PAGE_HELP``-style dict would (see that module's own
docstring) - except here a template-parsing contract test can't see the
dynamic ones either. Instead the client itself captures what it just showed
the user (``_page_explainer_script.html``'s ``collapse()``,
``onboarding-tour.ts``'s ``dismiss()``) into a capped sessionStorage ring and
sends it with every assistant turn. This module only parses and re-caps that
payload - the model only ever sees text the user's own page actually
rendered, and ``services.ai.tools.dismissals``'s ``user_content_fields``
declaration is what wraps it as untrusted content before it reaches a prompt.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Mirrors the client ring's own cap (``ul_explainer_recent``) - re-applied
#: here since the payload arrives over HTTP and is never trusted as-is.
MAX_DISMISSALS = 5
HEADING_MAX_CHARS = 120
BODY_MAX_CHARS = 600
_PAGE_MAX_CHARS = 200
_VALID_KINDS = frozenset({"explainer", "tour"})


@dataclass(frozen=True, slots=True)
class DismissalEntry:
    """One explainer or onboarding-tour card the user just dismissed, per the client's own ring.

    Attributes:
        id: The explainer's ``data-explainer-id``, or the tour card's own id.
        kind: ``"explainer"`` or ``"tour"``.
        heading: The dismissed panel's heading text, as rendered.
        body: The dismissed panel's body text, as rendered - may be empty.
        page: The path the dismissal happened on (``location.pathname``) -
            not necessarily the current turn's page.
        prefix: The tour's ``localStorage`` key prefix - set only for
            ``kind="tour"``; ``reopen_explainer`` needs it to restart the
            right tour. Always ``None`` for ``kind="explainer"``.
    """

    id: str
    kind: str
    heading: str
    body: str
    page: str
    prefix: str | None = None


def _entry_from_dict(item: Any) -> DismissalEntry | None:
    if not isinstance(item, dict):
        return None
    kind = item.get("kind")
    entry_id = item.get("id")
    heading = item.get("heading")
    body = item.get("body")
    page = item.get("page")
    prefix = item.get("prefix")
    if kind not in _VALID_KINDS:
        return None
    if not isinstance(entry_id, str) or not entry_id:
        return None
    if not isinstance(heading, str) or not isinstance(body, str) or not isinstance(page, str):
        return None
    if prefix is not None and not isinstance(prefix, str):
        return None
    return DismissalEntry(id=entry_id, kind=kind, heading=heading[:HEADING_MAX_CHARS], body=body[:BODY_MAX_CHARS], page=page[:_PAGE_MAX_CHARS], prefix=prefix)


def dismissals_from_list(data: list[Any] | None) -> tuple[DismissalEntry, ...]:
    """Rebuild verified :class:`DismissalEntry` objects from a Celery-safe list of dicts.

    Every entry is re-validated and re-capped, matching :func:`parse_dismissals_json` -
    a malformed or oversized item is dropped, never raised, since this also
    runs on ``ai-worker`` against whatever the web view enqueued.

    Args:
        data: The task's own ``dismissals`` argument, or ``None``.

    Returns:
        Up to :data:`MAX_DISMISSALS` verified entries, in the given order.
    """
    if not isinstance(data, list):
        return ()
    entries = (_entry_from_dict(item) for item in data[:MAX_DISMISSALS])
    return tuple(entry for entry in entries if entry is not None)


def dismissals_to_list(entries: tuple[DismissalEntry, ...]) -> list[dict[str, Any]]:
    """The Celery-safe (JSON-serializable) form of ``entries`` - round-trips via :func:`dismissals_from_list`."""
    return [asdict(entry) for entry in entries]


def parse_dismissals_json(raw: str) -> tuple[DismissalEntry, ...]:
    """Parse the request body's raw ``dismissals`` JSON string into verified entries.

    Never raises - a missing, malformed, or oversized payload just yields
    fewer (or zero) entries, the same way an unresolvable page path yields no
    page context.

    Args:
        raw: The POST body's ``dismissals`` value - a JSON array, or ``""``.

    Returns:
        Up to :data:`MAX_DISMISSALS` verified entries.
    """
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.debug("Ignoring malformed assistant dismissals payload")
        return ()
    return dismissals_from_list(data if isinstance(data, list) else None)
