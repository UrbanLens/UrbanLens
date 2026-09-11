"""Authenticated media gate for ``/media/...`` requests."""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.core.files.storage import FileSystemStorage, default_storage
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
__all__ = ["MediaByteSource", "MediaGateView", "MediaThrottledError"]


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

        source = self._resolve_media_path(path)

        if not self._authorized(profile, source.rel_path):
            logger.info("Denied media request for %s by profile %s", source.rel_path, profile.pk)
            raise Http404

        return apply_media_response_headers(request, serve_media_file(source))

    def _resolve_media_path(self, path: str) -> MediaByteSource:
        """Delegate to :func:`resolve_media_path`.

        Args:
            path: The untrusted relative path from the URL.

        Returns:
            The byte source for the requested file.

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


class MediaByteSource(ABC):
    """One already-located media file, and the cheapest way to put its bytes on the wire.

    A subclass per backing store, chosen by :func:`resolve_media_path` from
    whatever ``STORAGES["default"]`` resolves to. Splitting it this way keeps
    the path handling (which is security-critical and identical either way) in
    one place, and lets a third store be added by writing one class rather than
    by adding a branch to every caller.

    Authorization is *not* this class's job and must already have happened -
    see :func:`~urbanlens.dashboard.services.media.access.authorize_media`.
    """

    def __init__(self, rel_path: str) -> None:
        """Store the normalized path.

        Args:
            rel_path: Normalized, traversal-checked path relative to the media
                root (filesystem) or to the bucket (object store).
        """
        self.rel_path = rel_path

    @abstractmethod
    def response(self) -> HttpResponseBase:
        """Return the response that delivers this file's bytes.

        Returns:
            An ``X-Accel-Redirect`` hand-off, or a ``FileResponse``.

        Raises:
            Http404: The file is gone between resolution and delivery.
        """


class LocalMediaSource(MediaByteSource):
    """A file on the local filesystem under ``MEDIA_ROOT``."""

    def __init__(self, rel_path: str, full_path: Path) -> None:
        """Store the path pair.

        Args:
            rel_path: Path relative to ``MEDIA_ROOT``.
            full_path: The resolved absolute path, already checked to be inside
                ``MEDIA_ROOT`` and to be an existing file.
        """
        super().__init__(rel_path)
        self.full_path = full_path

    def response(self) -> HttpResponseBase:
        """Hand the file to nginx, or stream it.

        Returns:
            An ``X-Accel-Redirect`` into ``/_protected_media/`` when nginx
            fronts the app, otherwise a ``FileResponse``.

        Raises:
            Http404: The file was there when the resolver looked and is gone
                now. Async processing replaces an upload's stored file
                (``.jpg`` -> ``.webp``) while the row still names the old one,
                so authorization passes for a path this open then fails on.
        """
        if getattr(settings, "MEDIA_X_ACCEL", False):
            # Hand the actual byte-serving back to nginx: the internal-only
            # /_protected_media/ location aliases the media volume. Content-Type
            # is deliberately left unset so nginx derives it from the file
            # extension via its own mime.types. Nothing is opened here, so a
            # file that vanished is nginx's 404 to answer rather than this
            # process's - and re-checking would cost a stat per media request.
            return _accel_redirect(settings.MEDIA_X_ACCEL_PREFIX + quote(self.rel_path))

        try:
            handle = self.full_path.open("rb")  # lgtm[py/path-injection] -- already traversal-checked by resolve_media_path
        except OSError as exc:
            logger.info("Media file %r could not be opened: %s", self.rel_path, exc)
            raise Http404 from exc

        return mark_private_media(FileResponse(handle))


class ObjectMediaSource(MediaByteSource):
    """An object in an S3-compatible store, reached through ``STORAGES["default"]``.

    Existence is not checked up front, unlike the filesystem case: on an object
    store that is a second network round trip per media request, and the answer
    changes nothing. A key with no owning row is refused by authorization before
    this class is reached, and a key that vanishes between the two is a 404
    raised from :meth:`response` instead of from the resolver.
    """

    def response(self) -> HttpResponseBase:
        """Hand nginx a signed URL, or stream the object.

        Returns:
            An ``X-Accel-Redirect`` carrying a URL this process signed - which
            nginx consumes and strips, so it never reaches the client - when
            ``MEDIA_X_ACCEL_OBJECT_PREFIX`` names such a location. Otherwise a
            ``FileResponse`` streaming the object through this process.

        Raises:
            Http404: The object does not exist.
        """
        prefix = getattr(settings, "MEDIA_X_ACCEL_OBJECT_PREFIX", "")
        signer = getattr(default_storage, "signed_object_url", None)
        if prefix and callable(signer):
            ttl = getattr(settings, "MEDIA_X_ACCEL_OBJECT_URL_TTL_SECONDS", 60)
            # The signed URL rides in a response header nginx removes before it
            # answers the client, so it is a server-to-server credential rather
            # than something the requester ever holds.
            #
            # Only the path and query go into the header, never the scheme and
            # host. nginx therefore pins its own upstream in its config and this
            # response cannot point it anywhere: an absolute URL here would make
            # every bug that can influence a stored path into server-side request
            # forgery, executed by the one process that is inside the cluster.
            # The signature covers the Host header, so nginx must send the same
            # host UL_S3_ENDPOINT_URL names - see docs/MEDIA_PIPELINE.md.
            signed = urlsplit(signer(self.rel_path, ttl))
            target = signed.path if not signed.query else f"{signed.path}?{signed.query}"
            return _accel_redirect(prefix.rstrip("/") + "/" + target.lstrip("/"))

        try:
            stream = default_storage.open(self.rel_path, "rb")
        except (FileNotFoundError, OSError) as exc:
            logger.info("Media object %r could not be opened: %s", self.rel_path, exc)
            raise Http404 from exc

        return mark_private_media(FileResponse(stream))


def _accel_redirect(target: str) -> HttpResponseBase:
    """Build the empty response whose only job is the ``X-Accel-Redirect`` header.

    Args:
        target: The internal location (and, for an object store, the signed URL
            appended to it) nginx should fetch.

    Returns:
        A private, content-type-less response carrying the header.
    """
    response = HttpResponse()
    del response["Content-Type"]
    response["X-Accel-Redirect"] = target
    return mark_private_media(response)


def _normalize_object_key(path: str) -> str:
    """Normalize an untrusted media path into an object key, or refuse it.

    The filesystem resolver leans on ``Path.resolve`` and a containment check,
    neither of which means anything for a key in a bucket - ``resolve`` consults
    the local disk, and there is no root to be contained by. So the check here is
    structural: every segment must be a real name. That rejects the traversal
    (``..``), the empty segment (a leading, trailing or doubled slash), the
    current-directory segment, and the backslash - which is a legal character in
    an object key and a path separator to plenty of clients, so a key containing
    one could name one object and be read as another.

    Args:
        path: The untrusted relative path from the URL.

    Returns:
        The key, unchanged, once every segment has been accepted.

    Raises:
        Http404: Any segment is missing, relative, or contains a separator.
    """
    if not path or "\x00" in path or "\\" in path:
        logger.warning("Blocked media object key: %r", path)
        raise Http404

    if any(segment in ("", ".", "..") for segment in path.split("/")):
        logger.warning("Blocked media path traversal attempt: %r", path)
        raise Http404

    return path


def resolve_media_path(path: str) -> MediaByteSource:
    """Resolve a media path against the configured store, refusing anything that escapes.

    Args:
        path: The untrusted relative path.

    Returns:
        The byte source for the requested file, carrying the normalized
        POSIX-style path the authorizers key off.

    Raises:
        Http404: The path is empty, contains a NUL byte, escapes the media root
            (traversal attempt), or - on the filesystem - isn't an existing file.
    """
    if not isinstance(default_storage, FileSystemStorage):
        return ObjectMediaSource(_normalize_object_key(path))

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

    return LocalMediaSource(full_path.relative_to(media_root).as_posix(), full_path)


def serve_media_file(source: MediaByteSource) -> HttpResponseBase:
    """Serve one already-authorized media file.

    Authorization is the caller's job - this only moves bytes. Split out so a
    surface with its own credential (the safety contact portal authenticates by
    magic-link token, not by login) can reuse the nginx hand-off instead of
    reimplementing it.

    Args:
        source: The resolved byte source, from :func:`resolve_media_path`.

    Returns:
        Whatever the source's backing store delivers bytes with - an
        ``X-Accel-Redirect`` response, or a ``FileResponse``.

    Raises:
        Http404: The file disappeared between resolution and delivery.
    """
    return source.response()
