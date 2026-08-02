"""Name/alias selection for Named Place mode rounds.

See ``docs/designs/spotguessr.md`` ("Named Place mode") for the rules this
encodes.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.pins.public_pins import is_meaningful_name

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location


def candidate_name_for_location(location: Location, *, use_aliases: bool = True) -> str | None:
    """Pick a meaningful name/alias to show for ``location``, or None if it has none.

    Reuses ``services.pins.public_pins.is_meaningful_name`` verbatim rather than
    a second heuristic - it already filters blank/placeholder/coordinate-
    shaped strings.

    Args:
        location: The round's answer location.
        use_aliases: When True (``config.use_aliases``, default), prefer a
            random meaningful alias over the wiki's own name - false always
            uses the official wiki name.
    """
    wiki = getattr(location, "wiki", None)
    if wiki is None:
        return None

    if use_aliases:
        meaningful_aliases = [alias.name for alias in wiki.aliases.all() if is_meaningful_name(alias.name)]
        if meaningful_aliases:
            return random.choice(meaningful_aliases)  # noqa: S311 # nosec: B311 - game content selection, not security-sensitive

    return wiki.name if is_meaningful_name(wiki.name) else None
