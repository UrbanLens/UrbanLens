"""Whether a URL works as a password: anyone holding it can open what it points at.

A share link to a Drive folder, a Dropbox file, a photo album or a Google My Maps map is one, and so is any URL carrying
a token, key or signature. Such a URL must not reach a service that publishes what it fetches, as the Wayback
Machine's Save Page Now does. A false positive costs that service's copy of a public page; a false negative publishes
something its owner shared privately, so the rules lean wide.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from urbanlens.dashboard.services.security.redact import is_sensitive_param_name

#: Hosts that serve little but private or link-shared files, maps and albums; a subdomain counts.
SHARE_DOMAINS = frozenset(
    {
        "drive.google.com",
        "docs.google.com",
        "photos.google.com",
        "photos.app.goo.gl",
        "maps.app.goo.gl",
        "goo.gl",
        "dropbox.com",
        "dropboxusercontent.com",
        "db.tt",
        "1drv.ms",
        "onedrive.live.com",
        "sharepoint.com",
        "icloud.com",
        "box.com",
        "mega.nz",
        "mega.co.nz",
        "wetransfer.com",
        "we.tl",
        "discord.com",
        "discord.gg",
        "discordapp.com",
        "discordapp.net",
    }
)

#: Google My Maps and saved place lists, on any Google domain.
_GOOGLE_HOST = re.compile(r"^(?:www\.)?google\.[a-z]{2,3}(?:\.[a-z]{2})?$")
_GOOGLE_SHARED_MAP = re.compile(r"^/maps/(?:d|placelists)/", re.IGNORECASE)

#: A share-link path on any host: Nextcloud and Reddit ``/s/<id>``, Immich ``/share/<key>``, Flickr ``/gp/<code>``.
_SHARE_PATH = re.compile(r"/(?:s|share|shared|sharedalbum|sharing|invite|gp)/[^/]{6,}", re.IGNORECASE)

#: Parameter names that carry access rather than content, beyond the credential words logging redacts.
_ACCESS_PARAM_NAMES = frozenset({"pass", "passcode", "invite", "jwt", "otp"})


def is_capability_url(url: str) -> bool:
    """Whether *url* grants access to whoever holds it.

    Args:
        url: An absolute http(s) URL.

    Returns:
        True for a known share host, a share-link path, a user part, or a query or fragment parameter named for a
        credential.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.username is not None or parts.password is not None:
        return True
    if any(host == domain or host.endswith(f".{domain}") for domain in SHARE_DOMAINS):
        return True
    if _GOOGLE_HOST.match(host) and _GOOGLE_SHARED_MAP.match(parts.path):
        return True
    if _SHARE_PATH.search(parts.path):
        return True
    return any(_grants_access(name) for text in (parts.query, parts.fragment) for name, _value in parse_qsl(text, keep_blank_values=True))


def _grants_access(name: str) -> bool:
    return is_sensitive_param_name(name) or name.casefold() in _ACCESS_PARAM_NAMES
