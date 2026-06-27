"""Tools controller - data export and other user utilities."""

from __future__ import annotations

import csv
from datetime import date
import io
import json
import logging
import os
import pathlib
import shutil
import threading
from typing import Any
import uuid
import zipfile

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.db import DatabaseError
from django.http import FileResponse, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views import View

logger = logging.getLogger(__name__)

_EXPORT_TTL_SECONDS = 3600  # Clean up after 1 hour


# ── Helpers ────────────────────────────────────────────────────────────────────


def _export_dir(job_id: str) -> str:
    return os.path.join(settings.MEDIA_ROOT, "exports", job_id)


class ExportJobStatus:
    """Cache-backed progress state for a user export job.

    The export archive remains on disk because it is the final downloadable
    artifact, but transient status belongs in the application cache rather than
    a JSON sidecar file in MEDIA_ROOT.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.cache_key = f"dashboard:export:{job_id}:status"

    def write(self, status: str, progress: int, message: str, user_id: int | None = None) -> None:
        existing = self.read()
        data: dict[str, Any] = {"status": status, "progress": progress, "message": message}
        if user_id is not None:
            data["user_id"] = user_id
        elif "user_id" in existing:
            data["user_id"] = existing["user_id"]
        cache.set(self.cache_key, data, timeout=_EXPORT_TTL_SECONDS)

    def read(self) -> dict[str, Any]:
        return cache.get(self.cache_key) or {}

    def delete(self) -> None:
        cache.delete(self.cache_key)


def _export_error_partial(request: HttpRequest, job_id: str, message: str) -> HttpResponse:
    """Render a progress fragment in the error state for HTMX to swap in.

    Args:
        request: The HTTP request.
        job_id: Export job UUID string.
        message: User-facing error message.

    Returns:
        Rendered export progress partial with ``status="error"``.
    """
    return render(
        request,
        "dashboard/partials/export_progress.html",
        {"job_id": job_id, "status": "error", "message": message},
    )


def _schedule_cleanup(export_dir: str, job_status: ExportJobStatus | None = None) -> None:
    def cleanup_export() -> None:
        shutil.rmtree(export_dir, ignore_errors=True)
        if job_status is not None:
            job_status.delete()

    timer = threading.Timer(_EXPORT_TTL_SECONDS, cleanup_export)
    timer.daemon = True
    timer.start()


# ── Per-type export helpers (run inside thread) ────────────────────────────────


def _export_profile(profile: Any, temp_dir: str) -> None:
    data = {
        "username": profile.user.username,
        "email": profile.user.email,
        "first_name": profile.user.first_name,
        "last_name": profile.user.last_name,
        "bio": profile.bio or "",
        "area": profile.area or "",
        "birth_date": str(profile.birth_date) if profile.birth_date else None,
        "started_exploring": str(profile.started_exploring) if profile.started_exploring else None,
        "theme_mode": profile.theme_mode,
        "date_joined": str(profile.user.date_joined),
    }
    with open(os.path.join(temp_dir, "profile.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _export_pins(profile: Any, temp_dir: str, base_url: str) -> None:
    from urbanlens.dashboard.models.pin.model import Pin

    pins = (
        Pin.objects.filter(profile=profile)
        .select_related("location")
        .prefetch_related("badges")
        .order_by("created")
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Title", "Note", "URL", "Tags", "Comment"])

    for pin in pins:
        name = pin.nickname or (pin.location.name if pin.location else "")
        note = pin.description or ""
        url = f"{base_url.rstrip('/')}/dashboard/map/pin/{pin.slug}/" if pin.slug else ""
        tags = ", ".join(b.name for b in pin.badges.all() if hasattr(b, "name"))
        writer.writerow([name, note, url, tags, ""])

    pathlib.Path(os.path.join(temp_dir, "pins.csv")).write_text(buf.getvalue(), encoding="utf-8", newline="")


def _export_comments(profile: Any, temp_dir: str) -> None:
    from urbanlens.dashboard.models.comments.model import Comment

    comments = (
        Comment.objects.filter(profile=profile)
        .select_related("pin__location", "location")
        .order_by("created")
    )

    rows = []
    for comment in comments:
        if comment.pin:
            target = comment.pin.nickname or (comment.pin.location.name if comment.pin.location else "")
            target_type = "pin"
        elif comment.location:
            target = comment.location.name
            target_type = "location"
        else:
            target = ""
            target_type = ""

        rows.append(
            {
                "id": comment.pk,
                "target_type": target_type,
                "target_name": target,
                "text": comment.text,
                "created": str(comment.created),
            },
        )

    with open(os.path.join(temp_dir, "comments.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)


def _export_photos(profile: Any, temp_dir: str) -> None:
    from urbanlens.dashboard.models.images.model import Image

    images = Image.objects.filter(profile=profile).select_related("pin__location", "location").order_by("created")

    photos_dir = os.path.join(temp_dir, "photos")
    os.makedirs(photos_dir, exist_ok=True)

    metadata = []
    for image in images:
        if image.pin:
            target = image.pin.nickname or (image.pin.location.name if image.pin.location else "")
            target_type = "pin"
        elif image.location:
            target = image.location.name
            target_type = "location"
        else:
            target = ""
            target_type = ""

        file_path = image.image.path if image.image else None
        filename = os.path.basename(file_path) if file_path else None

        if file_path and filename is not None and os.path.exists(file_path):
            dest = os.path.join(photos_dir, filename)
            # Avoid name collisions
            if os.path.exists(dest):
                base, ext = os.path.splitext(filename)
                dest = os.path.join(photos_dir, f"{base}_{image.pk}{ext}")
                filename = os.path.basename(dest)
            shutil.copy2(file_path, dest)

        metadata.append(
            {
                "id": image.pk,
                "filename": filename,
                "caption": image.caption or "",
                "target_type": target_type,
                "target_name": target,
                "latitude": str(image.latitude) if image.latitude else None,
                "longitude": str(image.longitude) if image.longitude else None,
                "created": str(image.created),
            },
        )

    with open(os.path.join(photos_dir, "metadata.json"), "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, ensure_ascii=False)


def _export_trips(profile: Any, temp_dir: str) -> None:
    from urbanlens.dashboard.models.trips.model import Trip

    trips = (
        Trip.objects.filter(profiles=profile)
        .prefetch_related("profiles__user")
        .select_related("creator__user")
        .order_by("created")
    )

    rows = []
    for trip in trips:
        rows.append(
            {
                "id": trip.pk,
                "name": trip.name,
                "description": trip.description or "",
                "start_date": str(trip.start_date) if trip.start_date else None,
                "end_date": str(trip.end_date) if trip.end_date else None,
                "creator": trip.creator.user.username if trip.creator else None,
                "members": [p.user.username for p in trip.profiles.all()],
                "created": str(trip.created),
            },
        )

    with open(os.path.join(temp_dir, "trips.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)


# ── Background export runner ────────────────────────────────────────────────────


def _run_export(user_id: int, export_types: list[str], export_dir: str, base_url: str) -> None:
    """Run the export in a background thread."""
    from django.contrib.auth import get_user_model

    User = get_user_model()

    try:
        user = User.objects.select_related("profile").get(pk=user_id)
        profile = user.profile
    except (ObjectDoesNotExist, AttributeError):
        logger.exception("Export: could not load user %s", user_id)
        ExportJobStatus(os.path.basename(export_dir)).write("error", 0, "Failed to load user data.")
        _schedule_cleanup(export_dir, ExportJobStatus(os.path.basename(export_dir)))
        return

    temp_dir = os.path.join(export_dir, "data")
    os.makedirs(temp_dir, exist_ok=True)

    total_steps = len(export_types) + 1  # +1 for zipping
    step = 0

    exporters = {
        "profile": (_export_profile, "Exporting profile…"),
        "pins": (_export_pins, "Exporting pins…"),
        "comments": (_export_comments, "Exporting comments…"),
        "photos": (_export_photos, "Exporting photos…"),
        "trips": (_export_trips, "Exporting trips…"),
    }

    try:
        _run_export_steps(profile, export_types, exporters, step, total_steps, export_dir=export_dir, temp_dir=temp_dir, base_url=base_url, user_id=user_id)
    except (OSError, DatabaseError, ValueError):
        logger.exception("Export failed for user %s", user_id)
        ExportJobStatus(os.path.basename(export_dir)).write("error", 0, "Export failed. Please try again.")
    finally:
        _schedule_cleanup(export_dir, ExportJobStatus(os.path.basename(export_dir)))


def _run_export_steps(
    profile: Any,
    export_types: list[str],
    exporters: dict[str, Any],
    step: int,
    total_steps: int,
    *,
    export_dir: str,
    temp_dir: str,
    base_url: str,
    user_id: int,
) -> None:
    for key in ["profile", "pins", "comments", "photos", "trips"]:
        if key not in export_types:
            continue
        fn, msg = exporters[key]
        ExportJobStatus(os.path.basename(export_dir)).write("running", max(5, int(step / total_steps * 85)), msg)
        if key == "pins":
            fn(profile, temp_dir, base_url)
        else:
            fn(profile, temp_dir)
        step += 1

    ExportJobStatus(os.path.basename(export_dir)).write("running", 90, "Creating archive…")
    _build_zip(export_dir, temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)
    ExportJobStatus(os.path.basename(export_dir)).write("done", 100, "Export ready!")


def _build_zip(export_dir: str, temp_dir: str) -> None:
    today = date.today().isoformat()
    zip_path = os.path.join(export_dir, "export.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(temp_dir):
            for filename in files:
                file_path = os.path.join(root, filename)
                arcname = os.path.join(f"urbanlens_export_{today}", os.path.relpath(file_path, temp_dir))
                zf.write(file_path, arcname)


# ── Views ──────────────────────────────────────────────────────────────────────


class ToolsIndexView(LoginRequiredMixin, View):
    """Renders the Tools landing page (data export, invite friend, etc.)."""

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render tools/index.html.

        Friend invitations use the existing ``friend.invite_email`` endpoint;
        this view only serves the tools UI shell.

        Args:
            request: The authenticated HTTP request.

        Returns:
            Rendered tools page.
        """
        return render(request, "dashboard/pages/tools/index.html")


class ExportStartView(LoginRequiredMixin, View):
    """Start a data export job in a background thread."""

    def post(self, request: HttpRequest) -> HttpResponse:
        """Accept export parameters, spawn background thread, return progress fragment.

        Args:
            request: POST with ``export_types`` list (profile|pins|comments|photos|trips).

        Returns:
            Rendered export progress partial so HTMX can swap it in.
        """
        export_types = request.POST.getlist("export_types")
        valid = {"profile", "pins", "comments", "photos", "trips"}
        export_types = [t for t in export_types if t in valid]

        if not export_types:
            return HttpResponse(
                '<p class="export-error"><i class="material-icons">error</i> Select at least one item to export.</p>',
                status=400,
            )

        job_id = str(uuid.uuid4())
        exp_dir = _export_dir(job_id)
        os.makedirs(exp_dir, exist_ok=True)
        ExportJobStatus(job_id).write("pending", 0, "Preparing export…", user_id=request.user.pk)

        base_url = request.build_absolute_uri("/")
        thread = threading.Thread(
            target=_run_export,
            args=(request.user.pk, export_types, exp_dir, base_url),
            daemon=True,
        )
        thread.start()
        
        logger.info("Export started for user %s", request.user.pk)

        return render(
            request,
            "dashboard/partials/export_progress.html",
            {"job_id": job_id, "status": "pending", "progress": 0, "message": "Preparing export…"},
        )


class ExportStatusView(LoginRequiredMixin, View):
    """Return a progress fragment for an in-flight or completed export job."""

    def get(self, request: HttpRequest, job_id: str) -> HttpResponse:
        """Poll export status and return updated HTML fragment.

        Args:
            request: The authenticated HTTP request.
            job_id: UUID string for the export job.

        Returns:
            Rendered export progress partial.
        """
        try:
            uuid.UUID(job_id)
        except ValueError:
            logger.error("Invalid job ID: %s", job_id)  # noqa: TRY400
            return _export_error_partial(request, job_id, "Invalid export job. Please start a new export.")

        data = ExportJobStatus(job_id).read()

        if not data:
            logger.debug("Job not found or expired: job %s, user %s", job_id, request.user.pk)
            return _export_error_partial(request, job_id, "Export job not found or expired. Please start a new export.")

        if data.get("user_id") != request.user.pk:
            # TODO: Candidate for sending out a notice
            logger.warning("Attempting to view export for unauthorized user: job %s, user %s", job_id, request.user.pk)
            return _export_error_partial(
                request,
                job_id,
                "Could not verify export ownership. Please start a new export.",
            )

        return render(request, "dashboard/partials/export_progress.html", {"job_id": job_id, **data})


class ExportDownloadView(LoginRequiredMixin, View):
    """Serve the completed export ZIP file."""

    def get(self, request: HttpRequest, job_id: str) -> HttpResponse | FileResponse:
        """Stream the export zip to the browser.

        Args:
            request: The authenticated HTTP request.
            job_id: UUID string for the export job.

        Returns:
            FileResponse with the ZIP, or redirect to tools page on error.
        """
        try:
            uuid.UUID(job_id)
        except ValueError:
            logger.error("Invalid job ID: %s", job_id)  # noqa: TRY400
            return redirect("tools.index")

        exp_dir = _export_dir(job_id)
        data = ExportJobStatus(job_id).read()

        if not data or data.get("user_id") != request.user.pk:
            # TODO: Candidate for sending out a notice
            logger.warning("Attempting to download export for unauthorized user: job %s, user %s", job_id, request.user.pk)
            return redirect("tools.index")

        if data.get("status") != "done":
            logger.warning("Attempting to download export prior to being ready: job %s, user %s", job_id, request.user.pk)
            return redirect("tools.index")

        zip_path = os.path.join(exp_dir, "export.zip")
        if not os.path.exists(zip_path):
            logger.warning("Export file not found: job %s, user %s", job_id, request.user.pk)
            return redirect("tools.index")

        logger.info("Export complete, serving file: job %s, user %s", job_id, request.user.pk)

        today = date.today().isoformat()
        fh = open(zip_path, "rb")  # noqa: SIM115 — FileResponse takes ownership and closes the handle
        response = FileResponse(fh, content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="urbanlens_export_{today}.zip"'
        return response
