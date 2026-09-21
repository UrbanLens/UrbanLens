"""Whether a session may draw basemap tiles, answered without loading the viewer's row.

A viewport is ~30 tiles and each one was a full ``LoginRequiredMixin`` check, so a map load spent
30 ``auth_user`` queries establishing 30 times over that the same person was still signed in.
``P125`` measured the tile at 13.1% of all app CPU, and that query at roughly 1.5 ms of its ~4 ms.

The decision is remembered against the session key instead, in the same Dragonfly the tile bytes
come from - so the hot path is one round trip that fetches both, and no database query at all.

**This authorises basemap tiles and nothing else.** It is a cache of one boolean - "this session
was signed in when last asked" - and deliberately carries no identity, no permissions and no
profile. Every other view, the tile *catalogue* included, keeps the full check.

What it costs: a revocation that leaves the session record intact - an account disabled or deleted
by an admin, or a password change that invalidates a session other than the one making it - still
draws tiles until the entry expires. For that window those sessions can fetch map tiles, and
nothing else. Anything that ends the session itself takes the tiles with it immediately, because
the gate re-reads the session every time: signing out, a flush, an expiry.

``TILE_AUTH_TTL`` bounds a *fetch*, not a pixel. ``BasemapTileView`` hands the browser
``Cache-Control: private, max-age=604800, immutable`` (``_keep_for_a_week``), and a revocation
does not change the session cookie the response varies on, so tiles that browser already holds
keep rendering from its own disk for up to a week with no request reaching this deployment. That
is accepted rather than overlooked: the bytes are public vendor imagery, proxied so the vendor
never learns which coordinates a viewer is looking at, and they carry nothing about the account
that fetched them. A shorter header would re-fetch every tile a viewer has already seen, which is
the cost this module exists to remove.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth import SESSION_KEY

from urbanlens.dashboard.services.core import bounded_cache

if TYPE_CHECKING:
    from django.http import HttpRequest

#: Short enough that a revoked session loses its tiles within the hour, long enough that an ordinary
#: browsing session asks once rather than per viewport.
TILE_AUTH_TTL = 1800

_KEY_PREFIX = "ul_tileauth_"

#: Stored value. Only its presence is read; a word rather than a flag so a cache dump reads.
_GRANTED = "granted"


def tile_auth_key(session_key: str) -> str:
    """Cache key holding whether one session may draw tiles.

    Args:
        session_key: Django's own session key for the request.

    Returns:
        The cache key.
    """
    return f"{_KEY_PREFIX}{session_key}"


def session_key_for(request: HttpRequest) -> str | None:
    """The session key of a request whose session currently names a signed-in user.

    Both halves matter. Reading the session proves the session still exists - a flushed one, which
    is what signing out leaves behind, no longer names anyone however good the cookie looks - and
    it costs no database query, since sessions are read from the same cache as everything else
    here. The remembered answer on top of that is what says the session was *properly* verified,
    auth hash and all, recently; neither alone is the gate.

    Args:
        request: The current request.

    Returns:
        The session key, or None when nothing is signed in on this request.
    """
    session = getattr(request, "session", None)
    if session is None or not session.get(SESSION_KEY):
        return None
    return getattr(session, "session_key", None)


def remember_tile_viewer(session_key: str) -> None:
    """Record that this session was signed in, so the next tile need not ask the database.

    Args:
        session_key: Django's own session key for the request.
    """
    # A degraded Dragonfly must cost the shortcut, not the map: unwrapped, this raises on the very
    # first tile of a session and 500s every map load rather than falling back to the database check.
    bounded_cache.set_or_skip(tile_auth_key(session_key), _GRANTED, TILE_AUTH_TTL, label="Tile authorisation")


def forget_tile_viewer(sender: object, request: HttpRequest | None = None, **kwargs: Any) -> None:
    """Drop a session's tile access as it signs out, rather than leaving it to expire.

    Wired to ``user_logged_out``, which Django sends *before* flushing the session, so the key is
    still readable here.

    Args:
        sender: Unused; the signal's sender.
        request: The request signing out, when the sender supplied one.
        **kwargs: Unused; the rest of the signal's arguments.
    """
    session_key = session_key_for(request) if request is not None else None
    if session_key:
        # Raising here would 500 the sign-out itself, since this runs on `user_logged_out`.
        bounded_cache.delete_quietly(tile_auth_key(session_key), label="Tile authorisation")
