"""Per-row bulk actions that tell a failed row apart from a skipped one.

A bulk endpoint that acts row by row (accept these suggestions, log these visits) skips ids it may not or need not
act on, and can hit a row that crashes. :func:`run_each` gives each row its own savepoint, so a crash keeps none of
that row's writes, and counts the two outcomes separately so the page can say which happened.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from django.db import transaction

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BulkOutcome:
    """Counts for one bulk request.

    Attributes:
        requested: Ids the caller named.
        processed: Rows acted on.
        failed: Rows that raised; their writes were rolled back.
    """

    requested: int
    processed: int = 0
    failed: int = 0

    @property
    def skipped(self) -> int:
        """Ids neither acted on nor failed: not the caller's, already handled, or gone."""
        return max(0, self.requested - self.processed - self.failed)

    @property
    def ok(self) -> bool:
        """Whether no row failed."""
        return self.failed == 0

    def as_json(self) -> dict[str, int | bool]:
        """The response body every per-row bulk endpoint returns."""
        return {"ok": self.ok, "requested": self.requested, "processed": self.processed, "failed": self.failed, "skipped": self.skipped}


def run_each[T](items: Iterable[T], action: Callable[[T], object], *, requested: int, description: str) -> BulkOutcome:
    """Apply *action* to each item in its own savepoint, counting successes and failures.

    Args:
        items: The rows the caller may act on (already filtered to what is actionable).
        action: What to do with one row; any exception fails that row only.
        requested: How many ids the caller named, for the skipped count.
        description: What the action is, for the failure log.

    Returns:
        The counts.
    """
    outcome = BulkOutcome(requested=requested)
    for item in items:
        try:
            with transaction.atomic():
                action(item)
        except Exception:
            logger.exception("Bulk %s failed for %r", description, item)
            outcome.failed += 1
        else:
            outcome.processed += 1
    return outcome
