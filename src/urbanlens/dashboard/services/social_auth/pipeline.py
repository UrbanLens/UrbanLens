"""Custom python-social-auth pipeline steps.

These are inserted into ``SOCIAL_AUTH_PIPELINE`` in ``settings/local.py`` and
run for every OAuth login (Google, Discord, ...).

Step contracts
--------------
- Return ``None`` or an empty dict to do nothing and pass through.
- Return a dict to merge extra data into the pipeline state.
- Raise ``StopPipeline`` to abort the login.

All steps must accept ``**kwargs`` because the pipeline may pass extra
keyword arguments that we do not care about.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Any
from urllib.parse import urlparse

from django.contrib.auth.models import User
from django.core.files.base import ContentFile

from urbanlens.dashboard.models.colors import MaterialColor

logger = logging.getLogger(__name__)

# ── Username word lists ───────────────────────────────────────────────────────

_ADJECTIVES: tuple[str, ...] = (
    "agile", "amber", "ancient", "bold", "brave", "bright", "calm", "clear",
    "cool", "cosmic", "crisp", "daring", "deep", "deft", "dynamic", "early",
    "earthy", "epic", "fierce", "fleet", "free", "fresh", "golden", "grand",
    "green", "hollow", "humble", "keen", "kind", "late",
    "leafy", "light", "lofty", "lone", "loyal", "lucid", "lunar", "misty",
    "noble", "north", "open", "prime", "quick", "quiet", "rapid", "raw",
    "regal", "roaming", "rugged", "sharp", "silent", "silver", "sleek",
    "solar", "stark", "steady", "still", "stone", "stormy", "swift", "tall",
    "vast", "vivid", "warm", "wide", "wild", "wise", "worthy",
)

_ANIMALS: tuple[str, ...] = (
    "badger", "bear", "beetle", "bison", "bobcat", "buck", "crane",
    "crow", "deer", "dove", "duck", "eagle", "elk", "falcon", "ferret",
    "finch", "fox", "gecko", "goat", "grouse", "hawk", "heron", "ibis",
    "jackal", "jaguar", "jay", "kestrel", "kite", "lark", "linnet",
    "lynx", "mink", "mole", "moose", "moth", "newt", "nighthawk", "otter",
    "owl", "peregrine", "pika", "pine", "puma", "quail", "raven", "robin",
    "salamander", "shrew", "skunk", "snipe", "sparrow", "starling",
    "stoat", "stork", "swift", "thrush", "toad", "viper", "vole",
    "wagtail", "warbler", "weasel", "whippet", "widgeon", "wolf", "wren",
)

_FALLBACK_PREFIX = "explorer"
_MAX_RETRIES = 20

# ── Avatar helpers ────────────────────────────────────────────────────────────

_ANIMAL_EMOJIS: dict[str, str] = {
    "badger": "🦡", "bear": "🐻", "beetle": "🪲", "bison": "🦬",
    "bobcat": "🐱", "buck": "🦌", "crane": "🦢", "crow": "🐦‍⬛",
    "deer": "🦌", "dove": "🕊️", "duck": "🦆", "eagle": "🦅",
    "elk": "🫎", "falcon": "🦅", "ferret": "🐾", "finch": "🐦",
    "fox": "🦊", "gecko": "🦎", "goat": "🐐", "grouse": "🐦",
    "hawk": "🦅", "heron": "🦢", "ibis": "🦢", "jackal": "🐺",
    "jaguar": "🐆", "jay": "🐦", "kestrel": "🦅", "kite": "🐦",
    "lark": "🐦", "linnet": "🐦", "lynx": "🐱", "mink": "🦦",
    "mole": "🐭", "moose": "🫎", "moth": "🦋", "newt": "🦎",
    "nighthawk": "🦅", "otter": "🦦", "owl": "🦉", "peregrine": "🦅",
    "pika": "🐰", "pine": "🌲", "puma": "🦁", "quail": "🐦",
    "raven": "🐦‍⬛", "robin": "🐦", "salamander": "🦎", "shrew": "🐭",
    "skunk": "🦨", "snipe": "🐦", "sparrow": "🐦", "starling": "🐦",
    "stoat": "🐾", "stork": "🦢", "swift": "🐦", "thrush": "🐦",
    "toad": "🐸", "viper": "🐍", "vole": "🐭", "wagtail": "🐦",
    "warbler": "🐦", "weasel": "🐾", "whippet": "🐕", "widgeon": "🦆",
    "wolf": "🐺", "wren": "🐦",
}

_AVATAR_COLORS: list[str] = list(MaterialColor.values)


def generate_emoji_avatar_svg(emoji: str, color: str) -> str:
    """Return an SVG string: a filled circle with a centered emoji.

    Args:
        emoji: The Unicode emoji character to render.
        color: A CSS hex color string for the circle background.

    Returns:
        UTF-8-safe SVG markup.
    """
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">'
        f'<circle cx="100" cy="100" r="100" fill="{color}"/>'
        '<text x="100" y="140" text-anchor="middle" font-size="110" '
        'font-family="Segoe UI Emoji, Apple Color Emoji, Noto Color Emoji, sans-serif">'
        f'{emoji}</text>'
        '</svg>'
    )


def random_emoji_options(n: int = 4) -> list[dict[str, str]]:
    """Return *n* random (animal, emoji, color) dicts for the avatar picker.

    Both animals and colors are sampled without replacement so that no two
    suggestions share the same animal or the same background color.

    Args:
        n: Number of options to generate.

    Returns:
        List of dicts with keys ``animal``, ``emoji``, and ``color``.
    """
    import random as _random
    candidates = list(_ANIMAL_EMOJIS.items())
    n = min(n, len(candidates), len(_AVATAR_COLORS))
    chosen_animals = _random.sample(candidates, n)
    chosen_colors = _random.sample(_AVATAR_COLORS, n)
    return [
        {"animal": animal, "emoji": emoji, "color": chosen_colors[i]}
        for i, (animal, emoji) in enumerate(chosen_animals)
    ]


def generate_sso_username(
    backend: Any,
    user: User | None,
    response: dict[str, Any],
    details: dict[str, Any],
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Produce a random ``{adjective}{animal}{number}`` username for new SSO users.

    Replaces the default ``social_core.pipeline.user.get_username`` step so
    that SSO accounts never inherit a real name or email prefix as their
    username.  Existing users (``user`` is not None) are left unchanged.

    Args:
        backend: The social-auth backend in use.
        user: The existing Django User if this is a returning account, else None.
        response: Raw response from the OAuth provider.
        details: Normalised details dict produced by ``social_details``.

    Returns:
        Dict with ``username`` key for new users, or None for returning users.
    """
    if user is not None:
        # Returning user - keep their existing username.
        return {"username": user.username}

    for _ in range(_MAX_RETRIES):
        adj = secrets.choice(_ADJECTIVES)
        animal = secrets.choice(_ANIMALS)
        number = secrets.randbelow(9999) + 1
        username = f"{adj}{animal}{number}"
        if not User.objects.filter(username__iexact=username).exists():
            logger.debug("Generated SSO username: %s", username)
            return {"username": username}

    # Should essentially never happen given the size of the word lists.
    fallback = f"{_FALLBACK_PREFIX}{secrets.randbelow(90_000) + 10_000}"
    logger.warning("All username candidates collided; falling back to %s", fallback)
    return {"username": fallback}


def suppress_last_name_for_new_users(
    backend: Any,
    user: User | None,
    response: dict[str, Any],
    is_new: bool = False,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Clear ``last_name`` on the Django User for brand-new SSO accounts.

    Runs after ``user_details`` (which copies the provider's given/family name
    into the User model).  Preserving the first name lets the UI greet the
    user naturally while stripping the last name limits personal data exposure.

    Existing users are not affected so that a user who manually added their
    last name on their profile settings doesn't lose it on every subsequent
    login.

    Args:
        backend: The social-auth backend in use.
        user: The Django User being logged in.
        response: Raw response from the OAuth provider.
        is_new: True only when the User row was just created in this pipeline run.
    """
    if not is_new or user is None:
        return
    if user.last_name:
        user.last_name = ""
        user.save(update_fields=["last_name"])
        logger.debug("Cleared last_name for new SSO user %s", user.username)


def fetch_and_save_avatar(
    backend: Any,
    user: User | None,
    response: dict[str, Any],
    is_new: bool = False,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Download the provider avatar (or Gravatar) and store it on the Profile.

    Only fetches when the profile has no existing avatar so that users who
    upload their own photo are not overwritten on subsequent logins.

    Provider-specific URL resolution:
    - **Google OAuth2**: ``response['picture']``
    - **Discord OAuth2**: ``https://cdn.discordapp.com/avatars/{id}/{avatar}.png``
    - **Gravatar fallback**: ``https://www.gravatar.com/avatar/{md5(email)}``

    Args:
        backend: The social-auth backend in use (name is ``backend.name``).
        user: The Django User, or None if authentication failed earlier.
        response: Raw response from the OAuth provider.
        is_new: True when the User was just created in this pipeline run.
    """
    if user is None:
        return

    try:
        profile = user.profile
    except Exception:
        logger.warning("No profile found for user %s; skipping avatar fetch", user.pk)
        return

    if profile.avatar:
        # User already has an avatar - don't overwrite.
        return

    avatar_url = _resolve_avatar_url(backend, user, response)
    if not avatar_url:
        return

    image_bytes = _download_image(avatar_url)
    if not image_bytes:
        return

    filename = f"sso_avatar_{user.pk}.jpg"
    profile.avatar.save(filename, ContentFile(image_bytes), save=True)
    logger.info("Saved SSO avatar for user %s from %s", user.username, backend.name)


# ── Internal helpers ──────────────────────────────────────────────────────────


def mark_new_user_onboarding(
    backend: Any,
    user: User | None,
    is_new: bool = False,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Set profile_setup_complete=False for brand-new SSO users.

    Causes PostLoginRedirectView to send them to /profile/edit/ so they can
    choose a username and avatar before landing on the map.  Existing users
    and email-registered users are not affected.

    Args:
        backend: The social-auth backend in use.
        user: The Django User, or None if authentication failed earlier.
        is_new: True when the User was just created in this pipeline run.
    """
    if not is_new or user is None:
        return
    try:
        profile = user.profile
        profile.profile_setup_complete = False
        profile.save(update_fields=["profile_setup_complete"])
        logger.debug("Marked onboarding incomplete for new SSO user %s", user.username)
    except Exception:
        logger.warning("Could not mark onboarding for new SSO user pk=%s", getattr(user, "pk", "?"))


def save_discord_social_link(
    backend: Any,
    user: User | None,
    response: dict[str, Any],
    *args: Any,
    **kwargs: Any,
) -> None:
    """Store the Discord username as a SocialLink for Discord SSO users.

    Runs for every Discord login so that username changes on Discord are
    reflected in UrbanLens.  Only overwrites the stored handle; does not
    remove the link if the response is missing a username.

    Args:
        backend: The social-auth backend in use.
        user: The Django User, or None if authentication failed earlier.
        response: Raw OAuth response payload from Discord.
    """
    if user is None or getattr(backend, "name", "") != "discord":
        return

    username = response.get("username")
    if not username:
        return

    try:
        profile = user.profile
    except Exception:
        logger.warning("No profile found for user %s; skipping Discord social link", user.pk)
        return

    from urbanlens.dashboard.models.social_link.model import SocialLink

    SocialLink.objects.update_or_create(
        profile=profile,
        platform="discord",
        defaults={"handle": username},
    )
    logger.debug("Saved Discord social link for user %s: %s", user.username, username)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _resolve_avatar_url(backend: Any, user: User, response: dict[str, Any]) -> str | None:
    """Return the provider-specific or Gravatar avatar URL for this user.

    Args:
        backend: The social-auth backend in use.
        user: The authenticated Django User.
        response: Raw OAuth response payload.

    Returns:
        A URL string or None if no avatar could be determined.
    """
    name = getattr(backend, "name", "")

    if name == "google-oauth2":
        url = response.get("picture")
        if url:
            # Request a larger size (256 px) than the default.
            if "=s" in url:
                url = url.rsplit("=s", 1)[0] + "=s256-c"
            return url

    elif name == "discord":
        user_id = response.get("id")
        avatar_hash = response.get("avatar")
        if user_id and avatar_hash:
            ext = "gif" if avatar_hash.startswith("a_") else "png"
            return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.{ext}?size=256"
        # Discord users without a custom avatar have a default - skip rather
        # than downloading a generic coloured square.
        return None

    # Gravatar fallback for any provider (including unknown ones).
    email = user.email or ""
    if email:
        # MD5 is required by the Gravatar API spec; not used for security.
        digest = hashlib.md5(email.strip().lower().encode(), usedforsecurity=False).hexdigest()
        # ?d=404 means Gravatar returns HTTP 404 if no image exists (vs a default).
        return f"https://www.gravatar.com/avatar/{digest}?d=404&s=256"

    return None


def _download_image(url: str, timeout: int = 5) -> bytes | None:
    """Fetch image bytes from a URL, returning None on any failure.

    Only http and https URLs are accepted; any other scheme is rejected before
    the network request is made.

    Args:
        url: The full URL of the image to download.
        timeout: Request timeout in seconds.

    Returns:
        Raw image bytes, or None if the download failed or returned a non-200 status.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        logger.warning("Rejecting avatar URL with unexpected scheme: %s", parsed.scheme)
        return None

    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "UrbanLens/1.0"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if resp.status != 200:
                return None
            data = resp.read(512 * 1024)  # cap at 512 KiB
            return data or None
    except Exception as exc:
        logger.debug("Avatar download failed for %s: %s", url, exc)
        return None
