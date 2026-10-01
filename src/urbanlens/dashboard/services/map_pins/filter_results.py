"""Answer a map filter with identifiers when the client already holds the pins.

The filter panel posts on every change, and the answer used to be every matching
pin's full payload - for a 10,000-pin account, an 11.45MB document rebuilt each
time a slider moved. The client, meanwhile, holds every one of those pins
already: the map page loads the whole account up front (`map.document`, else the
paged fallback) and keeps them in its own store.

So the expensive half of the answer is data the asker has. What it lacks is
*which* of them matched, and that is a list of identifiers - one `values_list`,
no payload build, no model instantiation, ~360KB for the same 10,000 pins.

The client's store is only a safe substitute while it is complete and current,
which is what the fingerprint decides: the client sends the one it last saw from
`map.pins.meta`, and identifiers are served only when it still equals the
profile's. Anything else - a stale fingerprint, a client that never loaded
everything, an old page - gets payloads, so the transport is an optimisation and
never a correctness assumption. The client checks too: an identifier it cannot
resolve makes it ask again for payloads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.services.map_pins.document import max_pins
from urbanlens.dashboard.services.map_pins.fingerprint import pin_collection_state

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

#: Form field the client sends its fingerprint in.
STORE_FINGERPRINT_FIELD = "store_fingerprint"


def client_holds_every_pin(profile: Profile, claimed_fingerprint: str) -> bool:
    """Whether the asker's own store is the profile's current pin set.

    Args:
        profile: Whose pins the filter runs over.
        claimed_fingerprint: What the client last saw from `map.pins.meta`. Empty
            when the client is not claiming anything, which is the default and
            must stay the safe answer.

    Returns:
        True only when the claim is present and still current.
    """
    if not claimed_fingerprint:
        return False
    return claimed_fingerprint == pin_collection_state(profile).fingerprint


def matching_uuids(query: QuerySet[Any], limit: int | None = None) -> tuple[list[str], bool]:
    """The identifiers of the matching pins, bounded.

    Args:
        query: The filtered pin queryset.
        limit: The most to return. Defaults to the response ceiling, read at call
            time so a test can lower it.

    Returns:
        ``(uuids, truncated)``.
    """
    limit = max_pins() if limit is None else limit
    # Ordering is meaningless for a set of identifiers and sorting the whole
    # match by the model's default is not, so it is dropped before the ceiling.
    # One row past the ceiling is what distinguishes "exactly full" from "more".
    rows = list(query.order_by().values_list("uuid", flat=True)[: limit + 1])
    return [str(value) for value in rows[:limit]], len(rows) > limit


def bounded[Pins: QuerySet[Any]](query: Pins, limit: int | None = None) -> tuple[Pins, bool, int]:
    """Cap a filter result that has to travel as payloads.

    Counting first costs a query the payload build dwarfs, and it is what lets
    the response say how much it left out rather than only that it did.

    The cap is a `pk` boundary rather than a slice: the payload service batches
    by primary key and reorders as it goes, and a sliced queryset cannot be
    reordered. Taking the pk at the limit and filtering below it leaves an
    ordinary queryset that composes with whatever the caller does next.

    Args:
        query: The filtered pin queryset.
        limit: The most to serialize. Defaults to the response ceiling.

    Returns:
        ``(query, truncated, total)``.
    """
    limit = max_pins() if limit is None else limit
    total = query.count()
    if total <= limit:
        return query, False, total
    boundary = query.order_by("pk").values_list("pk", flat=True)[limit - 1]
    return query.filter(pk__lte=boundary), True, total
