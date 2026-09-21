"""Request-level middleware for the dashboard app."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils.html import escape

from urbanlens.dashboard.services.profile.profile_preview import SESSION_KEY, create_ghost_viewer, mode_label

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest

logger = logging.getLogger(__name__)


class MediaOriginCookieMiddleware:
    """Mint and refresh the media-origin cookie for authenticated requests."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Store the next handler in the chain.

        Args:
            get_response: The downstream handler.
        """
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Add, refresh, or clear the media cookie on the response.

        Args:
            request: The current request.

        Returns:
            The downstream response, with the cookie adjusted where needed.
        """
        from urbanlens.dashboard.services.media.origin import MEDIA_COOKIE_NAME, clear_media_cookie, is_media_origin_request, media_origin, needs_refresh, set_media_cookie

        response = self.get_response(request)
        if not media_origin() or is_media_origin_request(request):
            return response

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            if needs_refresh(request, user.pk):
                set_media_cookie(response, user.pk)
        elif MEDIA_COOKIE_NAME in request.COOKIES:
            clear_media_cookie(response)
        return response


class SecurityHeadersMiddleware:
    """Attach response headers with no Django setting."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Attach configured security headers to the response."""
        response = self.get_response(request)
        policy = getattr(settings, "PERMISSIONS_POLICY", "")
        if policy:
            response.setdefault("Permissions-Policy", policy)
        corp = getattr(settings, "CROSS_ORIGIN_RESOURCE_POLICY", "")
        if corp:
            response.setdefault("Cross-Origin-Resource-Policy", corp)
        cross_domain = getattr(settings, "X_PERMITTED_CROSS_DOMAIN_POLICIES", "")
        if cross_domain:
            response.setdefault("X-Permitted-Cross-Domain-Policies", cross_domain)
        # Report-only so pasted map-overlay images without CORP/CORS still load.
        embedder = getattr(settings, "CROSS_ORIGIN_EMBEDDER_POLICY_REPORT_ONLY", "")
        if embedder:
            response.setdefault("Cross-Origin-Embedder-Policy-Report-Only", embedder)
        return response


class ProfilePreviewMiddleware:
    """Render the owner's profile page as a throwaway ghost viewer during preview."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Store the downstream handler.

        Args:
            get_response: The next middleware/view callable in the chain.
        """
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Dispatch the request, simulating the ghost viewer when in scope.

        Args:
            request: The incoming HTTP request.

        Returns:
            The (possibly simulated and banner-decorated) response.
        """
        state = request.session.get(SESSION_KEY)
        if not state or not request.user.is_authenticated:
            return self.get_response(request)

        if not self._in_scope(request, state):
            # Leaving the profile page ends the preview; ignore asset/API noise.
            if self._is_page_navigation(request):
                del request.session[SESSION_KEY]
            return self.get_response(request)

        if request.method != "GET":
            return self._blocked_response(request)

        return self._respond_as_ghost(request, state)

    def _in_scope(self, request: HttpRequest, state: dict) -> bool:
        """Whether this request belongs to the previewed page.

        Args:
            request: The incoming HTTP request.
            state: The preview session state.

        Returns:
            Whether the request belongs to the previewed page.
        """
        preview_path = state.get("path", "")
        if not preview_path:
            return False
        if request.path == preview_path:
            return True
        if request.headers.get("HX-Request"):
            return urlparse(request.headers.get("Referer", "")).path == preview_path
        return False

    def _is_page_navigation(self, request: HttpRequest) -> bool:
        """Whether this looks like a full-page navigation.

        Args:
            request: The incoming HTTP request.

        Returns:
            Whether the request looks like the user navigating to a new page.
        """
        if request.method != "GET" or request.headers.get("HX-Request"):
            return False
        return "text/html" in request.headers.get("Accept", "")

    def _blocked_response(self, request: HttpRequest) -> HttpResponse:
        """Reject a write attempted during preview.

        Args:
            request: The incoming HTTP request.

        Returns:
            A 403 response carrying a toast trigger for HTMX callers.
        """
        response = HttpResponse("Actions are disabled while previewing your profile.", status=403)
        if request.headers.get("HX-Request"):
            response["HX-Trigger"] = json.dumps(
                {
                    "showToast": {
                        "level": "warning",
                        "message": "You're previewing your profile - actions are disabled. Exit the preview first.",
                    }
                },
            )
        return response

    def _respond_as_ghost(self, request: HttpRequest, state: dict) -> HttpResponse:
        """Run the request as a ghost user inside a rolled-back transaction.

        Args:
            request: The incoming HTTP request.
            state: The preview session state.

        Returns:
            The response as the ghost saw it, with the preview banner injected
            into full HTML pages.
        """
        from urbanlens.dashboard.models.profile.model import Profile

        real_user = request.user
        owner = Profile.objects.filter(user=real_user).first()
        if owner is None or owner.pk != state.get("owner_id"):
            del request.session[SESSION_KEY]
            return self.get_response(request)

        mode = state.get("mode", "")
        with transaction.atomic():
            request.user = create_ghost_viewer(owner, mode)
            try:
                response = self.get_response(request)
                if hasattr(response, "render") and not getattr(response, "is_rendered", True):
                    response.render()
            finally:
                transaction.set_rollback(True)
                request.user = real_user

        if not request.headers.get("HX-Request"):
            self._inject_banner(response, mode)
        return response

    def _inject_banner(self, response: HttpResponse, mode: str) -> None:
        """Insert the preview banner before </body> of an HTML response.

        Args:
            response: The rendered response to decorate (modified in place).
            mode: The active preview mode, used for the banner label.
        """
        content_type = response.get("Content-Type", "")
        if response.streaming or "text/html" not in content_type:
            return
        body_end = response.content.rfind(b"</body>")
        if body_end == -1:
            return

        from django.urls import reverse

        banner = (
            '<div class="profile-preview-banner" role="status">'
            '<i class="material-symbols-outlined">visibility</i>'
            f"<span>Previewing your profile as <strong>{escape(mode_label(mode))}</strong> - this is exactly what they see.</span>"
            f'<a href="{reverse("profile.preview.exit")}" class="profile-preview-exit btn btn--primary">'
            '<i class="material-symbols-outlined">close</i> Exit preview</a>'
            "</div>"
        ).encode()
        response.content = response.content[:body_end] + banner + response.content[body_end:]


class WriteSourceMiddleware:
    """Attribute request-time writes to the signed-in profile, else SYSTEM."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Run the request with the write source bound to the signed-in profile.

        Nothing here is decided unless the request writes. Naming the writer up front cost every
        authenticated request a ``dashboard_profiles`` query, and asking whether there *was* one
        cost an ``auth_user`` query - on a basemap tile, two dozen per map, that was everything the
        tile touched the database for. ``request.user`` resolves once and caches, so a request that
        does write still pays for it once however many rows it writes.
        """
        from urbanlens.dashboard.models.abstract.versioning import request_writer, writing_as

        source, actor = request_writer(request)
        with writing_as(source, actor=actor):
            return self.get_response(request)


class RequestTelemetryMiddleware:
    """Log slow requests with wall, CPU, and SQL breakdowns."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Time the request and log it over the threshold."""
        from django.conf import settings
        from django.db import connection

        threshold_ms = getattr(settings, "UL_SLOW_REQUEST_MS", 1000)
        if threshold_ms <= 0:
            return self.get_response(request)

        stats = _SqlStats()
        response: HttpResponse | None = None
        started, cpu_started = time.perf_counter(), time.thread_time()
        try:
            with connection.execute_wrapper(stats):
                response = self.get_response(request)
        finally:
            wall_ms = (time.perf_counter() - started) * 1000
            if wall_ms >= threshold_ms:
                self._log(request, response, wall_ms, (time.thread_time() - cpu_started) * 1000, stats)
        return response

    @staticmethod
    def _log(
        request: HttpRequest,
        response: HttpResponse | None,
        wall_ms: float,
        cpu_ms: float,
        stats: _SqlStats,
    ) -> None:
        """Emit one line with wall, CPU, and SQL time for a slow request.

        Args:
            request: The request being reported.
            response: The response, or None when the request raised.
            wall_ms: Total time, which is what the user experienced.
            cpu_ms: Time the thread serving the request spent running, which
                separates a busy request from one that was waiting.
            stats: The request's accumulated query time, count and rows.
        """
        match = getattr(request, "resolver_match", None)
        user = getattr(request, "user", None)
        logger.warning(
            "slow request view=%s method=%s status=%s wall_ms=%.0f cpu_ms=%.0f sql_ms=%.0f sql_n=%d sql_rows=%d user=%s bytes=%s path=%s",
            getattr(match, "view_name", "?") if match else "?",
            request.method,
            getattr(response, "status_code", "raised"),
            wall_ms,
            cpu_ms,
            stats.ms,
            stats.count,
            stats.rows,
            getattr(user, "pk", None) if user is not None and user.is_authenticated else None,
            response.get("Content-Length", "-") if response is not None else "-",
            request.path,
        )


class _SqlStats:
    """Per-request SQL time, count, and row accumulator."""

    __slots__ = ("count", "ms", "rows")

    def __init__(self) -> None:
        self.count = 0
        self.ms = 0.0
        self.rows = 0

    def __call__(self, execute: Callable[..., object], sql: str, params: object, many: bool, context: dict) -> object:
        """Run one statement, recording time, count, and rows."""
        started = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            self.ms += (time.perf_counter() - started) * 1000
            self.count += 1
            cursor = context.get("cursor")
            rowcount = getattr(cursor, "rowcount", -1)
            if isinstance(rowcount, int) and rowcount > 0:
                self.rows += rowcount
