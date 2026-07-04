"""Tools controller - data export/import and other user utilities."""

from __future__ import annotations

from datetime import date
import logging
import os
import uuid

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.http import FileResponse, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.services.export import (
    EXPORT_TTL_SECONDS as _EXPORT_TTL_SECONDS,
    VALID_EXPORT_TYPES,
    ExportJobStatus,
    cleanup_export_artifacts,
    export_dir as _export_dir_fn,
    schedule_export_cleanup,
)
from urbanlens.dashboard.services.import_data import (
    IMPORT_TTL_SECONDS as _IMPORT_TTL_SECONDS,
    ImportJobStatus,
    cleanup_import_artifacts,
    import_dir as _import_dir_fn,
    schedule_import_cleanup,
)

logger = logging.getLogger(__name__)

_MAX_IMPORT_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB


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
        return render(
            request,
            "dashboard/pages/tools/index.html",
            {
                "show_backup_tools": request.user.has_perm("dashboard.view_site_admin"),
            },
        )


class ExportStartView(LoginRequiredMixin, View):
    """Start a data export job in Celery."""

    def post(self, request: HttpRequest) -> HttpResponse:
        """Accept export parameters, enqueue a Celery task, return progress fragment.

        Args:
            request: POST with ``export_types`` list and optional ``google_takeout`` flag.

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

        job_id = str(uuid.uuid4())
        exp_dir = _export_dir(job_id)
        os.makedirs(exp_dir, exist_ok=True)
        ExportJobStatus(job_id).write("pending", 0, "Preparing export...", user_id=request.user.pk)

        base_url = request.build_absolute_uri("/")
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import run_user_data_export

        result = safely_enqueue_task(run_user_data_export, request.user.pk, export_types, exp_dir, base_url, job_id)
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

        today = date.today().isoformat()
        fh = open(zip_path, "rb")  # noqa: SIM115 - FileResponse takes ownership and closes the handle
        response = FileResponse(fh, content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="urbanlens_export_{today}.zip"'
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
