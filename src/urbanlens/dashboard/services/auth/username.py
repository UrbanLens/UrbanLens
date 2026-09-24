"""Username validation, normalization, and random generation."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
import re
import secrets
import unicodedata

from django.contrib.auth.models import User
from django.db.models import Q

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,30}$")

USERNAME_RULES = "3-30 characters: letters, numbers, and underscores only."
#: The one answer for a username that is malformed, reserved, taken, or too close to a taken one.
USERNAME_UNAVAILABLE = "That username isn't available."

# Maps individual characters to their canonical form for collision detection.
# Digits are replaced with the letters they visually resemble (leet speak); 'i' is replaced with 'l'
# because they are indistinguishable in many fonts.
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


def _fold(value: str) -> str:
    """Unicode compatibility-fold and case-fold ``value`` (NFKC_Casefold), so the result is stable under a second pass."""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", value).casefold())


def normalize_username_key(username: str) -> str:
    """Return the key two usernames share when they would read as the same name.

    Compatibility forms fold to plain characters, case is ignored, every separator and symbol is dropped, and
    look-alike characters collapse, so ``Foo.Bar``, ``foo_bar``, ``_f-o-o-b-a-r-`` and ``f00bar`` all key to
    ``foobar``'s key.

    Args:
        username: Raw username string.

    Returns:
        Normalized key suitable for equality checks; empty when nothing alphanumeric remains. Folding can
        make it longer than the username."""
    return "".join(_CONFUSABLE_CHAR_MAP.get(ch, ch) for ch in _fold(username) if ch.isalnum())


def _is_reserved(username: str) -> bool:
    """Whether ``username`` reads as a demo account's name, which ordinary accounts may not take."""
    from urbanlens.dashboard.services.demo import DEMO_USERNAME_PREFIX

    def confusable_folded(value: str) -> str:
        return "".join(_CONFUSABLE_CHAR_MAP.get(ch, ch) for ch in _fold(value))

    return confusable_folded(username.strip()).startswith(confusable_folded(DEMO_USERNAME_PREFIX))


def username_is_taken(username: str, *, exclude_user_id: int | None = None) -> bool:
    """Return True when another account already owns this username or any spelling that shares its key.
    The demo prefix is reserved rather than merely unused.

    Args:
        username: Candidate username.
        exclude_user_id: Optional user primary key to ignore (for self-edits).

    Returns:
        True when the username collides with an existing account, or is reserved."""
    from urbanlens.dashboard.models.profile.model import Profile

    if _is_reserved(username):
        return True
    key = normalize_username_key(username)
    exact = User.objects.filter(username__iexact=username.strip())
    holders = Profile.objects.filter(username_key=key) if key else Profile.objects.none()
    if exclude_user_id is not None:
        exact = exact.exclude(pk=exclude_user_id)
        holders = holders.exclude(user_id=exclude_user_id)
    return holders.exists() or exact.exists()


def username_is_available(username: str, *, exclude_user_id: int | None = None) -> bool:
    """Whether ``username`` is well-formed and free, so callers can refuse every other case with one message.

    Args:
        username: Candidate username.
        exclude_user_id: Optional user primary key to ignore (for self-edits).

    Returns:
        True only when the name matches :data:`USERNAME_RE` and :func:`username_is_taken` is False."""
    return bool(USERNAME_RE.match(username)) and not username_is_taken(username, exclude_user_id=exclude_user_id)


def find_user_by_username(username: str, *, active_only: bool = True) -> User | None:
    """The account a typed username names: an exact match, else the one account holding its key.

    Args:
        username: Any spelling of a username - case, separators and look-alike characters are ignored.
        active_only: When True, inactive accounts never match.

    Returns:
        The matching User, or None when nothing, or more than one legacy account, holds the key.
    """
    from urbanlens.dashboard.models.profile.model import Profile

    username = username.strip()
    if not username:
        return None
    users = User.objects.filter(is_active=True) if active_only else User.objects.all()
    exact = users.filter(username=username).first()
    if exact is not None:
        return exact
    key = normalize_username_key(username)
    if not key:
        return None
    holders = Profile.objects.filter(username_key=key)
    if active_only:
        holders = holders.filter(user__is_active=True)
    user_ids = list(holders.values_list("user_id", flat=True)[:2])
    if len(user_ids) != 1:
        return None
    return users.filter(pk=user_ids[0]).first()


def username_search_q(query: str, *, profile_path: str = "") -> Q:
    """Match profiles whose username contains ``query``, in any spelling that shares its key.

    Args:
        query: The typed search fragment.
        profile_path: ORM path to the Profile being matched, with its trailing ``__`` (e.g. ``"sender__"``);
            empty when filtering Profiles directly.

    Returns:
        A Q over the username itself and over its key.
    """
    match = Q(**{f"{profile_path}user__username__icontains": query})
    key = normalize_username_key(query)
    if key:
        match |= Q(**{f"{profile_path}username_key__contains": key})
    return match


class UsernameGenerator:
    """Random username generator using adjective + animal + number patterns.
    Word lists and generation parameters are class attributes so they can be overridden in a subclass without touching the generation logic.

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
        "labelr",
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
        The fallback should essentially never be reached given the size of the word lists.

        Returns:
            A unique username string."""
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
