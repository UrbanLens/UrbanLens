"""Serving user uploads from their own origin, without giving it the session."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from django.conf import settings
from django.core import signing

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse
    from django.http.response import HttpResponseBase

logger = logging.getLogger(__name__)

#: Cookie name. Deliberately unlike ``sessionid``/``csrftoken`` so it is obvious
#: in a browser's storage inspector that this is not the session.
MEDIA_COOKIE_NAME = "ul_media"

#: Signing salt. Namespaced so a value minted here can never verify against
#: another ``django.core.signing`` user (the preview-URL signer, say) that
#: happens to share ``SECRET_KEY``.
MEDIA_COOKIE_SALT = "urbanlens.media.origin"

#: How long a minted cookie stays acceptable to the media gate. Short enough
#: that a stolen cookie ages out on its own, long enough to cover a normal
#: browsing session without a re-mint on every page.
MEDIA_COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60

#: Re-mint once a cookie is older than this.
#: Half the lifetime, so an active session is always renewed with hours to spare and an idle one
#: expires.
MEDIA_COOKIE_REFRESH_AFTER_SECONDS = MEDIA_COOKIE_MAX_AGE_SECONDS // 2

#: Multi-part public suffixes :func:`cookie_domain` must never hand back.
#: Not a complete list - the complete one is the Public Suffix List, a dependency this project does
#: not carry - just the ones a deployment of this app might plausibly sit under.
PUBLIC_SUFFIXES = frozenset(
    {
        "co.uk",
        "org.uk",
        "me.uk",
        "ac.uk",
        "gov.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.nz",
        "co.za",
        "com.br",
        "co.jp",
        "co.in",
        "co.kr",
        "github.io",
        "gitlab.io",
        "pages.dev",
        "workers.dev",
        "vercel.app",
        "netlify.app",
        "web.app",
    },
)


def media_origin() -> str:
    """The configured media origin, e.g. ``https://media.urbanlens.org``.

    Returns:
        The origin with no trailing slash, or an empty string when uploads are served from the app's own hostname (the default, and what local development runs)."""
    return str(getattr(settings, "UL_MEDIA_BASE_URL", "") or "").rstrip("/")


def media_origin_host() -> str:
    """Hostname of the media origin, without scheme or port.

    Returns:
        The hostname, or an empty string when no media origin is configured.
    """
    return urlsplit(media_origin()).hostname or ""


def is_media_origin_request(request: HttpRequest) -> bool:
    """Whether this request arrived on the media origin rather than the app's.

    Args:
        request: The current request.

    Returns:
        True when the request's host matches the configured media origin."""
    host = media_origin_host()
    return bool(host) and request.get_host().split(":")[0].lower() == host


def shared_suffix(media_host: str, app_host: str) -> str:
    """The deepest domain two hosts share, with no safety filtering applied.

    Args:
        media_host: Hostname of the media origin.
        app_host: Hostname of the app origin.

    Returns:
        The shared suffix, e.g. ``"urbanlens.org"`` - which may be a public suffix, or a single label, or empty."""
    shared: list[str] = []
    for media_label, app_label in zip(reversed(media_host.lower().split(".")), reversed(app_host.lower().split(".")), strict=False):
        if media_label != app_label:
            break
        shared.append(media_label)
    return ".".join(reversed(shared))


def cookie_domain() -> str:
    """The ``Domain`` attribute the media cookie must carry, or ``""``.
    A deployment whose two hosts are not related this way (different registrable domains, or a media host that is not a sibling) has to say so explicitly via ``UL_MEDIA_COOKIE_DOMAIN``; there is no correct value to derive.

    Returns:
        The domain, or an empty string when no media origin is configured or the two hosts share nothing (in which case the caller should not set the cookie at all - see :func:`set_media_cookie`)."""
    if explicit := str(getattr(settings, "UL_MEDIA_COOKIE_DOMAIN", "") or "").strip():
        return explicit

    media_host = media_origin_host()
    # settings.SITE_URL, not UL_SITE_URL: the env var is spelled UL_SITE_URL but settings/base.py
    # exposes it as SITE_URL.
    app_host = urlsplit(str(getattr(settings, "SITE_URL", "") or "")).hostname or ""
    if not media_host or not app_host:
        return ""

    shared = shared_suffix(media_host, app_host).split(".") if shared_suffix(media_host, app_host) else []

    # Two labels is a floor, not the answer: a single shared label is always a public suffix
    # ("org"), but so are plenty of two-label ones ("co.uk", "pages.dev"), which this would
    # otherwise hand back for two unrelated hosts that happen to sit under the same registry.
    domain = ".".join(shared)
    if len(shared) < 2 or domain in PUBLIC_SUFFIXES:
        logger.warning("Media origin %s and site host %s share no usable cookie domain (%r); media cookie disabled", media_host, app_host, domain)
        return ""
    return domain


def mint_media_token(user_id: int) -> str:
    """Sign a media credential for the user with *user_id*.
    Carries the ``auth.User`` id rather than the ``Profile`` id so the middleware can decide whether a refresh is due from ``request.user.pk`` alone, with no database query on the overwhelmingly common path where the browser already holds a valid cookie.

    Args:
        user_id: Primary key of the authenticated user.

    Returns:
        The signed, timestamped cookie value."""
    return signing.dumps({"u": user_id}, salt=MEDIA_COOKIE_SALT)


def user_id_from_token(token: str, *, max_age: int = MEDIA_COOKIE_MAX_AGE_SECONDS) -> int | None:
    """Recover the user id from a media cookie value.

    Args:
        token: The raw cookie value as received.
        max_age: Reject a token older than this many seconds.

    Returns:
        The user id, or None when the token is absent, tampered with, too old, or does not carry an integer id."""
    if not token:
        return None
    try:
        payload = signing.loads(token, salt=MEDIA_COOKIE_SALT, max_age=max_age)
    except signing.BadSignature:
        return None
    user_id = payload.get("u") if isinstance(payload, dict) else None
    # `True` is an int in Python; a payload of {"u": true} must not be read as
    # user 1.
    return user_id if isinstance(user_id, int) and not isinstance(user_id, bool) else None


def needs_refresh(request: HttpRequest, user_id: int) -> bool:
    """Whether this response should (re-)set the media cookie.

    Args:
        request: The current request, carrying whatever cookie the browser has.
        user_id: Primary key of the authenticated user.

    Returns:
        True when the browser has no usable cookie, one that is past its refresh age, or one minted for a different user - the last of which is what makes a device shared between two accounts serve the right person's media after a re-login."""
    token = request.COOKIES.get(MEDIA_COOKIE_NAME, "")
    return user_id_from_token(token, max_age=MEDIA_COOKIE_REFRESH_AFTER_SECONDS) != user_id


def set_media_cookie(response: HttpResponse, user_id: int) -> HttpResponse:
    """Attach a freshly minted media cookie to *response*.

    Args:
        response: The outgoing response.
        user_id: Primary key of the user to authenticate as.

    Returns:
        The same response."""
    domain = cookie_domain()
    if not domain:
        return response

    response.set_cookie(
        MEDIA_COOKIE_NAME,
        mint_media_token(user_id),
        max_age=MEDIA_COOKIE_MAX_AGE_SECONDS,
        domain=domain,
        secure=bool(getattr(settings, "SESSION_COOKIE_SECURE", False)),
        httponly=True,
        # Lax, not None: the media origin is a sibling of the app origin, so every request that
        # matters here is same-*site* and Lax is sent.
        # None would additionally expose the cookie to genuinely cross-site embeds of our media
        # URLs, which is exactly what should not carry a credential.
        samesite="Lax",
    )
    return response


#: Content-Security-Policy for a media response produced *by Django* - which, behind nginx, is only
#: the local-development path.
MEDIA_ORIGIN_CSP = "default-src 'none'"


def apply_media_response_headers[ResponseT: HttpResponseBase](request: HttpRequest, response: ResponseT) -> ResponseT:
    """Set the framing and hardening headers for one media response.
    **Behind nginx, only the ``FileResponse`` (development) path actually delivers these** - see :data:`MEDIA_ORIGIN_CSP`.

    Args:
        request: The request being answered.
        response: The response about to be returned.

    Returns:
        The same response object that was passed in."""
    if not is_media_origin_request(request):
        response.setdefault("X-Frame-Options", "SAMEORIGIN")
        return response

    app_origin = str(getattr(settings, "SITE_URL", "") or "").rstrip("/")
    policy = str(getattr(settings, "UL_MEDIA_CSP", "") or MEDIA_ORIGIN_CSP)
    frame_ancestors = f"frame-ancestors {app_origin}" if app_origin else "frame-ancestors 'none'"
    response["Content-Security-Policy"] = f"{policy}; {frame_ancestors}"
    del response["X-Frame-Options"]
    response.xframe_options_exempt = True  # type: ignore[attr-defined]
    response["X-Content-Type-Options"] = "nosniff"
    # A media URL embeds an opaque per-upload token; sending it as a Referer to
    # whatever a document links out to would leak a working capability.
    response["Referrer-Policy"] = "no-referrer"
    return response


def clear_media_cookie(response: HttpResponse) -> HttpResponse:
    """Delete the media cookie, e.g. on logout.

    Args:
        response: The outgoing response.

    Returns:
        The same response.
    """
    domain = cookie_domain()
    if domain:
        response.delete_cookie(MEDIA_COOKIE_NAME, domain=domain)
    return response
