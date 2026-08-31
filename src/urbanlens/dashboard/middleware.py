"""Request-level middleware for the dashboard app."""

from __future__ import annotations

import json
import logging
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
    """Keep the browser holding a current credential for the media origin.

    Uploads are served from their own hostname (``UL_MEDIA_BASE_URL``), which
    the session cookie deliberately does not reach - see
    :mod:`urbanlens.dashboard.services.media.origin`. This mints the separate,
    media-only cookie that does, and refreshes it before it expires, so a page
    rendered on the app origin can display images from the media origin without
    the user noticing there are two hostnames involved.

    Placed below ``AuthenticationMiddleware`` because it needs ``request.user``.

    The common path costs nothing: a browser holding a cookie that is valid,
    unexpired and minted for the current user hits
    :func:`~urbanlens.dashboard.services.media.origin.needs_refresh`, which is a
    signature check against ``request.user.pk`` and no query at all. A cookie is
    written at most twice per lifetime.

    Logout needs no signal handler: the next response after the session ends has
    an anonymous ``request.user``, and a request still carrying the cookie has it
    deleted right there. Anything relying on a signal would have to fire on
    session expiry too, which no signal does.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Store the next handler in the chain.

        Args:
            get_response: The downstream handler.
        """
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Run the request, then add, refresh, or clear the media cookie.

        Args:
            request: The current request.

        Returns:
            The downstream response, with the cookie adjusted where needed.
        """
        from urbanlens.dashboard.services.media.origin import MEDIA_COOKIE_NAME, clear_media_cookie, is_media_origin_request, media_origin, needs_refresh, set_media_cookie

        response = self.get_response(request)
        if not media_origin() or is_media_origin_request(request):
            # Nothing to mint from on the media origin: it has no session, and
            # its responses are file bytes rather than pages a browser will
            # follow up on.
            return response

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            if needs_refresh(request, user.pk):
                set_media_cookie(response, user.pk)
        elif MEDIA_COOKIE_NAME in request.COOKIES:
            clear_media_cookie(response)
        return response


class SecurityHeadersMiddleware:
    """Attach the response headers ``SecurityMiddleware`` has no setting for.

    Django has no built-in setting for ``Permissions-Policy``,
    ``Cross-Origin-Resource-Policy`` or ``X-Permitted-Cross-Domain-Policies`` -
    unlike ``X-Content-Type-Options``/``Referrer-Policy``/``Cross-Origin-Opener-Policy``,
    which ``SecurityMiddleware`` already covers. Values come from
    ``settings.PERMISSIONS_POLICY``/``CROSS_ORIGIN_RESOURCE_POLICY``/``X_PERMITTED_CROSS_DOMAIN_POLICIES``.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Store the next handler in the chain."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Run the request, then attach the headers to whatever it returned."""
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
        return response


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
                {
                    "showToast": {
                        "level": "warning",
                        "message": "You're previewing your profile - actions are disabled. Exit the preview first.",
                    }
                },
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


class WriteSourceMiddleware:
    """Declare that writes during this request came from the signed-in person.

    Field provenance is recorded by interception rather than by asking callers
    to declare it (see ``models/abstract/versioned.py``), which leaves one
    question: whose write is this? Answering it per call site would not survive
    several hundred of them, so it is answered once, here, from context - a
    write inside an authenticated request is that profile's.

    Anonymous requests are left alone: they resolve to SYSTEM, which is
    correct, since nothing a signed-out visitor does should attribute a
    contribution to anybody.

    Placed innermost, below ``AuthenticationMiddleware``, because it needs
    ``request.user`` to already be resolved.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Store the next handler in the chain."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Run the request with the write source bound to the signed-in profile."""
        from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as

        user = getattr(request, "user", None)
        profile_id = None
        if user is not None and user.is_authenticated:
            profile_id = getattr(getattr(user, "profile", None), "pk", None)

        if profile_id is None:
            # Bind SYSTEM explicitly rather than returning early. An early
            # return leaves whatever the last binder on this thread set, so an
            # anonymous request could inherit a previous request's actor.
            with writing_as(WriteSource.SYSTEM, actor=None):
                return self.get_response(request)

        with writing_as(WriteSource.USER, actor=profile_id):
            return self.get_response(request)
