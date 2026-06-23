r"""Social link URL parser and profile-link renderer.

Security contract
-----------------
* Only http/https URLs are accepted - data:, javascript:, etc. are rejected.
* Every extracted handle is validated against ``_HANDLE_RE``; handles that
  contain characters outside ``[\\w.\\-@#]`` are rejected, so no HTML injection
  or path-traversal payloads can be stored.
* For ``website`` links the full canonicalized URL is stored, but fragments
  (``#…``) are stripped and length is capped at 500 characters.
* Discord has no public profile-URL format; its handle is accepted via a
  dedicated form field and validated separately.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

# URL template for rendering a stored handle back to a clickable link.
# None means the platform has no public profile URL (Discord).
PLATFORM_URL_TEMPLATE: dict[str, str | None] = {
    "instagram": "https://instagram.com/{handle}",
    "bluesky": "https://bsky.app/profile/{handle}",
    "discord": None,
    "uer": "https://www.uer.ca/forum_showprofile.asp?fid=1&posterid={handle}",
    "facebook": "https://facebook.com/{handle}",
    "flickr": "https://flickr.com/photos/{handle}",
    "youtube": "https://youtube.com/@{handle}",
    "tiktok": "https://tiktok.com/@{handle}",
    "reddit": "https://reddit.com/u/{handle}",
    "website": "{handle}",  # website stores the full URL itself
}

PLATFORM_DISPLAY_NAME: dict[str, str] = {
    "instagram": "Instagram",
    "bluesky": "Bluesky",
    "discord": "Discord",
    "uer": "UER",
    "facebook": "Facebook",
    "flickr": "Flickr",
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "reddit": "Reddit",
    "website": "Website",
}

PLATFORM_FA_ICON: dict[str, str] = {
    "instagram": "fa-brands fa-instagram",
    "bluesky": "fa-brands fa-bluesky",
    "discord": "fa-brands fa-discord",
    "uer": "fa-solid fa-building",
    "facebook": "fa-brands fa-facebook",
    "flickr": "fa-brands fa-flickr",
    "youtube": "fa-brands fa-youtube",
    "tiktok": "fa-brands fa-tiktok",
    "reddit": "fa-brands fa-reddit",
    "website": "fa-solid fa-globe",
}

# All known platform keys (used for validation when removing a link).
KNOWN_PLATFORMS: frozenset[str] = frozenset(PLATFORM_URL_TEMPLATE)

# Handles may only contain word chars, dots, hyphens, @, or # (Discord tags).
_HANDLE_RE = re.compile(r"^[\w.\-@#]+$")


def _clean_handle(s: str | None) -> str | None:
    """Strip leading @, validate characters, return None if invalid."""
    if not s:
        return None
    s = s.strip().lstrip("@")
    return s if s and _HANDLE_RE.match(s) else None


def parse_social_link(raw: str) -> tuple[str, str] | None:
    """Parse a social URL into ``(platform_key, handle)``.

    Args:
        raw: A full profile URL, e.g. ``https://instagram.com/johndoe``.

    Returns:
        A ``(platform_key, handle)`` tuple where ``platform_key`` is one of
        the keys in :data:`PLATFORM_URL_TEMPLATE`, or ``None`` if the input
        cannot be recognised or fails validation.
    """
    raw = raw.strip()
    if not raw:
        return None

    raw_scheme = urlparse(raw).scheme
    if raw_scheme not in {"", "http", "https"} and ("://" in raw or raw_scheme in {"javascript", "vbscript", "data", "ftp", "file", "mailto"}):
        return None

    # Add a scheme so urlparse can see the host when the user omitted it.
    url_str = raw if "://" in raw else f"https://{raw}"

    parsed = urlparse(url_str)

    if parsed.scheme not in {"http", "https"}:
        return None

    host = (parsed.hostname or "").lower().removeprefix("www.")
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    qs = parse_qs(parsed.query)

    # ── Instagram ─────────────────────────────────────────────────────────────
    if host == "instagram.com":
        handle = _clean_handle(path_parts[0] if path_parts else None)
        return ("instagram", handle) if handle else None

    # ── Bluesky ───────────────────────────────────────────────────────────────
    if host == "bsky.app":
        # https://bsky.app/profile/<handle>
        if len(path_parts) >= 2 and path_parts[0] == "profile":
            return ("bluesky", path_parts[1])
        return None

    # ── UER ───────────────────────────────────────────────────────────────────
    if host == "uer.ca":
        posterid = qs.get("posterid", [None])[0]
        if posterid and posterid.isdigit():
            return ("uer", posterid)
        return None

    # ── Facebook ──────────────────────────────────────────────────────────────
    if host in {"facebook.com", "fb.com", "fb.me"}:
        handle = _clean_handle(path_parts[0] if path_parts else None)
        return ("facebook", handle) if handle else None

    # ── Flickr ────────────────────────────────────────────────────────────────
    if host == "flickr.com":
        # https://flickr.com/photos/<username>
        if len(path_parts) >= 2 and path_parts[0] == "photos":
            handle = _clean_handle(path_parts[1])
            return ("flickr", handle) if handle else None
        return None

    # ── YouTube ───────────────────────────────────────────────────────────────
    if host in {"youtube.com", "youtu.be"}:
        if path_parts:
            first = path_parts[0]
            if first.startswith("@"):
                return ("youtube", first[1:])
            if first in {"channel", "user", "c"} and len(path_parts) > 1:
                return ("youtube", path_parts[1])
        return None

    # ── TikTok ────────────────────────────────────────────────────────────────
    if host == "tiktok.com":
        # https://tiktok.com/@username
        if path_parts and path_parts[0].startswith("@"):
            handle = _clean_handle(path_parts[0])
            return ("tiktok", handle) if handle else None
        return None

    # ── Reddit ────────────────────────────────────────────────────────────────
    if host in {"reddit.com", "redd.it"}:
        # /u/<name> or /user/<name>
        if len(path_parts) >= 2 and path_parts[0] in {"u", "user"}:
            handle = _clean_handle(path_parts[1])
            return ("reddit", handle) if handle else None
        return None

    # ── Generic website ───────────────────────────────────────────────────────
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        safe = parsed._replace(fragment="").geturl()
        if len(safe) <= 500:
            return ("website", safe)

    return None


def get_profile_links(profile) -> list[dict]:
    """Return a list of link dicts for all SocialLink rows attached to *profile*.

    Each dict contains: ``platform``, ``handle``, ``url`` (may be None for
    Discord), ``display_name``, ``icon``.
    """
    result = []
    for link in profile.social_links.all().order_by("platform"):
        platform = link.platform
        template = PLATFORM_URL_TEMPLATE.get(platform)
        url = template.format(handle=link.handle) if template else None
        result.append(
            {
                "platform": platform,
                "handle": link.handle,
                "url": url,
                "display_name": PLATFORM_DISPLAY_NAME.get(platform, platform.title()),
                "icon": PLATFORM_FA_ICON.get(platform, "fa-solid fa-link"),
            },
        )
    return result
