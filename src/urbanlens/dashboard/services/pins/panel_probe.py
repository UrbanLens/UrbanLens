"""Which of a pin's enrichment panels are already known to have nothing to show, decided from what is stored.

The Private Pin page renders a placeholder only for the panels that may have something, so a panel that would answer
204 costs no request (P53). Each decision is the panel's own - its gate, :func:`~external_data.fetch_blocked`, and
whether its landed payload shows anything - so the page never leaves out a panel that would show something. A panel
whose answer is not stored yet, or whose check raised, counts as possibly having content and loads as it always did.

Nothing here fetches: every decision runs with gateway requests refused and remote geo boundaries left unresolved, so
a check that would need either counts as "may have content" too.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.core.rate_limiter import ExternalCallForbiddenError, external_calls_forbidden
from urbanlens.dashboard.services.geo.geo_boundary import BoundaryNotLoadedError, remote_loading_deferred
from urbanlens.dashboard.services.pins.external_data import cached_entries, fetch_blocked
from urbanlens.dashboard.services.pins.search_names import names_remembered

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import CachedEntry, GalleryMediaSource, LocationCachePanelSource

logger = logging.getLogger(__name__)

#: What a check raises when it would have had to reach the network to answer.
_NEEDS_THE_NETWORK = (BoundaryNotLoadedError, ExternalCallForbiddenError)


@dataclass(frozen=True, slots=True)
class PanelProbe:
    """What one probe found.

    Attributes:
        empty_cards: Keys of the cards known to answer 204.
        empty_galleries: Keys of the Media gallery providers known to add no tile.
    """

    empty_cards: frozenset[str] = frozenset()
    empty_galleries: frozenset[str] = frozenset()


def _applies(source: LocationCachePanelSource, pin: Pin) -> bool | None:
    """The source's gate, or None when it raised: its panel then shows an error, which is not nothing."""
    try:
        return bool(source.gate(pin))
    except _NEEDS_THE_NETWORK:
        logger.debug("Panel source %s cannot decide its gate offline for pin %s", source.key, pin.pk)
        return None
    except Exception:
        logger.exception("Panel source %s raised from gate() while probing pin %s", source.key, pin.pk)
        return None


def _card_is_empty(source: LocationCachePanelSource, pin: Pin, applies: bool | None, entry: CachedEntry | None) -> bool:
    if source.always_shows(pin):
        return False
    if applies is None:
        return False
    if not applies:
        return True
    if entry is None:
        return fetch_blocked(source, pin)
    return not source.shows(pin, entry.data or {})


def _gallery_is_empty(source: GalleryMediaSource, pin: Pin, applies: bool | None, entry: CachedEntry | None) -> bool:
    if applies is None:
        return False
    if not applies:
        return True
    if entry is None or not source.media_is_ready(entry.data or {}):
        return fetch_blocked(source, pin)
    if pin.location is None:
        return True
    return not source.media_items(source.for_viewer(entry.data or {}, pin.profile, pin.location))


def _guarded(key: str, pin: Pin, decide: Callable[[], bool]) -> bool:
    """``decide()``, or False - possibly has content - when it raised."""
    try:
        return decide()
    except _NEEDS_THE_NETWORK:
        logger.debug("Panel source %s cannot decide offline for pin %s", key, pin.pk)
        return False
    except Exception:
        logger.exception("Panel source %s failed its probe for pin %s", key, pin.pk)
        return False


def probe_panels(pin: Pin, *, cards: Mapping[str, Sequence[LocationCachePanelSource]], galleries: Sequence[GalleryMediaSource]) -> PanelProbe:
    """Find which of ``pin``'s cards and gallery providers are known to have nothing, in one cache query.

    Args:
        pin: The pin being viewed, by its owner.
        cards: Each card's key mapped to the sources it shows, the card's own first; a card is empty only when every
            one of them is. Only sources the viewer may see belong here.
        galleries: The Media gallery providers the viewer may see.

    Returns:
        The keys known to be empty; every other card and provider may have something.
    """
    with external_calls_forbidden(), remote_loading_deferred(), names_remembered():
        return _probe(pin, cards, galleries)


def _probe(pin: Pin, cards: Mapping[str, Sequence[LocationCachePanelSource]], galleries: Sequence[GalleryMediaSource]) -> PanelProbe:
    sources = {source.key: source for members in cards.values() for source in members} | {source.key: source for source in galleries}
    applies = {key: _applies(source, pin) for key, source in sources.items()}
    try:
        entries = cached_entries(pin, [source for key, source in sources.items() if applies[key]])
    except Exception:
        logger.exception("Could not read the cached panels of pin %s; loading every panel", pin.pk)
        return PanelProbe()

    def card_member_is_empty(source: LocationCachePanelSource) -> bool:
        return _guarded(source.key, pin, lambda: _card_is_empty(source, pin, applies[source.key], entries.get(source.key)))

    def gallery_is_empty(source: GalleryMediaSource) -> bool:
        return _guarded(source.key, pin, lambda: _gallery_is_empty(source, pin, applies[source.key], entries.get(source.key)))

    return PanelProbe(
        empty_cards=frozenset(card for card, members in cards.items() if members and all(card_member_is_empty(source) for source in members)),
        empty_galleries=frozenset(source.key for source in galleries if gallery_is_empty(source)),
    )
