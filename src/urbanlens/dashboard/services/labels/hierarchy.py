"""Write-time safety checks for the ``Label.parents`` hierarchy.

``Label.parents`` is an unrestricted many-to-many self-relation, so nothing at
the database level prevents a cycle. Read paths were written defensively around
that (``Label.get_label_and_descendants`` is explicitly BFS-with-visited), but a
cycle still breaks any consumer that walks the hierarchy without the same
precaution, and it makes the tree meaningless to a user.

The guard itself was already implemented as a private helper inside
``controllers.labels``. It lives here now so every write path can reach it -
in particular the external API, where an unguarded ``parents`` write would let a
client build a cycle deliberately and turn any hierarchy walk into a denial of
service. The controller imports it from here rather than keeping its own copy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.labels.model import Label


def would_create_cycle(label: Label, parent_ids: Sequence[int]) -> bool:
    """Whether making any of *parent_ids* a parent of *label* would close a loop.

    Walks each proposed parent's own ancestor chain looking for ``label``. If
    ``label`` is found, it is already an ancestor of that proposed parent, so
    adding the reverse edge (proposed parent -> label) would make ``label`` its
    own ancestor.

    A ``visited`` set bounds the walk even against data that already contains a
    cycle, so this is safe to run against a hierarchy corrupted before the
    guard existed. Mirrors ``Pin.would_create_cycle``.

    Args:
        label: The label that would receive new parents. An unsaved label
            (no pk) can never close a loop, since nothing can point at it yet.
        parent_ids: Primary keys proposed as parents of *label*.

    Returns:
        True if adding any one of *parent_ids* would create a cycle.
    """
    from urbanlens.dashboard.models.labels.model import Label as LabelModel

    if label.pk is None:
        return False

    for proposed_parent_id in parent_ids:
        if proposed_parent_id == label.pk:
            return True
        visited: set[int] = set()
        queue: list[int] = [proposed_parent_id]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            if current == label.pk:
                return True
            # Labels for which `current` is a child are `current`'s parents
            # (Label.parents' related_name is "children").
            queue.extend(LabelModel.objects.filter(children__id=current).values_list("id", flat=True))
    return False


def safe_parent_ids(label: Label, parent_ids: Sequence[int]) -> list[int]:
    """Filter *parent_ids* down to those that can be applied without a cycle.

    Checking each candidate individually (rather than rejecting the whole set
    when one is bad) matches how ``controllers.labels`` already behaves: a
    parent picker submitting one unusable choice should still apply the rest.

    Args:
        label: The label that would receive the parents.
        parent_ids: Candidate parent primary keys.

    Returns:
        The subset of *parent_ids* that is safe to assign, in input order.
    """
    return [pid for pid in parent_ids if not would_create_cycle(label, [pid])]
