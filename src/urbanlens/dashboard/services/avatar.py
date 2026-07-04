"""Avatar resolution, download, and emoji-avatar generation."""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import requests

from urbanlens.dashboard.models.colors import MaterialColor

if TYPE_CHECKING:
    from typing import Any

    from django.contrib.auth.models import User

logger = logging.getLogger(__name__)


class AvatarService:
    """Avatar utilities: emoji SVG generation, provider-URL resolution, and image download.

    All methods are class methods so callers never need to instantiate this class.
    The class groups related constants (ANIMAL_EMOJIS, COLORS) with the functions
    that consume them, and can be subclassed to swap out the data or extend behavior.
    """

    ANIMAL_EMOJIS: dict[str, str] = {
        "badger": "🦡",
        "bear": "🐻",
        "beetle": "🪲",
        "bison": "🦬",
        "bobcat": "🐱",
        "buck": "🦌",
        "crane": "🦢",
        "crow": "🐦‍⬛",
        "deer": "🦌",
        "dove": "🕊️",
        "duck": "🦆",
        "eagle": "🦅",
        "elk": "🫎",
        "falcon": "🦅",
        "ferret": "🐾",
        "finch": "🐦",
        "fox": "🦊",
        "gecko": "🦎",
        "goat": "🐐",
        "grouse": "🐦",
        "hawk": "🦅",
        "heron": "🦢",
        "ibis": "🦢",
        "jackal": "🐺",
        "jaguar": "🐆",
        "jay": "🐦",
        "kestrel": "🦅",
        "kite": "🐦",
        "lark": "🐦",
        "linnet": "🐦",
        "lynx": "🐱",
        "mink": "🦦",
        "mole": "🐭",
        "moose": "🫎",
        "moth": "🦋",
        "newt": "🦎",
        "nighthawk": "🦅",
        "otter": "🦦",
        "owl": "🦉",
        "peregrine": "🦅",
        "pika": "🐰",
        "pine": "🌲",
        "puma": "🦁",
        "quail": "🐦",
        "raven": "🐦‍⬛",
        "robin": "🐦",
        "salamander": "🦎",
        "shrew": "🐭",
        "skunk": "🦨",
        "snipe": "🐦",
        "sparrow": "🐦",
        "starling": "🐦",
        "stoat": "🐾",
        "stork": "🦢",
        "swift": "🐦",
        "thrush": "🐦",
        "toad": "🐸",
        "viper": "🐍",
        "vole": "🐭",
        "wagtail": "🐦",
        "warbler": "🐦",
        "weasel": "🐾",
        "whippet": "🐕",
        "widgeon": "🦆",
        "wolf": "🐺",
        "wren": "🐦",
    }

    COLORS: list[str] = list(MaterialColor.values)

    @classmethod
    def generate_emoji_svg(cls, emoji: str, color: str) -> str:
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
            f"{emoji}</text>"
            "</svg>"
        )

    @classmethod
    def random_options(cls, n: int = 4) -> list[dict[str, str]]:
        """Return *n* random (animal, emoji, color) dicts for the avatar picker.

        Both animals and colors are sampled without replacement so that no two
        suggestions share the same animal or the same background color.

        Args:
            n: Number of options to generate.

        Returns:
            List of dicts with keys ``animal``, ``emoji``, and ``color``.
        """
        import random as _random

        candidates = list(cls.ANIMAL_EMOJIS.items())
        n = min(n, len(candidates), len(cls.COLORS))
        chosen_animals = _random.sample(candidates, n)
        chosen_colors = _random.sample(cls.COLORS, n)
        return [{"animal": animal, "emoji": emoji, "color": chosen_colors[i]} for i, (animal, emoji) in enumerate(chosen_animals)]

    @classmethod
    def resolve_provider_url(cls, backend: Any, user: User, response: dict[str, Any]) -> str | None:
        """Return the provider-specific or Gravatar avatar URL for this user.

        Provider-specific URL resolution:
        - **Google OAuth2**: ``response['picture']``
        - **Discord OAuth2**: ``https://cdn.discordapp.com/avatars/{id}/{avatar}.png``
        - **Gravatar fallback**: ``https://www.gravatar.com/avatar/{md5(email)}``

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
                if "=s" in url:
                    url = url.rsplit("=s", 1)[0] + "=s256-c"
                return url

        elif name == "discord":
            user_id = response.get("id")
            avatar_hash = response.get("avatar")
            if user_id and avatar_hash:
                ext = "gif" if avatar_hash.startswith("a_") else "png"
                return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.{ext}?size=256"
            return None

        email = user.email or ""
        if email:
            # MD5 is required by the Gravatar API spec; not used for security.
            digest = hashlib.md5(email.strip().lower().encode(), usedforsecurity=False).hexdigest()
            return f"https://www.gravatar.com/avatar/{digest}?d=404&s=256"

        return None

    @classmethod
    def download(cls, url: str, timeout: int = 5) -> bytes | None:
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
            with requests.get(
                url,
                headers={"User-Agent": "UrbanLens/1.0"},
                stream=True,
                timeout=timeout,
            ) as response:
                if response.status_code != 200:
                    return None

                chunks: list[bytes] = []
                total_bytes = 0
                max_bytes = 512 * 1024
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        logger.warning("Rejecting avatar response larger than %s bytes: %s", max_bytes, url)
                        return None
                    chunks.append(chunk)
                return b"".join(chunks) or None
        except requests.RequestException as exc:
            logger.debug("Avatar download failed for %s: %s", url, exc)
            return None
