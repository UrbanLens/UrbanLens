"""URL-slug construction: parent prefixes, word-boundary truncation, uniqueness.

``PublicDashboardModel._generate_slug`` is the only writer. Child pins and child
wikis pass a short parent prefix (an existing alias when one is compact enough,
otherwise one derived from the parent's long name) and a preferred length so a
building at Hudson River State Hospital lands at ``hrsh-powerhouse`` rather than
a mid-word clip of the building's own name.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from django.utils.text import slugify

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

#: Compact enough to lead a child slug (``hrsh-powerhouse``) without crowding
#: out the child's own name. Aliases longer than this are skipped in favour of
#: a generated prefix.
MAX_PREFIX_LENGTH = 8

#: Shorter than this is too cryptic (``S``, ``FM``) and we fall through to the
#: first word or a truncation of it.
MIN_PREFIX_LENGTH = 3

#: First-word prefixes may be a little longer than an acronym (``bannerman``)
#: before we treat them as too long and truncate.
MAX_FIRST_WORD_LENGTH = 10

#: When the first word itself is too long (``switzerland``), keep this many
#: characters (``switz``).
PREFIX_TRUNCATE_LENGTH = 5

#: Child pin/wiki slugs aim for this length, dropping whole trailing words
#: (including hyphenated compounds) rather than clipping mid-word. Uniqueness
#: and a too-short result may grow back toward the field's ``max_length``.
PREFERRED_CHILD_SLUG_LENGTH = 40

#: Ideal slugs shorter than this try to take back dropped words (including a
#: partial word if that's all that fits) before giving up.
MIN_SLUG_LENGTH = 8

#: Articles and light prepositions skipped when building an acronym so
#: "Hospital of the Hudson" does not become ``hoth``.
_ACRONYM_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "de",
        "el",
        "for",
        "in",
        "la",
        "las",
        "los",
        "of",
        "on",
        "the",
        "to",
    },
)

#: Punctuation stripped from the ends of a name token so
#: ``(non-contributing)`` stays one hyphenated word rather than growing extra
#: empty slug segments from the parentheses.
_WRAPPING_PUNCTUATION = "()[]{}<>,.;:!?\"'"


def is_uuid_slug(value: str | None) -> bool:
    """Return True when ``value`` is a UUID (the Location fallback slug).

    Child-wiki locations are often created before the wiki has a name, so they
    mint a UUID slug; once the wiki slug exists we replace that fallback.

    Args:
        value: A slug, or None.

    Returns:
        True when the string parses as a UUID.
    """
    if not value:
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def name_tokens(name: str) -> list[str]:
    """Split a place name into words, keeping hyphenated compounds together.

    Whitespace (and only whitespace) is the separator, so ``non-contributing``
    is one token while ``non contributing`` is two. Wrapping punctuation is
    stripped so a parenthetical still counts as the word inside it.

    Args:
        name: Raw display name.

    Returns:
        Non-empty tokens in order.
    """
    tokens: list[str] = []
    for raw in name.split():
        token = raw.strip(_WRAPPING_PUNCTUATION).strip("-")
        if token:
            tokens.append(token)
    return tokens


def slug_tokens(name: str) -> list[str]:
    """Slugify each :func:`name_tokens` entry, dropping tokens that slugify empty.

    ``Staff/Tenant`` becomes ``stafftenant`` (the slash is not a word break);
    ``non-contributing`` stays ``non-contributing``.

    Args:
        name: Raw display name.

    Returns:
        Slug tokens in order.
    """
    tokens: list[str] = []
    for token in name_tokens(name):
        slug = slugify(token)
        if slug:
            tokens.append(slug)
    return tokens


def parent_slug_prefix(names: Sequence[str]) -> str:
    """Choose a short slug prefix from a parent's names and aliases.

    Prefers the shortest existing alias that is already compact enough to lead
    a child slug. When none is, derives one from the primary (first) name:
    initials of significant words, or the first word, or a truncation of that
    word when even the first word is too long.

    Args:
        names: Display name first, then aliases and any other candidates
            (including an already-short parent slug). Empty strings are ignored.

    Returns:
        A lowercase slug prefix, or ``""`` when nothing usable can be derived.
    """
    cleaned = [name.strip() for name in names if name and name.strip()]
    if not cleaned:
        return ""

    candidates: list[str] = []
    for name in cleaned:
        slug = slugify(name)
        if slug and MIN_PREFIX_LENGTH <= len(slug) <= MAX_PREFIX_LENGTH:
            candidates.append(slug)
    if candidates:
        return min(candidates, key=lambda slug: (len(slug), slug.count("-"), slug))
    return generate_short_prefix(cleaned[0])


def generate_short_prefix(name: str) -> str:
    """Build a compact prefix from a long place name.

    ``Hudson River State Hospital`` → ``hrsh``. ``Switzerland`` → ``switz``.
    ``Ford Motors`` → ``ford`` (the initials ``fm`` are too short).

    Args:
        name: The parent's canonical name.

    Returns:
        A lowercase slug prefix, or ``""`` when the name slugifies empty.
    """
    words = _significant_words(name)
    if not words:
        slug = slugify(name)
        return slug[:MAX_PREFIX_LENGTH].rstrip("-") if slug else ""

    acronym = "".join(slugify(word)[0] for word in words if slugify(word))
    if len(acronym) >= MIN_PREFIX_LENGTH:
        return acronym[:MAX_PREFIX_LENGTH]

    first = slugify(words[0])
    if MIN_PREFIX_LENGTH <= len(first) <= MAX_FIRST_WORD_LENGTH:
        return first
    if len(first) > MAX_FIRST_WORD_LENGTH:
        return first[:PREFIX_TRUNCATE_LENGTH]

    parts = [first] if first else []
    for word in words[1:]:
        next_slug = slugify(word)
        if not next_slug:
            continue
        parts.append(next_slug)
        trial = "-".join(parts)
        if len(trial) >= MIN_PREFIX_LENGTH:
            if len(trial) <= MAX_PREFIX_LENGTH:
                return trial
            return trial[:MAX_PREFIX_LENGTH].rstrip("-")
    joined = "-".join(parts) if parts else slugify(name)
    return (joined or "")[:MAX_PREFIX_LENGTH].rstrip("-")


def unique_slug(
    name: str,
    *,
    is_taken: Callable[[str], bool],
    prefix: str = "",
    max_length: int,
    preferred_length: int | None = None,
    min_length: int = MIN_SLUG_LENGTH,
    fallback: str = "item",
) -> str:
    """Build a unique slug, preferring whole words over a mid-word clip.

    The ideal candidate is ``prefix`` plus as many leading name tokens as fit
    inside ``preferred_length``. Hyphenated compounds are one token, so
    ``non-contributing`` is dropped as a unit rather than becoming
    ``non-contributi``. When that ideal is taken, too short, or both, dropped
    tokens (whole, then partial) are added back up to ``max_length`` before a
    numeric suffix is appended.

    Args:
        name: Raw display name of the entity being slugged.
        is_taken: Returns True when a candidate is already in use in the
            relevant uniqueness scope.
        prefix: Optional parent-derived prefix (already a slug).
        max_length: Hard cap matching the slug column.
        preferred_length: Soft cap for the ideal slug; defaults to
            ``max_length``.
        min_length: Below this, leftover tokens are added back even if the
            ideal already fitted the preferred length.
        fallback: Used when ``name`` slugifies to nothing.

    Returns:
        A non-empty slug no longer than ``max_length``.
    """
    preferred = min(preferred_length or max_length, max_length)
    prefix_slug = slugify(prefix) if prefix else ""
    tokens = slug_tokens(name)
    if prefix_slug and tokens and tokens[0] == prefix_slug:
        tokens = tokens[1:]
    if not tokens:
        fallback_slug = slugify(fallback) or "item"
        tokens = [fallback_slug]

    for candidate in _slug_candidates(prefix_slug, tokens, preferred=preferred, max_length=max_length, min_length=min_length):
        if not is_taken(candidate):
            return candidate

    # Last resort: numeric suffix on the longest candidate that still leaves
    # room, trimmed at a hyphen so we do not clip a word to make space.
    longest = _join(prefix_slug, tokens)[:max_length].rstrip("-") or (prefix_slug or tokens[0] or "item")
    for _ in range(40):
        n = random.randint(2, 90_000)  # noqa: S311 # nosec: B311 - Used for slug generation
        suffix = f"-{n}"
        budget = max_length - len(suffix)
        if budget < 1:
            break
        trimmed = _trim_at_hyphen(longest, budget) or longest[:budget]
        candidate = (trimmed + suffix)[:max_length].rstrip("-")
        if candidate and not is_taken(candidate):
            return candidate

    return f"{uuid4()}"[:max_length]


def _significant_words(name: str) -> list[str]:
    """Name tokens with articles/prepositions removed, for acronyms."""
    words: list[str] = []
    for token in name_tokens(name):
        for part in token.replace("-", " ").split():
            slug = slugify(part)
            if slug and slug not in _ACRONYM_STOP_WORDS:
                words.append(part)
    return words


def _join(prefix: str, tokens: Sequence[str]) -> str:
    parts = [prefix, *tokens] if prefix else list(tokens)
    return "-".join(part for part in parts if part)


def _trim_at_hyphen(value: str, budget: int) -> str:
    """Cut ``value`` to ``budget`` at the last hyphen, not mid-word.

    Args:
        value: A slug.
        budget: Maximum length.

    Returns:
        A prefix of ``value`` no longer than ``budget``, or ``""`` when even
        the first segment does not fit.
    """
    if budget <= 0:
        return ""
    if len(value) <= budget:
        return value
    clipped = value[:budget]
    cut = clipped.rfind("-")
    if cut <= 0:
        return ""
    return clipped[:cut]


def _slug_candidates(
    prefix: str,
    tokens: list[str],
    *,
    preferred: int,
    max_length: int,
    min_length: int,
) -> list[str]:
    """Ideal slug first, then longer variants that reuse dropped tokens."""
    kept, dropped = _fit_whole_tokens(prefix, tokens, preferred)
    if not kept and dropped:
        # The first token alone is longer than the preferred length (a single
        # very long word). Take a partial so the ideal is still derived from
        # the name rather than collapsing to the prefix or "item".
        budget = preferred - (len(prefix) + 1 if prefix else 0)
        if budget > 0:
            partial = dropped[0][:budget].rstrip("-")
            if partial:
                kept.append(partial)
                remainder = dropped[0][len(partial) :].lstrip("-")
                dropped = [remainder, *dropped[1:]] if remainder else dropped[1:]
    ideal = _join(prefix, kept) or (prefix or "item")
    if len(ideal) > max_length:
        ideal = _trim_at_hyphen(ideal, max_length) or ideal[:max_length]
    ideal = ideal.rstrip("-") or "item"

    seen: set[str] = set()
    candidates: list[str] = []

    def add(value: str) -> None:
        slug = value[:max_length].rstrip("-")
        if slug and slug not in seen and len(slug) <= max_length:
            seen.add(slug)
            candidates.append(slug)

    add(ideal)

    # Too short: take back dropped words (whole, then a partial) until we
    # reach min_length or run out of leftover text.
    grown = list(kept)
    remaining = list(dropped)
    current = ideal
    if len(current) < min_length and remaining:
        grown, remaining, current = _grow(prefix, grown, remaining, min_length=min_length, limit=preferred)
        add(current)

    # Uniqueness: keep adding leftover tokens up to the column width.
    grown, remaining, current = _grow(prefix, grown, remaining, min_length=max_length, limit=max_length)
    add(current)
    while remaining:
        before = len(remaining)
        grown, remaining, current = _grow(prefix, grown, remaining, min_length=len(current) + 1, limit=max_length)
        add(current)
        if len(remaining) >= before or len(current) >= max_length:
            break

    return candidates


def _fit_whole_tokens(prefix: str, tokens: Sequence[str], limit: int) -> tuple[list[str], list[str]]:
    """Take leading tokens while the joined slug stays within ``limit``."""
    kept: list[str] = []
    dropped = list(tokens)
    while dropped:
        trial = _join(prefix, [*kept, dropped[0]])
        if len(trial) <= limit:
            kept.append(dropped.pop(0))
        else:
            break
    return kept, dropped


def _grow(
    prefix: str,
    kept: list[str],
    dropped: list[str],
    *,
    min_length: int,
    limit: int,
) -> tuple[list[str], list[str], str]:
    """Add dropped tokens until ``min_length`` or ``limit`` is reached.

    Whole tokens are preferred. A partial token is used only when a whole one
    will not fit and the slug is still short of ``min_length``.

    Args:
        prefix: Parent prefix, possibly empty.
        kept: Tokens already in the slug.
        dropped: Tokens not yet used.
        min_length: Stop once the joined slug reaches this length.
        limit: Never exceed this length.

    Returns:
        Updated ``(kept, dropped, current_slug)``.
    """
    kept = list(kept)
    dropped = list(dropped)
    current = _join(prefix, kept) or (prefix or "item")
    while dropped and len(current) < min_length:
        next_token = dropped[0]
        trial = _join(prefix, [*kept, next_token])
        if len(trial) <= limit:
            kept.append(dropped.pop(0))
            current = trial
            continue
        # Partial word: take as much of the next token as fits.
        separator = 1 if current else 0
        budget = limit - len(current) - separator
        if budget < 1:
            break
        partial = next_token[:budget].rstrip("-")
        if not partial:
            break
        kept.append(partial)
        remainder = next_token[len(partial) :].lstrip("-")
        if remainder:
            dropped[0] = remainder
        else:
            dropped.pop(0)
        current = _join(prefix, kept)
        break
    return kept, dropped, current[:limit].rstrip("-") or current
