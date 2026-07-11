"""Request-level middleware for the dashboard app."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from django.db import transaction
from django.http import HttpResponse
from django.utils.html import escape

from urbanlens.dashboard.services.profile_preview import SESSION_KEY, create_ghost_viewer, mode_label

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest

logger = logging.getLogger(__name__)


class ProfilePreviewMiddleware:
    """Render the owner's profile page as a simulated other user during a preview.

    While ``request.session[SESSION_KEY]`` is set (by
    ``ProfilePreviewStartView``), GET requests to the previewed profile page -
    and HTMX fragment requests originating from it - are executed as a
    throwaway "ghost" user created inside a database transaction that is
    rolled back after rendering.  The response is therefore exactly what a
    real user with the selected relationship would receive, including a 404
    when the owner's privacy settings hide the profile from that audience.

    Safety rails:
    - Non-GET requests within the preview scope are rejected (the ghost can
      never mutate data, and neither can the owner while disguised).
    - Navigating to any other page automatically ends the preview.
    - Every previewed full page gets a banner with an exit link injected.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Store the downstream handler.

        Args:
            get_response: The next middleware/view callable in the chain.
        """
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Dispatch the request, simulating the ghost viewer when in preview scope.

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
        """Return True when this request should be rendered as the ghost.

        In scope: the previewed profile page itself, plus any HTMX request
        issued from it (matched via the ``Referer`` path, so new HTMX
        fragments on the page are covered automatically).

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
        """Return True for a full-page browser navigation (not assets or HTMX).

        Args:
            request: The incoming HTTP request.

        Returns:
            Whether the request looks like the user navigating to a new page.
        """
        if request.method != "GET" or request.headers.get("HX-Request"):
            return False
        return "text/html" in request.headers.get("Accept", "")

    def _blocked_response(self, request: HttpRequest) -> HttpResponse:
        """Reject a write attempt made while disguised as the ghost.

        Args:
            request: The incoming HTTP request.

        Returns:
            A 403 response carrying a toast trigger for HTMX callers.
        """
        response = HttpResponse("Actions are disabled while previewing your profile.", status=403)
        if request.headers.get("HX-Request"):
            response["HX-Trigger"] = json.dumps(
                {"showToast": {"level": "warning", "message": "You're previewing your profile - actions are disabled. Exit the preview first."}},
            )
        return response

    def _respond_as_ghost(self, request: HttpRequest, state: dict) -> HttpResponse:
        """Run the request as a freshly-created ghost user and roll everything back.

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
                # TemplateResponses evaluate their querysets during render();
                # that must happen while the ghost's rows still exist.
                if hasattr(response, "render") and not getattr(response, "is_rendered", True):
                    response.render()
            finally:
                transaction.set_rollback(True)
                request.user = real_user

        if not request.headers.get("HX-Request"):
            self._inject_banner(response, mode)
        return response

    def _inject_banner(self, response: HttpResponse, mode: str) -> None:
        """Insert the preview banner just before ``</body>`` of an HTML response.

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
