"""How many ids one reorder request may name.

A drag-and-drop reorder submits the *complete* desired order, so the ceiling
cannot be a constant chosen for the size of the statement: any number low enough
to bound the SQL is one a large container could legitimately exceed, and silently
trimming a reorder produces an order nobody asked for.

So it is the same limit that governs how many items the container may hold. A
reorder naming more items than the container can contain is not a reorder.

Both governing settings use ``0`` to mean unlimited. There the fallback is that
setting's own validator maximum - the largest value an administrator could ever
configure - so the bound can never refuse a reorder a configured limit would have
allowed. On that path Django's ``DATA_UPLOAD_MAX_MEMORY_SIZE`` is the tighter
bound in practice: a 2.5MB JSON body holds roughly 180,000 ids, and a larger one
is refused before any view sees it.
"""

from __future__ import annotations

#: Ceiling used when ``max_pins_per_list`` is 0 (unlimited). Matches that
#: field's own ``MaxValueValidator``.
UNLIMITED_LIST_FALLBACK = 1_000_000

#: Ceiling used when ``max_trip_activities`` is 0 (unlimited). Matches that
#: field's own ``MaxValueValidator``.
UNLIMITED_TRIP_FALLBACK = 10_000


def reorder_id_ceiling(configured: int, unlimited_fallback: int) -> int:
    """The most ids a reorder of this container may name.

    Args:
        configured: The container's own item limit, where 0 means unlimited.
        unlimited_fallback: Ceiling to use when *configured* is unlimited.

    Returns:
        A positive ceiling.
    """
    return configured if configured > 0 else unlimited_fallback
