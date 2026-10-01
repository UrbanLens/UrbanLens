"""Confirmed pin imports run as one bulk task per account, never inside the request.

The preview step stops at ``GoogleMapsGateway.MAX_PREVIEW_PINS``, but the confirm
step gets the selection back from the client, so the ceiling is applied again here
before anything is stored. The selection waits on the media volume behind a job id
rather than riding along as a task argument: at the ceiling it is megabytes, and a
task argument that size would ride through the broker instead.

Location History, My Activity and GPS tracks never go to the client: the preview keeps them, and
the confirm step takes them from the preview it names into the stored selection.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import shutil
from typing import TYPE_CHECKING, Any
import uuid

from django.conf import settings
from django.core.cache import DEFAULT_CACHE_ALIAS

from urbanlens.dashboard.services.core import single_flight
from urbanlens.dashboard.services.core.bounded_cache import delete_quietly, get_or_none, set_or_skip
from urbanlens.dashboard.services.import_export.import_data import ImportJobStatus
from urbanlens.dashboard.services.pins.history_import import ImportedHistory, describe_outcome

if TYPE_CHECKING:
    from collections.abc import Generator

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

logger = logging.getLogger(__name__)

#: The task's own limits, sized to the ceiling at the measured per-pin cost (P96).
#: The site-wide hour ends a full-size import partway.
SOFT_TIME_LIMIT_SECONDS = 90 * 60
TIME_LIMIT_SECONDS = SOFT_TIME_LIMIT_SECONDS + 5 * 60

#: Outlives the hard limit, so a second import cannot start beside one still running.
GUARD_TTL_SECONDS = TIME_LIMIT_SECONDS + 10 * 60

#: Pins between progress writes, and between looks for a cancel.
PROGRESS_EVERY = 25

PAYLOAD_FILENAME = "confirmed.json"

#: Its own subtree of ``MEDIA_ROOT``, because a selection can wait for a worker longer than the
#: data import's hour; the vestigial sweep ages this one by the guard instead.
ARTIFACT_DIRNAME = "confirmed_imports"

TERMINAL_STATES = frozenset({"done", "error", "cancelled"})

_CANCEL_LABEL = "confirmed import cancel"

_EXPIRED = "This preview has expired. Please upload the files again."

_HISTORY_MESSAGES = {
    "location_history": "Importing your location history...",
    "my_activity": "Importing your My Activity history...",
    "route": "Saving your routes...",
}


class ConfirmedImportStatus(ImportJobStatus):
    """Job status that outlives the longest a confirmed import can wait for a worker."""

    ttl_seconds = GUARD_TTL_SECONDS


def job_dir(job_id: str) -> str:
    """Where a confirmed import's selection waits for its task.

    Args:
        job_id: The import.

    Returns:
        An absolute path under ``MEDIA_ROOT``.
    """
    return os.path.join(settings.MEDIA_ROOT, ARTIFACT_DIRNAME, job_id)


class ConfirmedImportRefusedError(Exception):
    """The import was not started, and nothing was left behind.

    Attributes:
        message: What to tell the user.
        status: The HTTP status to answer with.
        job_id: The import already running, when that is the reason.
    """

    def __init__(self, message: str, status: int, job_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.job_id = job_id


@dataclass(frozen=True)
class StartedImport:
    """An accepted import.

    Attributes:
        job_id: Identifies it for status and cancel.
        total: Pins across every list.
    """

    job_id: str
    total: int


def guard_key(profile_id: int) -> str:
    """The single-flight key allowing one confirmed import per account.

    Args:
        profile_id: The importing profile.

    Returns:
        The cache key.
    """
    return f"pin_import_confirmed:{profile_id}"


def _cancel_key(job_id: str) -> str:
    return f"pin_import_confirmed:{job_id}:cancel"


def import_cancel_requested(job_id: str) -> bool:
    """Whether someone asked the import to stop.

    Args:
        job_id: The import.

    Returns:
        True once :func:`cancel_confirmed_import` has asked.
    """
    return bool(get_or_none(_cancel_key(job_id), label=_CANCEL_LABEL, alias=DEFAULT_CACHE_ALIAS))


def count_confirmed_pins(confirmed_lists: object, *, ceiling: int, allow_empty: bool = False) -> int:
    """Count a confirmed selection's pins, refusing one the importer should not walk.

    The ceiling is checked on list lengths before any pin is looked at, so an
    oversized selection costs nothing per pin.

    Args:
        confirmed_lists: The ``lists`` value the client posted.
        ceiling: The most pins one import may hold.
        allow_empty: Accept a selection with no pins, for an import that brings history instead.

    Returns:
        Pins across every list.

    Raises:
        ConfirmedImportRefusedError: The selection is empty, malformed, or over
            the ceiling.
    """
    if not isinstance(confirmed_lists, list) or not (confirmed_lists or allow_empty):
        raise ConfirmedImportRefusedError("No lists provided.", 400)
    if not all(isinstance(entry, dict) and isinstance(entry.get("pins"), list) for entry in confirmed_lists):
        raise ConfirmedImportRefusedError("Invalid import payload.", 400)
    total = sum(len(entry["pins"]) for entry in confirmed_lists)
    if total == 0 and not allow_empty:
        raise ConfirmedImportRefusedError("No pins selected for import.", 400)
    if total > ceiling:
        raise ConfirmedImportRefusedError(f"An import can hold at most {ceiling:,} pins, and this one has {total:,}.", 400)
    if not all(isinstance(pin, dict) for entry in confirmed_lists for pin in entry["pins"]):
        raise ConfirmedImportRefusedError("Invalid import payload.", 400)
    return total


def start_confirmed_import(profile: Profile, confirmed_lists: object, *, auto_tag: bool, preview_id: object = None) -> StartedImport:
    """Store a confirmed selection and queue it as the account's one running import.

    Args:
        profile: The importing profile.
        confirmed_lists: The ``lists`` value the client posted.
        auto_tag: Whether created pins get AI category suggestions.
        preview_id: The preview whose history to import with the pins, or None for pins alone.

    Returns:
        The accepted import.

    Raises:
        ConfirmedImportRefusedError: The selection was refused, an import is
            already running, or the job could not be stored or queued.
    """
    from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.services.pins.import_preview import discard_preview_history, read_preview_history
    from urbanlens.dashboard.tasks import run_confirmed_pin_import

    wants_history = preview_id is not None
    total = count_confirmed_pins(confirmed_lists, ceiling=GoogleMapsGateway.MAX_PREVIEW_PINS, allow_empty=wants_history)

    guard = guard_key(profile.pk)
    if not single_flight.claim(guard, GUARD_TTL_SECONDS):
        running = single_flight.holder(guard)
        raise ConfirmedImportRefusedError("An import is already running.", 409, job_id=None if running in {None, single_flight.PENDING} else running)

    job_id = str(uuid.uuid4())
    directory = job_dir(job_id)
    status = ConfirmedImportStatus(job_id)
    history = read_preview_history(profile.user_id, preview_id) if wants_history else None
    if wants_history and history is None:
        _discard(directory, status, guard)
        raise ConfirmedImportRefusedError(_EXPIRED, 410)
    try:
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, PAYLOAD_FILENAME), "w", encoding="utf-8") as handle:
            json.dump({"lists": confirmed_lists, "auto_tag": auto_tag, "history": history}, handle)
        status.write("pending", 0, "Waiting to start...", user_id=profile.user_id, result={"total": total, "current": 0})
        # Recorded before the enqueue: a task that finishes first releases the guard, and adopting after would re-take it.
        single_flight.adopt(guard, job_id, GUARD_TTL_SECONDS)
    except OSError:
        logger.exception("Could not store confirmed import %s for profile %s", job_id, profile.pk)
        _discard(directory, status, guard)
        raise ConfirmedImportRefusedError("The import could not be saved. Please try again.", 503) from None

    if safely_enqueue_task(run_confirmed_pin_import, profile.pk, job_id, durable=False) is None:
        _discard(directory, status, guard)
        raise ConfirmedImportRefusedError("The import queue is unavailable. Please try again shortly.", 503)
    if wants_history:
        discard_preview_history(str(preview_id))
    return StartedImport(job_id=job_id, total=total)


def read_status(user_id: int, job_id: str) -> dict[str, Any] | None:
    """The status of one of *user_id*'s imports.

    Args:
        user_id: The requesting user.
        job_id: The import.

    Returns:
        ``status``, ``progress``, ``message`` and ``result``, or None when there
        is no such import or it is someone else's - deliberately the same answer.
    """
    data = ConfirmedImportStatus(job_id).read()
    if not data or data.get("user_id") != user_id:
        return None
    return {key: value for key, value in data.items() if key != "user_id"}


def cancel_confirmed_import(user_id: int, job_id: str) -> bool:
    """Ask a running import to stop at its next progress write.

    Args:
        user_id: The requesting user.
        job_id: The import.

    Returns:
        Whether *user_id* has such an import. A finished one is left as it is.
    """
    data = read_status(user_id, job_id)
    if data is None:
        return False
    if data.get("status") not in TERMINAL_STATES:
        set_or_skip(_cancel_key(job_id), value=True, timeout=GUARD_TTL_SECONDS, label=_CANCEL_LABEL, alias=DEFAULT_CACHE_ALIAS)
    return True


def run_confirmed_import(profile_id: int, job_id: str) -> dict[str, Any]:
    """Import a stored confirmed selection, writing progress where the client polls for it.

    However it ends, the stored selection is removed and the account's guard is
    released.

    Args:
        profile_id: The importing profile.
        job_id: The job :func:`start_confirmed_import` stored.

    Returns:
        The final counts.
    """
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
    from urbanlens.dashboard.services.core.bulk_followup import batching_follow_on_work
    from urbanlens.dashboard.services.core.task_limits import SOFT_TIME_LIMIT_ERRORS

    status = ConfirmedImportStatus(job_id)
    directory = job_dir(job_id)
    counts: dict[str, Any] = {"total": 0, "current": 0, "created": 0, "exists": 0, "skipped": 0, "deferred": 0}
    history_results: dict[str, dict[str, Any]] = {}
    notices: list[str] = []
    percent = 0
    try:
        profile = Profile.objects.filter(pk=profile_id).first()
        payload = _read_payload(directory)
        if profile is None or payload is None:
            status.write("error", 0, "This import could not be found. Please start it again.", result=counts)
            return counts
        if import_cancel_requested(job_id):
            status.write("cancelled", 0, "Cancelled before it started.", result=counts)
            return counts

        events = _import_events(GoogleMapsGateway(), payload, profile)
        try:
            # One import's per-pin signals (wiki creation, category suggestion, reputation scoring)
            # otherwise queue one broker task each - thousands for a large import, sharing the bulk
            # queue with every other account's jobs (P109). Coalesced into bounded chunks instead.
            with batching_follow_on_work():
                for event in events:
                    kind = event["type"]
                    subtype = event.get("subtype")
                    if subtype is not None:
                        if kind == "error":
                            notices.append(event["message"])
                            continue
                        if kind == "complete":
                            history_results[subtype] = {key: value for key, value in event.items() if key not in {"type", "subtype"}}
                            counts["history"] = describe_outcome(history_results)
                            continue
                        if kind == "progress" and event["current"] % PROGRESS_EVERY and event["current"] != event["total"]:
                            continue
                        if import_cancel_requested(job_id):
                            status.write("cancelled", event.get("percent", 0), "Stopped before your history finished importing.", result=counts)
                            return counts
                        status.write("running", event.get("percent", 0), _HISTORY_MESSAGES.get(subtype, "Importing..."), result=counts)
                        continue
                    if kind == "start":
                        counts["total"] = event["total"]
                        status.write("running", 0, "Importing...", result=counts)
                    elif kind == "progress":
                        percent = event["percent"]
                        counts.update({key: event[key] for key in ("current", "created", "exists", "skipped", "name", "outcome")})
                        if event["current"] % PROGRESS_EVERY and event["current"] != event["total"]:
                            continue
                        if import_cancel_requested(job_id):
                            status.write("cancelled", percent, f"Stopped after {counts['current']:,} of {counts['total']:,} pins.", result=counts)
                            return counts
                        status.write("running", percent, "Importing...", result=counts)
                    elif kind == "complete":
                        counts["deferred"] = event["deferred"]
                    elif kind == "error":
                        status.write("error", percent, event["message"], result=counts)
                        return counts
        finally:
            events.close()
        if notices:
            counts["notices"] = notices
        status.write("done", 100, "Import complete.", result=counts)
    except SOFT_TIME_LIMIT_ERRORS:
        message = f"The import ran out of time after {counts['current']:,} of {counts['total']:,} pins. Run it again to finish; pins already imported are matched, not duplicated."
        status.write("error", percent, message, result=counts)
    except Exception:
        logger.exception("Confirmed import %s failed", job_id)
        status.write("error", percent, "Import failed unexpectedly.", result=counts)
        raise
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        delete_quietly(_cancel_key(job_id), label=_CANCEL_LABEL, alias=DEFAULT_CACHE_ALIAS)
        single_flight.release(guard_key(profile_id))
    return counts


def _import_events(gateway: GoogleMapsGateway, payload: dict[str, Any], profile: Profile) -> Generator[dict[str, Any]]:
    """The pin events, then the history importers' events, each carrying its ``subtype``."""
    lists = payload["lists"]
    if any(entry.get("pins") for entry in lists):
        yield from gateway.iter_confirmed_import_events(lists, profile, auto_tag=bool(payload.get("auto_tag", True)))
    yield from ImportedHistory.from_json(payload.get("history")).iter_import_events(profile)


def _read_payload(directory: str) -> dict[str, Any] | None:
    try:
        with open(os.path.join(directory, PAYLOAD_FILENAME), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and isinstance(payload.get("lists"), list) else None


def _discard(directory: str, status: ImportJobStatus, guard: str) -> None:
    shutil.rmtree(directory, ignore_errors=True)
    status.delete()
    single_flight.release(guard)
