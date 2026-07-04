r"""Social link URL parser and profile-link renderer.

Security contract
-----------------
* Only http/https URLs are accepted - data:, javascript:, etc. are rejected.
* Every extracted handle is validated against per-platform rules (``_PLATFORM_HANDLE_RULES``);
  handles outside the allowed character set or length range are rejected, preventing
  HTML injection and path-traversal payloads.
* For ``website`` links the full canonicalized URL is stored, but fragments
  (``#...``) are stripped and length is capped at 500 characters.
* Discord has no public profile-URL format; its handle is accepted via a
  dedicated form field and validated separately.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

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

# Ordered display labels shown in the "Supported:" hint beneath the URL input.
# Discord is intentionally absent - it uses a dedicated username form, not URL parsing.
# "website" gets a friendlier label here instead of just "Website".
# When adding a new URL-parseable platform, append its key to this tuple and the label
# will appear automatically everywhere the hint is rendered.
URL_INPUT_PLATFORM_LABELS: list[str] = [PLATFORM_DISPLAY_NAME[p] for p in ("instagram", "bluesky", "uer", "facebook", "flickr", "youtube", "tiktok", "reddit")] + ["any website URL"]


class _HandleRule(NamedTuple):
    """Validation rule for a platform's username/handle field."""

    pattern: re.Pattern[str]
    min_len: int
    max_len: int
    hint: str


# Per-platform rules applied *after* extracting the handle from a URL.
# Platforms not listed here (discord, website) are validated elsewhere or not at all.
_PLATFORM_HANDLE_RULES: dict[str, _HandleRule] = {
    "instagram": _HandleRule(re.compile(r"^[a-zA-Z0-9._]+$"), 1, 30, "1-30 chars: letters, digits, dots, underscores"),
    "bluesky": _HandleRule(re.compile(r"^[a-zA-Z0-9._-]+$"), 3, 253, "3-253 chars: letters, digits, dots, hyphens"),
    "facebook": _HandleRule(re.compile(r"^[a-zA-Z0-9._]+$"), 1, 50, "1-50 chars: letters, digits, dots, underscores"),
    "flickr": _HandleRule(re.compile(r"^[a-zA-Z0-9@._-]+$"), 3, 64, "3-64 chars: letters, digits, @, dots, hyphens"),
    "youtube": _HandleRule(re.compile(r"^[a-zA-Z0-9_.-]+$"), 1, 100, "1-100 chars: letters, digits, underscores, dots, hyphens"),
    "tiktok": _HandleRule(re.compile(r"^[a-zA-Z0-9._]+$"), 1, 24, "1-24 chars: letters, digits, dots, underscores"),
    "reddit": _HandleRule(re.compile(r"^[a-zA-Z0-9_-]+$"), 3, 20, "3-20 chars: letters, digits, underscores, hyphens"),
    "uer": _HandleRule(re.compile(r"^\d+$"), 1, 10, "numeric ID (1-10 digits)"),
}

# Platforms for which we attempt to verify the URL resolves after saving.
# Excludes discord (no URL) and website (arbitrary URLs, not username-based).
VERIFIABLE_PLATFORMS: frozenset[str] = frozenset(_PLATFORM_HANDLE_RULES) - {"uer"}

# Broad pre-check: reject anything outside the superset of all platform chars
# before doing per-platform validation.
_HANDLE_RE = re.compile(r"^[\w.\-@#]+$")


def _clean_handle(s: str | None) -> str | None:
    """Strip leading @, apply broad character pre-check, return None if invalid."""
    if not s:
        return None
    s = s.strip().lstrip("@")
    return s if s and _HANDLE_RE.match(s) else None


def validate_handle(platform: str, handle: str) -> str | None:
    """Return an error message if *handle* violates the per-platform rules, else None.

    Args:
        platform: A key from :data:`_PLATFORM_HANDLE_RULES`.
        handle: The already-stripped (no leading @) username string.

    Returns:
        A human-readable error string, or ``None`` when the handle is valid.
    """
    rule = _PLATFORM_HANDLE_RULES.get(platform)
    if rule is None:
        return None
    if not rule.min_len <= len(handle) <= rule.max_len:
        return f"Username must be {rule.hint}."
    if not rule.pattern.match(handle):
        return f"Username must be {rule.hint}."
    return None


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

    # -- Instagram -------------------------------------------------------------
    if host == "instagram.com":
        handle = _clean_handle(path_parts[0] if path_parts else None)
        if not handle or validate_handle("instagram", handle) is not None:
            return None
        return ("instagram", handle)

    # -- Bluesky ---------------------------------------------------------------
    if host == "bsky.app":
        # https://bsky.app/profile/<handle>
        if len(path_parts) >= 2 and path_parts[0] == "profile":
            handle = _clean_handle(path_parts[1])
            if not handle or validate_handle("bluesky", handle) is not None:
                return None
            return ("bluesky", handle)
        return None

    # -- UER -------------------------------------------------------------------
    if host == "uer.ca":
        posterid = qs.get("posterid", [None])[0]
        if posterid and validate_handle("uer", posterid) is None:
            return ("uer", posterid)
        return None

    # -- Facebook --------------------------------------------------------------
    if host in {"facebook.com", "fb.com", "fb.me"}:
        handle = _clean_handle(path_parts[0] if path_parts else None)
        if not handle or validate_handle("facebook", handle) is not None:
            return None
        return ("facebook", handle)

    # -- Flickr ----------------------------------------------------------------
    if host == "flickr.com":
        # https://flickr.com/photos/<username>
        if len(path_parts) >= 2 and path_parts[0] == "photos":
            handle = _clean_handle(path_parts[1])
            if not handle or validate_handle("flickr", handle) is not None:
                return None
            return ("flickr", handle)
        return None

    # -- YouTube ---------------------------------------------------------------
    if host in {"youtube.com", "youtu.be"}:
        if path_parts:
            first = path_parts[0]
            if first.startswith("@"):
                handle = _clean_handle(first)
                if not handle or validate_handle("youtube", handle) is not None:
                    return None
                return ("youtube", handle)
            if first in {"channel", "user", "c"} and len(path_parts) > 1:
                handle = _clean_handle(path_parts[1])
                if not handle or validate_handle("youtube", handle) is not None:
                    return None
                return ("youtube", handle)
        return None

    # -- TikTok ----------------------------------------------------------------
    if host == "tiktok.com":
        # https://tiktok.com/@username
        if path_parts and path_parts[0].startswith("@"):
            handle = _clean_handle(path_parts[0])
            if not handle or validate_handle("tiktok", handle) is not None:
                return None
            return ("tiktok", handle)
        return None

    # -- Reddit ----------------------------------------------------------------
    if host in {"reddit.com", "redd.it"}:
        # /u/<name> or /user/<name>
        if len(path_parts) >= 2 and path_parts[0] in {"u", "user"}:
            handle = _clean_handle(path_parts[1])
            if not handle or validate_handle("reddit", handle) is not None:
                return None
            return ("reddit", handle)
        return None

    # -- Generic website -------------------------------------------------------
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        safe = parsed._replace(fragment="").geturl()
        if len(safe) <= 500:
            return ("website", safe)

    return None


def get_profile_links(profile: Profile) -> list[dict]:
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
