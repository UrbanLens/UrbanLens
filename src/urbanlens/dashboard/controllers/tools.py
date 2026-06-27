"""Tools controller - data export and other user utilities."""

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
    ExportJobStatus,
    cleanup_export_artifacts,
    export_dir as _export_dir_fn,
    schedule_export_cleanup,
)

logger = logging.getLogger(__name__)


def _export_dir(job_id: str) -> str:
    return _export_dir_fn(job_id)


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
        return render(request, "dashboard/pages/tools/index.html", {"show_backup_tools": request.user.has_perm("dashboard.view_site_admin")})


class ExportStartView(LoginRequiredMixin, View):
    """Start a data export job in Celery."""

    def post(self, request: HttpRequest) -> HttpResponse:
        """Accept export parameters, enqueue a Celery task, return progress fragment.

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
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import run_user_data_export

        result = safely_enqueue_task(run_user_data_export, request.user.pk, export_types, exp_dir, base_url, job_id)
        if result is None:
            ExportJobStatus(job_id).write("error", 0, "Export queue is unavailable. Please try again later.", user_id=request.user.pk)
            return render(
                request,
                "dashboard/partials/export_progress.html",
                {"job_id": job_id, "status": "error", "progress": 0, "message": "Export queue is unavailable. Please try again later."},
                status=503,
            )

        logger.info("Export task %s started for user %s", result.id, request.user.pk)

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


class BackupStartView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Queue a manual database backup from the admin tools page."""

    permission_required = "dashboard.view_site_admin"
    raise_exception = True

    def post(self, request: HttpRequest) -> JsonResponse:
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
