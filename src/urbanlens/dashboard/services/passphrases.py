"""Memorable passphrase generation for password suggestion UI."""

from __future__ import annotations

import secrets

# Curated diceware-style word list (memorable English words, no proper nouns).
# Size is intentionally modest so suggestions stay readable; entropy comes from
# combination count (4 words + digit/symbol) rather than dictionary size alone.
_WORDLIST: tuple[str, ...] = (
    "acorn",
    "amber",
    "anchor",
    "apricot",
    "arrow",
    "ashes",
    "atlas",
    "autumn",
    "badge",
    "bamboo",
    "basin",
    "beacon",
    "berry",
    "birch",
    "blanket",
    "blossom",
    "bluebird",
    "boulder",
    "breeze",
    "bridge",
    "brisk",
    "brook",
    "butter",
    "candle",
    "canyon",
    "cascade",
    "cedar",
    "celery",
    "chalk",
    "chapel",
    "cherry",
    "cinder",
    "citron",
    "cliff",
    "cloud",
    "clover",
    "cobalt",
    "comet",
    "compass",
    "coral",
    "cotton",
    "coyote",
    "creek",
    "cricket",
    "crimson",
    "crystal",
    "cushion",
    "cypress",
    "daisy",
    "dawn",
    "delta",
    "desert",
    "dewdrop",
    "diamond",
    "dolphin",
    "dragon",
    "drift",
    "dusk",
    "eagle",
    "ember",
    "emerald",
    "evergreen",
    "falcon",
    "fennel",
    "fern",
    "field",
    "finch",
    "fjord",
    "flame",
    "flint",
    "forest",
    "forge",
    "fossil",
    "fountain",
    "frost",
    "galaxy",
    "garden",
    "garlic",
    "glacier",
    "ginger",
    "glimmer",
    "grove",
    "harbor",
    "harvest",
    "hazel",
    "heather",
    "heron",
    "honey",
    "horizon",
    "island",
    "ivory",
    "jasper",
    "jungle",
    "juniper",
    "kettle",
    "lagoon",
    "lantern",
    "lark",
    "lattice",
    "lavender",
    "leaf",
    "lemon",
    "lichen",
    "lilac",
    "linen",
    "lotus",
    "lumen",
    "magma",
    "maple",
    "marble",
    "meadow",
    "meteor",
    "mirror",
    "mist",
    "moon",
    "moss",
    "mountain",
    "mustard",
    "nectar",
    "needle",
    "night",
    "nova",
    "oak",
    "oasis",
    "ocean",
    "olive",
    "onyx",
    "orange",
    "orchid",
    "otter",
    "oxide",
    "paddle",
    "palace",
    "paper",
    "pebble",
    "pepper",
    "petal",
    "phoenix",
    "pine",
    "planet",
    "plume",
    "pond",
    "poppy",
    "prairie",
    "prism",
    "pulse",
    "quail",
    "quartz",
    "quiver",
    "radar",
    "raven",
    "reef",
    "ridge",
    "river",
    "robin",
    "rocket",
    "root",
    "rose",
    "sable",
    "saffron",
    "sage",
    "salmon",
    "sandal",
    "sapphire",
    "scarab",
    "shadow",
    "shell",
    "silver",
    "sky",
    "slope",
    "sparrow",
    "spice",
    "spruce",
    "star",
    "stone",
    "storm",
    "stream",
    "summit",
    "sun",
    "swallow",
    "swift",
    "tablet",
    "temple",
    "thistle",
    "thunder",
    "tide",
    "timber",
    "topaz",
    "trail",
    "treasure",
    "tulip",
    "tundra",
    "tunnel",
    "twilight",
    "valley",
    "vapor",
    "velvet",
    "violet",
    "voyage",
    "walnut",
    "waterfall",
    "wave",
    "willow",
    "wind",
    "winter",
    "wolf",
    "woodland",
    "zephyr",
)

_SEPARATORS = ("-", ".", "_")
_DIGITS = "23456789"  # omit 0/1 to avoid O/l confusion
_SYMBOLS = "!@#$%&*?"


def generate_passphrases(count: int = 5, *, words: int = 4) -> list[str]:
    """Generate memorable passphrases that satisfy UrbanLens password rules.

    Each passphrase is Title-Cased words joined by a separator, then either a
    digit or a symbol is appended so the result always includes uppercase,
    lowercase, and a digit or symbol.

    Args:
        count: How many distinct passphrases to return (clamped to 1-10).
        words: How many dictionary words to include (clamped to 3-6).

    Returns:
        A list of passphrase strings.
    """
    count = max(1, min(int(count), 10))
    words = max(3, min(int(words), 6))
    results: list[str] = []
    seen: set[str] = set()
    # Cap retries so a pathological RNG collision cannot loop forever.
    for _ in range(count * 20):
        if len(results) >= count:
            break
        phrase = _one_passphrase(words)
        if phrase not in seen:
            seen.add(phrase)
            results.append(phrase)
    return results


def _one_passphrase(word_count: int) -> str:
    """Build a single passphrase meeting complexity requirements.

    Args:
        word_count: Number of words to sample.

    Returns:
        A passphrase string.
    """
    chosen = [secrets.choice(_WORDLIST).capitalize() for _ in range(word_count)]
    separator = secrets.choice(_SEPARATORS)
    base = separator.join(chosen)
    # Prefer a digit half the time, otherwise a symbol — either satisfies policy.
    if secrets.randbelow(2) == 0:
        return f"{base}{secrets.choice(_DIGITS)}"
    return f"{base}{secrets.choice(_SYMBOLS)}"
