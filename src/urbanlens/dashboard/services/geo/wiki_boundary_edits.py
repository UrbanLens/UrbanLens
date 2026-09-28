"""Writing, reverting and auditing a wiki's community-drawn boundary.

The web editor, the external API and the history revert all change a wiki's boundary through here, so each records
the same ``WikiEdit`` shape: ``{"boundary_<type>": {"from": <BoundaryRevision id | None>, "to": <id | None>}}``.
An outline is snapshotted once into ``BoundaryRevision`` and named by id, rather than written out as WKT in every
edit that touches it.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction

from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryRevision, BoundaryType
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.contrib.gis.geos import MultiPolygon

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: ``WikiEdit.changes`` key prefix for a boundary change; the suffix is the ``BoundaryType``.
BOUNDARY_CHANGE_PREFIX = "boundary_"

#: Coordinate tolerance (degrees, ~1 mm) for treating a stored outline as the same as a snapshot of it.
_SAME_OUTLINE_TOLERANCE = 1e-8


def boundary_change_key(boundary_type: str) -> str:
    """The ``WikiEdit.changes`` key recording a change to one boundary type."""
    return f"{BOUNDARY_CHANGE_PREFIX}{boundary_type}"


def boundary_type_for_change_key(key: str) -> str | None:
    """The ``BoundaryType`` a ``WikiEdit.changes`` key records, or None when it is not a boundary key."""
    if not key.startswith(BOUNDARY_CHANGE_PREFIX):
        return None
    boundary_type = key.removeprefix(BOUNDARY_CHANGE_PREFIX)
    return boundary_type if boundary_type in BoundaryType.values else None


def is_boundary_change_key(key: str) -> bool:
    """Whether a ``WikiEdit.changes`` key is a boundary change (of any type, known or not)."""
    return key.startswith(BOUNDARY_CHANGE_PREFIX)


def _same_outline(first: MultiPolygon | None, second: MultiPolygon | None) -> bool:
    if first is None or second is None:
        return first is None and second is None
    return bool(first.equals_exact(second, _SAME_OUTLINE_TOLERANCE))


def snapshot_outline(wiki: Wiki, boundary_type: str, polygon: MultiPolygon | None) -> BoundaryRevision | None:
    """The revision holding *polygon*, reusing the latest one when it already does.

    Args:
        wiki: The wiki the outline belongs to.
        boundary_type: Which of its boundaries.
        polygon: The outline, or None for "no drawn boundary".

    Returns:
        The revision, or None for no outline.
    """
    if polygon is None:
        return None
    latest = BoundaryRevision.objects.filter(wiki=wiki, boundary_type=boundary_type).order_by("-id").first()
    if latest is not None and _same_outline(latest.polygon, polygon):
        return latest
    return BoundaryRevision.objects.create(wiki=wiki, boundary_type=boundary_type, polygon=polygon)


def _set_outline(wiki: Wiki, row: Boundary | None, boundary_type: str, polygon: MultiPolygon | None) -> None:
    """Set or clear the wiki's drawn boundary row."""
    if polygon is None:
        if row is not None:
            row.delete()
        return
    if row is None:
        Boundary(wiki=wiki, location_id=wiki.location_id, boundary_type=boundary_type, polygon=polygon).save()
        return
    row.polygon = polygon
    row.location_id = wiki.location_id
    row.save(update_fields=["polygon", "location", "updated"])


def _lock(wiki: Wiki) -> None:
    Wiki.objects.select_for_update().filter(pk=wiki.pk).first()


def save_wiki_boundary(wiki: Wiki, boundary_type: str, polygon: MultiPolygon | None, editor: Profile) -> WikiEdit:
    """Draw (or, with None, clear) a wiki's community boundary and record the edit.

    Args:
        wiki: The writable wiki row.
        boundary_type: Which boundary.
        polygon: The validated new outline, or None to clear it.
        editor: Who made the change.

    Returns:
        The recorded edit.
    """
    with transaction.atomic():
        _lock(wiki)
        row = Boundary.objects.row_for_wiki(wiki, boundary_type)
        before = snapshot_outline(wiki, boundary_type, row.polygon if row is not None else None)
        _set_outline(wiki, row, boundary_type, polygon)
        after = snapshot_outline(wiki, boundary_type, polygon)
        return WikiEdit.objects.create(
            wiki=wiki,
            editor=editor,
            changes={boundary_change_key(boundary_type): {"from": before.pk if before else None, "to": after.pk if after else None}},
        )


@dataclass(frozen=True, slots=True)
class BoundaryRevert:
    """The outcome of reverting one boundary change: the diff to record, or None when it was changed again since."""

    diff: dict[str, int | None] | None


def revert_boundary_change(wiki: Wiki, boundary_type: str, change: Any) -> BoundaryRevert:
    """Put a wiki's boundary back to an edit's ``from`` outline, if it still shows the edit's ``to``.

    Args:
        wiki: The writable wiki row.
        boundary_type: Which boundary the change was to.
        change: The edit's ``{"from": id | None, "to": id | None}`` for that boundary.

    Returns:
        The reverting diff, or a ``None`` diff when the boundary no longer matches the edit (or an outline it names is
        gone), in which case nothing was written.
    """
    if not isinstance(change, dict):
        return BoundaryRevert(diff=None)
    revisions = {revision.pk: revision for revision in BoundaryRevision.objects.filter(wiki=wiki, pk__in=[pk for pk in (change.get("from"), change.get("to")) if isinstance(pk, int)])}

    def outline(key: str) -> tuple[bool, MultiPolygon | None]:
        value = change.get(key)
        if value is None:
            return True, None
        revision = revisions.get(value) if isinstance(value, int) else None
        return revision is not None, revision.polygon if revision is not None else None

    to_known, to_outline = outline("to")
    from_known, from_outline = outline("from")
    if not (to_known and from_known):
        return BoundaryRevert(diff=None)
    with transaction.atomic():
        _lock(wiki)
        row = Boundary.objects.row_for_wiki(wiki, boundary_type)
        if not _same_outline(row.polygon if row is not None else None, to_outline):
            return BoundaryRevert(diff=None)
        _set_outline(wiki, row, boundary_type, from_outline)
    return BoundaryRevert(diff={"from": change.get("to"), "to": change.get("from")})


def referenced_revision_ids(changes_rows: Iterable[Any]) -> set[int]:
    """Every ``BoundaryRevision`` id named by the given ``WikiEdit.changes`` values."""
    ids: set[int] = set()
    for changes in changes_rows:
        if not isinstance(changes, dict):
            continue
        for key, diff in changes.items():
            if is_boundary_change_key(key) and isinstance(diff, dict):
                ids.update(value for value in (diff.get("from"), diff.get("to")) if isinstance(value, int))
    return ids


def prune_unreferenced_revisions(wiki: Wiki) -> int:
    """Delete the wiki's outlines no remaining edit names, as after an edit is expunged.

    Returns:
        How many revisions were deleted.
    """
    keep = referenced_revision_ids(WikiEdit.objects.filter(wiki=wiki).values_list("changes", flat=True))
    deleted, _ = BoundaryRevision.objects.filter(wiki=wiki).exclude(pk__in=keep).delete()
    return deleted
