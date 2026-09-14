"""Uploads storage failed on wait for storage to recover, rather than being dropped or rejected (P119).

A held icon or avatar and a pending comment image are already in media storage, so retrying needs nothing from their
owner. Waiting must not cost anyone else: each upload's attempts back off from :data:`RETRY_BASE` to :data:`RETRY_CAP`,
one :func:`retry_waiting_uploads` run queues at most :data:`RETRY_BATCH`, and while storage is failing for everyone only
one upload is tried. An upload that keeps failing while storage accepts others is reported to the admins.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError
from django.core.cache import cache
from django.db.models import F
from django.utils import timezone
from redis.exceptions import RedisError

from urbanlens.dashboard.models.upload_retry import UploadRetry

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: How long an upload waits after it first fails past its task's own retries.
RETRY_BASE = timedelta(minutes=5)

#: The longest wait between attempts.
RETRY_CAP = timedelta(days=1)

#: The most attempts one sweep queues.
RETRY_BATCH = 20

#: How long an upload waits before the admins hear of it, if storage accepts other uploads meanwhile.
STUCK_AFTER = timedelta(days=1)

#: How long storage must go on saying a file is gone, while accepting others, before its upload is given up on. Storage
#: pointed at the wrong bucket or directory says every stored file is gone while new uploads still succeed.
GONE_GRACE = timedelta(days=7)

#: How long a pending comment waits for its first scan before it is taken to have been lost.
STALLED_SCAN_AGE = timedelta(hours=1)

COMMENT_IMAGE = "dashboard.Comment.image"
TRIP_COMMENT_IMAGE = "dashboard.TripComment.image"

_LAST_SUCCESS = "upload-storage:last-success"
_LAST_FAILURE = "upload-storage:last-failure"
_GONE_CODES = frozenset({"NoSuchKey", "404", "NotFound"})
_STUCK_LISTED = 50


def means_file_is_gone(exc: BaseException) -> bool:
    """Whether a storage error says the file itself is not there.

    Args:
        exc: What storage raised.

    Returns:
        True for a missing file; False for storage refusing, failing, or misconfigured.
    """
    if isinstance(exc, FileNotFoundError):
        return True
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in _GONE_CODES


def record_storage_success() -> None:
    """Note that storage just served an upload through, which the sweep reads as storage working."""
    _set_stamp(_LAST_SUCCESS)


def is_waiting(target: str, object_id: int) -> bool:
    """Whether an upload is already waiting for storage, so its task leaves pacing to the sweep.

    Args:
        target: The held field's key or comment image target.
        object_id: The row.

    Returns:
        Whether it is waiting.
    """
    return UploadRetry.objects.filter(target=target, object_id=object_id).exists()


def stop_waiting(target: str, object_id: int) -> None:
    """Forget an upload that no longer waits: it was published, rejected, dropped, or its row is gone.

    Args:
        target: The held field's key or comment image target.
        object_id: The row.
    """
    UploadRetry.objects.filter(target=target, object_id=object_id).delete()


def wait_for_storage(target: str, object_id: int, name: str, exc: BaseException) -> None:
    """Keep an upload storage failed on waiting for a retry. A second failure keeps its place in the backoff.

    Args:
        target: The held field's key or comment image target.
        object_id: The row.
        name: The stored name it failed on.
        exc: What storage raised.
    """
    _set_stamp(_LAST_FAILURE)
    _keep_waiting(target, object_id, name, exc)


def _keep_waiting(target: str, object_id: int, name: str, exc: BaseException) -> None:
    now = timezone.now()
    UploadRetry.objects.bulk_create(
        [UploadRetry(target=target, object_id=object_id, name=name, next_attempt_at=now + RETRY_BASE, last_error=f"{type(exc).__name__}: {exc}"[:255])],
        update_conflicts=True,
        unique_fields=["target", "object_id"],
        update_fields=["name", "last_error", "updated"],
    )


def file_is_gone(target: str, object_id: int, name: str, exc: BaseException) -> bool:
    """Whether to give up on an upload whose file storage says is gone; until then it waits, tried once a day.

    Storage pointed at the wrong place says every file is gone, so an upload is given up on only once storage has said
    so for :data:`GONE_GRACE` and has served another upload through since it first did.

    Args:
        target: The held field's key or comment image target.
        object_id: The row.
        name: The stored name.
        exc: What storage raised.

    Returns:
        Whether the caller should give up on it.
    """
    now = timezone.now()
    gone_since = UploadRetry.objects.filter(target=target, object_id=object_id).values_list("gone_since", flat=True).first()
    success = _stamp(_LAST_SUCCESS)
    if gone_since is not None and gone_since <= now - GONE_GRACE and success is not None and success > gone_since.timestamp():
        return True
    # A gone file says nothing about whether storage is working, so it leaves the failure stamp alone.
    _keep_waiting(target, object_id, name, exc)
    waiting = UploadRetry.objects.filter(target=target, object_id=object_id)
    waiting.filter(gone_since__isnull=True).update(gone_since=now)
    waiting.filter(next_attempt_at__lt=now + RETRY_CAP).update(next_attempt_at=now + RETRY_CAP)
    return False


def retry_waiting_uploads() -> int:
    """Queue the uploads whose next attempt is due, pushing each one's following attempt further out.

    Returns:
        How many attempts were queued.
    """
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task

    now = timezone.now()
    _report_stuck(now)
    limit = 1 if _storage_looks_down() else RETRY_BATCH
    queued = 0
    for waiting in UploadRetry.objects.due(now)[:limit]:
        attempt = _attempt(waiting)
        if attempt is None:
            logger.error("Forgetting an upload waiting for storage with an unknown target %r", waiting.target)
            waiting.delete()
            continue
        claimed = UploadRetry.objects.filter(pk=waiting.pk, next_attempt_at=waiting.next_attempt_at).update(attempts=F("attempts") + 1, next_attempt_at=now + _backoff(waiting.attempts + 1))
        if claimed and safely_enqueue_task(*attempt) is not None:
            queued += 1
    return queued


def adopt_stalled_comment_scans() -> int:
    """Have the sweep retry pending comments whose scan never ran, such as one the broker would not queue.

    Returns:
        How many comments were taken up.
    """
    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.models.trips.model import TripComment

    now = timezone.now()
    adopted = 0
    for target, model in ((COMMENT_IMAGE, Comment), (TRIP_COMMENT_IMAGE, TripComment)):
        waiting = UploadRetry.objects.filter(target=target).values("object_id")
        stalled = model.objects.filter(pending_scan=True, created__lt=now - STALLED_SCAN_AGE).exclude(image="").exclude(pk__in=waiting).values_list("pk", "image")[: RETRY_BATCH * 12]
        rows = [UploadRetry(target=target, object_id=pk, name=image, next_attempt_at=now) for pk, image in stalled]
        UploadRetry.objects.bulk_create(rows, ignore_conflicts=True)
        adopted += len(rows)
    return adopted


def give_up(waiting: UploadRetry) -> None:
    """Stop waiting on an upload: drop the held one, or reject the pending comment and tell its author.

    Args:
        waiting: The upload.
    """
    from urbanlens.dashboard.services.media.held_upload import HELD_FIELDS, drop_held

    if waiting.target in HELD_FIELDS:
        drop_held(waiting.target, waiting.object_id, waiting.name)
    elif (model := _comment_model(waiting.target)) is not None:
        from urbanlens.dashboard.tasks import reject_comment_upload

        comment = model.objects.filter(pk=waiting.object_id, pending_scan=True).first()
        if comment is not None:
            reject_comment_upload(comment, "That photo couldn't be processed.")
    waiting.delete()


def _backoff(attempts: int) -> timedelta:
    return min(RETRY_BASE * 2 ** min(attempts, 20), RETRY_CAP)


def _stamp(key: str) -> float | None:
    try:
        value = cache.get(key)
    except RedisError:
        logger.warning("Could not read %s from the cache", key, exc_info=True)
        return None
    return float(value) if value is not None else None


def _set_stamp(key: str) -> None:
    try:
        cache.set(key, timezone.now().timestamp(), timeout=None)
    except RedisError:
        logger.warning("Could not write %s to the cache", key, exc_info=True)


def _storage_looks_down() -> bool:
    failure, success = _stamp(_LAST_FAILURE), _stamp(_LAST_SUCCESS)
    return failure is not None and (success is None or failure >= success)


def _comment_model(target: str) -> Any:
    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.models.trips.model import TripComment

    return {COMMENT_IMAGE: Comment, TRIP_COMMENT_IMAGE: TripComment}.get(target)


def _attempt(waiting: UploadRetry) -> tuple[Callable[..., Any], *tuple[Any, ...]] | None:
    from urbanlens.dashboard import tasks
    from urbanlens.dashboard.services.media.held_upload import HELD_FIELDS

    if waiting.target in HELD_FIELDS:
        return (tasks.publish_held_upload, waiting.target, waiting.object_id, waiting.name)
    if waiting.target == COMMENT_IMAGE:
        return (tasks.scan_comment_image, waiting.object_id)
    if waiting.target == TRIP_COMMENT_IMAGE:
        return (tasks.scan_trip_comment_image, waiting.object_id)
    return None


def _report_stuck(now: datetime) -> None:
    """Tell the admins, once, of uploads that have waited past :data:`STUCK_AFTER` and that storage has served others since they last failed."""
    success = _stamp(_LAST_SUCCESS)
    if success is None or _storage_looks_down():
        return
    stuck = UploadRetry.objects.filter(created__lte=now - STUCK_AFTER, admin_notified_at__isnull=True, updated__lt=datetime.fromtimestamp(success, tz=UTC)).order_by("created")
    listed = list(stuck[:_STUCK_LISTED])
    if not listed:
        return
    from urbanlens.dashboard.services.notifications.notifications import NotificationEvent, notify

    lines = [f"{waiting.target} {waiting.object_id}: waiting since {waiting.created:%Y-%m-%d %H:%M} UTC, {waiting.attempts} attempt(s), last error: {waiting.last_error or 'none recorded'}" for waiting in listed]
    notify(
        NotificationEvent.UPLOAD_STUCK,
        f"{len(listed)} upload(s) stuck waiting for storage",
        "These uploads have kept failing for over a day while storage accepted others, so retrying may never fix them. "
        "They stay pending and are still tried once a day. To give up on one, use Give up in the admin's Upload retries.\n\n" + "\n".join(lines),
    )
    UploadRetry.objects.filter(pk__in=[waiting.pk for waiting in listed]).update(admin_notified_at=now)
