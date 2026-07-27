"""Authenticated media gate - every ``/media/...`` request is served through this view.

Historically nginx served ``location /media/`` straight off disk with no auth
check, so anyone who guessed (or was leaked) a filename could fetch any user's
uploaded photos. This view closes that hole: nginx now proxies ``/media/`` to
Django like any other app route, this view authenticates the requester and
authorizes them against the owning row for the requested file, and then either:

- **Behind nginx** (``settings.MEDIA_X_ACCEL``): responds with an
  ``X-Accel-Redirect`` header pointing at the ``internal``-only
  ``/_protected_media/`` location (see ``src/urbanlens/config/nginx/django.conf``),
  so nginx streams the bytes efficiently and picks the Content-Type itself.
- **Local dev / no nginx**: streams the file directly with ``FileResponse``.

Authorization is derived from the first path segment (the ``upload_to`` prefix
of the owning model's file field):

- ``pin_images/`` - :class:`~urbanlens.dashboard.models.images.model.Image`
  rows (pin/wiki/memories galleries, safety check-ins, DM attachments). The
  uploader always qualifies; DM-only attachments are restricted to the two
  conversation participants; everything else follows the same
  ``Image.objects.visible_to`` photo-visibility logic the gallery views use.
- ``comment_images/`` - pin/wiki ``Comment`` and ``TripComment`` images.
  The author always qualifies; pin comments are additionally visible to the
  pin's owner (pin comment threads are owner+author scoped, see
  ``controllers.comments.PinCommentsView``), wiki comments to anyone who can
  see the wiki (``services.wiki_access.location_visible_to``), and trip
  comments to the trip's members.
- ``avatars/`` - profile avatars render site-wide next to usernames, so any
  authenticated user may fetch them.
- ``pin_custom_icons/`` / ``label_icons/`` - map/label icon decorations;
  authenticated-only (see the TODOs inline and docs/PROBLEMS.md).

Unknown prefixes and files with no surviving owner row fall back to
authenticated-only access rather than 404, since the file may legitimately
exist without a resolvable owner (e.g. an orphan left by a deleted row).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import FileResponse, Http404, HttpResponse
from django.views import View

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.http.response import HttpResponseBase

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class MediaThrottledError(Exception):
    """A credential-authenticated media fetch that exceeded its rate budget."""


class MediaGateView(View):
    """Authenticate and authorize a request for one file under ``MEDIA_ROOT``.

    Accepts either a logged-in session (the browser case) or an external API
    credential holding the ``media:read`` scope (the native/mobile client
    case). A credential is resolved only as far as *which profile is asking*;
    it then walks the byte-for-byte identical authorization policy in
    :meth:`_authorized`, so holding a key can never reach a file the same
    person couldn't reach while logged in.

    Anonymous browser requests are redirected to the login page. An
    API-shaped request (one carrying an ``Authorization`` header) that fails
    to authenticate gets 404 instead, since redirecting an API client to an
    HTML login form is useless and the redirect itself would confirm the
    path exists.

    Authorization failures raise ``Http404`` rather than 403, deliberately
    indistinguishable from a file that doesn't exist - the same no-oracle
    policy the wiki access gate follows, so probing media URLs can't confirm
    that a particular file exists but belongs to someone else.
    """

    def get(self, request: HttpRequest, path: str) -> HttpResponseBase:
        """Serve (or hand off to nginx) one media file the requester may see.

        Args:
            request: The current request, carrying either a session or an
                external API credential.
            path: The requested path relative to ``MEDIA_ROOT``, straight from
                the URL (untrusted - may attempt traversal).

        Returns:
            An ``X-Accel-Redirect`` response when nginx fronts the app,
            otherwise a ``FileResponse`` streaming the file. A login redirect
            for an anonymous browser request, or 429 when a credential
            exceeded its media budget.

        Raises:
            Http404: The path escapes ``MEDIA_ROOT``, the file doesn't exist,
                or the requester isn't authorized to see it.
        """
        # Resolved before the path is touched: _resolve_media_path raises 404
        # for a nonexistent file, so running it first would let an
        # unauthenticated caller distinguish real paths from invented ones.
        try:
            profile = self._resolve_profile(request)
        except MediaThrottledError:
            return HttpResponse("Too many media requests.", status=429)

        if profile is None:
            if request.META.get("HTTP_AUTHORIZATION"):
                raise Http404
            return redirect_to_login(request.get_full_path(), str(settings.LOGIN_URL))

        rel_path, full_path = self._resolve_media_path(path)

        if not self._authorized(profile, rel_path):
            logger.info("Denied media request for %s by profile %s", rel_path, profile.pk)
            raise Http404

        if getattr(settings, "MEDIA_X_ACCEL", False):
            # Hand the actual byte-serving back to nginx: the internal-only
            # /_protected_media/ location aliases the media volume. Content-Type
            # is deliberately left unset so nginx derives it from the file
            # extension via its own mime.types.
            response = HttpResponse()
            del response["Content-Type"]
            response["X-Accel-Redirect"] = settings.MEDIA_X_ACCEL_PREFIX + quote(rel_path)
            return response

        return FileResponse(full_path.open("rb"))

    def _resolve_profile(self, request: HttpRequest) -> Profile | None:
        """Identify the profile making this request, by session or by credential.

        Args:
            request: The current request.

        Returns:
            The requesting profile, or None when the request is anonymous or
            carries a credential that doesn't grant ``media:read``.

        Raises:
            MediaThrottledError: A valid credential exceeded its media rate budget.
        """
        from urbanlens.dashboard.models.profile.model import Profile

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            profile, _ = Profile.objects.get_or_create(user=user)
            return profile

        resolved = self._profile_from_credential(request)
        if resolved is None:
            return None
        credential_user, credential = resolved

        # Metered only on this branch: a session request is already bounded by
        # the site's own login and session handling, while a bearer credential
        # is exactly the thing that could be scripted into a CDN.
        self._enforce_media_throttle(request, credential)

        profile, _ = Profile.objects.get_or_create(user=credential_user)
        return profile

    def _profile_from_credential(self, request: HttpRequest) -> tuple[object, object] | None:
        """Authenticate an API key or OAuth2 token and require ``media:read``.

        Args:
            request: The current request.

        Returns:
            A ``(user, credential)`` pair when a credential authenticated and
            grants ``media:read``, else None.
        """
        from oauth2_provider.contrib.rest_framework import OAuth2Authentication
        from rest_framework.exceptions import AuthenticationFailed
        from rest_framework.request import Request as DrfRequest

        from urbanlens.dashboard.external_api.authentication import ApiKeyAuthentication
        from urbanlens.dashboard.external_api.permissions import credential_grants
        from urbanlens.dashboard.models.account.model import ApiKeyScope

        if not request.META.get("HTTP_AUTHORIZATION"):
            return None

        # The DRF authenticators expect a DRF Request; wrapping keeps them on
        # their supported interface rather than relying on HttpRequest
        # happening to expose enough of it.
        drf_request = DrfRequest(request)
        for authenticator in (ApiKeyAuthentication(), OAuth2Authentication()):
            try:
                result = authenticator.authenticate(drf_request)
            except AuthenticationFailed:
                continue
            if result is None:
                continue
            credential_user, credential = result
            # The same scope check HasApiKeyScope runs, via the shared helper -
            # a second implementation here is exactly how the two would drift.
            if not credential_grants(credential, {ApiKeyScope.MEDIA_READ}):
                logger.info("Media request rejected: credential lacks %s", ApiKeyScope.MEDIA_READ.value)
                return None
            return credential_user, credential
        return None

    def _enforce_media_throttle(self, request: HttpRequest, credential: object) -> None:
        """Count this fetch against the credential's media budget.

        Args:
            request: The current request; ``auth`` is set on it so the
                throttle's per-credential cache key can be derived exactly as
                it is for the DRF-served endpoints.
            credential: The authenticated credential.

        Raises:
            MediaThrottledError: The credential is over its budget.
        """
        from urbanlens.dashboard.external_api.throttling import ExternalApiMediaThrottle

        request.auth = credential  # type: ignore[attr-defined]
        if not ExternalApiMediaThrottle().allow_request(request, self):
            raise MediaThrottledError

    def _resolve_media_path(self, path: str) -> tuple[str, Path]:
        """Resolve the requested path and verify it stays inside ``MEDIA_ROOT``.

        Args:
            path: The untrusted relative path from the URL.

        Returns:
            Tuple of (normalized POSIX-style path relative to ``MEDIA_ROOT``,
            resolved absolute ``Path`` of the file on disk).

        Raises:
            Http404: The path is empty, contains a NUL byte, resolves outside
                ``MEDIA_ROOT`` (traversal attempt), or isn't an existing file.
        """
        if not path or "\x00" in path:
            raise Http404

        media_root = Path(settings.MEDIA_ROOT).resolve()
        try:
            full_path = (media_root / path).resolve()
        except (OSError, ValueError) as exc:
            raise Http404 from exc

        if full_path == media_root or not full_path.is_relative_to(media_root):
            logger.warning("Blocked media path traversal attempt: %r", path)
            raise Http404

        if not full_path.is_file():
            raise Http404

        return full_path.relative_to(media_root).as_posix(), full_path

    def _authorized(self, profile: Profile, rel_path: str) -> bool:
        """Decide whether *profile* may fetch the file at *rel_path*.

        Dispatches on the leading path segment, which is the ``upload_to``
        prefix of the model field the file belongs to (see the module
        docstring for the family-by-family policy).

        Args:
            profile: The authenticated requester's profile.
            rel_path: Normalized path relative to ``MEDIA_ROOT``
                (e.g. ``"pin_images/abc.webp"``).

        Returns:
            True when the requester may see the file.
        """
        family = rel_path.split("/", 1)[0]
        if family == "pin_images":
            return self._authorize_image(profile, rel_path)
        if family == "comment_images":
            return self._authorize_comment_image(profile, rel_path)
        if family == "avatars":
            # Avatars render site-wide (comments, friend lists, messages)
            # beside their owner's username; any authenticated user may fetch.
            return True
        if family in {"pin_custom_icons", "label_icons"}:
            # TODO(media-auth): icon decorations are authenticated-only for now.
            # Enforcing strict ownership here risks breaking any surface that
            # renders another user's labeled/shared pin (shared pin views, trip
            # member maps). See docs/PROBLEMS.md "Authenticated media gate -
            # residual per-family risk".
            return True
        # TODO(media-auth): unknown path family - no owning model identified.
        # Authenticated-only fallback; see docs/PROBLEMS.md.
        logger.info("Media request for unrecognized path family %r served authenticated-only", family)
        return True

    def _authorize_image(self, profile: Profile, rel_path: str) -> bool:
        """Authorize a ``pin_images/`` file via its ``Image`` row.

        The uploader always qualifies. Direct-message-only attachments are
        restricted to the DM's sender and recipient. Anything else (pin/wiki
        gallery photos, memories uploads, safety check-in photos) follows the
        same ``Image.objects.visible_to`` filtering the gallery views apply.

        Args:
            profile: The authenticated requester's profile.
            rel_path: Path relative to ``MEDIA_ROOT``.

        Returns:
            True when the requester may see the image.
        """
        from urbanlens.dashboard.models.images.model import Image

        image = Image.objects.filter(image=rel_path).select_related("direct_message").first()
        if image is None:
            # Orphan file (row deleted, file left behind) - no owner to check.
            # TODO(media-auth): authenticated-only fallback; see docs/PROBLEMS.md.
            return True
        if image.profile_id == profile.pk:
            return True
        if image.direct_message_id:
            dm = image.direct_message
            if dm is not None and profile.pk in (dm.sender_id, dm.recipient_id):
                return True
            if not image.pin_id and not image.wiki_id:
                # A pure DM attachment is private to the two participants; an
                # image that *also* lives in a pin/wiki gallery falls through
                # to the normal photo-visibility check below.
                return False
        return Image.objects.visible_to(profile).filter(pk=image.pk).exists()

    def _authorize_comment_image(self, profile: Profile, rel_path: str) -> bool:
        """Authorize a ``comment_images/`` file via its Comment/TripComment row.

        Args:
            profile: The authenticated requester's profile.
            rel_path: Path relative to ``MEDIA_ROOT``.

        Returns:
            True when the requester may see the comment image.
        """
        from urbanlens.dashboard.models.comments.model import Comment
        from urbanlens.dashboard.models.trips.model import TripComment, TripMembership
        from urbanlens.dashboard.services.wiki_access import location_visible_to

        comment = Comment.objects.filter(image=rel_path).select_related("pin", "wiki__location").first()
        if comment is not None:
            if comment.profile_id == profile.pk:
                return True
            if comment.pending_scan:
                # Not yet cleared by the malware scan - author-only until then,
                # mirroring controllers.comments._build_context.
                return False
            if comment.pin is not None:
                # Pin comment threads are visible only on the owner's own pin
                # page (see PinCommentsView.get), so owner + comment authors
                # are the whole audience.
                return comment.pin.profile_id == profile.pk
            if comment.wiki is not None:
                return location_visible_to(comment.wiki.location, profile)
            return False

        trip_comment = TripComment.objects.filter(image=rel_path).first()
        if trip_comment is not None:
            if trip_comment.author_id == profile.pk:
                return True
            if trip_comment.pending_scan:
                return False
            return TripMembership.objects.filter(trip_id=trip_comment.trip_id, profile=profile).exists()

        # Orphan file - no surviving comment row to derive an owner from.
        # TODO(media-auth): authenticated-only fallback; see docs/PROBLEMS.md.
        return True
