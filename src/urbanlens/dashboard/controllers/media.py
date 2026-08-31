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

Neither half of the decision is implemented here.

*Authentication* - "a logged-in session, or a bearer credential holding
``media:read``" - lives in
:class:`~urbanlens.dashboard.controllers.media_auth.CredentialOrSessionMediaMixin`,
because the panel image proxy and the SpotGuessr round image need the identical
rule and a second copy of it would drift open.

*Authorization* lives in :mod:`urbanlens.dashboard.services.media.access`, as a
default-deny table keyed by the file's ``upload_to`` prefix. Read that module
for the per-family policy; a family with no registered authorizer is refused
here and reported by ``manage.py check``.

This module owns only the path handling in between: resolving the requested
path against ``MEDIA_ROOT`` without letting it escape, and handing the bytes to
nginx once someone has said yes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse
from django.views import View

from urbanlens.dashboard.controllers.media_auth import CredentialOrSessionMediaMixin, MediaThrottledError, mark_private_media
from urbanlens.dashboard.services.media.access import authorize_media
from urbanlens.dashboard.services.media.origin import apply_media_response_headers

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.http.response import HttpResponseBase

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Re-exported so ``controllers.media.MediaThrottledError`` keeps resolving for
#: anything that imported it from here before the session/credential resolution
#: moved to ``controllers.media_auth`` (where the panel image proxy and the
#: SpotGuessr round image share it). Listed explicitly because it is otherwise
#: an unused import as far as a linter is concerned.
__all__ = ["MediaGateView", "MediaThrottledError"]


class MediaGateView(CredentialOrSessionMediaMixin, View):
    """Authenticate and authorize a request for one file under ``MEDIA_ROOT``.

    Accepts either a logged-in session (the browser case) or an external API
    credential holding the ``media:read`` scope (the native/mobile client
    case) - that half is
    :class:`~urbanlens.dashboard.controllers.media_auth.CredentialOrSessionMediaMixin`,
    shared with the other byte-serving views. A credential is resolved only as
    far as *which profile is asking*; it then walks the byte-for-byte identical
    authorization policy in :meth:`_authorized`, so holding a key can never
    reach a file the same person couldn't reach while logged in.

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

        Framing and the other response headers are
        :func:`~urbanlens.dashboard.services.media.origin.apply_media_response_headers`'s
        job, including the ``X-Frame-Options: SAMEORIGIN`` that the Vault document
        lightbox needs and the site-wide ``DENY`` would block. It is called here
        rather than applied as a ``xframe_options_sameorigin`` decorator because
        the two origins need different framing rules and a decorator runs after
        this body - it would overwrite whatever the media-origin branch set.

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
        # This view has no cheaper pre-check to run ahead of authentication,
        # so it calls the mixin as its very first statement.
        try:
            profile = self.resolve_media_profile(request)
        except MediaThrottledError:
            return self.media_throttled_response()

        if profile is None:
            return self.media_auth_failure_response(request)

        rel_path, full_path = self._resolve_media_path(path)

        if not self._authorized(profile, rel_path):
            logger.info("Denied media request for %s by profile %s", rel_path, profile.pk)
            raise Http404

        return apply_media_response_headers(request, serve_media_file(rel_path, full_path))

    def _resolve_media_path(self, path: str) -> tuple[str, Path]:
        """Delegate to :func:`resolve_media_path`.

        Args:
            path: The untrusted relative path from the URL.

        Returns:
            Tuple of (path relative to ``MEDIA_ROOT``, absolute ``Path``).

        Raises:
            Http404: See :func:`resolve_media_path`.
        """
        return resolve_media_path(path)

    def _authorized(self, profile: Profile, rel_path: str) -> bool:
        """Delegate to :func:`~urbanlens.dashboard.services.media.access.authorize_media`.

        Args:
            profile: The authenticated requester's profile.
            rel_path: Normalized path relative to ``MEDIA_ROOT``
                (e.g. ``"pin_images/a7/Kd3xq.../IMG_4821.jpg"``).

        Returns:
            True when the requester may see the file.
        """
        return authorize_media(profile, rel_path)


def resolve_media_path(path: str) -> tuple[str, Path]:
    """Resolve a media path and verify it stays inside ``MEDIA_ROOT``.

    Args:
        path: The untrusted relative path.

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
        full_path = (media_root / path).resolve()  # lgtm[py/path-injection] -- checked against media_root just below, before any use
    except (OSError, ValueError) as exc:
        raise Http404 from exc

    if full_path == media_root or not full_path.is_relative_to(media_root):
        logger.warning("Blocked media path traversal attempt: %r", path)
        raise Http404

    if not full_path.is_file():  # lgtm[py/path-injection] -- reached only after the is_relative_to(media_root) check above
        raise Http404

    return full_path.relative_to(media_root).as_posix(), full_path


def serve_media_file(rel_path: str, full_path: Path) -> HttpResponseBase:
    """Serve one already-authorized media file.

    Authorization is the caller's job - this only moves bytes. Split out so a
    surface with its own credential (the safety contact portal authenticates by
    magic-link token, not by login) can reuse the nginx hand-off instead of
    reimplementing it.

    Args:
        rel_path: Path relative to ``MEDIA_ROOT``, already traversal-checked.
        full_path: The resolved absolute path on disk.

    Returns:
        An ``X-Accel-Redirect`` response when nginx fronts the app, otherwise a
        ``FileResponse`` streaming the file.
    """
    if getattr(settings, "MEDIA_X_ACCEL", False):
        # Hand the actual byte-serving back to nginx: the internal-only
        # /_protected_media/ location aliases the media volume. Content-Type
        # is deliberately left unset so nginx derives it from the file
        # extension via its own mime.types.
        response = HttpResponse()
        del response["Content-Type"]
        response["X-Accel-Redirect"] = settings.MEDIA_X_ACCEL_PREFIX + quote(rel_path)
        return mark_private_media(response)

    return mark_private_media(FileResponse(full_path.open("rb")))  # lgtm[py/path-injection] -- already traversal-checked by resolve_media_path
