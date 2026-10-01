"""The nightly re-sync of RoleSubscription rows from Stripe, a safety net for webhooks Stripe could not deliver.

The sweep walks ``Subscription.list`` one page per task, then retrieves individually only the live rows no page (and no
event since the sweep began) reached: the default listing omits canceled subscriptions, so those are the rows whose
``deleted`` webhook went missing. Every step is a snapshot applied through ``subscription_state``, so re-running a page
or chunk is harmless.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from django.utils import timezone
import stripe

from urbanlens.dashboard.models.billing import RoleSubscription
from urbanlens.dashboard.services.billing.subscription_state import apply_subscription, retrieve_subscription

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)

#: Stripe's maximum list page size.
PAGE_SIZE = 100
RECONCILE_CHUNK = 100


@dataclass(frozen=True)
class Progress:
    """How far one step of the sweep got.

    Attributes:
        applied: Rows the step brought up to date.
        resume_after: Where the next step starts, or None when this was the last.
    """

    applied: int
    resume_after: str | int | None


def _apply(row: RoleSubscription, snapshot: dict, as_of: datetime) -> bool:
    try:
        apply_subscription(row, snapshot, as_of)
    except Exception:
        # One subscription in an unexpected shape must not abort the sweep for everyone after it.
        logger.exception("Stripe sync: failed to apply %s", row.stripe_subscription_id)
        return False
    return True


def sync_page(starting_after: str | None) -> Progress:
    """Apply one page of ``Subscription.list`` to the rows it names.

    Subscriptions with no local row (another deployment's, or one whose checkout webhook has not landed) are skipped.

    Args:
        starting_after: The last subscription id of the previous page, or None for the first page.

    Returns:
        Rows applied, and the cursor for the next page.
    """
    params: stripe.params.SubscriptionListParams = {"limit": PAGE_SIZE}
    if starting_after:
        params["starting_after"] = starting_after
    sent_at = timezone.now().replace(microsecond=0)
    page = stripe.Subscription.list(**params)
    snapshots = [subscription.to_dict() for subscription in page.data]

    rows = RoleSubscription.objects.filter(stripe_subscription_id__in=[s["id"] for s in snapshots]).select_related("role")
    by_id = {row.stripe_subscription_id: row for row in rows}
    applied = sum(_apply(by_id[s["id"]], s, sent_at) for s in snapshots if s["id"] in by_id)
    resume_after = snapshots[-1]["id"] if page.has_more and snapshots else None
    return Progress(applied, resume_after)


def reconcile_unlisted(since: datetime, after_pk: int, limit: int = RECONCILE_CHUNK) -> Progress:
    """Retrieve, one by one, live rows that no Stripe state newer than *since* has reached.

    Args:
        since: When the sweep started.
        after_pk: Resume after this primary key.
        limit: Rows per chunk.

    Returns:
        Rows applied, and the primary key to resume after.
    """
    rows = list(RoleSubscription.objects.unsynced_since(since).filter(pk__gt=after_pk).select_related("role").order_by("pk")[:limit])
    applied = 0
    for row in rows:
        try:
            snapshot, as_of = retrieve_subscription(row.stripe_subscription_id)
        except stripe.StripeError:
            logger.exception("Stripe sync: failed to retrieve %s", row.stripe_subscription_id)
            continue
        applied += _apply(row, snapshot, as_of)
    resume_after = rows[-1].pk if len(rows) == limit else None
    return Progress(applied, resume_after)
