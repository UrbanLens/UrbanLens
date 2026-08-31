"""Serving user uploads from their own origin, without giving it the session.

Uploads are served from ``media.urbanlens.org`` rather than from the app's own
hostname, so that anything which does slip past validation and sanitising -
an SVG that turned out to be scriptable, an HTML file that sniffed as something
else, a PDF with active content - executes in an origin where there is nothing
to steal. The same-origin policy is doing the work: script on the media origin
cannot read the app's DOM, cannot call its API as the user, and cannot see its
cookies.

That last part is the constraint everything here follows from. The session
cookie is host-only for the app's own hostname (no ``SESSION_COOKIE_DOMAIN`` is
set), so it is *not* sent to the media origin - which is the point, and also
means the media origin cannot tell who is asking. Media is authorized per
viewer (:mod:`urbanlens.dashboard.services.media.access`), so "cannot tell who
is asking" would mean "cannot serve anything".

The answer is a second, much weaker credential: a signed cookie carrying one
user id and nothing else, readable on the media origin, useless anywhere else.
It grants exactly what ``authorize_media`` would have granted that user through
a session, so per-file authorization stays where it was and stays revocable the
instant a share is withdrawn. Signed URLs were the alternative and are worse
here: they would have to be minted per viewer at every one of the ~100 places a
template renders ``.url``, they leak access to anyone who sees the URL, and they
cannot be revoked before they expire.

The cookie is set with an explicit ``Domain`` - the deepest domain both hosts
share - because a host-only cookie set by the app origin would never be sent to
the media origin. That does mean it is also sent back to the app origin, which
is harmless: it is ``HttpOnly``, it authorizes reads of files the same user
could already read, and it is strictly weaker than the session cookie sitting
next to it.
"""

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

#: Re-mint once a cookie is older than this. Half the lifetime, so an active
#: session is always renewed with hours to spare and an idle one expires. The
#: middleware re-mints by *trying* to load at this age and treating expiry as
#: "time to refresh", which is why there is no separate timestamp in the payload.
MEDIA_COOKIE_REFRESH_AFTER_SECONDS = MEDIA_COOKIE_MAX_AGE_SECONDS // 2

#: Multi-part public suffixes :func:`cookie_domain` must never hand back. Not a
#: complete list - the complete one is the Public Suffix List, a dependency this
#: project does not carry - just the ones a deployment of this app might
#: plausibly sit under. See :func:`cookie_domain` for what goes wrong without it.
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
        The origin with no trailing slash, or an empty string when uploads are
        served from the app's own hostname (the default, and what local
        development runs).
    """
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
        True when the request's host matches the configured media origin. False
        when no media origin is configured, which keeps every caller on its
        pre-split behaviour.
    """
    host = media_origin_host()
    return bool(host) and request.get_host().split(":")[0].lower() == host


def shared_suffix(media_host: str, app_host: str) -> str:
    """The deepest domain two hosts share, with no safety filtering applied.

    Split out from :func:`cookie_domain` so
    ``dashboard.checks.check_media_origin_cookie_domain`` can tell an operator
    *why* no cookie domain was derived - "these hosts share nothing" and "these
    hosts share only a public suffix" need different fixes, and
    :func:`cookie_domain` deliberately collapses both to ``""``.

    Args:
        media_host: Hostname of the media origin.
        app_host: Hostname of the app origin.

    Returns:
        The shared suffix, e.g. ``"urbanlens.org"`` - which may be a public
        suffix, or a single label, or empty. Not safe to use as a cookie domain
        without the checks :func:`cookie_domain` applies.
    """
    shared: list[str] = []
    for media_label, app_label in zip(reversed(media_host.lower().split(".")), reversed(app_host.lower().split(".")), strict=False):
        if media_label != app_label:
            break
        shared.append(media_label)
    return ".".join(reversed(shared))


def cookie_domain() -> str:
    """The ``Domain`` attribute the media cookie must carry, or ``""``.

    The deepest domain the app host and the media host share, so the cookie
    reaches the media origin without being broadcast further up the tree than
    necessary - ``urbanlens.org`` + ``media.urbanlens.org`` yields
    ``urbanlens.org``, and ``dev.urbanlens.org`` + ``media.dev.urbanlens.org``
    yields ``dev.urbanlens.org`` rather than the whole apex.

    A deployment whose two hosts are not related this way (different registrable
    domains, or a media host that is not a sibling) has to say so explicitly via
    ``UL_MEDIA_COOKIE_DOMAIN``; there is no correct value to derive.

    Returns:
        The domain, or an empty string when no media origin is configured or the
        two hosts share nothing (in which case the caller should not set the
        cookie at all - see :func:`set_media_cookie`).
    """
    if explicit := str(getattr(settings, "UL_MEDIA_COOKIE_DOMAIN", "") or "").strip():
        return explicit

    media_host = media_origin_host()
    # settings.SITE_URL, not UL_SITE_URL: the env var is spelled UL_SITE_URL but
    # settings/base.py exposes it as SITE_URL. Reading the env-var spelling off
    # `settings` silently yields "" - no exception, no log - which made this
    # function return "" for every real deployment and turned set_media_cookie
    # into a no-op, i.e. the whole media origin 404ing with nothing to explain it.
    app_host = urlsplit(str(getattr(settings, "SITE_URL", "") or "")).hostname or ""
    if not media_host or not app_host:
        return ""

    shared = shared_suffix(media_host, app_host).split(".") if shared_suffix(media_host, app_host) else []

    # Two labels is a floor, not the answer: a single shared label is always a
    # public suffix ("org"), but so are plenty of two-label ones ("co.uk",
    # "pages.dev"), which this would otherwise hand back for two unrelated hosts
    # that happen to sit under the same registry. A PSL-aware browser rejects a
    # cookie scoped to one - the media origin then 404s with nothing to explain
    # it - and a client that is *not* PSL-aware attaches the credential to every
    # host under that suffix, which is the failure worth actually preventing.
    #
    # Deriving this properly needs the Public Suffix List, which this project
    # does not carry; refusing the handful a deployment might plausibly land on
    # is the honest approximation. dashboard.checks.check_media_origin_cookie_domain
    # turns the same condition into a startup error rather than a silent one.
    domain = ".".join(shared)
    if len(shared) < 2 or domain in PUBLIC_SUFFIXES:
        logger.warning("Media origin %s and site host %s share no usable cookie domain (%r); media cookie disabled", media_host, app_host, domain)
        return ""
    return domain


def mint_media_token(user_id: int) -> str:
    """Sign a media credential for the user with *user_id*.

    Carries the ``auth.User`` id rather than the ``Profile`` id so the
    middleware can decide whether a refresh is due from ``request.user.pk``
    alone, with no database query on the overwhelmingly common path where the
    browser already holds a valid cookie.

    Args:
        user_id: Primary key of the authenticated user.

    Returns:
        The signed, timestamped cookie value.
    """
    return signing.dumps({"u": user_id}, salt=MEDIA_COOKIE_SALT)


def user_id_from_token(token: str, *, max_age: int = MEDIA_COOKIE_MAX_AGE_SECONDS) -> int | None:
    """Recover the user id from a media cookie value.

    Args:
        token: The raw cookie value as received.
        max_age: Reject a token older than this many seconds.

    Returns:
        The user id, or None when the token is absent, tampered with, too old,
        or does not carry an integer id. Every failure is the same None: the
        media gate answers all of them with its usual 404, so none of them is
        distinguishable from a file that does not exist.
    """
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
        True when the browser has no usable cookie, one that is past its refresh
        age, or one minted for a different user - the last of which is what makes
        a device shared between two accounts serve the right person's media after
        a re-login.
    """
    token = request.COOKIES.get(MEDIA_COOKIE_NAME, "")
    return user_id_from_token(token, max_age=MEDIA_COOKIE_REFRESH_AFTER_SECONDS) != user_id


def set_media_cookie(response: HttpResponse, user_id: int) -> HttpResponse:
    """Attach a freshly minted media cookie to *response*.

    A no-op when no media origin is configured, or when the two hosts share no
    domain the cookie could be scoped to - in both cases media is served from
    the app's own origin and the session cookie already covers it.

    Args:
        response: The outgoing response.
        user_id: Primary key of the user to authenticate as.

    Returns:
        The same response.
    """
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
        # Lax, not None: the media origin is a sibling of the app origin, so
        # every request that matters here is same-*site* and Lax is sent. None
        # would additionally expose the cookie to genuinely cross-site embeds of
        # our media URLs, which is exactly what should not carry a credential.
        samesite="Lax",
    )
    return response


#: Content-Security-Policy for a media response produced *by Django* - which,
#: behind nginx, is only the local-development path.
#:
#: ``settings.MEDIA_X_ACCEL`` is on for every non-dev deployment, and an
#: X-Accel-Redirect response is not the one the browser receives: nginx follows
#: the redirect and builds its own, forwarding only a small allow-list of
#: upstream headers. Measured on the deployed image, ``Cache-Control`` survives
#: and ``Content-Security-Policy``/``X-Frame-Options``/``X-Content-Type-Options``/
#: ``Referrer-Policy`` are all dropped. So in production these headers come from
#: server-level ``add_header`` directives in ``config/nginx/media.conf.template``,
#: and this constant governs the ``FileResponse`` path only. Both are kept
#: deliberately: a dev server with no nginx in front of it needs the same policy,
#: and the two must not drift - change them together.
#:
#: ``default-src 'none'`` is the whole policy: an uploaded file that a browser
#: decides to treat as a document - an HTML file that sniffed as something else,
#: a scriptable SVG - can then load nothing at all, so even if it executes it has
#: no way to fetch, beacon, or pull in a second stage. It costs nothing for the
#: images, video and PDFs this actually serves, none of which fetch subresources.
#:
#: Deliberately no ``sandbox``: a sandboxed response is opaque to the browser's
#: built-in PDF viewer in some versions, and the Vault document lightbox frames
#: PDFs from this origin. A deployment that does not need in-browser document
#: preview can add it through ``UL_MEDIA_CSP``.
MEDIA_ORIGIN_CSP = "default-src 'none'"


def apply_media_response_headers[ResponseT: HttpResponseBase](request: HttpRequest, response: ResponseT) -> ResponseT:
    """Set the framing and hardening headers for one media response.

    **Behind nginx, only the ``FileResponse`` (development) path actually
    delivers these** - see :data:`MEDIA_ORIGIN_CSP`. In production nginx drops
    every one of them while following the X-Accel-Redirect, and
    ``config/nginx/media.conf.template`` sets the same policy at the server
    level instead. This function is still the right place for the dev path and
    for keeping the ``xframe_options_exempt`` bookkeeping correct, but do not
    read it as the enforcement point.

    Owns framing for *both* origins, rather than leaving the same-origin case to
    a ``xframe_options_sameorigin`` decorator on the view. Splitting it that way
    does not work: ``method_decorator`` wraps the handler, so the decorator runs
    **after** the view body and re-adds ``SAMEORIGIN`` to the very response this
    function just cleared it from. One function, both branches, no ordering to
    get wrong.

    - **App origin**: ``SAMEORIGIN``. The site-wide default is ``DENY``, which
      blocks even the same-origin Vault document lightbox
      (:mod:`partials._photo_lightbox`).
    - **Media origin**: no ``X-Frame-Options`` at all, and a CSP whose
      ``frame-ancestors`` names the app origin. That lightbox now frames a
      *different* host, so ``SAMEORIGIN`` would block the exact feature the
      relaxation exists for. ``frame-ancestors`` states the intended rule, and
      browsers ignore ``X-Frame-Options`` when it is present - but only if the
      header is actually gone, since a browser that does not implement
      ``frame-ancestors`` would still honour it.

    Clearing it takes both lines below. Deleting the header alone is not enough:
    ``XFrameOptionsMiddleware`` runs on the way out, sees no header, and adds
    ``DENY`` from ``X_FRAME_OPTIONS``. ``xframe_options_exempt`` is the flag it
    checks before doing that.

    Generic in the response type for the same reason
    :func:`~urbanlens.dashboard.controllers.media_auth.mark_private_media` is: the
    gate returns a ``FileResponse`` in development and a plain ``HttpResponse``
    behind nginx, and widening the return type here would make mypy reject the
    call site.

    Args:
        request: The request being answered.
        response: The response about to be returned.

    Returns:
        The same response object that was passed in.
    """
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
