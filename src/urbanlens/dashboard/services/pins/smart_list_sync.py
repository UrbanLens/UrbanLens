"""Smart-list membership, kept current off the request.

A pin save or label change used to re-evaluate the pin against every smart list its owner had, in the request's
commit hook, once per pin per signal. At 25 smart lists over 2,000 pins a bulk edit of 500 pins took about 84,000
queries and three minutes (N30, Batch 1). A change now records a ``SmartListSyncRequest`` in its own transaction,
so the request rolls back with it. Once the change commits, one sync is queued per account, and that sync evaluates
every requested pin against each list together
(:func:`~urbanlens.dashboard.services.pins.pin_list_membership.sync_pins_against_smart_lists`).

Single flight per account: a *queued* claim stops a burst of changes queueing one sync each, and a *running* lock
stops two syncs evaluating one account's lists at once. A sync gives up its queued claim before it reads, so a
change committed after that read queues the next sync instead of waiting on this one. While requests are
outstanding, the account's smart lists say they are catching up (:func:`membership_pending`).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from urbanlens.dashboard.services.core import single_flight
from urbanlens.dashboard.services.core.celery import follow_on_queue, safely_enqueue_task
from urbanlens.dashboard.services.core.locks import acquire_lock, release_lock
from urbanlens.dashboard.services.pins.pin_list_membership import sync_pins_against_smart_lists
from urbanlens.dashboard.services.sandbox.queues import Queue

if TYPE_CHECKING:
    from collections.abc import Iterator

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.pin_list.model import PinList

logger = logging.getLogger(__name__)

#: How long a queued claim holds when its sync never starts, after a refused or lost enqueue. The sweep re-queues.
QUEUED_CLAIM_SECONDS = 600

#: Outlasts any one sync, so a dead worker's lock lapses by itself.
RUNNING_LOCK_SECONDS = 600

#: When a sync finds another running for the same account, it comes back after this.
BUSY_RETRY_SECONDS = 15

#: Requests read per pass.
BATCH_SIZE = 200

#: Passes per sync before it hands the rest to a fresh one, so one account's backlog cannot hold a worker.
MAX_BATCHES_PER_RUN = 25

#: A request this old was not drained by the sync its change queued, so the sweep queues another.
STALE_REQUEST_AGE = timedelta(minutes=10)

#: Accounts the sweep queues per run.
SWEEP_BATCH = 500


def _queued_key(profile_id: int) -> str:
    return f"ul:smart-list-sync:queued:{profile_id}"


def _running_key(profile_id: int) -> str:
    return f"ul:smart-list-sync:running:{profile_id}"


def request_smart_list_sync(pin: Pin) -> None:
    """Record that *pin*'s smart-list membership needs re-evaluating, and queue its account's sync once this commits.

    Args:
        pin: The pin that was saved, or whose labels changed.
    """
    from urbanlens.dashboard.models.pin_list.model import PinList, SmartListSyncRequest

    profile_id = pin.profile_id
    if not pin.pk or not profile_id or not PinList.objects.active_smart_lists(profile_id).exists():
        return
    SmartListSyncRequest.objects.create(profile_id=profile_id, pin_id=pin.pk)
    transaction.on_commit(lambda: queue_smart_list_sync(profile_id))


def queue_smart_list_sync(profile_id: int, *, countdown: int | None = None) -> bool:
    """Queue *profile_id*'s sync, unless one is already queued and has not started.

    A change made inside a batch task (an import, an enrichment pass) syncs on the bulk queue, as every signal's
    follow-on work does (``follow_on_queue``). So does an account past ``settings.MAX_SMART_LISTS_PER_SYNC`` smart
    lists, where its size cannot become anyone else's wait.

    Args:
        profile_id: The account whose pins have outstanding requests.
        countdown: Seconds to wait before it runs.

    Returns:
        Whether this call queued it.
    """
    if not single_flight.claim(_queued_key(profile_id), QUEUED_CLAIM_SECONDS):
        return False
    from urbanlens.dashboard.tasks import sync_requested_smart_lists

    safely_enqueue_task(sync_requested_smart_lists, profile_id, countdown=countdown, queue=follow_on_queue() or _queue_for(profile_id))
    return True


def release_queued_claim(profile_id: int) -> None:
    """Let the next change queue a sync for *profile_id*, as a starting sync does.

    Args:
        profile_id: The account.
    """
    single_flight.release(_queued_key(profile_id))


def _queue_for(profile_id: int) -> str:
    from django.conf import settings

    from urbanlens.dashboard.models.pin_list.model import PinList

    ceiling = settings.MAX_SMART_LISTS_PER_SYNC
    # One past the ceiling, so "there are more" costs no full count.
    over = PinList.objects.active_smart_lists(profile_id)[: ceiling + 1].count() > ceiling
    return Queue.BULK if over else Queue.INTERACTIVE


@contextmanager
def running_sync(profile_id: int) -> Iterator[bool]:
    """Hold *profile_id*'s sync lock for the block, if no other sync does.

    Args:
        profile_id: The account.

    Yields:
        Whether the lock is held; when it is not, another sync is running and the caller must not evaluate.
    """
    token = acquire_lock(_running_key(profile_id), RUNNING_LOCK_SECONDS)
    try:
        yield token is not None
    finally:
        release_lock(_running_key(profile_id), token)


def drain_smart_list_sync_requests(profile_id: int, *, batch_size: int = BATCH_SIZE, max_batches: int = MAX_BATCHES_PER_RUN) -> int | None:
    """Apply *profile_id*'s outstanding requests, oldest first.

    Deletes exactly the rows each pass read, so a request written for the same pin during the pass outlives it.

    Args:
        profile_id: The account.
        batch_size: Requests read per pass.
        max_batches: Passes before the rest goes to a fresh sync.

    Returns:
        How many pins were evaluated, or None when another sync holds the account and this one was put off.
    """
    from urbanlens.dashboard.models.pin_list.model import SmartListSyncRequest

    # Before the lock and the reads: a change committing from here on queues the sync after this one.
    release_queued_claim(profile_id)
    with running_sync(profile_id) as held:
        if not held:
            # The running sync may already have read its last pass, so this one's requests cannot wait on it.
            queue_smart_list_sync(profile_id, countdown=BUSY_RETRY_SECONDS)
            return None
        evaluated = 0
        for _ in range(max_batches):
            rows = list(SmartListSyncRequest.objects.for_profile(profile_id).order_by("pk").values_list("pk", "pin_id")[:batch_size])
            if not rows:
                return evaluated
            pin_ids = {pin_id for _pk, pin_id in rows}
            sync_pins_against_smart_lists(profile_id, pin_ids)
            SmartListSyncRequest.objects.filter(pk__in=[pk for pk, _pin_id in rows]).delete()
            evaluated += len(pin_ids)
    if SmartListSyncRequest.objects.for_profile(profile_id).exists():
        queue_smart_list_sync(profile_id)
    return evaluated


def queue_stale_requests() -> int:
    """Queue a sync for every account holding a request its own sync never drained.

    A sync can be lost: a refused enqueue whose claim then lapsed, enqueues suppressed while demo data was seeded,
    or a worker that died mid-run.

    Returns:
        How many syncs were queued.
    """
    from urbanlens.dashboard.models.pin_list.model import SmartListSyncRequest

    cutoff = timezone.now() - STALE_REQUEST_AGE
    stale = SmartListSyncRequest.objects.filter(created__lt=cutoff).order_by("profile_id").values_list("profile_id", flat=True).distinct()[:SWEEP_BATCH]
    queued = sum(queue_smart_list_sync(profile_id) for profile_id in stale)
    if queued:
        logger.info("Queued %d smart-list sync(s) whose requests were never drained", queued)
    return queued


def membership_pending(pin_list: PinList) -> bool:
    """Whether *pin_list* may not yet reflect its owner's latest pin changes.

    True for a smart list with rules while any of its owner's pins awaits a sync. The request does not say which
    lists it will change, so every such list says so.

    Args:
        pin_list: The list being shown.

    Returns:
        True while the list may be stale.
    """
    from urbanlens.dashboard.models.pin_list.model import SmartListSyncRequest

    if not pin_list.is_smart or (pin_list.smart_filter is None and pin_list.smart_boundary is None):
        return False
    return SmartListSyncRequest.objects.for_profile(pin_list.profile_id).exists()
