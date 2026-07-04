"""Username validation, normalization, and random generation."""

from __future__ import annotations

from datetime import UTC, datetime, timezone
import logging
import re
import secrets

from django.contrib.auth.models import User

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,30}$")

# Maps individual characters to their canonical form for collision detection.
# Digits are replaced with the letters they visually resemble (leet speak);
# 'i' is replaced with 'l' because they are indistinguishable in many fonts.
# Underscores are stripped entirely so "JohnM", "John_M", and "JohnM_" all
# compare equal.
_CONFUSABLE_CHAR_MAP: dict[str, str] = {
    "0": "o",
    "1": "l",
    "2": "z",
    "3": "e",
    "4": "a",
    "5": "s",
    "6": "g",
    "7": "t",
    "8": "b",
    "9": "g",
    "i": "l",
}


def normalize_username_key(username: str) -> str:
    """Return a case-, underscore-, and confusable-insensitive key for username comparison.

    Strips underscores so that ``john_m`` and ``johnm`` are treated as the same
    username.  Applies leet-speak substitutions so that ``j0hn`` and ``john``
    are also treated as the same.

    Args:
        username: Raw username string.

    Returns:
        Normalized key suitable for equality checks.
    """
    return "".join(_CONFUSABLE_CHAR_MAP.get(ch, ch) for ch in username.casefold() if ch != "_")


def username_is_taken(username: str, *, exclude_user_id: int | None = None) -> bool:
    """Return True when another account already owns this username or a confusable variant.

    Comparison is case-insensitive, underscore-insensitive, and treats visually
    confusable characters as equivalent (e.g. ``o``/``0``, ``l``/``1``/``i``,
    ``john_m``/``johnm``).

    Args:
        username: Candidate username.
        exclude_user_id: Optional user primary key to ignore (for self-edits).

    Returns:
        True when the username collides with an existing account.
    """
    candidate_key = normalize_username_key(username)
    queryset = User.objects.all()
    if exclude_user_id is not None:
        queryset = queryset.exclude(pk=exclude_user_id)
    return any(normalize_username_key(existing) == candidate_key for existing in queryset.values_list("username", flat=True))


class UsernameGenerator:
    """Random username generator using adjective + animal + number patterns.

    Word lists and generation parameters are class attributes so they can be
    overridden in a subclass without touching the generation logic.

    Example::

        username = UsernameGenerator.generate()
    """

    ADJECTIVES: tuple[str, ...] = (
        "agile",
        "amber",
        "ancient",
        "bold",
        "brave",
        "bright",
        "calm",
        "clear",
        "cool",
        "cosmic",
        "crisp",
        "daring",
        "deep",
        "deft",
        "dynamic",
        "early",
        "earthy",
        "epic",
        "fierce",
        "fleet",
        "free",
        "fresh",
        "golden",
        "grand",
        "green",
        "hollow",
        "humble",
        "keen",
        "kind",
        "late",
        "leafy",
        "light",
        "lofty",
        "lone",
        "loyal",
        "lucid",
        "lunar",
        "misty",
        "noble",
        "north",
        "open",
        "prime",
        "quick",
        "quiet",
        "rapid",
        "raw",
        "regal",
        "roaming",
        "rugged",
        "sharp",
        "silent",
        "silver",
        "sleek",
        "solar",
        "stark",
        "steady",
        "still",
        "stone",
        "stormy",
        "swift",
        "tall",
        "vast",
        "vivid",
        "warm",
        "wide",
        "wild",
        "wise",
        "worthy",
    )

    ANIMALS: tuple[str, ...] = (
        "badger",
        "bear",
        "beetle",
        "bison",
        "bobcat",
        "buck",
        "crane",
        "crow",
        "deer",
        "dove",
        "duck",
        "eagle",
        "elk",
        "falcon",
        "ferret",
        "finch",
        "fox",
        "gecko",
        "goat",
        "grouse",
        "hawk",
        "heron",
        "ibis",
        "jackal",
        "jaguar",
        "jay",
        "kestrel",
        "kite",
        "lark",
        "linnet",
        "lynx",
        "mink",
        "mole",
        "moose",
        "moth",
        "newt",
        "nighthawk",
        "otter",
        "owl",
        "peregrine",
        "pika",
        "pine",
        "puma",
        "quail",
        "raven",
        "robin",
        "salamander",
        "shrew",
        "skunk",
        "snipe",
        "sparrow",
        "starling",
        "stoat",
        "stork",
        "swift",
        "thrush",
        "toad",
        "viper",
        "vole",
        "wagtail",
        "warbler",
        "weasel",
        "whippet",
        "widgeon",
        "wolf",
        "wren",
    )

    FALLBACK_PREFIX: str = "explorer"
    MAX_RETRIES: int = 20

    @classmethod
    def generate(cls) -> str:
        """Return a random ``{adjective}{animal}{number}`` username that is not already taken.

        Tries up to ``MAX_RETRIES`` random combinations before falling back to a
        numeric suffix on ``FALLBACK_PREFIX``.  The fallback should essentially
        never be reached given the size of the word lists.

        Returns:
            A unique username string.
        """
        for _ in range(cls.MAX_RETRIES):
            adj = secrets.choice(cls.ADJECTIVES)
            animal = secrets.choice(cls.ANIMALS)
            number = secrets.randbelow(9_998) + 1
            username = f"{adj}{animal}{number}"
            if not username_is_taken(username):
                logger.debug("Generated random username: %s", username)
                return username

        logger.warning("All username candidates collided; falling back to %s", cls.FALLBACK_PREFIX)
        now = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")

        for _ in range(cls.MAX_RETRIES):
            fallback = f"{cls.FALLBACK_PREFIX}{now}{secrets.randbelow(8_000) + 1_000}"
            if not username_is_taken(fallback):
                logger.debug("Generated random username from fallback: %s", fallback)
                return fallback

        raise ValueError("All username candidates collided")
