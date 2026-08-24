"""Enums and key helpers for the reputation ledger.

Separate from both the models and the rule registry so each can import it
without a cycle: the models need ``TargetKind`` for a field's choices, and the
registry in ``services.reputation.rules`` needs the period helpers, while the
registry itself is what supplies the models' ``rule_key`` choices.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.abstract.choices import TextChoices

if TYPE_CHECKING:
    import datetime


class TargetKind(TextChoices):
    """What a ledger row is *about*.

    Stored as a label plus an integer id rather than a real
    ``GenericForeignKey``: the ledger is written on every contribution and read
    in bulk by the aggregator, and a contenttypes join on both paths buys
    nothing here - nothing dereferences the target generically, and the two
    consumers that care (per-wiki caps, the admin breakdown) both filter on
    :attr:`ReputationEvent.wiki` instead.
    """

    NONE = "none", "No target"
    WIKI_EDIT = "wiki_edit", "Wiki edit"
    IMAGE = "image", "Photo"
    COMMENT = "comment", "Comment"
    PIN = "pin", "Pin"
    WIKI = "wiki", "Wiki"
    ARTICLE_REVISION = "article_revision", "Article revision"
    FRIEND_INVITATION = "friend_invitation", "Invitation"
    PROFILE = "profile", "Profile"


#: Periods are calendar months. The source memo said both "30 days later" and
#: "in February" for the same mechanic; a calendar month is what makes a
#: per-period cap explainable to whoever reads the admin dashboard, and it lets
#: the cap be a plain indexed equality filter instead of a rolling window scan.
PERIOD_FORMAT = "%Y-%m"


def period_key_for(moment: datetime.datetime | datetime.date | None = None) -> str:
    """Return the period bucket a moment falls in.

    Args:
        moment: The time to bucket. Defaults to now, in the project timezone.

    Returns:
        A sortable ``YYYY-MM`` string.
    """
    if moment is None:
        from django.utils import timezone

        moment = timezone.localdate()
    return moment.strftime(PERIOD_FORMAT)
