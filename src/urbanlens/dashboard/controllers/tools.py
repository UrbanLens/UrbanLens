"""Tools controller - data export/import and other user utilities."""

from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Any
import uuid

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.models.pin_suggestions.model import MAX_STORED_VISIT_DATES, MAX_SUGGESTION_PHOTOS, PinSuggestion, PinSuggestionOrigin, PinSuggestionStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.export import (
    EXPORT_TTL_SECONDS as _EXPORT_TTL_SECONDS,
    VALID_EXPORT_TYPES,
    ExportJobStatus,
    cleanup_export_artifacts,
    export_dir as _export_dir_fn,
    schedule_export_cleanup,
)
from urbanlens.dashboard.services.images import compute_checksum
from urbanlens.dashboard.services.import_data import (
    IMPORT_TTL_SECONDS as _IMPORT_TTL_SECONDS,
    ImportJobStatus,
    cleanup_import_artifacts,
    import_dir as _import_dir_fn,
    schedule_import_cleanup,
)
from urbanlens.dashboard.services.pin_suggestions import LocationHit, ingest_location_hits
from urbanlens.dashboard.services.visits import visit_logging_allowed

logger = logging.getLogger(__name__)

_MAX_IMPORT_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB
_MAX_CLUSTERS_PER_REQUEST = 500
_MAX_COUNT_PER_CLUSTER = 2000


def _export_dir(job_id: str) -> str:
    return _export_dir_fn(job_id)


def _import_dir(job_id: str) -> str:
    return _import_dir_fn(job_id)


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
        "dashboard/partials/tools/export_progress.html",
        {"job_id": job_id, "status": "error", "message": message},
    )


def _import_error_partial(request: HttpRequest, job_id: str, message: str) -> HttpResponse:
    """Render an import progress fragment in the error state.

    Args:
        request: The HTTP request.
        job_id: Import job UUID string.
        message: User-facing error message.

    Returns:
        Rendered import progress partial with ``status="error"``.
    """
    return render(
        request,
        "dashboard/partials/tools/import_progress.html",
        {"job_id": job_id, "status": "error", "message": message},
    )


# -- Views ----------------------------------------------------------------------


class ToolsIndexView(LoginRequiredMixin, View):
    """Renders the Tools landing page (data export, import, invite friend, etc.)."""

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render tools/index.html.

        Args:
            request: The authenticated HTTP request.

        Returns:
            Rendered tools page.
        """
        from urbanlens.dashboard.controllers.immich import get_active_scan_task_id

        profile, _ = Profile.objects.get_or_create(user=request.user)
        return render(
            request,
            "dashboard/pages/tools/index.html",
            {
                "show_backup_tools": request.user.has_perm("dashboard.view_site_admin"),
                "profile_uuid": profile.uuid,
                "immich_account": ImmichAccount.objects.get_for_profile(profile),
                "immich_active_scan_task_id": get_active_scan_task_id(profile.pk),
            },
        )


class ExportStartView(LoginRequiredMixin, View):
    """Start a data export job in Celery."""

    def post(self, request: HttpRequest) -> HttpResponse:
        """Accept export parameters, enqueue a Celery task, return progress fragment.

        Args:
            request: POST with ``export_types`` list, optional ``google_takeout``
                flag, and optional ``email_export`` flag (email the finished
                archive to the account address, UL-373).

        Returns:
            Rendered export progress partial so HTMX can swap it in.
        """
        export_types = request.POST.getlist("export_types")
        export_types = [t for t in export_types if t in VALID_EXPORT_TYPES]

        # google_takeout is a format flag passed as a separate checkbox value in export_types.
        if not export_types:
            return HttpResponse(
                '<p class="export-error"><i class="material-symbols-outlined">error</i> Select at least one item to export.</p>',
                status=400,
            )

        email_to_user = bool(request.POST.get("email_export"))

        job_id = str(uuid.uuid4())
        exp_dir = _export_dir(job_id)
        os.makedirs(exp_dir, exist_ok=True)
        ExportJobStatus(job_id).write("pending", 0, "Preparing export...", user_id=request.user.pk)

        base_url = request.build_absolute_uri("/")
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import run_user_data_export

        result = safely_enqueue_task(run_user_data_export, request.user.pk, export_types, exp_dir, base_url, job_id, email_to_user)
        if result is None:
            ExportJobStatus(job_id).write("error", 0, "Export queue is unavailable. Please try again later.", user_id=request.user.pk)
            return render(
                request,
                "dashboard/partials/tools/export_progress.html",
                {"job_id": job_id, "status": "error", "progress": 0, "message": "Export queue is unavailable. Please try again later."},
                status=503,
            )

        logger.info("Export task %s started for user %s", result.id, request.user.pk)

        return render(
            request,
            "dashboard/partials/tools/export_progress.html",
            {"job_id": job_id, "status": "pending", "progress": 0, "message": "Preparing export..."},
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

        try:
            data = ExportJobStatus(job_id).read()

            if not data:
                logger.debug("Job not found or expired: job %s, user %s", job_id, request.user.pk)
                return _export_error_partial(request, job_id, "Export job not found or expired. Please start a new export.")

            if data.get("user_id") != request.user.pk:
                logger.warning("Attempting to view export for unauthorized user: job %s, user %s", job_id, request.user.pk)
                return _export_error_partial(
                    request,
                    job_id,
                    "Could not verify export ownership. Please start a new export.",
                )

            return render(request, "dashboard/partials/tools/export_progress.html", {"job_id": job_id, **data})
        except Exception:
            # Surface a friendly, non-polling error state instead of letting HTMX's poller
            # hang on a raw 500 with no feedback to the user (see ImportStatusView for the
            # same pattern - the fragment's "error" state removes the hx-get polling attrs).
            logger.exception("Unexpected error rendering export status: job %s, user %s", job_id, request.user.pk)
            return _export_error_partial(request, job_id, "Something went wrong checking export status. Please try again.")


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
            job_id = str(uuid.UUID(job_id))
        except ValueError:
            logger.error("Invalid job ID: %s", job_id)  # noqa: TRY400
            return redirect("tools.index")

        exp_dir = _export_dir(job_id)
        data = ExportJobStatus(job_id).read()

        if not data or data.get("user_id") != request.user.pk:
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

        today = datetime.date.today().isoformat()
        fh = open(zip_path, "rb")  # noqa: SIM115 - FileResponse takes ownership and closes the handle
        response = FileResponse(fh, content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="urbanlens_export_{today}.zip"'
        return response


class ExportFormatDownloadView(LoginRequiredMixin, View):
    """Download ALL of the requester's root pins as a single GeoJSON/KML/GPX/CSV file (UL-382).

    The quick, synchronous counterpart to the full ZIP export above: no Celery
    job, no polling - one GET straight to a file download, using the same
    per-format writers the targeted bulk export uses
    (``controllers.pin_bulk.PinBulkExportView``). Root pins only: detail (sub)
    pins share their parent's site and would just duplicate coordinates in
    formats that carry nothing but name/coords/description.
    """

    def get(self, request: HttpRequest, fmt: str) -> HttpResponse:
        """Serve every root pin the requester owns in the requested format.

        Args:
            request: The authenticated HTTP request.
            fmt: Format key from ``EXPORT_FORMATS`` (geojson, kml, gpx, csv).

        Returns:
            The serialized file with an attachment Content-Disposition.

        Raises:
            Http404: If ``fmt`` is not a known export format.
        """
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.services.export_formats import EXPORT_FORMATS

        if fmt not in EXPORT_FORMATS:
            raise Http404("Unknown export format.")

        profile, _ = Profile.objects.get_or_create(user=request.user)
        pins = Pin.objects.filter(profile=profile, parent_pin__isnull=True).select_related("location").order_by("created")

        writer, extension, content_type = EXPORT_FORMATS[fmt]
        content = writer(pins)

        today = datetime.date.today().isoformat()
        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="urbanlens_pins_{today}.{extension}"'
        return response


class ImportStartView(LoginRequiredMixin, View):
    """Accept a UrbanLens export ZIP and start an import job in Celery."""

    def post(self, request: HttpRequest) -> HttpResponse:
        """Validate the upload, save it, enqueue an import task, return progress fragment.

        Args:
            request: POST with ``import_file`` file field.

        Returns:
            Rendered import progress partial so HTMX can swap it in.
        """
        upload = request.FILES.get("import_file")
        if not upload:
            return HttpResponse(
                '<p class="import-error"><i class="material-symbols-outlined">error</i> Please select a file to import.</p>',
                status=400,
            )

        if not (upload.name and upload.name.lower().endswith(".zip")):
            return HttpResponse(
                '<p class="import-error"><i class="material-symbols-outlined">error</i> Only .zip export files are accepted.</p>',
                status=400,
            )

        if upload.size and upload.size > _MAX_IMPORT_SIZE_BYTES:
            return HttpResponse(
                '<p class="import-error"><i class="material-symbols-outlined">error</i> File is too large (max 500 MB).</p>',
                status=400,
            )

        job_id = str(uuid.uuid4())
        imp_dir = _import_dir(job_id)
        os.makedirs(imp_dir, exist_ok=True)

        zip_path = os.path.join(imp_dir, "upload.zip")
        try:
            with open(zip_path, "wb") as fh:
                fh.writelines(upload.chunks())
        except OSError:
            logger.exception("Failed to save import file for user %s", request.user.pk)
            return _import_error_partial(request, job_id, "Failed to save the uploaded file. Please try again.")

        ImportJobStatus(job_id).write("pending", 0, "Preparing import...", user_id=request.user.pk)

        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import run_user_data_import

        result = safely_enqueue_task(run_user_data_import, request.user.pk, zip_path, job_id)
        if result is None:
            ImportJobStatus(job_id).write("error", 0, "Import queue is unavailable. Please try again later.", user_id=request.user.pk)
            return render(
                request,
                "dashboard/partials/tools/import_progress.html",
                {"job_id": job_id, "status": "error", "progress": 0, "message": "Import queue is unavailable. Please try again later."},
                status=503,
            )

        logger.info("Import task %s started for user %s", result.id, request.user.pk)
        return render(
            request,
            "dashboard/partials/tools/import_progress.html",
            {"job_id": job_id, "status": "pending", "progress": 0, "message": "Preparing import..."},
        )


class ImportStatusView(LoginRequiredMixin, View):
    """Return a progress fragment for an in-flight or completed import job."""

    def get(self, request: HttpRequest, job_id: str) -> HttpResponse:
        """Poll import status and return updated HTML fragment.

        Args:
            request: The authenticated HTTP request.
            job_id: UUID string for the import job.

        Returns:
            Rendered import progress partial.
        """
        try:
            uuid.UUID(job_id)
        except ValueError:
            logger.error("Invalid import job ID: %s", job_id)  # noqa: TRY400
            return _import_error_partial(request, job_id, "Invalid import job. Please try again.")

        try:
            data = ImportJobStatus(job_id).read()

            if not data:
                return _import_error_partial(request, job_id, "Import job not found or expired. Please try again.")

            if data.get("user_id") != request.user.pk:
                logger.warning("Unauthorized import status access: job %s, user %s", job_id, request.user.pk)
                return _import_error_partial(request, job_id, "Could not verify import ownership. Please try again.")

            return render(request, "dashboard/partials/tools/import_progress.html", {"job_id": job_id, **data})
        except Exception:
            # Never let an unexpected error surface as a raw 500 to the HTMX poller - it has
            # no error handling and will just silently stop, leaving the progress bar spinning
            # forever with no feedback to the user. Render the error state instead, which
            # drops the hx-get polling attributes and shows a message.
            logger.exception("Unexpected error rendering import status: job %s, user %s", job_id, request.user.pk)
            return _import_error_partial(request, job_id, "Something went wrong checking import status. Please try again.")


class AdminToolsView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Admin-only tools landing page (database backups, future admin utilities)."""

    permission_required = "dashboard.view_site_admin"
    raise_exception = True

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render tools/admin.html.

        Args:
            request: The authenticated admin HTTP request.

        Returns:
            Rendered admin tools page.
        """
        return render(request, "dashboard/pages/tools/admin.html")


class BackupStartView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Queue a manual database backup from the admin tools page."""

    permission_required = "dashboard.view_site_admin"
    raise_exception = True

    def post(self, request: HttpRequest) -> JsonResponse:
        """Queue a database backup task.

        Args:
            request: The authenticated admin HTTP request.

        Returns:
            JSON with task id and status URL, or error.
        """
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import run_database_backup

        result = safely_enqueue_task(run_database_backup)
        if result is None:
            return JsonResponse({"ok": False, "message": "Unable to enqueue backup task."}, status=503)
        return JsonResponse(
            {
                "ok": True,
                "task_id": result.id,
                "status_url": reverse("celery_task_status", kwargs={"task_id": result.id}),
                "message": "Database backup queued.",
            },
            status=202,
        )


def _parse_cluster(cluster: Any) -> list[LocationHit]:
    """Parse and validate one cluster row from the local-scan upload payload.

    Each cluster becomes exactly *one* synthetic hit, carrying the cluster's
    full ``count`` as its ``weight`` and every one of its ``dates`` (see
    ``LocationHit.weight``/``extra_dates``) - matching/clustering only ever
    need one representative point per distinct location, since every hit a
    single cluster used to expand into shared the exact same coordinates.
    This used to create ``count`` separate synthetic hits (one per date,
    cycling through ``dates``) so the shape matched individually-scanned
    hits exactly; with up to 500 clusters allowed per request and up to 2000
    photos per cluster, that could balloon into hundreds of thousands of
    hits, each checked against every one of the profile's pin boundaries in
    ``_match_hits_to_pins`` - easily enough synchronous work to trip a
    reverse proxy's read timeout (504) on submit for a large scan.

    Args:
        cluster: One raw JSON object from the ``clusters`` array.

    Returns:
        A single-item list with the synthetic LocationHit for this cluster,
        or an empty list if the row is malformed (missing/invalid
        coordinates or no valid dates).
    """
    if not isinstance(cluster, dict):
        return []
    try:
        latitude = float(cluster["latitude"])
        longitude = float(cluster["longitude"])
    except (KeyError, TypeError, ValueError):
        return []
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return []

    dates_raw = cluster.get("dates")
    valid_dates: list[str] = []
    if isinstance(dates_raw, list):
        for raw_date in dates_raw[:MAX_STORED_VISIT_DATES]:
            if not isinstance(raw_date, str):
                continue
            try:
                datetime.date.fromisoformat(raw_date)
            except ValueError:
                continue
            valid_dates.append(raw_date)
    if not valid_dates:
        return []

    try:
        count = int(cluster.get("count", len(valid_dates)))
    except (TypeError, ValueError):
        count = len(valid_dates)
    count = max(1, min(count, _MAX_COUNT_PER_CLUSTER))

    label = cluster.get("label")
    label = label.strip()[:255] if isinstance(label, str) and label.strip() else None

    cluster_id = cluster.get("id")
    source_key = cluster_id if isinstance(cluster_id, str) and cluster_id else None

    first_day = datetime.date.fromisoformat(valid_dates[0])
    taken_at = datetime.datetime.combine(first_day, datetime.time(12, 0), tzinfo=datetime.UTC)
    return [
        LocationHit(
            latitude=latitude,
            longitude=longitude,
            taken_at=taken_at,
            label=label,
            source_key=source_key,
            weight=count,
            extra_dates=tuple(valid_dates),
        )
    ]


class PhotoLocationScanUploadView(LoginRequiredMixin, View):
    """POST /tools/photo-scan/upload/ - ingest results from the local folder scanner.

    The scanner (``frontend/ts/entries/photo-location-scan.ts``) clusters and
    de-dupes matches entirely client-side before uploading, so this payload is
    small - one row per cluster, not per photo - and the photo/video files
    themselves never reach the server, only the extracted lat/lng/date/label
    metadata. Feeds the same ``ingest_location_hits`` pipeline the Immich
    sweep uses, so results merge/dedupe against any existing suggestions.
    """

    def post(self, request: HttpRequest) -> JsonResponse:
        """Ingest a batch of pre-clustered location results.

        Args:
            request: POST with a JSON body ``{"clusters": [{"latitude", "longitude", "dates", "count", "label"}, ...]}``.

        Returns:
            JSON summary of suggestions created/updated, or a 400 error.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if not visit_logging_allowed(profile):
            return JsonResponse({"error": "Visit-history tracking is turned off."}, status=403)
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return JsonResponse({"error": "Invalid request body."}, status=400)

        clusters = body.get("clusters") if isinstance(body, dict) else None
        if not isinstance(clusters, list) or not clusters:
            return JsonResponse({"error": "No location clusters provided."}, status=400)
        if len(clusters) > _MAX_CLUSTERS_PER_REQUEST:
            return JsonResponse({"error": f"Too many results at once (max {_MAX_CLUSTERS_PER_REQUEST})."}, status=400)

        hits: list[LocationHit] = []
        for cluster in clusters:
            hits.extend(_parse_cluster(cluster))
        if not hits:
            return JsonResponse({"error": "No valid location data found in upload."}, status=400)

        summary = ingest_location_hits(profile, hits, origin=PinSuggestionOrigin.LOCAL_SCAN)
        return JsonResponse(
            {
                "ok": True,
                "matched_suggestions": summary.matched_suggestions,
                "new_pin_suggestions": summary.new_pin_suggestions,
                "hits_processed": summary.hits_processed,
                "suggestion_ids": summary.suggestion_ids_by_key,
                "review_url": reverse("memories.locations"),
            },
        )


class PhotoLocationScanPhotoUploadView(LoginRequiredMixin, View):
    """POST /tools/photo-scan/upload-photo/ - upload one opt-in candidate photo.

    The local-folder scanner never uploads photo files by default (see
    ``PhotoLocationScanUploadView``) - this endpoint exists only for photos the
    user explicitly checked in the scanner's opt-in picker, immediately after
    the cluster metadata upload has told the client which ``PinSuggestion``
    each cluster became. The image is staged unattached (candidate only) until
    the suggestion is accepted or rejected - see ``services.pin_suggestions.accept_pin_suggestion``
    and ``reject_pin_suggestion``.
    """

    def post(self, request: HttpRequest) -> JsonResponse:
        """Create a candidate Image staged against a pending local-scan suggestion.

        Args:
            request: POST with multipart fields ``suggestion_id`` and ``image``.

        Returns:
            ``{"ok": True, "image_id": ...}`` on success, or an error JSON.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        suggestion_id = request.POST.get("suggestion_id")
        if not suggestion_id or not suggestion_id.isdigit():
            return JsonResponse({"error": "Missing or invalid suggestion_id."}, status=400)

        suggestion = PinSuggestion.objects.filter(
            pk=int(suggestion_id),
            profile=profile,
            origin=PinSuggestionOrigin.LOCAL_SCAN,
            status=PinSuggestionStatus.PENDING,
        ).first()
        if suggestion is None:
            return JsonResponse({"error": "That suggestion is no longer available."}, status=404)

        image_file = request.FILES.get("image")
        if not image_file:
            return JsonResponse({"error": "No image provided."}, status=400)
        content_type = image_file.content_type or ""
        if not content_type.startswith("image/"):
            return JsonResponse({"error": "That file is not an image."}, status=400)

        if Image.objects.filter(pin_suggestion=suggestion).count() >= MAX_SUGGESTION_PHOTOS:
            return JsonResponse({"error": f"You can attach up to {MAX_SUGGESTION_PHOTOS} photos per location."}, status=400)

        from urbanlens.dashboard.models.images.model import MediaKind
        from urbanlens.dashboard.services.images import image_upload_error

        upload_error = image_upload_error(image_file, MediaKind.PHOTO)
        if upload_error:
            message, status = upload_error
            return JsonResponse({"error": message}, status=status)

        checksum = compute_checksum(image_file)
        if Image.objects.filter(profile=profile, checksum=checksum).exists():
            return JsonResponse({"error": "You already uploaded this photo."}, status=409)

        from urbanlens.dashboard.services.storage import quota_error_for_upload

        quota_error = quota_error_for_upload(profile, image_file.size)
        if quota_error:
            return JsonResponse({"error": quota_error}, status=413)

        img = Image.objects.create(image=image_file, profile=profile, checksum=checksum, file_size=image_file.size, pin_suggestion=suggestion)

        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import process_image_upload

        safely_enqueue_task(process_image_upload, img.pk)
        return JsonResponse({"ok": True, "image_id": img.pk}, status=201)
