"""The import preview reads uploads in the sandbox worker, never in the web process.

Every format the preview accepts - archives, KML, GPX, shapefiles, Word documents - is an
``untrusted_parse`` operation, which refuses the web process under
``UL_UNTRUSTED_PARSE_POLICY=deny``. So the request stores the upload and returns, the sandbox worker
parses it, and whatever needs the network afterwards - a CSV row only a lookup can place, AI
extraction from a document, the admin's parse-failure notice - is finished on an interactive worker,
only when there is some. The dialog polls for the result.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
import json
import logging
import os
import shutil
from typing import TYPE_CHECKING, Any
import uuid

from django.conf import settings
from django.utils import timezone

from urbanlens.dashboard.services.core import single_flight
from urbanlens.dashboard.services.import_export.import_data import ImportJobStatus

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from django.core.files.uploadedfile import UploadedFile

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.import_export.archive_extractor import ExtractedFile, ExtractionBudget

logger = logging.getLogger(__name__)

#: The sandbox parse's own limits, matching the request it replaced: nginx gave that 120 seconds.
PARSE_SOFT_TIME_LIMIT_SECONDS = 110
PARSE_TIME_LIMIT_SECONDS = 130

#: How long a preview's files and status are kept: past a queue wait and both halves.
KEEP_SECONDS = 60 * 60

#: One preview per account at a time, held no longer than its files are kept.
GUARD_TTL_SECONDS = KEEP_SECONDS

#: How long a preview may stay unfinished before the dialog stops waiting and the account is freed:
#: the parse's hard limit, a queue wait, and the finishing lookups. Past it a worker is missing or was
#: killed, and nothing else would ever end the preview.
STALL_AFTER = timedelta(minutes=10)

#: How long a preview waiting for a free parse slot sleeps before asking again.
PARSE_SLOT_RETRY_SECONDS = 15

#: Enough retries to wait out ``STALL_AFTER``, where the preview ends itself instead.
PARSE_SLOT_MAX_RETRIES = int(STALL_AFTER.total_seconds()) // PARSE_SLOT_RETRY_SECONDS + 1

#: A slot outlives the parse's hard limit, so a killed worker's slot frees itself.
_PARSE_SLOT_TTL_SECONDS = PARSE_TIME_LIMIT_SECONDS + 60

ARTIFACT_DIRNAME = "import_previews"

_UPLOADS = "uploads"
_MANIFEST = "manifest.json"
_PARSED = "parsed.json"
_RESULT = "preview.json"

_NO_LISTS = "No valid location files found in the upload."
_NOT_FOUND = "This upload could not be found. Please upload it again."
_UNREADABLE = "The files could not be read."
_UNFINISHED = "Part of this upload needs a lookup that is unavailable right now, so it is not shown."
_STALLED = "Reading these files did not finish. Please try again."

_WAITING = frozenset({"pending", "running"})


class ImportPreviewStatus(ImportJobStatus):
    """Job status for a preview, kept as long as its files are."""

    ttl_seconds = KEEP_SECONDS


class ImportPreviewRefusedError(Exception):
    """The preview was not started, and nothing was left behind.

    Attributes:
        message: What to tell the user.
        status: The HTTP status to answer with.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class _UnreadableUploadError(Exception):
    """An upload the preview gives up on whole, carrying the message to show."""


def job_dir(job_id: str) -> str:
    """Where a preview's upload, intermediate parse and result are kept.

    Args:
        job_id: The preview.

    Returns:
        An absolute path under ``MEDIA_ROOT``.
    """
    return os.path.join(settings.MEDIA_ROOT, ARTIFACT_DIRNAME, job_id)


def guard_key(profile_id: int) -> str:
    """The single-flight key allowing one preview per account.

    Args:
        profile_id: The uploading profile.

    Returns:
        The cache key.
    """
    return f"pin_import_preview:{profile_id}"


def parse_slot_key(index: int) -> str:
    """The single-flight key for one of the site-wide slots a preview is read in.

    Args:
        index: Which slot, below ``IMPORT_PREVIEW_MAX_CONCURRENT_PARSES``.

    Returns:
        The cache key.
    """
    return f"pin_import_preview_parse_slot:{index}"


def start_import_preview(profile: Profile, uploads: Iterable[UploadedFile]) -> str:
    """Store an upload for the sandbox worker to read, as the account's one preview.

    Args:
        profile: The uploading profile.
        uploads: The validated uploaded files.

    Returns:
        The preview's job id.

    Raises:
        ImportPreviewRefusedError: A preview is already being read, or this one could not be
            stored or queued.
    """
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import parse_import_preview_task

    guard = guard_key(profile.pk)
    if not single_flight.claim(guard, GUARD_TTL_SECONDS):
        raise ImportPreviewRefusedError("Your previous upload is still being read.", 409)

    job_id = str(uuid.uuid4())
    directory = job_dir(job_id)
    status = ImportPreviewStatus(job_id)
    try:
        os.makedirs(os.path.join(directory, _UPLOADS), exist_ok=True)
        names: list[str] = []
        for index, upload in enumerate(uploads):
            with open(os.path.join(directory, _UPLOADS, str(index)), "wb") as handle:
                handle.writelines(upload.chunks())
            names.append(upload.name or "")
        _write_json(directory, _MANIFEST, names)
        status.write("pending", 0, "Waiting to read your files...", user_id=profile.user_id, result={"started_at": timezone.now().isoformat(), "profile_id": profile.pk})
        # Recorded before the enqueue: a task that finishes first releases the guard, and adopting after would re-take it.
        single_flight.adopt(guard, job_id, GUARD_TTL_SECONDS)
    except OSError:
        logger.exception("Could not store import preview %s for profile %s", job_id, profile.pk)
        _discard(directory, status, guard)
        raise ImportPreviewRefusedError("The upload could not be saved. Please try again.", 503) from None

    if safely_enqueue_task(parse_import_preview_task, profile.pk, job_id) is None:
        _discard(directory, status, guard)
        raise ImportPreviewRefusedError("File reading is unavailable. Please try again shortly.", 503)
    return job_id


def parse_import_preview(profile_id: int, job_id: str) -> bool:
    """Read a stored upload in the sandbox worker, finishing here when nothing needs the network.

    Only ``IMPORT_PREVIEW_MAX_CONCURRENT_PARSES`` previews are read at once site-wide. One that finds
    no slot free is left as it is to be tried again, until it has waited past ``STALL_AFTER``.
    However a read ends the uploaded files are removed, and the account's guard is released unless
    the networked half was queued, which releases it instead.

    Args:
        profile_id: The uploading profile.
        job_id: The preview :func:`start_import_preview` stored.

    Returns:
        False when the preview is waiting for a slot and should be tried again, otherwise True.
    """
    from celery.exceptions import SoftTimeLimitExceeded

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import finish_import_preview_task

    status = ImportPreviewStatus(job_id)
    directory = job_dir(job_id)
    slot = _claim_parse_slot(job_id)
    if slot is None:
        state = status.read()
        waiting = state.get("status") in _WAITING
        if waiting and not _stalled(state):
            status.write("pending", 0, "Waiting for other uploads to finish reading...")
            return False
        if waiting:
            status.write("error", 100, _STALLED)
        shutil.rmtree(os.path.join(directory, _UPLOADS), ignore_errors=True)
        _release_guard(profile_id, job_id)
        return True

    handed_off = False
    try:
        profile = Profile.objects.filter(pk=profile_id).first()
        names = _read_json(directory, _MANIFEST)
        if profile is None or not isinstance(names, list):
            status.write("error", 0, _NOT_FOUND)
            return True
        status.write("running", 0, "Reading your files...")
        parsed = _read_uploads(profile, directory, names)
        warnings: list[str] = []
        if parsed["unresolved"] or parsed["documents"] or parsed["failed_formats"]:
            _write_json(directory, _PARSED, parsed)
            handed_off = safely_enqueue_task(finish_import_preview_task, profile_id, job_id) is not None
            if handed_off:
                return True
            warnings.append(_UNFINISHED)
        _write_result(job_id, directory, parsed["lists"], warnings)
    except _UnreadableUploadError as exc:
        status.write("error", 100, str(exc))
    except SoftTimeLimitExceeded:
        status.write("error", 0, "These files took too long to read. Try a smaller upload.")
    except Exception:
        logger.exception("Import preview %s could not be read", job_id)
        status.write("error", 0, _UNREADABLE)
        raise
    finally:
        _release_key(slot, job_id)
        shutil.rmtree(os.path.join(directory, _UPLOADS), ignore_errors=True)
        if not handed_off:
            _release_guard(profile_id, job_id)
    return True


def finish_import_preview(profile_id: int, job_id: str) -> None:
    """Finish what the sandbox could not: lookups, AI extraction, and the admin's parse-failure notice.

    Releases the account's guard however it ends.

    Args:
        profile_id: The uploading profile.
        job_id: The preview :func:`parse_import_preview` handed off.
    """
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.ai.document_import import DocumentTooLargeError, ai_document_import_available, extract_pins_from_text
    from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway, _notify_pin_import_parse_failure

    status = ImportPreviewStatus(job_id)
    directory = job_dir(job_id)
    try:
        profile = Profile.objects.filter(pk=profile_id).first()
        parsed = _read_json(directory, _PARSED)
        if profile is None or not isinstance(parsed, dict):
            status.write("error", 0, _NOT_FOUND)
            return
        for fmt in parsed["failed_formats"]:
            _notify_pin_import_parse_failure(fmt)

        gateway = GoogleMapsGateway()
        lists: list[dict[str, Any]] = parsed["lists"]
        room = gateway.MAX_PREVIEW_PINS - sum(len(entry["pins"]) for entry in lists)
        placed, unavailable = gateway.resolve_preview_rows(parsed["unresolved"], profile, room=room)
        for entry in placed:
            _merge(lists, entry)

        warnings: list[str] = [_UNFINISHED] if unavailable else []
        if ai_document_import_available(profile):
            for document in parsed["documents"]:
                too_large = f"Document too large: {document['name']}"
                if document["too_large"]:
                    warnings.append(too_large)
                    continue
                if not document["text"]:
                    continue
                try:
                    found, warning = extract_pins_from_text(document["name"], document["text"], profile)
                except DocumentTooLargeError:
                    warnings.append(too_large)
                    continue
                if warning:
                    warnings.append(warning)
                if found:
                    lists.append(found)
        _write_result(job_id, directory, lists, warnings)
    except Exception:
        logger.exception("Import preview %s could not be finished", job_id)
        status.write("error", 0, _UNREADABLE)
        raise
    finally:
        with contextlib.suppress(OSError):
            os.remove(os.path.join(directory, _PARSED))
        _release_guard(profile_id, job_id)


def read_preview(user_id: int, job_id: str) -> dict[str, Any] | None:
    """One of *user_id*'s previews: its status, and its result once it is done.

    Args:
        user_id: The requesting user.
        job_id: The preview.

    Returns:
        ``status``, ``progress`` and ``message``, plus ``result`` once done; None when there is
        no such preview or it is someone else's - deliberately the same answer.
    """
    data = ImportPreviewStatus(job_id).read()
    if not data or data.get("user_id") != user_id:
        return None
    state = {key: value for key, value in data.items() if key != "user_id"}
    if state.get("status") in _WAITING and _stalled(state):
        ImportPreviewStatus(job_id).write("error", 100, _STALLED)
        profile_id = (state.get("result") or {}).get("profile_id")
        if isinstance(profile_id, int):
            _release_guard(profile_id, job_id)
        return {"status": "error", "progress": 100, "message": _STALLED}
    if state.get("status") == "done":
        result = _read_json(job_dir(job_id), _RESULT)
        if not isinstance(result, dict):
            return {"status": "error", "progress": 100, "message": "This preview has expired. Please upload the files again."}
        state["result"] = result
    return state


def _read_uploads(profile: Profile, directory: str, names: list[str]) -> dict[str, Any]:
    from urbanlens.dashboard.services.ai.document_import import is_supported_document_filename, read_document_text
    from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

    documents: list[dict[str, Any]] = []
    others: list[tuple[str, str]] = []
    for index, name in enumerate(names):
        path = os.path.join(directory, _UPLOADS, str(index))
        # Documents first: a .docx starts with ZIP magic bytes.
        if not is_supported_document_filename(name):
            others.append((name, path))
            continue
        with open(path, "rb") as handle:
            text, too_large = read_document_text(name, handle.read())
        documents.append({"name": name, "text": text, "too_large": too_large})

    parse = GoogleMapsGateway().parse_for_preview(_uploaded_files(others), profile)
    return {"lists": parse.lists, "unresolved": parse.unresolved, "failed_formats": parse.failed_formats, "documents": documents}


def _uploaded_files(uploads: list[tuple[str, str]]) -> Iterator[tuple[str, bytes]]:
    """Each file the upload holds, archives expanded, read only when the parser asks for it.

    Raises:
        _UnreadableUploadError: An uploaded archive could not be extracted.
    """
    from urbanlens.dashboard.services.import_export.archive_extractor import ExtractionBudget, is_archive

    # One allowance for the whole upload: the extractor's limits are per archive, and each nested archive calls it again.
    budget = ExtractionBudget()
    for name, path in uploads:
        with open(path, "rb") as handle:
            data = handle.read()
        if is_archive(data):
            yield from _archive_files(name, data, budget)
        else:
            yield name, data


def _archive_files(name: str, data: bytes, budget: ExtractionBudget) -> Iterator[tuple[str, bytes]]:
    from urbanlens.dashboard.services.apis.locations.google.maps import _filename_stem
    from urbanlens.dashboard.services.import_export.archive_extractor import is_archive, iter_archive

    try:
        entries = iter_archive(data, budget)
        first = next(entries, None)
        if first is None:
            return
        second = next(entries, None)
        # A KMZ wraps one "doc.kml" whatever the user named it, so the outer name is the useful one.
        if second is None and not is_archive(first.data) and _filename_stem(first.name) == "doc":
            suffix = first.name.rsplit(".", 1)[-1] if "." in first.name else ""
            stem = _filename_stem(name)
            yield (f"{stem}.{suffix}" if suffix else stem), first.data
            return
        yield from _expanded(first, budget)
        del first
        if second is not None:
            yield from _expanded(second, budget)
            del second
        for entry in entries:
            yield from _expanded(entry, budget)
    except ValueError as exc:
        logger.warning("Could not extract archive: %s", exc)
        raise _UnreadableUploadError("Invalid archive.") from None


def _expanded(entry: ExtractedFile, budget: ExtractionBudget) -> Iterator[tuple[str, bytes]]:
    from urbanlens.dashboard.services.import_export.archive_extractor import is_archive, iter_archive

    if not is_archive(entry.data):
        yield entry.name, entry.data
        return
    try:
        for inner in iter_archive(entry.data, budget):
            yield inner.name, inner.data
    except ValueError:
        logger.warning("Could not extract nested archive during preview")


def _claim_parse_slot(job_id: str) -> str | None:
    for index in range(settings.IMPORT_PREVIEW_MAX_CONCURRENT_PARSES):
        key = parse_slot_key(index)
        if single_flight.holder(key) == job_id or single_flight.claim(key, _PARSE_SLOT_TTL_SECONDS):
            single_flight.adopt(key, job_id, _PARSE_SLOT_TTL_SECONDS)
            return key
    return None


def _write_result(job_id: str, directory: str, lists: list[dict[str, Any]], warnings: list[str]) -> None:
    from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

    status = ImportPreviewStatus(job_id)
    if not lists:
        status.write("error", 100, warnings[0] if warnings else _NO_LISTS)
        return
    total = sum(len(entry["pins"]) for entry in lists)
    ceiling = GoogleMapsGateway.MAX_PREVIEW_PINS
    if total >= ceiling:
        # Said out loud: a preview stopped at the cap looks exactly like a file that only had that many pins.
        warnings = [*warnings, f"This upload is at the preview limit of {ceiling:,} pins - anything beyond that is not shown."]
    _write_json(directory, _RESULT, {"lists": lists, "total": total, "warnings": warnings})
    status.write("done", 100, "Ready.")


def _stalled(state: dict[str, Any]) -> bool:
    started = (state.get("result") or {}).get("started_at")
    if not isinstance(started, str):
        return False
    try:
        return timezone.now() - datetime.fromisoformat(started) > STALL_AFTER
    except ValueError:
        return False


def _release_guard(profile_id: int, job_id: str) -> None:
    _release_key(guard_key(profile_id), job_id)


def _release_key(key: str, job_id: str) -> None:
    if single_flight.holder(key) == job_id:
        single_flight.release(key)


def _merge(lists: list[dict[str, Any]], placed: dict[str, Any]) -> None:
    for entry in lists:
        if entry["stem"] == placed["stem"]:
            entry["pins"].extend(placed["pins"])
            return
    lists.append(placed)


def _read_json(directory: str, name: str) -> Any:
    try:
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write_json(directory: str, name: str, value: object) -> None:
    os.makedirs(directory, exist_ok=True)
    scratch = os.path.join(directory, f"{name}.part")
    with open(scratch, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
    os.replace(scratch, os.path.join(directory, name))


def _discard(directory: str, status: ImportPreviewStatus, guard: str) -> None:
    shutil.rmtree(directory, ignore_errors=True)
    status.delete()
    single_flight.release(guard)
