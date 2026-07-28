"""Avatar resolution, download, emoji-avatar generation, and avatar writes.

:class:`AvatarService` is the read/generate half - it answers "what image
should this account have" from an OAuth payload, a Gravatar hash, or a
generated emoji SVG. The module-level ``set_profile_avatar*`` functions are the
*write* half, and exist because the two call sites that previously wrote
``Profile.avatar`` did not agree with each other: the profile hero's upload
form ran the shared ``image_upload_error`` gauntlet (size cap, magic-byte
sniffing, antivirus) while the inline auto-save field
(``ProfileFieldUpdateView``, ``field=avatar``) assigned the uploaded file
straight onto the model with no checks at all - an unauthenticated-by-content
write of arbitrary bytes into media storage, reachable by any logged-in user.
Putting the write behind one function means a new surface (the external API's
``PUT /profiles/{slug}/avatar/``) cannot pick the unguarded variant by
accident, because the unguarded variant no longer exists.

The gravatar path is deliberately *not* offered here as a reusable function:
it makes an outbound HTTP fetch keyed on the account's email address, which is
fine as an explicit button the account owner presses and is not something an
API credential should be able to trigger on the owner's behalf.
"""

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
    from django.core.files.uploadedfile import UploadedFile

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Fallback animal when the caller names one that isn't in ``ANIMAL_EMOJIS``.
DEFAULT_AVATAR_ANIMAL = "fox"

#: Background used when a caller names no colour at all.
DEFAULT_AVATAR_COLOR = MaterialColor.GREEN.value

#: Background substituted when a caller names a colour outside
#: ``MaterialColor``. Restricting to the palette is not cosmetic: the value is
#: interpolated into generated SVG markup, so an arbitrary caller-supplied
#: string there would be an injection point into a file the site then serves.
#: Kept distinct from :data:`DEFAULT_AVATAR_COLOR` so "you sent nothing" and
#: "you sent something we refused" stay visibly different in the result.
UNRECOGNIZED_AVATAR_COLOR_FALLBACK = MaterialColor.GREY.value


class AvatarUploadError(Exception):
    """An avatar write was refused before anything was stored.

    Carries the HTTP status the refusal maps to, because the underlying
    ``services.images.image_upload_error`` already distinguishes them - 413 for
    an over-large file, 400 for a content-type that doesn't match its bytes,
    422 for a malware hit, 503 when the scanner itself is unreachable - and
    collapsing them would tell a client "bad image" when the real answer is
    "try again in a minute".
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        """Initialize with a caller-safe message and its HTTP status.

        Args:
            message: Human-readable detail, safe to surface verbatim.
            status_code: The HTTP status this refusal maps to.
        """
        super().__init__(message)
        self.status_code = status_code


class AvatarService:
    """Avatar utilities: emoji SVG generation, provider-URL resolution, and image download.

    All methods are class methods so callers never need to instantiate this class.
    The class groups related constants (ANIMAL_EMOJIS, COLORS) with the functions
    that consume them, and can be subclassed to swap out the data or extend behavior.
    """

    ANIMAL_EMOJIS: dict[str, str] = {
        "labelr": "🦡",
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
        chosen_animals = _random.sample(candidates, n)  # nosec B311 - cosmetic avatar suggestions, not security-sensitive
        chosen_colors = _random.sample(cls.COLORS, n)  # nosec B311 - cosmetic avatar suggestions, not security-sensitive
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


def set_profile_avatar(profile: Profile, uploaded_file: UploadedFile) -> Profile:
    """Store an uploaded image as ``profile``'s avatar.

    Runs the shared ``services.images.image_upload_error`` gauntlet first -
    site-wide size cap, magic-byte content sniffing, antivirus - and stores
    nothing at all when any check fails. The sniffing step is the one that
    matters most here: an avatar is rendered by every page that names its
    owner, so a file that claims to be a PNG and isn't gets the widest possible
    distribution of anything a user can upload.

    No ``Image`` row is created and no photo quota is consumed: an avatar is a
    field on the profile, not a library item. That is why the external API
    gates this on ``social:write`` rather than ``photos:write``.

    Args:
        profile: The profile whose avatar is being replaced.
        uploaded_file: The submitted file.

    Returns:
        The same profile, with ``avatar`` saved.

    Raises:
        AvatarUploadError: The file failed one of the pre-storage checks. The
            exception carries the status code that check maps to.
    """
    from urbanlens.dashboard.models.images.model import MediaKind
    from urbanlens.dashboard.services.images import image_upload_error

    upload_error = image_upload_error(uploaded_file, MediaKind.PHOTO)
    if upload_error:
        message, status_code = upload_error
        raise AvatarUploadError(message, status_code)

    profile.avatar = uploaded_file
    profile.save(update_fields=["avatar"])
    return profile


def set_profile_avatar_from_emoji(profile: Profile, animal: str, color: str) -> Profile:
    """Generate an emoji avatar and store it as ``profile``'s avatar.

    Both inputs are coerced to known-good values rather than rejected, because
    the site's own picker offers a fixed set of suggestions and a stale one
    should still produce *an* avatar rather than an error dialog. The coercion
    is a security boundary as well as a convenience: ``color`` is interpolated
    directly into the generated SVG, so anything outside ``MaterialColor``
    would be markup injection into a file the site subsequently serves.
    Surfaces that want strict validation (the external API does, so a client
    learns it sent a typo) validate before calling.

    No ``image_upload_error`` pass here, deliberately: the bytes are generated
    by :meth:`AvatarService.generate_emoji_svg` from a fixed template and two
    values this function has just restricted to enum members, so there is no
    untrusted content to sniff or scan.

    Args:
        profile: The profile whose avatar is being replaced.
        animal: A key of :attr:`AvatarService.ANIMAL_EMOJIS`.
        color: A ``MaterialColor`` hex value.

    Returns:
        The same profile, with the generated SVG saved to ``avatar``.
    """
    from django.core.files.base import ContentFile

    emoji = AvatarService.ANIMAL_EMOJIS.get(animal) or AvatarService.ANIMAL_EMOJIS[DEFAULT_AVATAR_ANIMAL]
    if (color or "").lower() not in {value.lower() for value in MaterialColor.values}:
        color = UNRECOGNIZED_AVATAR_COLOR_FALLBACK

    svg = AvatarService.generate_emoji_svg(emoji, color)
    profile.avatar.save(f"emoji_{profile.pk}.svg", ContentFile(svg.encode("utf-8")), save=True)
    return profile


def clear_profile_avatar(profile: Profile) -> Profile:
    """Remove ``profile``'s avatar, deleting the stored file.

    Idempotent - clearing an already-empty avatar is a no-op rather than an
    error, so a retried mobile DELETE stays safe.

    Uses ``FieldFile.delete`` rather than assigning ``None``, so the underlying
    file leaves storage too. Leaving it behind would keep a previous avatar
    fetchable by anyone who had ever seen its URL, which is precisely what a
    user removing their avatar is asking not to happen.

    Args:
        profile: The profile whose avatar is being cleared.

    Returns:
        The same profile, with ``avatar`` empty.
    """
    if profile.avatar:
        profile.avatar.delete(save=True)
    return profile
